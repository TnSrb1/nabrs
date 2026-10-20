"""Tests of the common read-only gate-reader of the AI levels of the bot (#513).

Without a network and without a real DB: a mock session via AsyncMock, the same style as
``tests/bots/test_bid_verdict_service.py`` (``_session_returning`` /
``_make_session_seq``). We cover:
- each status of ``GateStatus`` separately;
- the order of the axes: a frozen tenant with a simultaneously exhausted limit gives
  ``frozen``, NOT ``limit_exhausted`` (proved by a mock session, where the limit query
  is never executed — it would fail if the gate reached it);
- read-only: ``session.execute`` does not call INSERT/UPDATE (only the SELECT text).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.usage import _TIER_LIMITS

_TENANT_ID = "dddd4444-dddd-dddd-dddd-dddddddddddd"
_CHAT_ID = 123456789

_NOW = datetime.now(timezone.utc)
_PAST = _NOW - timedelta(days=1)
_FUTURE = _NOW + timedelta(days=365)


def _session_returning(row) -> AsyncMock:
    """Mock session: the single ``execute`` returns a result with ``.first() == row``."""
    result = MagicMock()
    result.first.return_value = row
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


def _make_session_seq(rows: list) -> AsyncMock:
    """A mock session with sequential responses to ``execute`` (positional)."""
    call_idx = [0]
    session = AsyncMock()

    async def _exec(sql, params=None):
        idx = call_idx[0]
        call_idx[0] += 1
        row = rows[idx] if idx < len(rows) else None
        r = MagicMock()
        r.first.return_value = row
        return r

    session.execute = _exec
    return session


@pytest.mark.asyncio
async def test_no_account_when_no_tenant():
    """No chat_id link → no_account, not a single extra SELECT."""
    from src.bots.ai_gate import ai_gate

    session = AsyncMock()
    with patch("src.bots.ai_gate.resolve_tenant_id", AsyncMock(return_value=None)):
        result = await ai_gate(session, chat_id=999999, level="deep")

    assert result.status == "no_account"
    assert result.tenant_id is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_frozen_takes_priority_over_everything():
    """access_until expired → frozen, EVEN IF the limit formally would also be exhausted.

    The mock session returns EXACTLY one row (tier/access) — if the gate went
    further to limit_decision, the second ``execute`` call would fail on an empty
    sequence (``rows[1]`` does not exist → None → used_count would get None and the trace would differ from the expected one). The explicit check of the number of calls is done separately.
    """
    from src.bots.ai_gate import ai_gate

    session = _session_returning(SimpleNamespace(tier="pro", access_until=_PAST))
    with patch(
        "src.bots.ai_gate.resolve_tenant_id", AsyncMock(return_value=_TENANT_ID)
    ):
        result = await ai_gate(session, chat_id=_CHAT_ID, level="deep")

    assert result.status == "frozen"
    assert result.tenant_id == _TENANT_ID
    # The order of the axes: the gate stopped at the freeze — exactly one SELECT.
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_gate_tier_when_level_unavailable_on_tier():
    """lite: deep=0 on the tier → gate_tier, the limit is not read (the second SELECT does not go)."""
    from src.bots.ai_gate import ai_gate

    session = _session_returning(SimpleNamespace(tier="lite", access_until=None))
    with patch(
        "src.bots.ai_gate.resolve_tenant_id", AsyncMock(return_value=_TENANT_ID)
    ):
        result = await ai_gate(
            session, chat_id=_CHAT_ID, level="deep", tier_limits=_TIER_LIMITS
        )

    assert result.status == "gate_tier"
    assert result.tier == "lite"
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_limit_exhausted_when_used_meets_limit():
    """pro/deep=10, used 10 → limit_exhausted (the second SELECT — the counter)."""
    from src.bots.ai_gate import ai_gate

    rows = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),  # tier/access
        SimpleNamespace(count=10),  # used_count
    ]
    session = _make_session_seq(rows)
    with patch(
        "src.bots.ai_gate.resolve_tenant_id", AsyncMock(return_value=_TENANT_ID)
    ):
        result = await ai_gate(
            session, chat_id=_CHAT_ID, level="deep", tier_limits=_TIER_LIMITS
        )

    assert result.status == "limit_exhausted"
    assert result.tier == "pro"


@pytest.mark.asyncio
async def test_ok_when_below_limit():
    """pro/deep=10, used 3 → ok."""
    from src.bots.ai_gate import ai_gate

    rows = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),
        SimpleNamespace(count=3),
    ]
    session = _make_session_seq(rows)
    with patch(
        "src.bots.ai_gate.resolve_tenant_id", AsyncMock(return_value=_TENANT_ID)
    ):
        result = await ai_gate(
            session, chat_id=_CHAT_ID, level="deep", tier_limits=_TIER_LIMITS
        )

    assert result.status == "ok"
    assert result.tier == "pro"


@pytest.mark.asyncio
async def test_enterprise_unlimited_never_exhausts():
    """enterprise: deep=None (unlimited) → ok without reading the counter (the short path of limit_decision)."""
    from src.bots.ai_gate import ai_gate

    session = _session_returning(SimpleNamespace(tier="enterprise", access_until=None))
    with patch(
        "src.bots.ai_gate.resolve_tenant_id", AsyncMock(return_value=_TENANT_ID)
    ):
        result = await ai_gate(
            session, chat_id=_CHAT_ID, level="deep", tier_limits=_TIER_LIMITS
        )

    assert result.status == "ok"
    # limit=None → limit_decision returns 'ok' without the used_count query.
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_readonly_no_write_statements():
    """The gate executes not a single INSERT/UPDATE/DELETE — the write-off is outside its contour."""
    from src.bots.ai_gate import ai_gate

    seen_sql: list[str] = []
    rows = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),
        SimpleNamespace(count=0),
    ]
    call_idx = [0]
    session = AsyncMock()

    async def _exec(sql, params=None):
        seen_sql.append(str(sql))
        idx = call_idx[0]
        call_idx[0] += 1
        r = MagicMock()
        r.first.return_value = rows[idx] if idx < len(rows) else None
        return r

    session.execute = _exec
    with patch(
        "src.bots.ai_gate.resolve_tenant_id", AsyncMock(return_value=_TENANT_ID)
    ):
        result = await ai_gate(
            session, chat_id=_CHAT_ID, level="bid", tier_limits=_TIER_LIMITS
        )

    assert result.status == "ok"
    for sql in seen_sql:
        upper = sql.upper()
        assert "INSERT" not in upper
        assert "UPDATE" not in upper
        assert "DELETE" not in upper
