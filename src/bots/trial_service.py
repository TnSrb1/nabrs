"""The service of the decisions on the trial access for the Telegram bot (STAGE T).

A privileged CROSS-tenant operation: the decision is made by the owner-operator, while the
data belongs to a FOREIGN tenant. The bot works under the role ``tendersrb_app``
(NOBYPASSRLS), ``trial_requests``/``tenants``/``users``/``user_channels`` — under
FORCE RLS, and ``session_scope()`` does not set the GUC ``app.current_tenant_id``,
therefore ANY raw query from here failed with ``UndefinedObjectError`` (the buttons
“3/15/30 days / decline” and ``/trials`` never worked — verified on
production 06.08.2026). The former docstring about “an owner connection, RLS is not forced”
was a stale record and itself produced the defect.

The regular contour: cross-tenant reads and writes — ONLY through SECURITY
DEFINER (``app_admin_list_trial_requests`` 0034, ``app_admin_decide_trial_request``
0035, ``app_admin_extend_access`` / ``app_admin_tenant_notify_target`` 0041,
``app_admin_write_audit`` 0030, ``app_billing_*`` 0028/0033). The GUC is not
suitable here: the operation has no “own” tenant.

- :func:`list_pending` — the list of the pending requests with the email of the initiator (for the
  ``/trials`` insurance in the bot: in case the push notification of the cabinet did not arrive).
  The upgrade request is told apart by the field ``plan`` (as in the web admin); the free
  comment of the subscriber reaches the operator in the push notification itself
  (``api/cabinet/trial_service.notify_admin``), the function 0034 does not return it.
- :func:`decide` — idempotently decides the request: a guarded UPDATE
  ``WHERE id = :rid AND status = 'pending'`` (a repeat press on an already decided one →
  0 rows → outcome ``already``, the data is not touched). On the approval of an ordinary
  trial access it EXTENDS ``tenants.access_until`` by N days from the larger of the
  dates — the current term and ``now()`` (an extension has no right to shorten an
  access already granted, see :func:`src.domain.access.extend_until_sql`); the upgrade
  request (``upgrade_request:<plan>``) is granted by the SINGLE billing path
  (:func:`src.billing.service.activate_plan_manual` — identical to a real payment).
  Returns the outcome + the data for the notification of the user (telegram chat_id /
  email / the granted plan).

The functions take an ``AsyncSession`` — they are tested on a mock session (ADR-013), without
the network and a real DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from src.billing.service import activate_plan_manual, enqueue_decision_notification
from src.domain.access import clamp_extension_days
from src.domain.access_grants import extend_access
from src.domain.audit import write_audit
from src.plans.catalog import parse_upgrade_plan

# The upper bound of the /trials list. The function 0034 is paginated (the same call as the
# web admin); the bot sends one message per request, therefore the window is finite.
PENDING_LIMIT = 50

# The requests in the status pending of all tenants + the email of the initiator and the plan of the request —
# SECURITY DEFINER (0034), the same call as the web admin. A raw SELECT from here is
# impossible: trial_requests is under FORCE RLS, the bot has no GUC.
_LIST_PENDING_SQL = text(
    "SELECT * FROM app_admin_list_trial_requests("
    "CAST(:limit AS int), CAST(:offset AS int))"
)

# The idempotent decision — SECURITY DEFINER (0035): a guarded UPDATE
# WHERE status='pending' inside the function (a repeat → 0 rows → outcome “already”).
# Returns comment, in order to tell the upgrade request (upgrade_request:<plan>) apart from the
# ordinary trial access — the first is granted by the billing path (tier + subscription).
# decision_comment is not present in the bot path (the operator’s comment exists only in the web form) →
# NULL, as it was before the switch to the function.
_DECIDE_SQL = text(
    "SELECT * FROM app_admin_decide_trial_request("
    "CAST(:rid AS uuid), CAST(:new_status AS text), CAST(:days AS int), "
    "CAST(:comment AS text))"
)

# The addressee of the decision notification (the Telegram chat + email) — SECURITY DEFINER (0041):
# users/user_channels are under FORCE RLS, and the tenant is foreign.
_TARGET_SQL = text(
    "SELECT * FROM app_admin_tenant_notify_target(CAST(:tenant_id AS uuid))"
)


@dataclass(frozen=True)
class PendingRequest:
    """A pending request to be shown in ``/trials``.

    ``plan`` — the plan of the upgrade request (the tail ``comment='upgrade_request:<plan>'``)
    or ``None`` for an ordinary trial access; the web admin shows the same field.
    """

    request_id: str
    email: str | None
    plan: str | None
    requested_at: datetime | None


@dataclass(frozen=True)
class DecideOutcome:
    """The outcome of :func:`decide`.

    status: ``approved`` / ``rejected`` / ``already`` (it was decided earlier).
    days: the approved term (for approve) or ``None``.
    access_until: the new access term of the tenant (for approve) or ``None``.
    tenant_id / chat_id / email: for the notification of the user.
    """

    status: str
    days: int | None = None
    access_until: datetime | None = None
    tenant_id: str | None = None
    chat_id: str | None = None
    email: str | None = None
    # For the upgrade request (upgrade_request): the granted plan and its tier — so that
    # the notification text named the subscription, not the “trial access”. None for an ordinary
    # trial access.
    plan_key: str | None = None
    tier: str | None = None


async def list_pending(db, *, limit: int = PENDING_LIMIT) -> list[PendingRequest]:
    """The list of the pending requests (for ``/trials``) — SECURITY DEFINER 0034."""
    rows = (await db.execute(_LIST_PENDING_SQL, {"limit": limit, "offset": 0})).all()
    return [
        PendingRequest(
            request_id=str(r.id),
            email=r.email,
            plan=r.plan,
            requested_at=r.requested_at,
        )
        for r in rows
    ]


async def decide(
    db,
    *,
    request_id: str,
    approve: bool,
    days: int | None,
    actor: str = "telegram:admin",
) -> DecideOutcome:
    """Idempotently decides the request. On approve it EXTENDS ``tenants.access_until``.

    A repeated decision on an already processed request → ``status='already'`` (the guarded
    UPDATE inside ``app_admin_decide_trial_request`` touched 0 rows), nothing
    changes. A cross-tenant operation under the role ``tendersrb_app``: every step is
    SECURITY DEFINER, ``set_tenant`` is not applicable here (the operation has no own tenant),
    no raw queries to the tenant-scoped tables remain.

    The upgrade request (``comment='upgrade_request:<plan>'``) on approve is granted by the
    SINGLE billing path (:func:`activate_plan_manual`: the ``manual`` subscription +
    the tier from the catalog + the extension of access) — identical to a real payment, and not to a
    separate logic. An ordinary trial access — as before, only ``access_until``
    (we do not touch the tier). The subscription decision (approve/reject of an upgrade)
    is written into ``admin_audit_log`` (0030) in the same transaction.
    """
    # An approval without a term does not happen (the bot buttons give 3/15/30). We check BEFORE the
    # decision of the request: a NULL term would reach the extension and zero the access_until
    # (`GREATEST(...) + NULL` = NULL = “unlimited”) — a silently granted eternal access.
    if approve and days is None:
        raise ValueError("decide: approve without a term (days) — no access is granted")

    new_status = "approved" if approve else "rejected"
    row = (
        await db.execute(
            _DECIDE_SQL,
            {
                "new_status": new_status,
                "days": days if approve else None,
                "rid": str(request_id),
                "comment": None,
            },
        )
    ).first()
    if row is None:
        return DecideOutcome(status="already")

    tenant_id = str(row.tenant_id)
    plan = parse_upgrade_plan(row.comment)
    if days is not None:
        # A free extension — not more than 30 days (owner 10.10).
        days = clamp_extension_days(row.comment, days)
    access_until = None
    plan_key = None
    tier = None
    if approve and plan is not None and days is not None:
        act = await activate_plan_manual(
            db, tenant_id=tenant_id, plan_key=plan.key, days=days
        )
        access_until = act.until
        plan_key = act.plan_key
        tier = act.tier
    elif approve and days is not None:
        # An ordinary trial access: an EXTENSION by N days from the larger of the dates
        # (the current term / now) — a single helper, the same one for the web and the billing.
        access_until = await extend_access(db, tenant_id=tenant_id, days=days)

    # The audit — only for the subscription decision (upgrade). The ordinary trial access
    # does not change the behavior (we do not add an extra record).
    if plan is not None:
        details: dict = {"request_id": str(request_id), "plan": plan.key}
        if approve:
            details.update({"tier": tier, "days": days, "until": _iso(access_until)})
        await write_audit(
            db,
            actor=actor,
            action="approve_subscription" if approve else "reject_subscription",
            target_tenant=tenant_id,
            details=details,
        )

    target = (await db.execute(_TARGET_SQL, {"tenant_id": tenant_id})).first()
    chat_id = target.chat_id if target is not None else None
    email = target.email if target is not None else None

    # A reliable reply to the subscriber about the decision, independent of Telegram: an email
    # + the reflection in the cabinet. A single enqueue helper (the same one is called by the web admin), in the SAME
    # transaction — idempotent by (provider, event_id). Telegram (_notify_user in
    # trial_handlers) remains as an additional channel, it is not replaced.
    await enqueue_decision_notification(
        db, request_id=str(request_id), tenant_id=tenant_id, approve=approve
    )
    await db.commit()
    return DecideOutcome(
        status=new_status,
        days=days if approve else None,
        access_until=access_until,
        tenant_id=tenant_id,
        chat_id=chat_id,
        email=email,
        plan_key=plan_key,
        tier=tier,
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def fmt_date(value: datetime | None) -> str:
    """``datetime`` → ``DD.MM.YYYY`` (or ``—``)."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.strftime("%d.%m.%Y")
