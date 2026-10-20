"""Unit tests of the on_bid_verdict handler (the P0-fix BUTTON_DATA_INVALID).

We check:
- The parsing of the new format “ai:bid:{tender_id}” (43 bytes).
- The defensive parsing of the old format “ai:bid:{tender_id}:{profile_id}” — tender_id is correct.
- An empty tail → an error answer about the format.
- no_account: resolve_tenant_id returned None → the message «/start».
- no_profile: _latest_profile returned None → the message “Template not found”.
- success: run_bid_verdict is called with the resolved profile_id.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.bots.telegram import on_bid_verdict

_TENDER_ID = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_PROFILE_ID = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_CHAT_ID = 123456789


def _make_callback(data: str) -> AsyncMock:
    cb = AsyncMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = AsyncMock()
    cb.message.chat = SimpleNamespace(id=_CHAT_ID)
    cb.message.answer = AsyncMock()
    return cb


def _fake_scope(session):
    @asynccontextmanager
    async def _scope():
        yield session

    return _scope


def _patch_gate_ok():
    """The gate (#513) rings first in on_bid_verdict — these tests check

    the behavior AFTER the gate (profile/service), therefore the gate must let through.
    """
    from src.bots.ai_gate import GateDecision

    return patch(
        "src.bots.ai_gate.ai_gate",
        AsyncMock(return_value=GateDecision(status="ok", tenant_id="t1", tier="pro")),
    )


@pytest.mark.asyncio
async def test_new_format_calls_run_bid_verdict_with_resolved_profile():
    """The new format: tender_id from the callback, profile_id from the DB → run_bid_verdict."""
    session = AsyncMock()
    profile = SimpleNamespace(id=_PROFILE_ID)
    result_obj = SimpleNamespace(status="success", html="<b>OK</b>")

    with (
        patch("src.bots.telegram.session_scope", _fake_scope(session)),
        _patch_gate_ok(),
        patch(
            "src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="tenant-1")
        ),
        patch("src.bots.telegram._latest_profile", AsyncMock(return_value=profile)),
        patch(
            "src.bots.bid_verdict_service.run_bid_verdict",
            AsyncMock(return_value=result_obj),
        ) as mock_run,
    ):
        cb = _make_callback(f"ai:bid:{_TENDER_ID}")
        await on_bid_verdict(cb)

    mock_run.assert_awaited_once()
    _, kwargs = mock_run.call_args
    assert kwargs["tender_id"] == _TENDER_ID
    assert kwargs["profile_id"] == _PROFILE_ID


@pytest.mark.asyncio
async def test_old_format_defensive_parsing_extracts_tender_id():
    """The old format “ai:bid:{tender_id}:{profile_id}” → tender_id is correct, the handler does not fall over."""
    session = AsyncMock()
    profile = SimpleNamespace(id=_PROFILE_ID)
    result_obj = SimpleNamespace(status="success", html="<b>OK</b>")

    with (
        patch("src.bots.telegram.session_scope", _fake_scope(session)),
        _patch_gate_ok(),
        patch(
            "src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="tenant-1")
        ),
        patch("src.bots.telegram._latest_profile", AsyncMock(return_value=profile)),
        patch(
            "src.bots.bid_verdict_service.run_bid_verdict",
            AsyncMock(return_value=result_obj),
        ) as mock_run,
    ):
        cb = _make_callback(f"ai:bid:{_TENDER_ID}:{_PROFILE_ID}")
        await on_bid_verdict(cb)

    _, kwargs = mock_run.call_args
    assert kwargs["tender_id"] == _TENDER_ID


@pytest.mark.asyncio
async def test_empty_tender_id_returns_error():
    """«ai:bid:» without a tail → callback.answer with an error message."""
    cb = _make_callback("ai:bid:")
    await on_bid_verdict(cb)
    cb.answer.assert_awaited_once()
    args = cb.answer.await_args.args
    assert args and "Некорректный" in args[0]


@pytest.mark.asyncio
async def test_no_account_sends_start_message():
    """resolve_tenant_id = None → the message «/start», run_bid_verdict is not called."""
    session = AsyncMock()

    with (
        patch("src.bots.telegram.session_scope", _fake_scope(session)),
        _patch_gate_ok(),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value=None)),
        patch("src.bots.bid_verdict_service.run_bid_verdict", AsyncMock()) as mock_run,
    ):
        cb = _make_callback(f"ai:bid:{_TENDER_ID}")
        await on_bid_verdict(cb)

    mock_run.assert_not_awaited()
    cb.message.answer.assert_awaited_once()
    text = cb.message.answer.await_args.args[0]
    assert text.startswith("Чат не привязан к аккаунту.")
    button = cb.message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "🔗 Подключить Telegram"
    assert button.url == "https://app.nab.rs/cabinet/telegram"


@pytest.mark.asyncio
async def test_no_profile_sends_profile_not_found_message():
    """_latest_profile = None → the message “Template not found”, run_bid_verdict is not called."""
    session = AsyncMock()

    with (
        patch("src.bots.telegram.session_scope", _fake_scope(session)),
        _patch_gate_ok(),
        patch(
            "src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="tenant-1")
        ),
        patch("src.bots.telegram._latest_profile", AsyncMock(return_value=None)),
        patch("src.bots.bid_verdict_service.run_bid_verdict", AsyncMock()) as mock_run,
    ):
        cb = _make_callback(f"ai:bid:{_TENDER_ID}")
        await on_bid_verdict(cb)

    mock_run.assert_not_awaited()
    cb.message.answer.assert_awaited_once()
    text = cb.message.answer.await_args.args[0]
    assert "Шаблон не найден" in text


@pytest.mark.asyncio
async def test_immediate_answer_before_model_call():
    """callback.answer() is called before run_bid_verdict (anti-expiry 10s)."""
    session = AsyncMock()
    profile = SimpleNamespace(id=_PROFILE_ID)
    call_order: list[str] = []

    async def _track_answer(*args, **kwargs):
        call_order.append("answer")

    async def _track_run(*args, **kwargs):
        call_order.append("run")
        return SimpleNamespace(status="success", html="<b>OK</b>")

    with (
        patch("src.bots.telegram.session_scope", _fake_scope(session)),
        _patch_gate_ok(),
        patch(
            "src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="tenant-1")
        ),
        patch("src.bots.telegram._latest_profile", AsyncMock(return_value=profile)),
        patch("src.bots.bid_verdict_service.run_bid_verdict", _track_run),
    ):
        cb = _make_callback(f"ai:bid:{_TENDER_ID}")
        cb.answer = _track_answer
        await on_bid_verdict(cb)

    assert call_order[0] == "answer", "callback.answer() must be the first"


@pytest.mark.asyncio
async def test_handler_passes_channel_locale_to_render():
    """The channel language reaches run_bid_verdict → _render_bid_verdict (owner 30.09)."""
    session = AsyncMock()
    profile = SimpleNamespace(id=_PROFILE_ID)
    result_obj = SimpleNamespace(status="success", html="<b>OK</b>")

    with (
        patch("src.bots.telegram.session_scope", _fake_scope(session)),
        _patch_gate_ok(),
        patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value="en")),
        patch(
            "src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="tenant-1")
        ),
        patch("src.bots.telegram._latest_profile", AsyncMock(return_value=profile)),
        patch(
            "src.bots.bid_verdict_service.run_bid_verdict",
            AsyncMock(return_value=result_obj),
        ) as mock_run,
    ):
        await on_bid_verdict(_make_callback(f"ai:bid:{_TENDER_ID}"))

    assert mock_run.call_args.kwargs["locale"] == "en"
