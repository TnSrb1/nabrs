"""Shared helpers for AI usage accounting (§14.5).

Moved out of :mod:`src.workers.ai` for reuse in the on-demand L3
(:mod:`src.bots.deep_analysis_service`) without duplicating code:

- :data:`_TIER_LIMITS` — limits of the levels by tier (0 = no access, None = no limit).
- :func:`period_now` — the current accounting period YYYY-MM (UTC).
- :func:`cache_lookup` — SELECT of the cached analysis by (tender_id, profile_id, level).
- :func:`used_count` — the ONLY read of the spent amount (gate and UI counter).
- :func:`limit_decision` — check of the monthly limit of the level by tier.
- :func:`auto_limit_decision` — gate of the AUTO analysis of the flow (tier/demo trial).
- :func:`usage_snapshot` — the remainder over all levels of the tier, for showing to the client.
- :func:`record_usage` — UPSERT ai_usage without writing an analysis (translation VG-26).
- :func:`finalize_with_insert` — INSERT tender_ai_analyses + UPSERT ai_usage + status.

Dependencies are injected (session, tier_limits) — no network for unit tests.

‼️ The RLS contour of the readers is DIFFERENT, and it was verified by action on 15.08.2026, not by
the docstring: the AI worker (``deploy-ai-1``) runs under the role ``tendersrb``
(``rolsuper=t``, ``rolbypassrls=t``) — the ``tenant_isolation`` policy on
``ai_usage`` does not touch it, ``set_tenant`` is not needed; the web (``deploy-api-1``)
runs under ``tendersrb_app`` (NOBYPASSRLS, the table is FORCE RLS) — there ``set_tenant``
must be the FIRST action of the transaction, otherwise the counter silently shows zeros.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.domain.tiers import AUTO_ANALYSIS_LEVELS, effective_tier, normalize_tier

logger = logging.getLogger(__name__)

# Limits L1/L2/L3/bid/checklist/draft/translate by plan (§14.5). None = no limit,
# 0 = not available on the plan.
# The single source of truth — used by both workers/ai.py and deep_analysis_service/bid_verdict_service/compliance_checklist_service/draft_service.
#
# ‼️ THE LIMITS ARE CALCULATED FOR THE PUBLIC TEST, not for “as much as fits”: trial = the rights of Pro
# (``src/domain/tiers.effective_tier``, ARCHITECTURE §4), and sign-up on the site is
# self-service. So ANY person who signed up gets the Pro limits, and
# there must be no unlimited PAID levels on pro/business: earlier bid/checklist
# on pro/business were None → unlimited Anthropic spend on self-service.
# The ceiling is lifted only on enterprise (a contractual plan, paid by actual use).
#
# bid (VG-08): Pro+ only, balanced tier = claude-haiku-5-5 (infra/config.yaml), cheaper than deep.
# (the figure $0.02 per call — from VG-08, not recalculated for haiku-5-5.)
# checklist (VG-09): Pro+ only, balanced tier (haiku-5-5) — the same rule as bid. (the figure $0.003 — from VG-09, not recalculated for haiku-5-5.)
# draft (VG-10): Pro+ only, the model is differentiated by plan (pro→balanced=haiku-5-5, business/enterprise→premium=opus-5-5).
# translate (VG-26): translation of the card/description by button — a cheap model, but the most
# clicked level (every display of a foreign language). The limit unit — ONE
# user translation (title OR description), regardless of the number of chunks;
# a cache hit does NOT spend the limit (see ``src/ai/translation.TranslationUsage``).
_TIER_LIMITS: dict[str, dict[str, int | None]] = {
    "lite": {
        "quick": 0,
        "standard": 0,
        "deep": 0,
        "bid": 0,
        "checklist": 0,
        "draft": 0,
        "translate": 20,
    },
    "starter": {
        "quick": 200,
        "standard": 50,
        "deep": 0,
        "bid": 0,
        "checklist": 0,
        "draft": 0,
        "translate": 100,
    },
    "pro": {
        # ‼️ Finite limits instead of None (15.08.2026, owner decision). No limit on the
        # auto analysis — this is unlimited spend: the subscription spends money without
        # any human action, for each delivered tender’s announcement
        # (~170 per day, ≈$0.022 each at a median description of 50,002 characters).
        # 400 analyzes ≈ $9 per month per tenant — a ceiling, not the expected bill:
        # a profile with a normal topic gives dozens of matches, not thousands.
        # None remains only for enterprise (contractual).
        "quick": 400,
        "standard": 400,
        "deep": 10,
        "bid": 30,
        "checklist": 30,
        "draft": 5,
        "translate": 400,
    },
    "business": {
        "quick": 1500,
        "standard": 1500,
        "deep": 50,
        "bid": 150,
        "checklist": 150,
        "draft": 30,
        "translate": 1500,
    },
    "enterprise": {
        "quick": None,
        "standard": None,
        "deep": None,
        "bid": None,
        "checklist": None,
        "draft": None,
        "translate": None,
    },
}

_SOFT_LIMIT_FRACTION = 0.8

# ‼️ DEMO of the auto analysis on the trial (owner decision of 15.08.2026). The trial gives the rights of Pro
# (``effective_tier``), but the auto analysis is spend WITHOUT a human action, therefore
# on the trial it works exactly like a display: N analyzes "so that one can see how it
# looks", after that — only on the chosen plan.
#
# The unit — ONE analyzed tender. The counter is for the WHOLE trial (the sum of ``ai_usage``
# over all periods of the level ``standard``), and NOT monthly: 14 days × a monthly limit
# would again give the paid flow for free. The metric is exact, because the level
# ``standard`` is created ONLY by the auto worker: button-driven analyzes are written with the levels
# bid/checklist/draft/deep (verified by the predicate on 15.08.2026,
# ``tender_ai_generate.GENERATABLE_LEVELS``).
TRIAL_AUTO_DEMO_LIMIT = 10

# SQL queries (the same tables as in workers/ai.py).
#
# PITFALL: do NOT combine a repeated named :profile_id with ::uuid IS NULL
# in one text() query — with profile_id=None asyncpg does not bind the parameter
# (the log parameters contain only 2 values instead of 3).
# Decision: two separate queries by the profile_id is None branch.
_CACHE_LOOKUP_NULL_SQL = text("""
    SELECT id FROM tender_ai_analyses
    WHERE tender_id = :tender_id
      AND level = :level
      AND profile_id IS NULL
    LIMIT 1
    """)

_CACHE_LOOKUP_PROFILE_SQL = text("""
    SELECT id FROM tender_ai_analyses
    WHERE tender_id = :tender_id
      AND level = :level
      AND profile_id = :profile_id
    LIMIT 1
    """)

_USAGE_COUNT_SQL = text("""
    SELECT count FROM ai_usage
    WHERE tenant_id = :tenant_id AND period = :period AND level = :level
    """)

# Spend of the level over ALL periods — the counter of the trial’s demo analyzes. A separate SQL, and
# not `period IS NULL OR period = :period` in one query: a repeated named
# parameter with a cast breaks asyncpg (see the pitfall above), and the meaning is different anyway.
# SUM, not count: there are as many rows per tenant as calendar months the trial
# touched (14 days may fall across a month boundary — then there are two).
_USAGE_COUNT_ALL_PERIODS_SQL = text("""
    SELECT COALESCE(SUM(count), 0) AS count FROM ai_usage
    WHERE tenant_id = :tenant_id AND level = :level
    """)

_INSERT_ANALYSIS_SQL = text("""
    INSERT INTO tender_ai_analyses
        (tender_id, profile_id, level, model, output,
         tokens_input, tokens_output, tokens_cached, cost_usd)
    VALUES
        (:tender_id, :profile_id, :level, :model, CAST(:output AS jsonb),
         :ti, :to, :tc, :cost)
    RETURNING id
    """)

# The counter increment is the parameter ``:calls``, not the constant 1: the translation
# debits ONE limit unit per user action (see record_usage),
# the analysis — one too (finalize_with_insert passes calls=1). One UPSERT for both
# meanings: there is no point in letting two copies of this query diverge.
_UPSERT_USAGE_SQL = text("""
    INSERT INTO ai_usage (tenant_id, period, level, count, tokens_total, cost_usd_total)
    VALUES (:tenant_id, :period, :level, CAST(:calls AS integer), :tokens, :cost)
    ON CONFLICT (tenant_id, period, level) DO UPDATE
    SET count          = COALESCE(ai_usage.count, 0) + EXCLUDED.count,
        tokens_total   = COALESCE(ai_usage.tokens_total, 0) + EXCLUDED.tokens_total,
        cost_usd_total = COALESCE(ai_usage.cost_usd_total, 0) + EXCLUDED.cost_usd_total
    """)


def period_now() -> str:
    """The current accounting period ``YYYY-MM`` (UTC) for ``ai_usage``."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def cache_lookup(
    session: Any, tender_id: str, profile_id: str | None, level: str
) -> str | None:
    """SELECT of the existing analysis by UNIQUE(tender_id, profile_id, level).

    For the levels with a shared cache (L1 quick, L3 deep) pass ``profile_id=None``.
    Returns the string UUID of the analysis or ``None`` on a miss.

    Uses two separate SQL queries by the ``profile_id is None`` branch, to avoid a repeated named
    parameter ``:profile_id`` with a ``::uuid IS NULL`` cast — the combination breaks asyncpg when the value is None.
    """
    if profile_id is None:
        row = (
            await session.execute(
                _CACHE_LOOKUP_NULL_SQL,
                {"tender_id": tender_id, "level": level},
            )
        ).first()
    else:
        row = (
            await session.execute(
                _CACHE_LOOKUP_PROFILE_SQL,
                {"tender_id": tender_id, "level": level, "profile_id": profile_id},
            )
        ).first()
    return str(row.id) if row else None


