"""Tests of the status funnel service (src.bots.status_service).

Without a network and a real DB: ``session`` is a mock, ``repo.set_tenant`` is patched.
We check the resolve of ``tenant_id`` by chat_id via the SECURITY DEFINER function
(migration 0021), the UPSERT parameters, the mandatory SET LOCAL
``app.current_tenant_id`` before the write, and the validation of the status.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bots.status_service import resolve_tenant_id, set_tender_status

_TID = "11111111-1111-1111-1111-111111111111"


# --------------------------------------------------------------------------- #
# resolve_tenant_id — calls app_resolve_tenant_id() + set_current_tenant.
# --------------------------------------------------------------------------- #


def _session_resolving(tenant_id_or_none) -> AsyncMock:
    """Session: the first execute → scalar() = tenant_id_or_none (SECURITY DEFINER);
    the second execute → set_config() GUC (if the tenant is found)."""
    session = AsyncMock()
    first_result = MagicMock()
    first_result.scalar.return_value = tenant_id_or_none
    second_result = MagicMock()  # set_config answer (ignored)
    session.execute = AsyncMock(side_effect=[first_result, second_result])
    return session


@pytest.mark.asyncio
async def test_resolve_tenant_id_returns_tenant():
    session = _session_resolving(_TID)
    tid = await resolve_tenant_id(session, 123456789)
    assert tid == str(_TID)
    # two calls: app_resolve_tenant_id + set_config
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_resolve_tenant_id_no_channel_returns_none():
    """app_resolve_tenant_id returned None → the GUC is not set, we return None."""
    session = _session_resolving(None)
    tid = await resolve_tenant_id(session, 123456789)
    assert tid is None
    # only one call: app_resolve_tenant_id (set_config is not called)
    assert session.execute.await_count == 1


# --------------------------------------------------------------------------- #
# set_tender_status — the UPSERT with the mandatory SET LOCAL (RLS).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_set_tender_status_sets_tenant_then_upserts_then_commits():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    with patch("src.api.repo.set_tenant", AsyncMock()) as set_tenant:
        await set_tender_status(
            session, tenant_id="t1", tender_id=_TID, status="active"
        )
    # SET LOCAL app.current_tenant_id before the write (the cabinet contract)
    set_tenant.assert_awaited_once_with(session, "t1")
    # exactly one execute — the UPSERT (set_tenant is mocked, does not make its own execute)
    assert session.execute.await_count == 1
    params = session.execute.await_args.args[1]
    assert params == {"tenant_id": "t1", "tender_id": _TID, "status": "active"}
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_tender_status_rejects_unknown_status():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    with patch("src.api.repo.set_tenant", AsyncMock()) as set_tenant:
        with pytest.raises(ValueError):
            await set_tender_status(
                session, tenant_id="t1", tender_id=_TID, status="bogus"
            )
    # an invalid status — we do not reach the DB or SET LOCAL
    set_tenant.assert_not_awaited()
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "status", ["new", "watching", "active", "submitted", "won", "lost", "skipped"]
)
@pytest.mark.asyncio
async def test_set_tender_status_accepts_all_seven(status):
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    with patch("src.api.repo.set_tenant", AsyncMock()):
        await set_tender_status(session, tenant_id="t1", tender_id=_TID, status=status)
    assert session.execute.await_args.args[1]["status"] == status
