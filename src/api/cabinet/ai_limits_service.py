"""The tenant’s remaining AI limits for display in the cabinet (read-only).

The owner’s requirement of 15.08.2026: "nothing may be switched off silently — the subscriber
must see that they have exhausted the limit, and this must work across all limited services".
Therefore the remainder is needed BEFORE the refusal, not only after it.

‼️ It is computed by the SAME predicate as the gates: :func:`src.ai.usage.usage_snapshot`
on top of :func:`src.ai.usage.used_count`, which is called by both ``limit_decision``
(button levels) and ``auto_limit_decision`` (auto-analysis of the stream). There is deliberately
no SQL of its own here — "a predicate duplicating another module’s selection is mirrored,
not rewritten“ (the project rules): otherwise the counter would show ”5 left" while the gate
refuses.

‼️ RLS: the web runs under ``tendersrb_app`` (NOBYPASSRLS), ``ai_usage`` and ``tenants``
under FORCE RLS → ``repo.set_tenant`` MUST be the FIRST action of the transaction,
otherwise the counter will silently show zeros. (The AI worker has a different contour — a role with
BYPASSRLS, verified by a run in the container, see ``src/workers/ai.py``.)
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from src.ai.usage import AUTO_LEVEL, LevelUsage, period_now, usage_snapshot
from src.api.cabinet import subscription_service
from src.domain.tiers import AUTO_ANALYSIS_LEVELS, effective_tier

# The auto-analysis level of a SPECIFIC profile + an explicit check of its belonging
# to the tenant (defense-in-depth against RLS — as in tender_ai_generate._LOAD_PROFILE_SQL).
_PROFILE_AI_LEVEL_SQL = text("""
    SELECT ai_analysis_level AS level
    FROM profiles
    WHERE id = CAST(:pid AS uuid)
      AND tenant_id = CAST(:tid AS uuid)
      AND deleted_at IS NULL
    LIMIT 1
    """)


@dataclass(frozen=True)
class AiLimitsView:
    """Snapshot of the tenant’s limits: rows by level + the axes by which they were read."""

    rows: tuple[LevelUsage, ...]
    period: str
    tier: str
    status: str | None

    def by_level(self, level: str) -> LevelUsage | None:
        """The usage row of a specific level or ``None`` if it is not shown.

        Needed by the tender card: the level’s button must know its state BEFORE
        the press — otherwise “Generate” leads to a refusal that the client would
        see only after the fact (the owner’s requirement “nothing may be switched off silently”).
        """
        for row in self.rows:
            if row.level == level:
                return row
        return None

    @property
    def auto(self) -> LevelUsage | None:
        """The auto-analysis row of the stream (on the trial this is the demo quota)."""
        for row in self.rows:
            if row.level == AUTO_LEVEL:
                return row
        return None


async def get_ai_limits(db, *, tid: str) -> AiLimitsView:
    """The remainder across all limited levels of the tenant ``tid``.

    ``repo.set_tenant(db, tid)`` must be called EARLIER in the same transaction.
    """
    # Plan/status is read with the other module’s existing predicate
    # (``subscription_service.get_tenant_tier``), not with a copy of its own of the same
    # SELECT: two copies of one meaning inevitably drift apart.
    tier, status = await subscription_service.get_tenant_tier(db, str(tid))
    rows = await usage_snapshot(db, tenant_id=str(tid), status=status, tier=tier)
    return AiLimitsView(
        rows=tuple(rows),
        period=period_now(),
        tier=effective_tier(status, tier),
        status=status,
    )


async def get_auto_analysis_state(
    db, *, tid: str, profile_id: str
) -> LevelUsage | None:
    """The auto-analysis state for the profile ``profile_id`` or ``None``.

    ``None`` = the profile did NOT enable auto-analysis (and there is nothing to explain). Otherwise —
    a usage row: ``state='ok'`` means “the analysis will be/was done”, everything
    else is the reason why the tender arrived without analysis, and the subscriber
    MUST be shown it (the worker at that moment silently degrades
    the row to an ordinary delivery, ``AiWorker._degrade_to_plain``).

    ``repo.set_tenant(db, tid)`` must be called EARLIER in the same transaction.
    """
    row = (
        await db.execute(
            _PROFILE_AI_LEVEL_SQL, {"pid": str(profile_id), "tid": str(tid)}
        )
    ).first()
    level = ((row.level if row is not None else None) or "").strip().lower()
    if level == "none" or level not in AUTO_ANALYSIS_LEVELS:
        return None
    limits = await get_ai_limits(db, tid=tid)
    return limits.auto