async def used_count(
    session: Any,
    *,
    tenant_id: str | None,
    level: str,
    period: str | None,
) -> int:
    """Units of the limit spent by the level ``level`` of the tenant.

    The ONLY place where the spend is read: both the gate (:func:`limit_decision`,
    :func:`auto_limit_decision`) and the remainder counter in the cabinet (:func:`usage_snapshot`)
    call this one. A predicate that duplicates another module’s query must be
    mirrored, not rewritten (CLAUDE.md): otherwise the client sees one
    number while another one refuses them.

    ``period`` — the accounting period ``YYYY-MM`` or ``None`` = over ALL periods
    (the trial’s demo quota is counted over the whole trial, not per month).
    """
    if period is None:
        row = (
            await session.execute(
                _USAGE_COUNT_ALL_PERIODS_SQL,
                {"tenant_id": tenant_id, "level": level},
            )
        ).first()
    else:
        row = (
            await session.execute(
                _USAGE_COUNT_SQL,
                {"tenant_id": tenant_id, "period": period, "level": level},
            )
        ).first()
    return int(row.count) if row and row.count is not None else 0


async def limit_decision(
    session: Any,
    *,
    tenant_id: str | None,
    tier: str | None,
    level: str,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
) -> str:
    """Checks the monthly limit of the level by the tenant's plan (§14.5).

    Returns one of the strings:
    - ``'ok'`` — the pass is allowed (the limit is not exhausted or there is no limit).
    - ``'unavailable'`` — the level is not available on the plan (0 / plan unknown).
    - ``'exhausted'`` — the monthly limit is exhausted.

    The ``soft`` threshold (80%) is logged as a warning, but the pass is allowed.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS
    tier_row = limits.get(tier or "", {})
    if level not in tier_row:
        return "unavailable"
    limit = tier_row[level]
    if limit == 0:
        return "unavailable"
    if limit is None:
        return "ok"

    used = await used_count(
        session, tenant_id=tenant_id, level=level, period=period_now()
    )
    if used >= limit:
        return "exhausted"
    if used >= int(limit * _SOFT_LIMIT_FRACTION):
        logger.warning(
            "ai: soft limit %s tenant=%s %d/%d (80%%+)", level, tenant_id, used, limit
        )
    return "ok"


async def auto_limit_decision(
    session: Any,
    *,
    tenant_id: str | None,
    status: str | None,
    tier: str | None,
    level: str,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
) -> str:
    """Gate of the AUTO analysis of the flow (``profiles.ai_analysis_level``) BEFORE the model call.

    It differs from :func:`limit_decision` in that it is not a human action that pays here,
    but the subscription itself: EVERY delivered tender is analyzed. Therefore the
    ``tenants.status`` axis here is not cosmetic, but part of the gate (owner decision of
    15.08.2026):

    - a level outside :data:`~src.domain.tiers.AUTO_ANALYSIS_LEVELS` (or ``none``)
      → ``'unavailable'``: ``quick`` gives service metadata that no template
      renders, ``deep`` is forbidden on the auto path by the circuit breaker;
    - ``status='trial'`` → DEMO: :data:`TRIAL_AUTO_DEMO_LIMIT` analyzes for the WHOLE
      trial (not per month) → ``'demo_exhausted'`` when exhausted. The rights of Pro
      (``effective_tier``) are deliberately NOT applied here: the auto analysis under the Pro
      rights on self-service sign-up = the paid flow for free;
    - ``status='active'`` → the usual monthly limit of the tier;
    - any other status (``suspended``/``cancelled``/``NULL``) →
      ``'unavailable'``: the auto analysis is switched on ONLY on a paid tier.

    The freeze axis (``access_until``) is NOT checked here — it is separate and
    is checked EARLIER by the calling code (``src/domain/access.is_frozen``).

    Returns ``'ok'`` / ``'unavailable'`` / ``'exhausted'`` / ``'demo_exhausted'``.
    """
    lvl = (level or "").strip()
    if lvl not in AUTO_ANALYSIS_LEVELS or lvl == "none":
        return "unavailable"

    st = (status or "").strip().lower()
    if st == "trial":
        used = await used_count(session, tenant_id=tenant_id, level=lvl, period=None)
        if used >= TRIAL_AUTO_DEMO_LIMIT:
            return "demo_exhausted"
        return "ok"
    if st == "active":
        # normalize_tier, NOT effective_tier: trial is handled by the branch above, and
        # there is nothing left to mix the Pro rights into here.
        return await limit_decision(
            session,
            tenant_id=tenant_id,
            tier=normalize_tier(tier),
            level=lvl,
            tier_limits=tier_limits,
        )
    return "unavailable"


# Levels shown to the client in the “AI limits” counter, in the display order.
# ``quick`` is deliberately NOT shown: it no longer gets into the auto path
# (``AUTO_ANALYSIS_LEVELS``), the buttons do not call it, and the line "used 0 of
# 400" about an unreachable level would be an interface lie, not completeness.
USAGE_DISPLAY_LEVELS: tuple[str, ...] = (
    "standard",
    "bid",
    "checklist",
    "draft",
    "deep",
    "translate",
)

# The level of the flow’s auto analysis: the only one that is spent WITHOUT a human action,
# and the only one that has its own demo quota on the trial.
AUTO_LEVEL = "standard"


@dataclass(frozen=True)
class LevelUsage:
    """The remainder of the limit of one level for showing to the client.

    ``scope``: ``'month'`` — the monthly limit of the plan, ``'trial'`` — the demo quota over
    the whole trial period. ``limit=None`` — no limit (enterprise).
    ``state``: ``'ok'`` / ``'exhausted'`` / ``'demo_exhausted'`` /
    ``'unavailable'`` — the same strings that the gates return
    (:func:`limit_decision`, :func:`auto_limit_decision`), so that the template does
    not invent a third dictionary of states.
    """

    level: str
    used: int
    limit: int | None
    scope: str
    state: str

    @property
    def left(self) -> int | None:
        """How much is left (``None`` — no limit; we do not drop below zero)."""
        if self.limit is None:
            return None
        return max(self.limit - self.used, 0)


async def usage_snapshot(
    session: Any,
    *,
    tenant_id: str | None,
    status: str | None,
    tier: str | None,
    levels: tuple[str, ...] = USAGE_DISPLAY_LEVELS,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
) -> list[LevelUsage]:
    """The remainder over all limited levels of the tenant — for showing in the cabinet.

    ‼️ It reads the spend with the SAME :func:`used_count` as the gates, and on the same
    status axis as :func:`auto_limit_decision`: “nothing must switch off silently” works only if the counter and the refusal count the same number.

    For ``status='trial'`` the auto-analysis row (``standard``) shows the
    demo quota over the whole trial; the button levels on the trial are counted by the rights of Pro
    (``effective_tier``) — there the human action pays, and this has not changed.

    ‼️ A call from the web (role ``tendersrb_app``) must run AFTER
    ``repo.set_tenant`` — ``ai_usage`` is under FORCE RLS, otherwise zeros come back.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS
    st = (status or "").strip().lower()
    tier_row = limits.get(effective_tier(st, tier), {})

    out: list[LevelUsage] = []
    for level in levels:
        is_auto_demo = st == "trial" and level == AUTO_LEVEL
        if is_auto_demo:
            limit: int | None = TRIAL_AUTO_DEMO_LIMIT
            scope = "trial"
            period: str | None = None
        elif level == AUTO_LEVEL and st != "active":
            # The auto analysis outside trial/active does not work at all (auto_limit_decision
            # returns unavailable) — we show this, and not the monthly limit of the plan.
            limit, scope, period = 0, "month", period_now()
        else:
            limit = tier_row.get(level, 0)
            scope = "month"
            period = period_now()

        used = await used_count(
            session, tenant_id=tenant_id, level=level, period=period
        )
        if limit == 0:
            state = "unavailable"
        elif limit is not None and used >= limit:
            state = "demo_exhausted" if is_auto_demo else "exhausted"
        else:
            state = "ok"
        out.append(
            LevelUsage(level=level, used=used, limit=limit, scope=scope, state=state)
        )
    return out


