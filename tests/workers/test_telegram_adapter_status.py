"""The integration test of the funnel of statuses into the delivery (TelegramAdapter.format).

Each delivered tender gets 3 buttons of the funnel of the stage 'new' (the port of v6). The button “Open tender” is removed (#168, Chat 45): there is no cabinet on the domain, the deep-link led nowhere — the transition to the tender is now a line “🔗 Source” in the body of the message. The presses of the buttons of the funnel are caught by src.bots.telegram.on_tender_status. The network is not touched (the Bot is mocked, format makes no network calls).
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram.types import InlineKeyboardMarkup

from src.workers.adapters.base import DeliveryJob
from src.workers.adapters.telegram import TelegramAdapter


class _FakeBot:
    async def send_message(self, **kwargs):  # pragma: no cover — is not called
        return object()


def _job(tender_id: str = "abc-123") -> DeliveryJob:
    return DeliveryJob(
        outbox_id=1,
        tender_id=tender_id,
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc-1",
        target="555",
        channel_status="active",
        title="Тестовый тендер",
        source_url="https://jnportal.rs/tender/1",
        amount_eur=1000.0,
        deadline_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc),
    )


def test_delivered_message_carries_new_funnel_buttons():
    adapter = TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs")
    msg = adapter.format(_job(tender_id="abc-123"))
    assert isinstance(msg.reply_markup, InlineKeyboardMarkup)

    # Without profile_id “Open in the cabinet” is not drawn (the page requires profile_id):
    # no button carries a URL — the transition to the tender is a line “🔗 Source”.
    assert all(
        btn.url is None for row in msg.reply_markup.inline_keyboard for btn in row
    )

    # The collapsed card: [Follow][Hide] + “🧠 AI analysis ▾” (the flag 0: there is no profile).
    datas = [
        btn.callback_data
        for row in msg.reply_markup.inline_keyboard
        for btn in row
        if btn.callback_data
    ]
    assert datas == [
        "st:watching:abc-123",
        "st:skipped:abc-123",
        "ai:menu:abc-123:0",
    ]
