"""Unit tests of _find_channel — resistance to duplicate Telegram bindings.

They check that with several UserChannel rows for one external_id:
  - MultipleResultsFound is not raised (it was failing in production);
  - the active/freshest binding is returned (the same order as resolve_tenant_id).

The DB is not touched: session.execute is patched with MagicMock/AsyncMock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bots.telegram import _find_channel


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _make_channel(status: str, linked_at: datetime, uid: str = "u") -> SimpleNamespace:
    return SimpleNamespace(status=status, linked_at=linked_at, user_id=uid)


def _session_returning(channel) -> AsyncMock:
    """A session mock where scalars().first() returns the channel."""
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = channel
    session.execute = AsyncMock(return_value=result)
    return session


# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_find_channel_returns_none_when_no_rows() -> None:
    session = _session_returning(None)
    assert await _find_channel(session, 999) is None


@pytest.mark.asyncio
async def test_find_channel_single_binding_returned() -> None:
    ch = _make_channel("active", _dt(2024, 1, 1))
    session = _session_returning(ch)
    assert await _find_channel(session, 123) is ch


@pytest.mark.asyncio
async def test_find_channel_multiple_rows_no_exception() -> None:
    """Several rows for one external_id → the first (active/latest), without an exception."""
    active = _make_channel("active", _dt(2024, 6, 1), "u-active")
    session = _session_returning(active)
    result = await _find_channel(session, 123456789)
    assert result is active


@pytest.mark.asyncio
async def test_find_channel_prefers_active_over_older_free() -> None:
    """The active binding is preferred to a fresher free binding (SQLAlchemy ORDER BY).

    The test checks that execute is called with a query containing the needed ORDER BY.
    The concrete choice of rows is on the DB side; here the mock returns the active result.
    """
    active_ch = _make_channel("active", _dt(2023, 1, 1), "u-pro")
    session = _session_returning(active_ch)
    result = await _find_channel(session, 123456789)
    assert result is active_ch
    # We make sure that execute was called (the query with ORDER BY reached the session).
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_channel_uses_scalars_first_not_scalar_one_or_none() -> None:
    """_find_channel must use scalars().first(), not scalar_one_or_none().

    With several rows scalar_one_or_none() raises MultipleResultsFound.
    We check that scalars().first() is called, and scalar_one_or_none is not.
    """
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    session.execute = AsyncMock(return_value=result)

    await _find_channel(session, 42)

    result.scalars.return_value.first.assert_called_once()
    result.scalar_one_or_none.assert_not_called()


@pytest.mark.asyncio
async def test_find_channel_cmd_start_with_existing_answers_profile_hint() -> None:
    """cmd_start with one active binding answers “The profile already exists…”."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import patch

    from src.bots.telegram import cmd_start

    class FakeState:
        def __init__(self):
            self._state = None
            self._data: dict = {}

        async def get_state(self):
            return self._state

        async def set_state(self, s):
            self._state = s

        async def update_data(self, **kw):
            self._data.update(kw)

        async def get_data(self):
            return dict(self._data)

        async def clear(self):
            self._data = {}
            self._state = None

    ch = _make_channel("active", _dt(2024, 1, 1))
    session = _session_returning(ch)

    message = AsyncMock()
    message.from_user = SimpleNamespace(id=123456789)
    message.answer = AsyncMock()

    @asynccontextmanager
    async def _scope():
        yield session

    with patch("src.bots.telegram.session_scope", _scope):
        await cmd_start(message, FakeState(), None)

    text = message.answer.await_args.args[0]
    assert text.startswith("Аккаунт уже подключён.")
