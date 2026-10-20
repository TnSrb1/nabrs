"""The mark “the same tender on other sources” in the delivery: the card and the digest.

Each render point of the delivery (the Telegram card, the letter, the digest of both channels) must show the same mark as the public card (0047): ``dup`` → ``also_on``, ``probable`` → ``also_on_probable``, N = the other sources. The expectations are LITERALS of the catalog (an assertion through the same ``common_t``/``email_t`` would be a tautology, ``.claude/rules/tests-known-defects.md``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.api.emailer import MockEmailSender
from src.workers.adapters.base import DeliveryJob
from src.workers.adapters.email import EmailAdapter
from src.workers.adapters.telegram import TelegramAdapter


class _FakeBot:
    async def send_message(self, **kwargs):  # pragma: no cover — is not called
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
        title="Набавка рачунарске опреме",
        source_url="https://jnportal.rs/tender/1",
        amount_eur=1_234_567.0,
        deadline_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc),
        source_key="ted_balkan",
        profile_id="prof-1",
        profile_name="ИТ-опрема",
        canonical_confidence="dup",
        also_on_n=1,
        locale="sr",
    )
    base.update(over)
    return DeliveryJob(**base)


def _tg() -> TelegramAdapter:
    return TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs")


def _email() -> EmailAdapter:
    return EmailAdapter(MockEmailSender(), cabinet_base_url="https://nab.rs")


# --- Telegram ------------------------------------------------------------- #


def test_telegram_card_marks_dup_in_channel_language():
    text = _tg().format(_job()).text
    assert "🔁 Isti tender je i na drugim platformama: 1" in text


def test_telegram_card_marks_probable_as_probable():
    text = _tg().format(_job(canonical_confidence="probable", locale="ru")).text
    assert "🔁 Вероятно, тот же тендер есть и на других площадках: 1" in text


def test_telegram_card_without_cluster_has_no_mark():
    text = _tg().format(_job(canonical_confidence=None, also_on_n=0)).text
    assert "🔁" not in text


def test_telegram_digest_marks_each_clustered_tender():
    jobs = [
        _job(outbox_id=1, also_on_n=1),
        _job(outbox_id=2, tender_id="t-2", canonical_confidence=None, also_on_n=0),
    ]
    text = _tg().format_digest(jobs).text
    assert text.count("🔁 Isti tender je i na drugim platformama: 1") == 1


# --- Email ---------------------------------------------------------------- #


def test_email_card_marks_in_text_and_html():
    msg = _email().format(_job(channel="email", locale="en", also_on_n=2))
    line = "🔁 The same tender is also on other platforms: 2"
    assert line in msg.text
    assert line in msg.html_body


def test_email_card_without_cluster_has_no_mark():
    msg = _email().format(_job(channel="email", canonical_confidence=None, also_on_n=0))
    assert "🔁" not in msg.text
    assert "🔁" not in msg.html_body


def test_email_digest_marks_probable_in_text_and_html():
    jobs = [_job(channel="email", canonical_confidence="probable", locale="sr")]
    msg = _email().format_digest(jobs)
    line = "🔁 Verovatno isti tender je i na drugim platformama: 1"
    assert line in msg.text
    assert line in msg.html_body
