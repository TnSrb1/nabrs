"""Reading the tenant’s subscription for the “My subscription” section of the cabinet (VG-11.2).

Read-only. Called FROM the route under a session — the RLS context must be up (``repo.set_tenant`` FIRST as the transaction’s action), otherwise FORCE RLS returns 0 rows (``billing_subscriptions``) / the query fails. Therefore, in addition to the RLS policy, we keep an explicit ``WHERE tenant_id = :tid`` (defense-in-depth, ADR-002) and never read another tenant. asyncpg → ``CAST(:p AS type)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.plans.catalog import parse_upgrade_plan

# The tenant’s latest subscription (RLS + an explicit filter by tenant_id, defense-in-depth).
_SUBSCRIPTION_SQL = text(
    "SELECT provider, status, plan_key, current_period_end "
    "FROM billing_subscriptions "
    "WHERE tenant_id = CAST(:tid AS uuid) "
    "ORDER BY updated_at DESC "
    "LIMIT 1"
)

# The tenant’s current tier / status — for “You are on Free / trial”, when there is no subscription.
_TENANT_TIER_SQL = text(
    "SELECT tier, status FROM tenants WHERE id = CAST(:tid AS uuid)"
)

# The last DECIDED decision on the tenant’s request (for the cabinet banner — a reliable
# channel of the decision reply that does not depend on Telegram). RLS (tenant_isolation, FORCE
# 0012/0032) + an explicit WHERE tenant_id: only the tenant’s own. decision_comment — 0035.
_LAST_DECISION_SQL = text(
    "SELECT status, days, decided_at, comment, decision_comment "
    "FROM trial_requests "
    "WHERE tenant_id = CAST(:tid AS uuid) AND status IN ('approved', 'rejected') "
    "ORDER BY decided_at DESC NULLS LAST "
    "LIMIT 1"
)


@dataclass(frozen=True)
class SubscriptionView:
    """The subscription projection for the template (provider-neutral)."""

    provider: str
    status: str
    plan_key: str | None
    current_period_end: datetime | None


async def get_subscription(db: AsyncSession, tenant_id: str) -> SubscriptionView | None:
    """The tenant’s latest subscription or ``None`` (Free / trial, no payment yet).

    ``repo.set_tenant`` MUST have been called earlier in this same transaction.
    """
    row = (await db.execute(_SUBSCRIPTION_SQL, {"tid": str(tenant_id)})).first()
    if row is None:
        return None
    return SubscriptionView(
        provider=row.provider,
        status=row.status,
        plan_key=row.plan_key,
        current_period_end=row.current_period_end,
    )


async def get_tenant_tier(
    db: AsyncSession, tenant_id: str
) -> tuple[str | None, str | None]:
    """The tenant’s ``(tier, status)`` for computing the current plan (fallback display)."""
    row = (await db.execute(_TENANT_TIER_SQL, {"tid": str(tenant_id)})).first()
    if row is None:
        return (None, None)
    return (row.tier, row.status)


@dataclass(frozen=True)
class DecisionView:
    """The last decision on the tenant's request — for the cabinet banner (ru).

    approved: ``True`` approved / ``False`` rejected. plan — from ``comment='upgrade_request:<plan>'`` (None for an ordinary trial access). comment — the operator's comment (``decision_comment``, may be None).
    """

    approved: bool
    plan: str | None
    decided_at: datetime | None
    comment: str | None


async def get_last_decision(db: AsyncSession, tenant_id: str) -> DecisionView | None:
    """The last decided decision on the tenant’s request, or ``None``.

    A reliable reply channel that does not depend on Telegram: the subscriber sees the decision and the operator’s comment in the cabinet, even if TG is not connected or has dropped out. ``repo.set_tenant`` MUST have been called earlier in this same transaction (RLS + an explicit WHERE tenant_id — only the tenant’s own).
    """
    row = (await db.execute(_LAST_DECISION_SQL, {"tid": str(tenant_id)})).first()
    if row is None:
        return None
    plan = parse_upgrade_plan(row.comment)
    return DecisionView(
        approved=row.status == "approved",
        plan=plan.key if plan is not None else None,
        decided_at=row.decided_at,
        comment=row.decision_comment,
    )
