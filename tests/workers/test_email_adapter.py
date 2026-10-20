"""Unit tests of the EmailAdapter (the delivery of tenders by letter).

We check the rendering of the letter (the subject, the plain-text and HTML body, the link to the card instead of the inline buttons), the sending via MockEmailSender (there is no network) and the mapping of the failures to DeliveryResult: an error of the sender → ok=False without an exception going out, a refused address → blocked, an empty target → ok=False. Separately — that the recipient’s address does not leak into error_text (PII).

A separate block — fail-closed: the mail is not configured (the delivery service in production has neither SMTP_HOST nor POSTMARK_SERVER_TOKEN) → send returns a retryable error, and NOT ok=True for an unsent letter. The difference between “the Mock was passed explicitly” (the test — we trust) and “the Mock came as a silent fallback from an empty env” (production — we do not trust) is checked here too.
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone

import pytest

from src.api import emailer
from src.api.emailer import EmailSender, MockEmailSender, SmtpSender
from src.workers.adapters import default_adapters, email_sender_from_env
from src.workers.adapters.base import DeliveryJob
from src.workers.adapters.email import EmailAdapter

TARGET = "buyer@example.com"


class _BoomSender(EmailSender):
    """A sender that always falls with the given exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def send(self, *, to, subject, text_body, html_body=None, **_kw) -> None:
        raise self._exc


def _job(**over) -> DeliveryJob:
    base = dict(
        outbox_id=7,
        tender_id="abc-123",
        channel="email",
        requires_ai=False,
        user_channel_id="uc-1",
        target=TARGET,
        channel_status="active",
        title="Видеонаблюдение & монтаж",
        source_url="https://jnportal.rs/tender/1?x=1",
        amount_eur=1234567.0,
        deadline_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc),
        source_uid="374138",
        source_key="jnportal",
        amount_original=144809500.0,
        currency_original="RSD",
        is_amount_estimated=True,
        published_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        procedure_type="open",
        regions=["Београдски округ"],
        contract_type=None,
        classifications=None,
        profile_name="Видеонаблюдение",
    )
    base.update(over)
    return DeliveryJob(**base)


def _adapter(sender: EmailSender | None = None) -> EmailAdapter:
    return EmailAdapter(sender or MockEmailSender(), cabinet_base_url="https://nab.rs")


# -- format ---------------------------------------------------------------- #


def test_format_subject_and_body_with_card_link():
    message = _adapter().format(_job())

    assert "Видеонаблюдение & монтаж" in message.subject
    # The letter has no buttons — instead there is a link to the tender card.
    assert message.reply_markup is None
    assert "https://nab.rs/tender/abc-123" in message.text
    assert "https://nab.rs/tender/abc-123" in (message.html_body or "")
    # The lines of the card — as in the Telegram version.
    assert "№ тендера: 374138" in message.text
    assert "Регион: Београдски округ" in message.text
    # format_deadline converts UTC→Belgrade: 14:30Z of June (CEST +2) → 16:30.
    assert "Приём до: 01.06.2026 16:30" in message.text
    assert "https://jnportal.rs/tender/1?x=1" in message.text


def test_format_escapes_html_body_but_not_text():
    message = _adapter().format(_job(title="Ремонт <b>школы</b> & сада"))

    assert "Ремонт <b>школы</b> & сада" in message.text
    assert "<b>школы</b>" not in (message.html_body or "")
    assert "&lt;b&gt;школы&lt;/b&gt;" in (message.html_body or "")


def test_format_subject_has_no_newlines():
    """Header injection: a line break in the subject is not allowed."""
    message = _adapter().format(_job(title="Ремонт\nBcc: attacker@example.com"))

    assert "\n" not in message.subject
    assert "\r" not in message.subject


def test_format_long_title_trims_subject():
    message = _adapter().format(_job(title="Тендер " * 60))

    assert len(message.subject) <= 120


