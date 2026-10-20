"""Tests of the freeze gate in the on-demand deep analysis (FIX-SG-04).

Without a network and without a real DB: a mock session via AsyncMock (ADR-013). We cover:
- The freeze (access_until expired) → status="frozen" BEFORE the tier gate, the cache, the limit and
  BEFORE the model call: ``ai_client.analyze`` is NOT called, execute — exactly one
  (only the loading of tier/access).
- Not frozen (access_until in the future) → execution goes further (starter →
  gate_pro), the model is not called.
- ``access_until = NULL`` (permanent access) → NOT frozen → gate_pro.

The existing deep branches (gate_pro/limit/success/...) are checked in
``test_deep_analysis_service.py`` — here only the new frozen branch and the proof
that the old mock (a tier row without ``access_until``) remains not-frozen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.client import AiClient

_TENDER_ID = "11111111-1111-1111-1111-111111111111"
_TENANT_ID = "22222222-2222-2222-2222-222222222222"
_CHAT_ID = 123456789

_NOW = datetime.now(timezone.utc)
_PAST = _NOW - timedelta(days=1)
_FUTURE = _NOW + timedelta(days=365)


def _session_returning(row: SimpleNamespace | None) -> AsyncMock:
    """Mock session: the single ``execute`` returns a result with ``.first() == row``."""
    result = MagicMock()
    result.first.return_value = row
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _ai_client_not_called() -> AiClient:
    """The AiClient mock with ``analyze`` as an AsyncMock — for ``assert_not_called``."""
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_frozen_returns_frozen_without_calling_model():
    """access_until expired → status='frozen', opus NOT called, one execute (tier/access)."""
    from src.bots.deep_analysis_service import run_deep_analysis

    session = _session_returning(SimpleNamespace(tier="pro", access_until=_PAST))
    client = _ai_client_not_called()

    with patch(
        "src.bots.deep_analysis_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_deep_analysis(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
        )

    assert result.status == "frozen"
    assert result.html_chunks == ()
    # The model is not called for a frozen account.
    client.analyze.assert_not_called()
    # The gate fired BEFORE the cache/limit/tender → exactly one SELECT (tier+access_until).
    assert session.execute.await_count == 1
    # The status is not written → no commit.
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_not_frozen_future_access_proceeds_to_gate_pro():
    """access_until in the future + starter → NOT frozen → gate_pro (deep_limit=0)."""
    from src.bots.deep_analysis_service import run_deep_analysis

    session = _session_returning(SimpleNamespace(tier="starter", access_until=_FUTURE))
    client = _ai_client_not_called()

    with patch(
        "src.bots.deep_analysis_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_deep_analysis(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits={"starter": {"deep": 0}},
        )

    assert result.status == "gate_pro"
    client.analyze.assert_not_called()


@pytest.mark.asyncio
async def test_null_access_until_is_not_frozen():
    """access_until = NULL (permanent) → NOT frozen → gate_pro for starter."""
    from src.bots.deep_analysis_service import run_deep_analysis

    session = _session_returning(SimpleNamespace(tier="starter", access_until=None))
    client = _ai_client_not_called()

    with patch(
        "src.bots.deep_analysis_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_deep_analysis(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits={"starter": {"deep": 0}},
        )

    assert result.status == "gate_pro"
    client.analyze.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_tier_row_without_access_field_not_frozen():
    """Compatibility: a tier row without the access_until field (old mocks) → NOT frozen.

    The gate reads ``getattr(row, 'access_until', None)`` → None → permanent → execution goes further as before (starter → gate_pro). This guarantees that the existing deep tests do not break.
    """
    from src.bots.deep_analysis_service import run_deep_analysis

    session = _session_returning(SimpleNamespace(tier="starter"))
    client = _ai_client_not_called()

    with patch(
        "src.bots.deep_analysis_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_deep_analysis(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits={"starter": {"deep": 0}},
        )

    assert result.status == "gate_pro"
    client.analyze.assert_not_called()
