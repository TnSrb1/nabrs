"""Regression: a refusal by the limit in Telegram no longer stays silent (#513).

Before this fix all four AI buttons (``ai:deep/bid/chk/drf``) answered with an immediate toast “⏳ Starting…”, and only the SERVICE called after it decided
that the limit was exhausted — and by that moment Telegram no longer allows showing an alert
(``callback.answer`` answers the press exactly once). The client got the exhaustion of the limit as an ordinary message in the feed, without an explanation “what to do”.

These tests prove by ACTION (not by their own model of the code) that for each of the
four handlers ``ai_gate(status=“limit_exhausted”)``:
1. ``callback.answer(text, show_alert=True)`` is called, and it is the FIRST call of
   ``callback.answer`` (the service does not manage to call a second progress toast);
2. the service/model is NOT called at all (money is not spent on an obvious refusal);
3. a permanent message with an upsell (a button to app.nab.rs) goes to the chat, when
   there is a higher plan, and without a button — when the client is already at the ceiling.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.bots import telegram as tg
from src.bots.ai_gate import GateDecision

_TENDER = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_PROFILE = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _callback(data: str, chat_id: int = 555) -> AsyncMock:
    cb = AsyncMock()
    cb.data = data
    cb.message = AsyncMock()
    cb.message.chat = SimpleNamespace(id=chat_id)
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


@asynccontextmanager
async def _fake_scope():
    yield AsyncMock()


def _patch_scope():
    return patch("src.bots.telegram.session_scope", _fake_scope)


def _patch_locale(locale: str = "ru"):
    return patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value=locale))


def _patch_gate(status: str, *, tier: str | None = "pro"):
    return patch(
        "src.bots.ai_gate.ai_gate",
        AsyncMock(return_value=GateDecision(status=status, tenant_id="t1", tier=tier)),
    )


# --------------------------------------------------------------------------- #
# Each of the four handlers: limit_exhausted → alert as the FIRST answer, the service
# is not called.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_deep_limit_exhausted_alerts_and_skips_service():
    cb = _callback(f"ai:deep:{_TENDER}")
    with (
        _patch_scope(),
        _patch_locale(),
        _patch_gate("limit_exhausted"),
        patch(
            "src.bots.deep_analysis_service.run_deep_analysis", AsyncMock()
        ) as mock_run,
    ):
        await tg.on_deep_analysis(cb)

    mock_run.assert_not_awaited()
    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.await_args
    assert kwargs.get("show_alert") is True
    assert "лимит" in args[0].lower() or "исчерпан" in args[0].lower()
    cb.message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_bid_limit_exhausted_alerts_and_skips_service():
    cb = _callback(f"ai:bid:{_TENDER}")
    with (
        _patch_scope(),
        _patch_locale(),
        _patch_gate("limit_exhausted"),
        patch(
            "src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="t1")
        ) as mock_resolve,
        patch(
            "src.bots.telegram._latest_profile",
            AsyncMock(return_value=SimpleNamespace(id=_PROFILE)),
        ),
        patch("src.bots.bid_verdict_service.run_bid_verdict", AsyncMock()) as mock_run,
    ):
        await tg.on_bid_verdict(cb)

    mock_run.assert_not_awaited()
    # the profile resolve (after the gate) must not go either — the gate cut it off earlier.
    mock_resolve.assert_not_awaited()
    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.await_args
    assert kwargs.get("show_alert") is True
    cb.message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_checklist_limit_exhausted_alerts_and_skips_service():
    cb = _callback(f"ai:chk:{_TENDER}")
    with (
        _patch_scope(),
        _patch_locale(),
        _patch_gate("limit_exhausted"),
        patch(
            "src.bots.compliance_checklist_service.run_compliance_checklist",
            AsyncMock(),
        ) as mock_run,
    ):
        await tg.on_compliance_checklist(cb)

    mock_run.assert_not_awaited()
    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.await_args
    assert kwargs.get("show_alert") is True
    cb.message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_draft_limit_exhausted_alerts_and_skips_service():
    cb = _callback(f"ai:drf:{_TENDER}")
    with (
        _patch_scope(),
        _patch_locale(),
        _patch_gate("limit_exhausted"),
        patch("src.bots.draft_service.run_draft_application", AsyncMock()) as mock_run,
    ):
        await tg.on_draft_application(cb)

    mock_run.assert_not_awaited()
    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.await_args
    assert kwargs.get("show_alert") is True
    cb.message.answer.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Upsell: the button is present when there is a plan with a larger limit higher up the ladder;
# no button (but the text stays) when the client is already at the ceiling.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_upsell_button_present_when_higher_tier_exists():
    """pro hit deep → the upsell offers business (deep is higher) with a button."""
    cb = _callback(f"ai:deep:{_TENDER}")
    with (
        _patch_scope(),
        _patch_locale(),
        _patch_gate("limit_exhausted", tier="pro"),
        patch("src.bots.deep_analysis_service.run_deep_analysis", AsyncMock()),
    ):
        await tg.on_deep_analysis(cb)

    _, kwargs = cb.message.answer.await_args
    markup = kwargs.get("reply_markup")
    assert markup is not None
    button = markup.inline_keyboard[0][0]
    assert button.url == "https://app.nab.rs/cabinet/subscription"
    text = cb.message.answer.await_args.args[0]
    assert "Business" in text or "business" in text.lower()


@pytest.mark.asyncio
async def test_no_upsell_button_when_at_ceiling():
    """business — the ceiling of the ladder (enterprise is not sold) → no button."""
    cb = _callback(f"ai:deep:{_TENDER}")
    with (
        _patch_scope(),
        _patch_locale(),
        _patch_gate("limit_exhausted", tier="business"),
        patch("src.bots.deep_analysis_service.run_deep_analysis", AsyncMock()),
    ):
        await tg.on_deep_analysis(cb)

    args, kwargs = cb.message.answer.await_args
    assert kwargs.get("reply_markup") is None
    # The base text of the limit stays — silence is not allowed even without an upsell.
    assert args[0] == cb.answer.await_args.args[0]


# --------------------------------------------------------------------------- #
# The correct catalog key per level (limit_deep/limit_ai/limit_ai/limit_draft).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bid_and_checklist_share_limit_ai_key():
    """bid and checklist use the same key 'limit_ai' (not limit_bid)."""
    from src.bots.i18n import bot_t

    cb_bid = _callback(f"ai:bid:{_TENDER}")
    with (
        _patch_scope(),
        _patch_locale(),
        _patch_gate("limit_exhausted"),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock()),
        patch("src.bots.bid_verdict_service.run_bid_verdict", AsyncMock()),
    ):
        await tg.on_bid_verdict(cb_bid)
    assert cb_bid.answer.await_args.args[0] == bot_t("ru", "limit_ai")

    cb_chk = _callback(f"ai:chk:{_TENDER}")
    with (
        _patch_scope(),
        _patch_locale(),
        _patch_gate("limit_exhausted"),
        patch(
            "src.bots.compliance_checklist_service.run_compliance_checklist",
            AsyncMock(),
        ),
    ):
        await tg.on_compliance_checklist(cb_chk)
    assert cb_chk.answer.await_args.args[0] == bot_t("ru", "limit_ai")
