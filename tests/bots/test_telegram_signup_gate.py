"""Tests of the registration gate in the bot (BOT_SIGNUP_MODE, STAGE 1).

The network/DB are not touched: ``session_scope`` is patched to a mock session, the gate mode — via a substitution of :func:`get_bot_settings` (bypassing lru_cache and ENV). We cover the 4 cases of the brief: the open mode, an existing subscriber in token_only, a valid token in token_only and a bare ``/start`` in token_only (a polite refusal, nothing is written to the DB).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bots.telegram import OnboardingFSM, cmd_reset, cmd_start

# --------------------------------------------------------------------------- #
# Fakes (a mirror of test_telegram_bind for the self-sufficiency of the file).
# --------------------------------------------------------------------------- #


class FakeState:
    def __init__(self) -> None:
        self._data: dict = {}
        self._state = None

    async def update_data(self, **kwargs) -> None:
        self._data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self._data)

    async def set_state(self, state) -> None:
        self._state = state

    async def get_state(self):
        return self._state

    async def clear(self) -> None:
        self._data = {}
        self._state = None


def _make_message(uid: int = 12345) -> AsyncMock:
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=uid)
    message.answer = AsyncMock()
    return message


def _patch_scope(session):
    @asynccontextmanager
    async def _scope():
        yield session

    return patch("src.bots.telegram.session_scope", _scope)


def _patch_mode(mode: str):
    """Substitutes the gate mode, bypassing lru_cache get_bot_settings and ENV."""
    return patch(
        "src.bots.telegram.get_bot_settings",
        lambda: SimpleNamespace(bot_signup_mode=mode),
    )


def _future(minutes: int = 15) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _make_token(*, user_id="user-1"):
    return SimpleNamespace(
        token_hash="hash",
        purpose="channel_bind",
        consumed_at=None,
        expires_at=_future(),
        user_id=user_id,
    )


def _channel_session(existing_channel) -> AsyncMock:
    """A mock session for the branch without a payload (cmd_start/cmd_reset).

    After 0022 cmd_start/cmd_reset call _RESOLVE_TENANT_SQL (scalar()), not _find_channel. None = no binding; a row = the binding exists.
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = "tenant-1" if existing_channel is not None else None
    session.execute = AsyncMock(return_value=result)
    return session


def _bind_session(*, token, existing_channel=None) -> AsyncMock:
    """A mock session for the payload branch (bind_channel_by_token).

    After 0022: the 1st — the token, the 2nd — app_resolve_tenant_id_by_user_id (scalar()), the 3rd — set_config GUC (noop), the 4th — _find_channel_for_user.
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    token_result = MagicMock()
    token_result.scalars.return_value.first.return_value = token
    tenant_result = MagicMock()
    tenant_result.scalar.return_value = "tenant-1" if token is not None else None
    noop_result = MagicMock()
    channel_result = MagicMock()
    channel_result.scalars.return_value.first.return_value = existing_channel
    # adopt_channel_into_empty_profiles: the tenant’s plan + UPDATE of the empty templates.
    tier_result = MagicMock()
    tier_result.first.return_value = SimpleNamespace(status="trial", tier="lite")
    adopt_result = MagicMock()
    adopt_result.fetchall.return_value = []
    session.execute = AsyncMock(
        side_effect=[
            token_result,
            tenant_result,
            noop_result,
            channel_result,
            tier_result,
            adopt_result,
        ]
    )
    return session


# --------------------------------------------------------------------------- #
# Case 1 — the open mode: a bare /start starts the onboarding (the previous behavior).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_open_mode_bare_start_runs_onboarding():
    session = _channel_session(existing_channel=None)
    state = FakeState()
    message = _make_message(uid=1)
    with _patch_mode("open"), _patch_scope(session):
        await cmd_start(message, state, SimpleNamespace(args=None))
    assert await state.get_state() == OnboardingFSM.waiting_language


# --------------------------------------------------------------------------- #
# Case 2 — token_only, an existing subscriber: all as before (the hint).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_token_only_existing_subscriber_hints():
    session = _channel_session(existing_channel=object())
    state = FakeState()
    message = _make_message(uid=2)
    with _patch_mode("token_only"), _patch_scope(session):
        await cmd_start(message, state, None)
    text = message.answer.await_args.args[0]
    assert text.startswith("Аккаунт уже подключён.")
    # The onboarding is not started, the profile/subscriber is not touched.
    assert await state.get_state() is None
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Case 3 — token_only, a valid token: the deep-link binding is not affected by the gate.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_token_only_valid_token_binds():
    session = _bind_session(token=_make_token(), existing_channel=None)
    state = FakeState()
    message = _make_message(uid=999)
    command = SimpleNamespace(args="raw-token")
    with _patch_mode("token_only"), _patch_scope(session):
        await cmd_start(message, state, command)
    text = message.answer.await_args.args[0]
    assert "подключён" in text.lower()
    # The binding took place, the onboarding was not started.
    assert await state.get_state() is None


# --------------------------------------------------------------------------- #
# Case 4 — token_only, a bare /start: a polite refusal, nothing is written to the DB.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_token_only_bare_start_refused_no_db_write():
    session = _channel_session(existing_channel=None)
    state = FakeState()
    message = _make_message(uid=3)
    with _patch_mode("token_only"), _patch_scope(session):
        await cmd_start(message, state, SimpleNamespace(args=None))
    text = message.answer.await_args.args[0]
    # The text of the refusal leads to the site, without the onboarding.
    assert "nab.rs" in text
    assert "сайте" in text.lower()
    assert await state.get_state() is None
    # The profile/subscriber are not created — neither add nor commit.
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Case 5 — token_only, a bare /reset of a non-subscriber: a refusal, nothing is written to the DB.
# (closing the bypass of the gate via /reset → onboarding → the creation of a subscriber)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_token_only_bare_reset_refused_no_db_write():
    session = _channel_session(existing_channel=None)
    state = FakeState()
    message = _make_message(uid=4)
    with _patch_mode("token_only"), _patch_scope(session):
        await cmd_reset(message, state)
    text = message.answer.await_args.args[0]
    # The same refusal as for /start; the onboarding is not started.
    assert "nab.rs" in text
    assert "сайте" in text.lower()
    assert await state.get_state() is None
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Case 6 — token_only, /reset of an existing subscriber: it sends to the cabinet.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_token_only_reset_existing_subscriber_points_to_cabinet():
    session = _channel_session(existing_channel=object())
    state = FakeState()
    message = _make_message(uid=5)
    with _patch_mode("token_only"), _patch_scope(session):
        await cmd_reset(message, state)
    # The onboarding is NOT started: the templates are edited in the cabinet, the bot does not overwrite them.
    assert await state.get_state() is None
    text = message.answer.await_args.args[0]
    assert text.startswith("Шаблоны поиска настраиваются в кабинете")
    session.commit.assert_not_awaited()
