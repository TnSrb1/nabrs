"""Regression R1: trial = the Pro rights on ALL the bot AI gates (and parity with the web).

Before the fix the AI gates read the raw ``tenants.tier`` (for a trial ``free``/``NULL`` → ``gate_pro``). Now they gate by ``effective_tier(status, tier)`` AFTER ``is_frozen``. We check for each gate site:

- trial + tier='free' AND tier=NULL, ``access_until`` in the future → the gate is PASSED (the status is NOT ``gate_pro``/``frozen``; further it runs into the mocked ``no_tender``);
- free/NULL WITHOUT trial (status='active') → ``gate_pro`` as before;
- pro (status='active') → the gate is passed (the behavior did not change);
- a frozen trial (``access_until`` in the past) → ``frozen`` (Pro is NOT given).

Mock session: 1st ``execute`` → the tenant's row, the subsequent ones → ``None`` (the tender is not found) — this is enough to distinguish “passed the gate” from «gate_pro». The model is not called (gate-only). ``resolve_tenant_id`` is mocked.
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
_TENANT = "dddd4444-dddd-dddd-dddd-dddddddddddd"
_TENDER = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_PROFILE = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _row(*, tier, status, access_until):
    return SimpleNamespace(tier=tier, status=status, access_until=access_until)


def _seq_session(first_row):
    """Mock session: 1st execute → the tenant’s row, then None (a gate-only test)."""
    idx = [0]

    async def _exec(sql, params=None):
        i = idx[0]
        idx[0] += 1
        r = MagicMock()
        r.first.return_value = first_row if i == 0 else None
        r.all.return_value = []
        return r

    s = AsyncMock()
    s.execute = _exec
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    return s


# --------------------------------------------------------------------------- #
# Invokers of the gate sites: (module_name, callable) → status. resolve_tenant_id is mocked.
# --------------------------------------------------------------------------- #


async def _call_bid(session):
    from src.bots import bid_verdict_service as m

    with patch.object(m, "resolve_tenant_id", AsyncMock(return_value=_TENANT)):
        res = await m.run_bid_verdict(
            session,
            tender_id=_TENDER,
            profile_id=_PROFILE,
            chat_id=1,
            ai_client=MagicMock(),
            tier_limits=_TIER_LIMITS,
        )
    return res.status


async def _call_checklist(session):
    from src.bots import compliance_checklist_service as m

    with patch.object(m, "resolve_tenant_id", AsyncMock(return_value=_TENANT)):
        res = await m.run_compliance_checklist(
            session,
            tender_id=_TENDER,
            chat_id=1,
            ai_client=MagicMock(),
            tier_limits=_TIER_LIMITS,
        )
    return res.status


async def _call_draft(session):
    from src.bots import draft_service as m

    with patch.object(m, "resolve_tenant_id", AsyncMock(return_value=_TENANT)):
        res = await m.run_draft_application(
            session,
            tender_id=_TENDER,
            chat_id=1,
            ai_client=MagicMock(),
            tier_limits=_TIER_LIMITS,
        )
    return res.status


async def _call_deep(session):
    from src.bots import deep_analysis_service as m

    with patch.object(m, "resolve_tenant_id", AsyncMock(return_value=_TENANT)):
        res = await m.run_deep_analysis(
            session,
            tender_id=_TENDER,
            chat_id=1,
            ai_client=MagicMock(),
            tier_limits=_TIER_LIMITS,
        )
    return res.status


async def _call_customer(session):
    from src.bots import awards_analytics_service as m

    with patch.object(m, "resolve_tenant_id", AsyncMock(return_value=_TENANT)):
        res = await m.run_customer_analytics(
            session, tender_id=_TENDER, chat_id=1, tier_limits=_TIER_LIMITS
        )
    return res.status


async def _call_competitor(session):
    from src.bots import awards_analytics_service as m

    with patch.object(m, "resolve_tenant_id", AsyncMock(return_value=_TENANT)):
        res = await m.run_competitor_analytics(
            session, tender_id=_TENDER, chat_id=1, tier_limits=_TIER_LIMITS
        )
    return res.status


async def _call_reprocurement(session):
    from src.bots import awards_analytics_service as m

    with patch.object(m, "resolve_tenant_id", AsyncMock(return_value=_TENANT)):
        res = await m.run_reprocurement_forecast(
            session, tender_id=_TENDER, chat_id=1, tier_limits=_TIER_LIMITS
        )
    return res.status


async def _call_award_watch(session):
    from src.bots import award_watch_service as m

    with patch.object(m, "resolve_tenant_id", AsyncMock(return_value=_TENANT)):
        res = await m.run_watch_winner(session, tender_id=_TENDER, chat_id=1)
    return res.status


_GATES = [
    ("bid", _call_bid),
    ("checklist", _call_checklist),
    ("draft", _call_draft),
    ("deep", _call_deep),
    ("customer_analytics", _call_customer),
    ("competitor_analytics", _call_competitor),
    ("reprocurement", _call_reprocurement),
    ("award_watch", _call_award_watch),
]

_PASSED = {"gate_pro", "frozen", "no_account"}  # the statuses “the gate is NOT passed”


@pytest.mark.asyncio
@pytest.mark.parametrize("name,call", _GATES, ids=[g[0] for g in _GATES])
@pytest.mark.parametrize("raw_tier", ["free", None], ids=["tier_free", "tier_null"])
async def test_trial_passes_all_ai_gates(name, call, raw_tier):
    """trial + free/NULL + access active → the gate is PASSED (not gate_pro/frozen)."""
    session = _seq_session(_row(tier=raw_tier, status="trial", access_until=_FUTURE))
    status = await call(session)
    assert status not in _PASSED, f"{name}: trial(tier={raw_tier}) → {status}"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,call", _GATES, ids=[g[0] for g in _GATES])
@pytest.mark.parametrize("raw_tier", ["free", None], ids=["tier_free", "tier_null"])
async def test_free_without_trial_is_gated(name, call, raw_tier):
    """free/NULL WITHOUT trial (status='active') → gate_pro (as before)."""
    session = _seq_session(_row(tier=raw_tier, status="active", access_until=_FUTURE))
    status = await call(session)
    assert status == "gate_pro", f"{name}: active(tier={raw_tier}) → {status}"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,call", _GATES, ids=[g[0] for g in _GATES])
async def test_pro_unchanged(name, call):
    """pro (status='active') → the gate is passed (the behavior did not change)."""
    session = _seq_session(_row(tier="pro", status="active", access_until=_FUTURE))
    status = await call(session)
    assert status not in _PASSED, f"{name}: pro → {status}"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,call", _GATES, ids=[g[0] for g in _GATES])
async def test_frozen_trial_gets_frozen_not_pro(name, call):
    """A frozen trial (access_until in the past) → frozen, Pro is NOT given."""
    session = _seq_session(_row(tier="free", status="trial", access_until=_PAST))
    status = await call(session)
    assert status == "frozen", f"{name}: frozen-trial → {status}"
