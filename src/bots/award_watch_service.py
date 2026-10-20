"""The service of subscriptions to winners (VG-06 E3b) — the business logic for the Telegram handlers.

Three operations:
  run_watch_winner  — subscription (UPSERT) + a watermark of the existing awards
  run_list_watching — the list of the tenant’s active subscriptions
  run_unwatch       — unsubscribe (only one’s own subscription)

The Pro+/freeze gate — the ``_resolve_gate`` pattern from
``awards_analytics_service`` is used. The resolution of the top winner — via ``customer_award_stats``
(the same path as ``run_competitor_analytics``).

DB parameters: asyncpg — only CAST, do not duplicate the parameter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from src.ai.usage import _TIER_LIMITS
from src.analytics.awards_analytics import customer_award_stats
from src.bots.awards_analytics_service import _LOAD_TENDER_CUSTOMER_SQL, _is_pro_plus
from src.bots.status_service import resolve_tenant_id
from src.domain.access import is_frozen
from src.domain.tiers import effective_tier

logger = logging.getLogger(__name__)

_LOAD_TENANT_TIER_SQL = text("""
    SELECT tier, access_until, status FROM tenants WHERE id = CAST(:tenant_id AS UUID)
    """)

# UPSERT of the subscription: a conflict (tenant_id, winner_pib) → reactivate + update the name
_UPSERT_SUBSCRIPTION_SQL = text("""
    INSERT INTO award_watch_subscriptions (tenant_id, winner_pib, winner_name, is_active)
    VALUES (CAST(:tenant_id AS UUID), :winner_pib, :winner_name, true)
    ON CONFLICT (tenant_id, winner_pib) DO UPDATE
        SET is_active = true,
            winner_name = EXCLUDED.winner_name
    RETURNING id
    """)

# Watermark: all the existing award PIBs are written into sent right at subscription.
# So the notifier will send only about NEW wins (after the moment of the subscription).
_BACKFILL_SENT_SQL = text("""
    INSERT INTO award_watch_sent (subscription_id, award_id)
    SELECT :sub_id, ta.id FROM tender_awards ta WHERE ta.winner_pib = :winner_pib
    ON CONFLICT (subscription_id, award_id) DO NOTHING
    """)

_LIST_WATCHING_SQL = text("""
    SELECT id, winner_name, winner_pib
    FROM award_watch_subscriptions
    WHERE tenant_id = CAST(:tenant_id AS UUID) AND is_active = true
    ORDER BY created_at DESC
    """)

_UNWATCH_SQL = text("""
    UPDATE award_watch_subscriptions SET is_active = false
    WHERE id = :sub_id AND tenant_id = CAST(:tenant_id AS UUID)
    RETURNING id
    """)


@dataclass(frozen=True)
class WatchResult:
    """The result of the subscription operation."""

    status: str  # 'success'|'no_account'|'frozen'|'gate_pro'|'no_tender'|'no_customer'|'no_data'
    winner_name: str = ""
    winner_pib: str = ""


@dataclass(frozen=True)
class WatchingItem:
    """One active subscription for /watching."""

    sub_id: int
    winner_name: str
    winner_pib: str


@dataclass(frozen=True)
class WatchingResult:
    """The list of the tenant’s active subscriptions."""

    status: str  # 'success'|'no_account'
    items: list[WatchingItem] = field(default_factory=list)


@dataclass(frozen=True)
class UnwatchResult:
    """The result of the unsubscription."""

    status: str  # 'success'|'no_account'|'not_found'


async def run_watch_winner(
    session: Any,
    *,
    tender_id: str,
    chat_id: int,
    months: int | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
) -> WatchResult:
    """Subscription to the strongest winner of the contracting authority of the current tender.

    Steps:
      a. Resolve tenant_id; the Pro+/freeze gate.
      b. The customer of the current tender → the top winner (as in run_competitor_analytics).
      c. UPSERT into award_watch_subscriptions (idempotent).
      d. Watermark: backfill of award_watch_sent for all EXISTING awards of this PIB.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    # a. Resolve tenant_id
    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return WatchResult(status="no_account")

    row = (
        await session.execute(_LOAD_TENANT_TIER_SQL, {"tenant_id": str(tenant_id)})
    ).first()
    access_until = getattr(row, "access_until", None) if row is not None else None
    if is_frozen(access_until, datetime.now(timezone.utc)):
        return WatchResult(status="frozen")
    # The effective tier: trial → the rights of Pro (ARCHITECTURE §4), AFTER the frozen gate.
    status_val = str(row.status) if row and getattr(row, "status", None) else None
    raw_tier = str(row.tier) if row and row.tier else None
    tier = effective_tier(status_val, raw_tier)
    if not _is_pro_plus(tier, limits):
        return WatchResult(status="gate_pro")

    # b. Customer → top winner
    trow = (
        await session.execute(_LOAD_TENDER_CUSTOMER_SQL, {"tender_id": tender_id})
    ).first()
    if trow is None:
        return WatchResult(status="no_tender")
    customer_pib = str(trow.customer_tax_id) if trow.customer_tax_id else ""
    customer_name = str(trow.customer_name) if trow.customer_name else ""
    if not customer_pib and not customer_name:
        return WatchResult(status="no_customer")

    cust_stats = await customer_award_stats(
        session,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        months=months,
    )
    if not cust_stats.top_winners or not cust_stats.top_winners[0].pib:
        return WatchResult(status="no_data")

    top = cust_stats.top_winners[0]
    winner_pib = top.pib
    winner_name = top.name or winner_pib

    # c. UPSERT of the subscription
    upsert_result = await session.execute(
        _UPSERT_SUBSCRIPTION_SQL,
        {
            "tenant_id": str(tenant_id),
            "winner_pib": winner_pib,
            "winner_name": winner_name,
        },
    )
    sub_row = upsert_result.first()
    sub_id = sub_row.id if sub_row else None
    await session.commit()

    # d. Watermark — backfill of sent for all existing awards of this PIB
    if sub_id is not None:
        await session.execute(
            _BACKFILL_SENT_SQL,
            {"sub_id": sub_id, "winner_pib": winner_pib},
        )
        await session.commit()

    logger.info(
        "watch: tenant=%s subscribed to pib=%s sub_id=%s", tenant_id, winner_pib, sub_id
    )
    return WatchResult(status="success", winner_name=winner_name, winner_pib=winner_pib)


async def run_list_watching(
    session: Any,
    *,
    chat_id: int,
) -> WatchingResult:
    """The list of the tenant’s active subscriptions (the /watching command)."""
    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return WatchingResult(status="no_account")

    rows = list(
        await session.execute(_LIST_WATCHING_SQL, {"tenant_id": str(tenant_id)})
    )
    items = [
        WatchingItem(
            sub_id=r.id,
            winner_name=str(r.winner_name or r.winner_pib or "—"),
            winner_pib=str(r.winner_pib or ""),
        )
        for r in rows
    ]
    return WatchingResult(status="success", items=items)


async def run_unwatch(
    session: Any,
    *,
    sub_id: int,
    chat_id: int,
) -> UnwatchResult:
    """Unsubscribe from a winner (checks that the subscription belongs to the tenant)."""
    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return UnwatchResult(status="no_account")

    result = await session.execute(
        _UNWATCH_SQL, {"sub_id": sub_id, "tenant_id": str(tenant_id)}
    )
    await session.commit()
    rows = list(result)
    if not rows:
        return UnwatchResult(status="not_found")

    logger.info("unwatch: tenant=%s sub_id=%s", tenant_id, sub_id)
    return UnwatchResult(status="success")
