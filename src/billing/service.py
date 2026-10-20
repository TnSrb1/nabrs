"""The core of applying billing events (VG-11.1).

:func:`apply_billing_event` — the only point that applies the normalized
:class:`~src.billing.models.BillingEvent` to the system. Provider-neutral:
idempotency, tenant resolution, subscription upsert, applying the plan to the tenant.

The RLS contour: called under the role ``tendersrb_app`` (FORCE RLS) from the webhook route
without a session. All CROSS-TENANT writes — ONLY through SECURITY DEFINER
(``app_billing_upsert_subscription`` + the ones reused from 0025
``app_admin_set_tier`` / ``app_admin_grant_access``). ``billing_webhook_events``
is a platform table without RLS (only the dedup of ids). asyncpg → ``CAST(:p AS ...)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.billing.models import BillingEvent
from src.domain.access_grants import extend_access
from src.plans.catalog import get_plan

logger = logging.getLogger(__name__)

# The provider of manual plan granting (approval of a request by the owner). The same billing path as
# a real payment, only the source is not a payment gateway but the decision of the operator.
MANUAL_PROVIDER = "manual"

# The kind of the reply email to the subscriber about the decision on the request (approve/reject). Separate from the
# kind of the payment lifecycle (activated/renewed/...): the texts and the context (plan,
# the admin’s comment) are different — see workers.billing_notifications.build_email_*.
MANUAL_DECISION_APPROVED_KIND = "manual_approved"
MANUAL_DECISION_REJECTED_KIND = "manual_rejected"

# Idempotency: we try to “claim” the event. ON CONFLICT DO NOTHING + RETURNING —
# if the row is not in the response, the event has already been processed (protection against webhook retries).
_CLAIM_EVENT_SQL = text(
    "INSERT INTO billing_webhook_events (provider, event_id) "
    "VALUES (CAST(:provider AS text), CAST(:event_id AS text)) "
    "ON CONFLICT (provider, event_id) DO NOTHING "
    "RETURNING id"
)

# Tenant resolution by customer_id (a cross-tenant read) — SECURITY DEFINER (0028).
_RESOLVE_TENANT_SQL = text(
    "SELECT app_billing_resolve_tenant_by_customer("
    "CAST(:provider AS text), CAST(:customer_id AS text))"
)

# Subscription upsert (a cross-tenant write) — SECURITY DEFINER (0028).
_UPSERT_SUBSCRIPTION_SQL = text(
    "SELECT app_billing_upsert_subscription("
    "CAST(:tenant_id AS uuid), CAST(:provider AS text), "
    "CAST(:customer_id AS text), CAST(:subscription_id AS text), "
    "CAST(:status AS text), CAST(:plan_key AS text), "
    "CAST(:period_end AS timestamptz))"
)

# Applying the plan to the tenant — we reuse the SECURITY DEFINER from 0025.
_SET_TIER_SQL = text(
    "SELECT app_admin_set_tier(CAST(:tenant_id AS uuid), CAST(:tier AS text))"
)
_GRANT_ACCESS_SQL = text(
    "SELECT app_admin_grant_access(CAST(:tenant_id AS uuid), "
    "CAST(:until AS timestamptz))"
)

# Queuing the payment lifecycle email (0033) — SECURITY DEFINER.
# Idempotent by (provider, event_id): a double webhook = one email. Called IN
# THE SAME TRANSACTION as the dedup claim — the enqueue is committed together with the claim.
_ENQUEUE_NOTIFICATION_SQL = text(
    "SELECT app_billing_enqueue_notification("
    "CAST(:provider AS text), CAST(:event_id AS text), "
    "CAST(:tenant_id AS uuid), CAST(:kind AS text))"
)

# BillingEvent.type → the status in billing_subscriptions.
_STATUS_MAP = {
    "activated": "active",
    "renewed": "active",
    "past_due": "past_due",
    "canceled": "canceled",
}


async def apply_billing_event(db: AsyncSession, event: BillingEvent) -> None:
    """Idempotently apply a billing event.

    The order:
      1. Claim ``event_id`` in ``billing_webhook_events`` (dedup). A repeat → no-op.
      2. Tenant resolution: ``event.tenant_id`` (from metadata) OR by
         ``provider_customer_id`` from ``billing_subscriptions``.
      3. Upsert ``billing_subscriptions`` (status/plan/period).
      4. Apply to the tenant: ``plan_key→tier`` → ``app_admin_set_tier``;
         ``activated/renewed`` → ``app_admin_grant_access(period_end)``.
         ``canceled/past_due`` do NOT cut access — it expires by itself by ``access_until``.
      5. Put the email to the subscriber (``kind = event.type``) into
         ``billing_notification_outbox`` — idempotent by ``(provider,
         event_id)``, sent by the standalone worker ``workers.billing_notifications``.

    All in one transaction: on an exception both the dedup row and the enqueued email are rolled back,
    so a webhook retry after the error is fixed is processed again.
    """
    claimed = await db.execute(
        _CLAIM_EVENT_SQL,
        {"provider": event.provider, "event_id": event.provider_event_id},
    )
    if claimed.first() is None:
        # Already processed earlier — we do nothing (idempotency).
        await db.commit()
        logger.info(
            "billing: event %s/%s already processed — no-op",
            event.provider,
            event.provider_event_id,
        )
        return

    tenant_id = await _resolve_tenant(db, event)
    if tenant_id is None:
        # We could not bind the event to a tenant. The dedup row is recorded (so as not to
        # loop the retries), but the plan is not applied. A pathological case:
        # the initial event always carries tenant_id in custom_data/client_reference_id.
        await db.commit()
        logger.warning(
            "billing: no tenant resolved for %s/%s (customer=%s) — the event "
            "accepted, but not applied",
            event.provider,
            event.provider_event_id,
            event.provider_customer_id,
        )
        return

    if event.provider_subscription_id:
        await db.execute(
            _UPSERT_SUBSCRIPTION_SQL,
            {
                "tenant_id": tenant_id,
                "provider": event.provider,
                "customer_id": event.provider_customer_id,
                "subscription_id": event.provider_subscription_id,
                "status": _STATUS_MAP[event.type],
                "plan_key": event.plan_key,
                "period_end": event.current_period_end,
            },
        )

    await _apply_to_tenant(db, tenant_id, event)

    # The email to the subscriber about the money status — into the queue IN THE SAME TRANSACTION as the
    # claim (see the module workers/billing_notifications). So the enqueue is idempotent
    # together with the claim: a rollback of the application will roll back the enqueued email too, and a repeat of
    # the webhook after a failure will not double the email (the claim does not pass → the whole event is a no-op).
    await db.execute(
        _ENQUEUE_NOTIFICATION_SQL,
        {
            "provider": event.provider,
            "event_id": event.provider_event_id,
            "tenant_id": tenant_id,
            "kind": event.type,
        },
    )

    await db.commit()
    logger.info(
        "billing: applied %s/%s type=%s tenant=%s plan=%s",
        event.provider,
        event.provider_event_id,
        event.type,
        tenant_id,
        event.plan_key,
    )


async def _resolve_tenant(db: AsyncSession, event: BillingEvent) -> str | None:
    """tenant_id from the event OR by customer_id (SECURITY DEFINER, cross-tenant)."""
    if event.tenant_id:
        return event.tenant_id
    if not event.provider_customer_id:
        return None
    result = await db.execute(
        _RESOLVE_TENANT_SQL,
        {"provider": event.provider, "customer_id": event.provider_customer_id},
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    return str(row[0])


async def _apply_to_tenant(
    db: AsyncSession, tenant_id: str, event: BillingEvent
) -> None:
    """Apply the plan to the tenant (tier + extension of access) through SECURITY DEFINER.

    Only ``activated``/``renewed`` change the tier and extend the access.
    ``canceled``/``past_due`` do not cut the access — until ``current_period_end``, after which
    ``is_frozen`` (src/domain/access.py) takes effect by itself.
    """
    if event.type not in ("activated", "renewed"):
        return
    if not event.plan_key:
        logger.warning(
            "billing: %s without plan_key — tier not applied (tenant=%s)",
            event.type,
            tenant_id,
        )
        return
    plan = get_plan(event.plan_key)
    if plan is None:
        logger.warning(
            "billing: unknown plan_key=%s — tier not applied (tenant=%s)",
            event.plan_key,
            tenant_id,
        )
        return
    await db.execute(_SET_TIER_SQL, {"tenant_id": tenant_id, "tier": plan.tier})
    if event.current_period_end is not None:
        await db.execute(
            _GRANT_ACCESS_SQL,
            {"tenant_id": tenant_id, "until": event.current_period_end},
        )


@dataclass(frozen=True)
class ManualActivation:
    """The result of manual plan granting (:func:`activate_plan_manual`)."""

    plan_key: str
    tier: str
    days: int
    until: datetime


async def activate_plan_manual(
    db: AsyncSession, *, tenant_id: str, plan_key: str, days: int
) -> ManualActivation:
    """Grant the plan to the tenant manually (approval of a request) by the SAME billing path as
    a real payment — without separate logic and without a raw ``UPDATE tenants``.

    Does exactly the same as the webhook ``activated`` (:func:`_apply_to_tenant` +
    the subscription upsert), only ``provider='manual'`` and the term is an EXTENSION by ``days``:

      1. ``app_admin_extend_access(days)`` (0041) — the extension of access from the LARGER
         of the dates (the current ``access_until`` / ``now()``), returns the new term.
         It goes FIRST: this same term becomes the subscription's ``current_period_end``,
         so that both axes name one date.
      2. ``app_billing_upsert_subscription`` — the subscription row ``status='active'``,
         ``plan_key``. Idempotency by ``(provider, provider_subscription_id)``:
         ``provider_subscription_id = tenant_id`` — one manual subscription per
         tenant, a repeated approval UPDATES its row (does not double it).
      3. ``app_admin_set_tier(plan.tier)`` — the tier from the catalog (the axes are not mixed).

    An extension, not a setting: before 0041 the term was computed as ``now() + days``, and
    an approval for N days for a tenant whose access already ran further SHORTENED
    its term (incident 04.08.2026 on a neighbouring admin button: −11 days with an active
    subscription). There was nothing to read the base from in Python — both call points
    (``admin_service.approve_request`` and ``bots.trial_service.decide``) run under the
    role ``tendersrb_app`` (FORCE RLS) and do not see a foreign ``tenants``; therefore the base is computed by
    the function ITSELF inside the UPDATE. The single entry —
    :func:`src.domain.access_grants.extend_access`, the arithmetic is one for the system
    (:func:`src.domain.access.extend_until` / ``extend_until_sql``).

    The webhook path (:func:`_apply_to_tenant`) is not affected by this: there the term is
    the provider's ``current_period_end``, and it is an ABSOLUTE date (the SSOT of payment), not a conversion of “N days”
    into a date; ``app_admin_grant_access`` remains its contract.

    All three writes are existing SECURITY DEFINER functions (0025/0028/0041,
    GRANT EXECUTE tendersrb_app), with validation at the catalog level. It does NOT commit:
    the caller writes the request decision + the audit in the SAME transaction and commits
    once. ``days`` — the term of the approval (the bot buttons / the admin form).

    ``ValueError`` — if the ``plan_key`` is unknown to the catalog (protection against garbage in
    ``comment``; the caller validates via ``parse_upgrade_plan``).
    ``LookupError`` — if the tenant does not exist (the extension touched not a single row).
    """
    plan = get_plan(plan_key)
    if plan is None:
        raise ValueError(f"activate_plan_manual: unknown plan_key={plan_key!r}")
    until = await extend_access(db, tenant_id=tenant_id, days=days)
    # provider_subscription_id = provider_customer_id = tenant_id: a manual subscription
    # one per tenant (upsert by it), a cross-tenant write — through SECURITY
    # DEFINER (bypasses FORCE RLS), as the webhook does.
    await db.execute(
        _UPSERT_SUBSCRIPTION_SQL,
        {
            "tenant_id": tenant_id,
            "provider": MANUAL_PROVIDER,
            "customer_id": tenant_id,
            "subscription_id": tenant_id,
            "status": "active",
            "plan_key": plan_key,
            "period_end": until,
        },
    )
    await db.execute(_SET_TIER_SQL, {"tenant_id": tenant_id, "tier": plan.tier})
    return ManualActivation(plan_key=plan_key, tier=plan.tier, days=days, until=until)


async def enqueue_decision_notification(
    db: AsyncSession, *, request_id: str, tenant_id: str, approve: bool
) -> None:
    """Put the reply email to the subscriber about the decision on the request into the payment queue.

    A SINGLE enqueue helper for BOTH decision points (the bot
    :func:`src.bots.trial_service.decide` and the web
    :func:`src.api.admin.admin_service.approve_request`/``reject_request``) —
    the logic is not duplicated (the invariant “mirror the selection/predicate, do not copy”).
    We reuse the payment emails circuit (0033) WITHOUT a second worker/outbox/enqueue:

      * ``provider = 'manual'`` — as with the manual plan granting (:func:`activate_plan_manual`).
      * ``event_id = 'decision:<request_id>'`` — a deterministic key of the decision.
        Idempotency by the UNIQUE ``(provider, event_id)`` of 0033: a repeated approve
        (the guarded UPDATE returned 0 rows → the caller does not come here) does NOT double
        the email, and even a direct repeat of the enqueue is stopped by ON CONFLICT DO NOTHING.
      * ``kind`` — ``manual_approved`` / ``manual_rejected`` (the texts are assembled by the
        worker ``billing_notifications`` from the plan + the term + the admin's comment).

    Called IN THE SAME TRANSACTION as the decision: it does NOT commit — the caller
    commits the decision + the audit + the enqueued email once. So the email is not sent
    if the decision is rolled back.
    """
    kind = MANUAL_DECISION_APPROVED_KIND if approve else MANUAL_DECISION_REJECTED_KIND
    await db.execute(
        _ENQUEUE_NOTIFICATION_SQL,
        {
            "provider": MANUAL_PROVIDER,
            "event_id": f"decision:{request_id}",
            "tenant_id": str(tenant_id),
            "kind": kind,
        },
    )
