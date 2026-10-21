"""The label of the source in the alerts (SRC-0d, the owner’s decision 30.09 item 6): the client sees the human-readable name of the source (``sources.display_name``), not the key ``jnportal``. The expectations — literals."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.api.emailer import MockEmailSender
from src.workers.adapters.base import DeliveryJob
from src.workers.adapters.email import EmailAdapter
from src.workers.adapters.telegram import TelegramAdapter

NAME = "Portal javnih nabavki"


class _Bot:
    async def send_message(self, **kwargs):  # pragma: no cover
        return object()


def _job(**over) -> DeliveryJob:
    base = dict(
        outbox_id=1,
        tender_id="abc-123",
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc-1",
        target="555",
        channel_status="active",
        title="Набавка опреме",
        source_url="https://jnportal.rs/tender/1",
        amount_eur=1000.0,
        deadline_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc),
        source_key="jnportal",
        source_label=NAME,
        profile_name="ИТ",
        locale="sr",
    )
    base.update(over)
    return DeliveryJob(**base)


def _platform_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if "Platforma" in ln]


def test_telegram_alert_shows_display_name_not_key():
    text = TelegramAdapter(_Bot()).format(_job()).text
    assert _platform_lines(text) == ["🏛 Platforma: Portal javnih nabavki"]


def test_email_alert_shows_display_name_not_key():
    msg = EmailAdapter(MockEmailSender(), cabinet_base_url="https://nab.rs").format(
        _job(channel="email", target="a@b.rs")
    )
    assert _platform_lines(msg.text) == ["Platforma: Portal javnih nabavki"]
    assert "<b>Platforma:</b> Portal javnih nabavki" in (msg.html_body or "")


@pytest.mark.parametrize("label", [None, "", "  "])
def test_unknown_display_name_falls_back_to_key(label):
    text = TelegramAdapter(_Bot()).format(_job(source_label=label)).text
    assert _platform_lines(text) == ["🏛 Platforma: jnportal"]