def test_format_skips_missing_fields():
    message = _adapter().format(
        _job(source_uid=None, source_url=None, regions=None, profile_name=None)
    )

    assert "№ тендера" not in message.text
    assert "Регион" not in message.text
    assert "Источник:" not in message.text
    assert "Найдено по шаблону: (без имени)" in message.text


def test_supports_buttons_is_false():
    assert _adapter().supports_buttons() is False


# -- send ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_send_ok_through_mock_sender():
    sender = MockEmailSender()
    adapter = _adapter(sender)
    job = _job()
    message = adapter.format(job)

    result = await adapter.send(job, message)

    assert result.ok is True
    assert result.response_code == 200
    assert len(sender.outbox) == 1
    sent = sender.outbox[0]
    assert sent.to == TARGET
    assert sent.subject == message.subject
    assert sent.text_body == message.text
    assert sent.html_body == message.html_body


@pytest.mark.asyncio
async def test_send_empty_target_fails_without_sending():
    sender = MockEmailSender()
    adapter = _adapter(sender)
    job = _job(target="   ")

    result = await adapter.send(job, adapter.format(job))

    assert result.ok is False
    assert result.blocked is True
    assert sender.outbox == []


@pytest.mark.asyncio
async def test_send_sender_error_returns_retryable_not_raise():
    adapter = _adapter(_BoomSender(RuntimeError("SMTP_HOST не задан")))
    job = _job()

    result = await adapter.send(job, adapter.format(job))

    assert result.ok is False
    assert result.retryable is True
    assert result.blocked is False
    assert "RuntimeError" in (result.error_text or "")


@pytest.mark.asyncio
async def test_send_recipient_refused_blocks_channel():
    exc = smtplib.SMTPRecipientsRefused({TARGET: (550, b"no such user")})
    adapter = _adapter(_BoomSender(exc))
    job = _job()

    result = await adapter.send(job, adapter.format(job))

    assert result.ok is False
    assert result.blocked is True
    assert result.retryable is False


@pytest.mark.asyncio
async def test_send_error_text_has_no_recipient_address():
    """PII: the recipient’s address does not get into error_text (it goes into delivery_attempts)."""
    exc = smtplib.SMTPRecipientsRefused({TARGET: (550, b"no such user")})
    adapter = _adapter(_BoomSender(exc))
    job = _job()

    result = await adapter.send(job, adapter.format(job))

    assert TARGET not in (result.error_text or "")


@pytest.mark.asyncio
async def test_send_error_does_not_log_recipient_address(caplog):
    adapter = _adapter(_BoomSender(RuntimeError(f"relay refused {TARGET}")))
    job = _job()

    with caplog.at_level("ERROR"):
        result = await adapter.send(job, adapter.format(job))

    assert result.ok is False
    assert TARGET not in caplog.text
    assert "uc-1" in caplog.text


# -- adapter registry ------------------------------------------------------ #


def test_default_adapters_gives_working_email_adapter():
    sender = MockEmailSender()
    adapters = default_adapters(object(), email_sender=sender)

    email = adapters["email"]
    assert isinstance(email, EmailAdapter)
    # Working, not a stub: format does not raise NotImplementedError.
    assert email.format(_job()).subject


# -- fail-closed: transport not configured ------------------------------------ #