async def record_usage(
    session: Any,
    *,
    tenant_id: str | None,
    level: str,
    calls: int = 1,
    tokens: int = 0,
    cost: float = 0.0,
) -> None:
    """UPSERT ``ai_usage`` WITHOUT writing an analysis — accounting of the paid calls outside L1/L2/L3.

    ``finalize_with_insert`` cannot be reused here by its meaning: it writes the
    ``tender_ai_analyses`` row, while the translation (VG-26) creates no artifact —
    the result lives in the Redis cache. Nevertheless it must be accounted for: without a record
    in ``ai_usage`` the level is visible neither to the limit nor to the spend report.

    ``calls`` — the increment of the limit counter (a unit = one USER action),
    ``tokens``/``cost`` — the actual sum over all model calls
    within this action (the translation of a description = several chunks).

    ‼️ The decision “debit or not” is made by the CALLER (a cache hit = 0 model
    calls → this function should not be called at all): there is no second predicate here.
    ‼️ RLS: ``ai_usage`` is under the tenant policy → ``set_tenant`` must be the FIRST
    action of the transaction, otherwise the write goes to 0 rows fail-closed.
    """
    await session.execute(
        _UPSERT_USAGE_SQL,
        {
            "tenant_id": tenant_id,
            "period": period_now(),
            "level": level,
            "calls": calls,
            "tokens": tokens,
            "cost": cost,
        },
    )
    await session.commit()
    logger.info(
        "ai: usage tenant=%s level=%s calls=%d tokens=%d cost=$%.4f",
        tenant_id,
        level,
        calls,
        tokens,
        cost,
    )


