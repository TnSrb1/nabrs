"""Tests of the deep link of the cabinet↔bot binding (Chat 26, T3, C-04).

The network and the DB are not touched: ``session_scope`` is patched to a mock session, the token/channels are substituted. We check the consumption of the ``channel_bind`` token and the creation/update of ``user_channels(telegram)`` for the user of the token, idempotency, refusals and
the keeping of the previous behavior of ``/start`` without a payload.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bots.telegram import (
    OnboardingFSM,
    bind_channel_by_token,
    cmd_start,
)

# --------------------------------------------------------------------------- #
# Fakes.
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


def _future(minutes: int = 15) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _past(minutes: int = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _make_token(*, consumed_at=None, expires_at=None, user_id="user-1"):
    return SimpleNamespace(
        token_hash="hash",
        purpose="channel_bind",
        consumed_at=consumed_at,
        expires_at=expires_at if expires_at is not None else _future(),
        user_id=user_id,
    )


def _make_bind_session(*, token=None, existing_channel=None) -> AsyncMock:
    """The mock session for bind_channel_by_token.

    The order of execute (0022):
    1. token_result    — the search of auth_token (scalars().first()).
    2. resolve_result  — app_resolve_tenant_id_by_user_id (scalar()), only for a valid token.
    3. noop_result     — set_config GUC.
    4. channel_result  — the search of user_channels (scalars().first()).
    5. tier_result     — the tenant’s tier (adopt_channel_into_empty_profiles).
    6. adopt_result    — UPDATE profiles with empty delivery_channels.
    For an invalid token only token_result is used.
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    token_result = MagicMock()
    token_result.scalars.return_value.first.return_value = token
    resolve_result = MagicMock()
    resolve_result.scalar.return_value = "tenant-1"
    noop_result = MagicMock()
    channel_result = MagicMock()
    channel_result.scalars.return_value.first.return_value = existing_channel

    tier_result = MagicMock()
    tier_result.first.return_value = SimpleNamespace(status="trial", tier="lite")
    adopt_result = MagicMock()
    adopt_result.fetchall.return_value = []

    session.execute = AsyncMock(
        side_effect=[
            token_result,
            resolve_result,
            noop_result,
            channel_result,
            tier_result,
            adopt_result,
        ]
    )
    return session


def _patch_scope(session):
    @asynccontextmanager
    async def _scope():
        yield session

    return patch("src.bots.telegram.session_scope", _scope)


def _added(session, type_name: str):
    for call in session.add.call_args_list:
        obj = call.args[0]
        if type(obj).__name__ == type_name:
            return obj
    return None


# --------------------------------------------------------------------------- #
# bind_channel_by_token.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bind_valid_token_creates_channel():
    token = _make_token()
    session = _make_bind_session(token=token, existing_channel=None)
    outcome = await bind_channel_by_token(session, raw_token="raw", telegram_id=999)
    assert outcome == "bound"
    channel = _added(session, "UserChannel")
    assert channel is not None
    assert channel.channel == "telegram"
    assert channel.external_id == "999"
    assert channel.status == "active"
    assert channel.user_id == "user-1"
    assert channel.linked_at is not None
    # the token is spent, the changes are committed
    assert token.consumed_at is not None
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_bind_idempotent_updates_existing_channel():
    token = _make_token(user_id="user-1")
    existing = SimpleNamespace(
        user_id="user-1",
        channel="telegram",
        external_id="999",
        status="active",
        linked_at=None,
    )
    session = _make_bind_session(token=token, existing_channel=existing)
    outcome = await bind_channel_by_token(session, raw_token="raw", telegram_id=999)
    assert outcome == "bound"
    # No duplicate: a new UserChannel was not added, the existing one was updated.
    assert _added(session, "UserChannel") is None
    assert existing.external_id == "999"
    assert existing.status == "active"
    assert existing.linked_at is not None
    assert token.consumed_at is not None


@pytest.mark.asyncio
async def test_bind_expired_token_rejected():
    token = _make_token(expires_at=_past())
    session = _make_bind_session(token=token)
    outcome = await bind_channel_by_token(session, raw_token="raw", telegram_id=999)
    assert outcome == "invalid"
    assert _added(session, "UserChannel") is None
    assert token.consumed_at is None  # we do not spend it again in vain
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_consumed_token_rejected():
    token = _make_token(consumed_at=_past())
    session = _make_bind_session(token=token)
    outcome = await bind_channel_by_token(session, raw_token="raw", telegram_id=999)
    assert outcome == "invalid"
    assert _added(session, "UserChannel") is None
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_unknown_token_rejected():
    session = _make_bind_session(token=None)
    outcome = await bind_channel_by_token(session, raw_token="raw", telegram_id=999)
    assert outcome == "invalid"
    assert _added(session, "UserChannel") is None
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_empty_payload_rejected():
    session = _make_bind_session(token=_make_token())
    outcome = await bind_channel_by_token(session, raw_token="", telegram_id=999)
    assert outcome == "invalid"
    # an empty payload — we do not even go to the DB
    session.execute.assert_not_awaited()


# --------------------------------------------------------------------------- #
# cmd_start: the payload branch vs the previous behavior.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cmd_start_with_valid_payload_binds():
    token = _make_token()
    session = _make_bind_session(token=token, existing_channel=None)
    state = FakeState()
    message = _make_message(uid=999)
    command = SimpleNamespace(args="raw-token")
    with _patch_scope(session):
        await cmd_start(message, state, command)
    text = message.answer.await_args.args[0]
    assert "подключён" in text.lower()
    # onboarding is not started
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_cmd_start_with_invalid_payload_reports_error():
    session = _make_bind_session(token=None)
    state = FakeState()
    message = _make_message(uid=999)
    command = SimpleNamespace(args="bad")
    with _patch_scope(session):
        await cmd_start(message, state, command)
    text = message.answer.await_args.args[0]
    assert "недействительна" in text.lower()
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_cmd_start_without_payload_starts_onboarding():
    """Without a payload and without a binding — the previous FSM onboarding (nothing is broken)."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = None  # app_resolve_tenant_id → no binding (0022)
    session.execute = AsyncMock(return_value=result)
    state = FakeState()
    message = _make_message(uid=1)
    open_mode = patch(
        "src.bots.telegram.get_bot_settings",
        lambda: SimpleNamespace(bot_signup_mode="open"),
    )
    with open_mode, _patch_scope(session):
        await cmd_start(message, state, SimpleNamespace(args=None))
    assert await state.get_state() == OnboardingFSM.waiting_language


@pytest.mark.asyncio
async def test_cmd_start_without_payload_existing_channel_hints():
    """Without a payload, but the binding exists — the hint /profile (without /reset)."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = (
        "tenant-uuid"  # app_resolve_tenant_id → a binding (0022)
    )
    session.execute = AsyncMock(return_value=result)
    state = FakeState()
    message = _make_message(uid=2)
    with _patch_scope(session):
        await cmd_start(message, state, None)
    text = message.answer.await_args.args[0]
    assert text.startswith("Аккаунт уже подключён.")
    assert await state.get_state() is None
