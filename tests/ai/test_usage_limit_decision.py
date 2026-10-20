"""Unit tests of limit_decision from src/ai/usage.py — the core of «allowed vs blocked».

It decides the availability of an AI level by the tier and the monthly limit (§14.5). Until now it was covered
only indirectly through the bot services; here — the direct branches without a DB
(mock session, ADR-013). The key boundaries: an unknown tier, limit=0, limit=None
(without SQL), used==limit (exhaustion), the soft threshold 80%, an empty counter row,
injection of custom tier_limits.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai.usage import limit_decision

_TENANT = "11111111-1111-1111-1111-111111111111"


def _session(count=None, *, has_row=True) -> AsyncMock:
    """Mock session: execute → result.first() returns a row with the counter.

    ``count`` — the value of ai_usage.count; ``has_row=False`` → first()=None.
    The number of execute calls is available via ``session.exec_calls``.
    """
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = SimpleNamespace(count=count) if has_row else None
    calls = {"n": 0}

    async def _exec(sql, params=None):
        calls["n"] += 1
        return result

    session.execute = _exec
    session.exec_calls = calls  # type: ignore[attr-defined]
    return session


# --------------------------------------------------------------------------- #
#  Branches without touching the DB (plan/level/limit=0/None)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_none_tier_unavailable_no_sql():
    s = _session()
    d = await limit_decision(s, tenant_id=_TENANT, tier=None, level="deep")
    assert d == "unavailable"
    assert s.exec_calls["n"] == 0  # unknown plan → the counter is not read


@pytest.mark.asyncio
async def test_unknown_tier_unavailable():
    s = _session()
    d = await limit_decision(s, tenant_id=_TENANT, tier="mythic", level="deep")
    assert d == "unavailable"
    assert s.exec_calls["n"] == 0


@pytest.mark.asyncio
async def test_unknown_level_unavailable():
    s = _session()
    d = await limit_decision(s, tenant_id=_TENANT, tier="pro", level="telepathy")
    assert d == "unavailable"
    assert s.exec_calls["n"] == 0


@pytest.mark.asyncio
async def test_zero_limit_unavailable_no_sql():
    # lite/deep = 0 → the level is unavailable on the plan, the counter is not read.
    s = _session()
    d = await limit_decision(s, tenant_id=_TENANT, tier="lite", level="deep")
    assert d == "unavailable"
    assert s.exec_calls["n"] == 0


@pytest.mark.asyncio
async def test_none_limit_ok_without_sql():
    """``limit=None`` → 'ok' WITHOUT touching ai_usage.

    ‼️ The expectation was moved from ``pro/quick`` to ``enterprise``: since 15.08.2026
    there are NO unlimited levels for pro/business (``None`` on the self-service registration = unlimited spend);
    ``None`` remained only for the contractual enterprise. The test on ``pro`` was failing on the branch before this fix.
    """
    s = _session()
    d = await limit_decision(s, tenant_id=_TENANT, tier="enterprise", level="quick")
    assert d == "ok"
    assert s.exec_calls["n"] == 0  # an unlimited level does not count the usage


# --------------------------------------------------------------------------- #
#  Branches with reading the counter (the monthly limit)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_under_limit_ok():
    # pro/deep = 10, used 3 → 'ok'.
    s = _session(count=3)
    d = await limit_decision(s, tenant_id=_TENANT, tier="pro", level="deep")
    assert d == "ok"
    assert s.exec_calls["n"] == 1


@pytest.mark.asyncio
async def test_exact_limit_exhausted_boundary():
    # used == limit (10/10) → exhausted (the boundary is inclusive).
    s = _session(count=10)
    d = await limit_decision(s, tenant_id=_TENANT, tier="pro", level="deep")
    assert d == "exhausted"


@pytest.mark.asyncio
async def test_over_limit_exhausted():
    s = _session(count=11)
    d = await limit_decision(s, tenant_id=_TENANT, tier="pro", level="deep")
    assert d == "exhausted"


@pytest.mark.asyncio
async def test_soft_limit_boundary_still_ok():
    # pro/deep=10, 80% = 8 → at the boundary of the soft threshold the pass is still 'ok'.
    s = _session(count=8)
    d = await limit_decision(s, tenant_id=_TENANT, tier="pro", level="deep")
    assert d == "ok"


@pytest.mark.asyncio
async def test_no_usage_row_treated_as_zero():
    # There is no ai_usage row (the first request of the month) → used=0 → 'ok'.
    s = _session(has_row=False)
    d = await limit_decision(s, tenant_id=_TENANT, tier="business", level="deep")
    assert d == "ok"


@pytest.mark.asyncio
async def test_null_count_treated_as_zero():
    # The row exists, but count=None → used=0 → 'ok'.
    s = _session(count=None, has_row=True)
    d = await limit_decision(s, tenant_id=_TENANT, tier="business", level="deep")
    assert d == "ok"


# --------------------------------------------------------------------------- #
#  The paid self-service levels: bid/checklist/translate (VG-SPEND)
#
#  ‼️ trial = the Pro rights, and the registration on the site is self-service. So
#  there cannot be unlimited PAID levels on pro/business: bid and checklist
#  were None → any registered user burned Anthropic without a ceiling. The tests
#  below nail exactly this: the ceiling EXISTS everywhere except enterprise (contractual).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", ["bid", "checklist"])
@pytest.mark.parametrize(("tier", "limit"), [("pro", 30), ("business", 150)])
@pytest.mark.asyncio
async def test_paid_levels_have_a_ceiling_on_pro_and_business(tier, limit, level):
    """At the limit — 'exhausted' (earlier here it was None → infinity)."""
    assert (
        await limit_decision(
            _session(count=limit - 1), tenant_id=_TENANT, tier=tier, level=level
        )
        == "ok"
    )
    assert (
        await limit_decision(
            _session(count=limit), tenant_id=_TENANT, tier=tier, level=level
        )
        == "exhausted"
    )


@pytest.mark.parametrize("level", ["bid", "checklist"])
@pytest.mark.asyncio
async def test_paid_levels_stay_unlimited_on_enterprise(level):
    """enterprise — a contractual plan with payment by the fact: no ceiling is introduced."""
    s = _session(count=10_000)
    d = await limit_decision(s, tenant_id=_TENANT, tier="enterprise", level=level)
    assert d == "ok"
    assert s.exec_calls["n"] == 0  # None → the counter is not even read


@pytest.mark.parametrize("level", ["bid", "checklist"])
@pytest.mark.parametrize("tier", ["lite", "starter"])
@pytest.mark.asyncio
async def test_paid_levels_remain_closed_below_pro(tier, level):
    """Regression of the Pro+ gate: on lite/starter the level is still unavailable."""
    s = _session(count=0)
    d = await limit_decision(s, tenant_id=_TENANT, tier=tier, level=level)
    assert d == "unavailable"
    assert s.exec_calls["n"] == 0


@pytest.mark.parametrize(
    ("tier", "limit"),
    [("lite", 20), ("starter", 100), ("pro", 400), ("business", 1500)],
)
@pytest.mark.asyncio
async def test_translate_has_a_ceiling_on_every_paid_tier(tier, limit):
    """‼️ Translation is a paid call on ANY tier, including lite.

    The level ``translate`` was absent from ``_TIER_LIMITS`` altogether before VG-SPEND,
    therefore ``limit_decision`` would return 'unavailable' — and the translation route did not
    even ask it: the translation went around the limit on all the tiers at once.
    """
    assert (
        await limit_decision(
            _session(count=limit - 1), tenant_id=_TENANT, tier=tier, level="translate"
        )
        == "ok"
    )
    assert (
        await limit_decision(
            _session(count=limit), tenant_id=_TENANT, tier=tier, level="translate"
        )
        == "exhausted"
    )


@pytest.mark.asyncio
async def test_translate_unlimited_on_enterprise():
    s = _session(count=99_999)
    d = await limit_decision(s, tenant_id=_TENANT, tier="enterprise", level="translate")
    assert d == "ok"
    assert s.exec_calls["n"] == 0


@pytest.mark.asyncio
async def test_translate_available_on_lite_unlike_analysis_levels():
    """Translation is available even on lite (it is reading a card, not analysis) —
    unlike deep/bid/checklist, which are closed by the Pro+ gate."""
    assert (
        await limit_decision(
            _session(count=0), tenant_id=_TENANT, tier="lite", level="translate"
        )
        == "ok"
    )
    assert (
        await limit_decision(
            _session(count=0), tenant_id=_TENANT, tier="lite", level="deep"
        )
        == "unavailable"
    )


# --------------------------------------------------------------------------- #
#  Injection of custom tier_limits (overrides _TIER_LIMITS)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_custom_tier_limits_override():
    custom = {"pro": {"deep": 2}}
    s = _session(count=2)
    d = await limit_decision(
        s, tenant_id=_TENANT, tier="pro", level="deep", tier_limits=custom
    )
    assert d == "exhausted"  # custom limit 2, used 2 → exhausted


@pytest.mark.asyncio
async def test_custom_tier_limits_unlimited():
    custom = {"pro": {"deep": None}}
    s = _session(count=9999)
    d = await limit_decision(
        s, tenant_id=_TENANT, tier="pro", level="deep", tier_limits=custom
    )
    assert d == "ok"
    assert s.exec_calls["n"] == 0  # None → without reading the counter