@pytest.fixture()
def no_email_env(monkeypatch) -> None:
    """The production state of the delivery service: there are no mail env at all."""
    for name in ("SMTP_HOST", "POSTMARK_SERVER_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_email_sender_from_env_is_none_without_transport(no_email_env):
    """An empty env → None, and not a silent MockEmailSender."""
    assert email_sender_from_env() is None


def test_email_sender_from_env_builds_smtp_when_configured(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")

    assert isinstance(email_sender_from_env(), SmtpSender)


@pytest.mark.asyncio
async def test_send_without_transport_is_retryable_not_ok():
    """There is no transport → the row stays in the queue, and not “delivered”."""
    adapter = EmailAdapter(None, cabinet_base_url="https://nab.rs")
    job = _job()

    result = await adapter.send(job, adapter.format(job))

    assert result.ok is False
    assert result.retryable is True
    assert result.blocked is False
    assert "транспорт не настроен" in (result.error_text or "")
    assert adapter.is_configured() is False


@pytest.mark.asyncio
async def test_default_adapters_without_email_env_fails_closed(no_email_env):
    """Through the registry: an empty env → the Email channel does not report success."""
    adapter = default_adapters(object())["email"]
    job = _job()

    result = await adapter.send(job, adapter.format(job))

    assert adapter.is_configured() is False
    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_send_without_transport_does_not_log_recipient_address(caplog):
    """PII: a silent channel makes noise in the log, but never with the recipient’s address."""
    adapter = EmailAdapter(None)
    job = _job()

    with caplog.at_level("ERROR"):
        await adapter.send(job, adapter.format(job))

    assert TARGET not in caplog.text
    assert "uc-1" in caplog.text


def test_explicit_mock_sender_is_trusted_transport(no_email_env):
    """A Mock passed EXPLICITLY (in the tests) — is a legitimate transport even with an empty env."""
    adapter = default_adapters(object(), email_sender=MockEmailSender())["email"]

    assert adapter.is_configured() is True


@pytest.mark.asyncio
async def test_send_through_real_smtp_sender_reports_ok(monkeypatch):
    """A real transport (the network is mocked) → ok=True: the fail-closed does not interfere with the sending."""
    sent: list = []

    class _FakeSmtp:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            sent.append(("starttls",))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("send", msg["To"], msg["Subject"]))

    monkeypatch.setattr(emailer.smtplib, "SMTP", _FakeSmtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "postmaster@nab.rs")
    adapter = EmailAdapter(email_sender_from_env(), cabinet_base_url="https://nab.rs")
    job = _job()
    message = adapter.format(job)

    result = await adapter.send(job, message)

    assert result.ok is True
    assert result.response_code == 200
    assert ("send", TARGET, message.subject) in sent


# --------------------------------------------------------------------------- #
#  The digest: one letter with a list instead of N letters
# --------------------------------------------------------------------------- #


def test_format_digest_subject_and_bodies():
    """The digest letter: the subject with the counter, both bodies with all the tenders."""
    adapter = EmailAdapter(MockEmailSender(), cabinet_base_url="https://nab.rs")
    jobs = [
        _job(outbox_id=1, tender_id="t-1", title="Первый"),
        _job(outbox_id=2, tender_id="t-2", title="Второй"),
    ]

    msg = adapter.format_digest(jobs)

    assert msg.subject == "Дайджест: 2 новых тендера"
    assert "Первый" in msg.text and "Второй" in msg.text
    # The link — the card in the cabinet (as in the single letter), for each tender.
    assert "https://nab.rs/tender/t-1" in msg.text
    assert "https://nab.rs/tender/t-2" in msg.text
    assert 'href="https://nab.rs/tender/t-1"' in msg.html_body
    assert "Найдено по шаблону: Видеонаблюдение" in msg.text


def test_format_digest_escapes_html_and_subject_has_no_injection():
    """The HTML part escapes the name from the source; the subject — only the counter."""
    adapter = EmailAdapter(MockEmailSender(), cabinet_base_url="https://nab.rs")
    msg = adapter.format_digest([_job(title="Монтаж <b>&</b>\nвторая строка")])

    assert "&amp;" in msg.html_body
    assert "<b>&</b>" not in msg.html_body
    assert "\n" not in msg.subject


@pytest.mark.asyncio
async def test_digest_send_uses_digest_subject():
    """send returns the letter with the digest subject (the adapter assembles it itself)."""
    sender = MockEmailSender()
    adapter = EmailAdapter(sender, cabinet_base_url="https://nab.rs")
    jobs = [_job(outbox_id=1), _job(outbox_id=2)]
    msg = adapter.format_digest(jobs)

    result = await adapter.send(jobs[0], msg)

    assert result.ok is True
    assert sender.outbox[-1].subject == "Дайджест: 2 новых тендера"
