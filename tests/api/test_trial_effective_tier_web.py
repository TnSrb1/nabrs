"""Regression R1 (web): trial = Pro rights on the web AI gates + parity with the bot.

The web gate sites:
- ``tender_ai_generate._prepare_generation`` (bid/checklist/draft from the cabinet);
- ``customer_analytics_service._gate`` (customer/competitor/re-procurement analytics).

Both now gate by ``effective_tier(status, tier)`` AFTER ``is_frozen``.
The mock session ignores ``set_config`` (the RLS ``set_tenant``) and gives the tenant row
for the plan query. Parity: for one (status, tier) the web and the bot give one verdict.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.usage import _TIER_LIMITS

_NOW = datetime.now(timezone.utc)
_FUTURE = _NOW + timedelta(days=30)
_PAST = _NOW - timedelta(days=1)
_TID = "11111111-1111-1111-1111-111111111111"
_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"


def _row(*, tier, status, access_until):
    return SimpleNamespace(tier=tier, status=status, access_until=access_until)


class _WebSession:
    """Mock AsyncSession: ``set_config`` (RLS) does not touch the row queue."""

    def __init__(self, tenant_row):
        self._rows = [tenant_row]  # only the plan; further queries → None
        self.executed: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append(sql)
        r = MagicMock()
        if "set_config" in sql or "app.current_tenant_id" in sql:
            r.first.return_value = None
            return r
        r.first.return_value = self._rows.pop(0) if self._rows else None
        r.all.return_value = []
        return r


# --------------------------------------------------------------------------- #
# The web invokers → status
# --------------------------------------------------------------------------- #


async def _web_generate(tenant_row, level="bid"):
    from src.api.cabinet import tender_ai_generate as m

    session = _WebSession(tenant_row)
    return await m.precheck_generation(
        session,
        tid=_TID,
        tender_id=_TENDER,
        profile_id=_PROFILE,
        level=level,
        tier_limits=_TIER_LIMITS,
    )


async def _web_gate(tenant_row):
    """A direct call of the _gate of the customer analytics (set_tenant is called by the route)."""
    from src.api.cabinet import customer_analytics_service as m

    session = _WebSession(tenant_row)
    return await m._gate(session, tid=_TID, limits=_TIER_LIMITS)


_WEB_PASSED = {"gate_pro", "frozen"}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_tier", ["free", None], ids=["tier_free", "tier_null"])
async def test_web_generate_trial_passes(raw_tier):
    """web-ai-2: trial + free/NULL is active → the gate is passed (not gate_pro/frozen)."""
    status = await _web_generate(
        _row(tier=raw_tier, status="trial", access_until=_FUTURE)
    )
    assert status not in _WEB_PASSED


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_tier", ["free", None], ids=["tier_free", "tier_null"])
async def test_web_generate_free_gated(raw_tier):
    status = await _web_generate(
        _row(tier=raw_tier, status="active", access_until=_FUTURE)
    )
    assert status == "gate_pro"


@pytest.mark.asyncio
async def test_web_generate_frozen_trial():
    status = await _web_generate(_row(tier="free", status="trial", access_until=_PAST))
    assert status == "frozen"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_tier", ["free", None], ids=["tier_free", "tier_null"])
async def test_web_gate_trial_passes(raw_tier):
    """web-ai-3/4 customer analytics: trial → _gate returns None (passed)."""
    err = await _web_gate(_row(tier=raw_tier, status="trial", access_until=_FUTURE))
    assert err is None


@pytest.mark.asyncio
async def test_web_gate_free_gated():
    err = await _web_gate(_row(tier="free", status="active", access_until=_FUTURE))
    assert err == "gate_pro"


@pytest.mark.asyncio
async def test_web_gate_frozen_trial():
    err = await _web_gate(_row(tier="free", status="trial", access_until=_PAST))
    assert err == "frozen"


# --------------------------------------------------------------------------- #
# Parity bot ↔ web: one gate verdict for one (status, tier)
# --------------------------------------------------------------------------- #


async def _bot_customer_gate_status(tenant_row):
    """The verdict of the bot gate of the customer analytics through run_customer_analytics."""
    from src.bots import awards_analytics_service as m

    idx = [0]

    async def _exec(sql, params=None):
        i = idx[0]
        idx[0] += 1
        r = MagicMock()
        r.first.return_value = tenant_row if i == 0 else None
        return r

    session = AsyncMock()
    session.execute = _exec
    with patch.object(m, "resolve_tenant_id", AsyncMock(return_value=_TID)):
        res = await m.run_customer_analytics(
            session, tender_id=_TENDER, chat_id=1, tier_limits=_TIER_LIMITS
        )
    # Bot: gate_pro/frozen — refusal; otherwise the gate is passed (it runs into no_tender).
    if res.status in {"gate_pro", "frozen"}:
        return res.status
    return "passed"


def _web_verdict(err):
    return err if err in {"gate_pro", "frozen"} else "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,tier,access,expected",
    [
        ("trial", "free", _FUTURE, "passed"),
        ("trial", None, _FUTURE, "passed"),
        ("active", "free", _FUTURE, "gate_pro"),
        ("active", None, _FUTURE, "gate_pro"),
        ("active", "pro", _FUTURE, "passed"),
        ("active", "business", _FUTURE, "passed"),
        ("trial", "free", _PAST, "frozen"),
        ("active", "pro", _PAST, "frozen"),
    ],
)
async def test_bot_web_parity(status, tier, access, expected):
    """One tenant → one gate verdict in the bot and in the web (customer analytics)."""
    row = _row(tier=tier, status=status, access_until=access)
    bot = await _bot_customer_gate_status(row)
    web = _web_verdict(await _web_gate(row))
    assert bot == web == expected, f"bot={bot} web={web} expected={expected}"