async def record_error_spend(
    session: Any,
    *,
    tenant_id: str | None,
    level: str,
    exc: BaseException,
) -> None:
    """The spend of a call that did not produce an accepted result (truncation/model refusal).

    The model answered and the tokens are paid — we write them and the price into ``ai_usage``, but with
    ``calls=0``: the limit unit is NOT debited (the user action did not take place). The error status
    does not change — the caller still gets the failure. An error without ``spend`` (a failure before the model’s
    answer) — a no-op.
    """
    spend = getattr(exc, "spend", None)
    if spend is None:
        return
    await record_usage(
        session,
        tenant_id=tenant_id,
        level=level,
        calls=0,
        tokens=spend.tokens,
        cost=spend.cost_usd,
    )


async def finalize_with_insert(
    session: Any,
    *,
    tender_id: str,
    profile_id: str | None,
    tenant_id: str | None,
    level: str,
    result: Any,
) -> str | None:
    """INSERT tender_ai_analyses + UPSERT ai_usage. Returns the UUID of the analysis record.

    On a race on UNIQUE(tender_id, profile_id, level) — rollback + a repeated
    cache_lookup; returns the found UUID (the work is not lost).
    Returns ``None`` on an unresolvable conflict (an edge case).
    """
    try:
        inserted = (
            await session.execute(
                _INSERT_ANALYSIS_SQL,
                {
                    "tender_id": tender_id,
                    "profile_id": profile_id,
                    "level": level,
                    "model": result.model,
                    "output": json.dumps(result.output, ensure_ascii=False),
                    "ti": result.tokens_input,
                    "to": result.tokens_output,
                    "tc": result.tokens_cached,
                    "cost": result.cost_usd,
                },
            )
        ).first()
        analysis_id = str(inserted.id)
    except IntegrityError:
        await session.rollback()
        existing = await cache_lookup(session, tender_id, profile_id, level)
        if existing is None:
            logger.error(
                "ai: UNIQUE conflict without a found cache tender=%s level=%s",
                tender_id,
                level,
            )
            return None
        await session.commit()
        logger.info("ai: UNIQUE race → using the existing analysis=%s", existing)
        return existing

    await session.execute(
        _UPSERT_USAGE_SQL,
        {
            "tenant_id": tenant_id,
            "period": period_now(),
            "level": level,
            "calls": 1,
            "tokens": result.tokens_input + result.tokens_output,
            "cost": result.cost_usd,
        },
    )
    await session.commit()
    logger.info(
        "ai: finalize tender=%s level=%s analysis=%s cost=$%.4f",
        tender_id,
        level,
        analysis_id,
        result.cost_usd,
    )
    return analysis_id
