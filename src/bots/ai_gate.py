"""Read-only AI-level gate reader for the Telegram handlers (#513).

Telegram answers the press of an inline button EXACTLY ONCE (``callback.answer``),
and all four AI handlers (``ai:deep/bid/chk/drf``) already call it immediately
with a progress toast — before the service/model call (anti-staleness 10 s). So,
in order to show a pop-up alert about a refusal on the limit, the refusal must be known
BEFORE this first ``callback.answer`` — but the gate check itself lived INSIDE
each service (``bid_verdict_service`` / ``deep_analysis_service`` /
``compliance_checklist_service`` / ``draft_service``), four copies of one
predicate, and was visible only by the status returned AFTER the toast.

This module is the fifth, COMMON reading of the same predicate, specifically for the early
call from the handler. The order of the axes — a mirror of the steps a/b/d of the docstring of
``bid_verdict_service.run_bid_verdict`` (moved, not rewritten):
resolve_tenant_id → freeze (``is_frozen``) → the effective tier
(``effective_tier``) → ``tier_row[level] == 0`` → ``limit_decision``.

‼️ READ ONLY: ``limit_decision`` debits nothing and does not move the cache —
the debit stays exactly where it is now (the second, full pass of the
same predicate inside the service, if the gate here gave ``ok``). There is no double debit
for one press.

‼️ The gate does NOT check the cache (step c of the services, skipped deliberately): a client with
an exhausted limit will get an early refusal even for an already cached
tender+profile, although the service would give it for free (a cache hit
debits nothing). A deliberate trade-off by the brief — the predicate here is the same as in the
diagnosis (frozen → tier → limit_decision), without the cache step; a full repeated
cache lookup here would double the reads for the rare repeated visit on an exhausted
limit. If it becomes noticeable in production — move the cache lookup here too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import text

from src.ai.usage import _TIER_LIMITS, limit_decision
from src.bots.status_service import resolve_tenant_id
from src.domain.access import is_frozen
from src.domain.tiers import effective_tier

GateStatus = Literal["ok", "no_account", "frozen", "gate_tier", "limit_exhausted"]

# A mirror of _LOAD_TENANT_TIER_SQL from bid_verdict_service.py / deep_analysis_service.py /
# compliance_checklist_service.py / draft_service.py — the same selection, the same meaning.
_LOAD_TENANT_TIER_SQL = text("""
    SELECT tier, access_until, status FROM tenants WHERE id = :tenant_id
    """)


@dataclass(frozen=True)
class GateDecision:
    """A snapshot of the AI level gate at the moment of the call — without side effects."""

    status: GateStatus
    tenant_id: str | None = None
    tier: str | None = None


async def ai_gate(
    session,
    *,
    chat_id: int,
    level: str,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
) -> GateDecision:
    """A read-only decision of the gate of the level ``level`` for the Telegram ``chat_id``.

    Returns one of the ``GateStatus``:
    - ``ok`` — the call is allowed (the service can be called as before).
    - ``no_account`` — the chat_id is not bound to any tenant.
    - ``frozen`` — the tenant’s access has expired (``is_frozen``), it is CHECKED BEFORE the tier.
    - ``gate_tier`` — the level is not available on the tier (``tier_row[level] == 0``).
    - ``limit_exhausted`` — the monthly limit of the level is exhausted (§14.5).

    The order of the axes is FIXED: a frozen tenant with a simultaneously exhausted
    limit gets ``frozen``, not ``limit_exhausted`` — the same as
    in the services that this gate mirrors.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return GateDecision(status="no_account")

    row = (
        await session.execute(_LOAD_TENANT_TIER_SQL, {"tenant_id": tenant_id})
    ).first()
    access_until = getattr(row, "access_until", None) if row is not None else None
    if is_frozen(access_until, datetime.now(timezone.utc)):
        return GateDecision(status="frozen", tenant_id=tenant_id)

    status_val = str(row.status) if row and getattr(row, "status", None) else None
    raw_tier = str(row.tier) if row and row.tier else None
    tier = effective_tier(status_val, raw_tier)
    tier_row = limits.get(tier, {})
    if tier_row.get(level, 0) == 0:
        return GateDecision(status="gate_tier", tenant_id=tenant_id, tier=tier)

    decision = await limit_decision(
        session, tenant_id=tenant_id, tier=tier, level=level, tier_limits=limits
    )
    if decision == "exhausted":
        return GateDecision(status="limit_exhausted", tenant_id=tenant_id, tier=tier)

    return GateDecision(status="ok", tenant_id=tenant_id, tier=tier)
