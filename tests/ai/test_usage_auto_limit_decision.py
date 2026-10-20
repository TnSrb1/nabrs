"""Unit tests of the gate of AUTO-analysis of the flow (``src/ai/usage.auto_limit_decision``).

The difference from ``limit_decision`` is that it is not a person’s action that pays but the
subscription itself: EVERY delivered tender is analyzed. Therefore the axis ``tenants.status`` is checked here, which the button gate does not have (the owner’s decision of 15.08.2026):

- ``active`` → the monthly limit of the plan;
- ``trial``  → the DEMO ``TRIAL_AUTO_DEMO_LIMIT`` for the WHOLE trial (not per month);
- other (``suspended``/``cancelled``/``NULL``) → a refusal without a single paid call;
- a level outside ``AUTO_ANALYSIS_LEVELS`` (``quick``/``deep``/garbage) → a refusal.

Plus ``usage_snapshot`` — the same spend in the client’s eyes: the counter and the refusal must
count ONE number (otherwise “exhausted” comes with “5 left”).
No DB is needed: the session is mocked (ADR-013).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.ai.usage import (
    TRIAL_AUTO_DEMO_LIMIT,
    auto_limit_decision,
    period_now,
    usage_snapshot,
)

_TENANT = "11111111-1111-1111-1111-111111111111"


class _Session:
    """Mock session: returns ``count`` by level and remembers the parameters of the requests."""

    def __init__(self, counts: dict[str, int] | int | None = None):
        if counts is None:
            counts = {}
        self._counts = counts
        self.params: list[dict] = []

    async def execute(self, sql, params=None):
        self.params.append(params or {})
        level = (params or {}).get("level")
        value = (
            self._counts if isinstance(self._counts, int) else self._counts.get(level)
        )
        rows = [SimpleNamespace(count=value)] if value is not None else []
        return SimpleNamespace(first=lambda: rows[0] if rows else None)


# --------------------------------------------------------------------------- #
#  level: only standard is admitted to the auto path
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", ["none", "quick", "deep", "", "telepathy"])
@pytest.mark.asyncio
async def test_non_auto_levels_unavailable_without_sql(level):
    s = _Session()
    d = await auto_limit_decision(
        s, tenant_id=_TENANT, status="active", tier="pro", level=level
    )
    assert d == "unavailable"
    assert s.params == []  # the spend is not even read


# --------------------------------------------------------------------------- #
#  status: auto-analysis is switched on only on a paid plan or in the demo
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [None, "", "suspended", "cancelled", "pending"])
@pytest.mark.asyncio
async def test_non_paying_status_unavailable_without_sql(status):
    s = _Session({"standard": 0})
    d = await auto_limit_decision(
        s, tenant_id=_TENANT, status=status, tier="pro", level="standard"
    )
    assert d == "unavailable"
    assert s.params == []


@pytest.mark.asyncio
async def test_active_pro_within_monthly_limit_is_ok():
    s = _Session({"standard": 399})  # pro standard = 400
    d = await auto_limit_decision(
        s, tenant_id=_TENANT, status="active", tier="pro", level="standard"
    )
    assert d == "ok"
    # monthly limit → the spend is read FOR THE PERIOD
    assert s.params[0]["period"] == period_now()


@pytest.mark.asyncio
async def test_active_pro_at_limit_is_exhausted():
    s = _Session({"standard": 400})
    d = await auto_limit_decision(
        s, tenant_id=_TENANT, status="active", tier="pro", level="standard"
    )
    assert d == "exhausted"


@pytest.mark.asyncio
async def test_active_lite_has_no_auto_analysis():
    """Lite: ``standard`` = 0 in the plan grid → unavailable, not “exhausted”."""
    s = _Session({"standard": 0})
    d = await auto_limit_decision(
        s, tenant_id=_TENANT, status="active", tier="lite", level="standard"
    )
    assert d == "unavailable"


@pytest.mark.asyncio
async def test_active_unknown_tier_falls_back_to_lite_and_denies():
    """A garbage tier gives no rights: normalize_tier → lite → standard=0."""
    s = _Session({"standard": 0})
    d = await auto_limit_decision(
        s, tenant_id=_TENANT, status="active", tier="mythic", level="standard"
    )
    assert d == "unavailable"


# --------------------------------------------------------------------------- #
#  trial: the demo quota for the whole trial period
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_trial_below_demo_limit_is_ok():
    s = _Session({"standard": TRIAL_AUTO_DEMO_LIMIT - 1})
    d = await auto_limit_decision(
        s, tenant_id=_TENANT, status="trial", tier="lite", level="standard"
    )
    assert d == "ok"


@pytest.mark.asyncio
async def test_trial_at_demo_limit_is_demo_exhausted():
    s = _Session({"standard": TRIAL_AUTO_DEMO_LIMIT})
    d = await auto_limit_decision(
        s, tenant_id=_TENANT, status="trial", tier="lite", level="standard"
    )
    assert d == "demo_exhausted"


@pytest.mark.asyncio
async def test_trial_demo_counts_all_periods_not_current_month():
    """‼️ The demo is counted for the WHOLE trial: a monthly account would give the flow for free.

    We check by the FACT of the request: there is no ``period`` in the parameters.
    """
    s = _Session({"standard": 3})
    await auto_limit_decision(
        s, tenant_id=_TENANT, status="trial", tier="pro", level="standard"
    )
    assert "period" not in s.params[0]
    assert s.params[0]["level"] == "standard"


@pytest.mark.asyncio
async def test_trial_demo_ignores_pro_rights():
    """The trial has the Pro rights (``effective_tier``), but the auto-analysis does not go by them.

    400 Pro analyzes on a trial = a paid flow for free: the demo quota is tighter.
    """
    s = _Session({"standard": TRIAL_AUTO_DEMO_LIMIT})
    assert (
        await auto_limit_decision(
            s, tenant_id=_TENANT, status="trial", tier="pro", level="standard"
        )
        == "demo_exhausted"
    )


@pytest.mark.asyncio
async def test_empty_usage_row_counts_as_zero():
    s = _Session()  # there is no spend row at all
    d = await auto_limit_decision(
        s, tenant_id=_TENANT, status="trial", tier="pro", level="standard"
    )
    assert d == "ok"


@pytest.mark.asyncio
async def test_custom_tier_limits_are_injected():
    s = _Session({"standard": 5})
    limits = {"pro": {"standard": 5}}
    d = await auto_limit_decision(
        s,
        tenant_id=_TENANT,
        status="active",
        tier="pro",
        level="standard",
        tier_limits=limits,
    )
    assert d == "exhausted"


# --------------------------------------------------------------------------- #
#  usage_snapshot — the same number in the client’s eyes
# --------------------------------------------------------------------------- #


def _by_level(rows):
    return {r.level: r for r in rows}


@pytest.mark.asyncio
async def test_snapshot_matches_gate_for_active_pro():
    s = _Session({"standard": 400})
    rows = _by_level(
        await usage_snapshot(s, tenant_id=_TENANT, status="active", tier="pro")
    )
    assert rows["standard"].limit == 400
    assert rows["standard"].used == 400
    assert rows["standard"].left == 0
    assert rows["standard"].scope == "month"
    # the same word the gate would return
    assert rows["standard"].state == "exhausted"
    gate = await auto_limit_decision(
        _Session({"standard": 400}),
        tenant_id=_TENANT,
        status="active",
        tier="pro",
        level="standard",
    )
    assert gate == rows["standard"].state


@pytest.mark.asyncio
async def test_snapshot_trial_shows_demo_quota_for_auto_level():
    s = _Session({"standard": 4, "bid": 1})
    rows = _by_level(
        await usage_snapshot(s, tenant_id=_TENANT, status="trial", tier="lite")
    )
    auto = rows["standard"]
    assert (auto.limit, auto.used, auto.left, auto.scope) == (
        TRIAL_AUTO_DEMO_LIMIT,
        4,
        TRIAL_AUTO_DEMO_LIMIT - 4,
        "trial",
    )
    # the button levels on the trial are counted by the Pro rights and monthly
    assert rows["bid"].limit == 30
    assert rows["bid"].scope == "month"


@pytest.mark.asyncio
async def test_snapshot_trial_demo_exhausted_state():
    s = _Session({"standard": TRIAL_AUTO_DEMO_LIMIT})
    rows = _by_level(
        await usage_snapshot(s, tenant_id=_TENANT, status="trial", tier="pro")
    )
    assert rows["standard"].state == "demo_exhausted"
    assert rows["standard"].left == 0


@pytest.mark.asyncio
async def test_snapshot_marks_unavailable_levels_for_lite():
    s = _Session({})
    rows = _by_level(
        await usage_snapshot(s, tenant_id=_TENANT, status="active", tier="lite")
    )
    assert rows["standard"].state == "unavailable"
    assert rows["deep"].state == "unavailable"
    assert rows["translate"].state == "ok"  # lite translate = 20


@pytest.mark.asyncio
async def test_snapshot_enterprise_unlimited_has_no_left():
    s = _Session({"standard": 9999})
    rows = _by_level(
        await usage_snapshot(s, tenant_id=_TENANT, status="active", tier="enterprise")
    )
    assert rows["standard"].limit is None
    assert rows["standard"].left is None
    assert rows["standard"].state == "ok"


@pytest.mark.asyncio
async def test_snapshot_hides_quick_level():
    """``quick`` is unreachable (AUTO_ANALYSIS_LEVELS does not admit it to the auto path).

    The line “used 0 of 400” about a level that cannot be switched on is a lie of the interface, not completeness.
    """
    s = _Session({})
    rows = _by_level(
        await usage_snapshot(s, tenant_id=_TENANT, status="active", tier="pro")
    )
    assert "quick" not in rows


@pytest.mark.asyncio
async def test_snapshot_non_paying_status_shows_auto_unavailable():
    s = _Session({"standard": 0})
    rows = _by_level(
        await usage_snapshot(s, tenant_id=_TENANT, status="suspended", tier="pro")
    )
    assert rows["standard"].state == "unavailable"
