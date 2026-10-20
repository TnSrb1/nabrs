"""The welcome e-mail after the e-mail confirmation (owner 16.10.2026).

The letter goes out ONCE — at the moment the ``email_verified_at`` transitions NULL→NOT NULL
inside :func:`auth_service.verify_email_token` (without a migration). A repeated
confirmation, a login by magic-link and a repeated click — do not send.

Expectations — literals from the owner’s text, not via ``email_t``/``WELCOME``: otherwise the
test would check itself (see tests-known-defects: tautology).
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone

import pytest

from src.api.cabinet import auth_service as svc
from src.api.config import CabinetSettings
from src.api.emailer import MockEmailSender
from src.i18n_email import welcome_parts
from tests.api.conftest import FakeResult, FakeSession, make_token, make_user

SR_SUBJECT = "Dobrodošli na nab.rs — besplatan probni period"
EN_SUBJECT = "Welcome to nab.rs — free trial period"
SR_GREETING = "Poštovani user@company.rs,"
EN_GREETING = "Dear user@company.rs,"
SR_LINES = [
    "hvala što ste se registrovali na nab.rs.",
    "Servis je trenutno u probnom periodu.",
    "Korišćenje je potpuno besplatno.",
    "Pišite nam šta ne radi, šta nedostaje i šta biste promenili.",
    "Servis dorađujemo upravo na osnovu vaših primedbi i predloga.",
    "e-mail: info@nab.rs",
    "Telegram: t.me/nabrs_news",
    "Prijava u servis: https://app.nab.rs",
    "Srdačan pozdrav,",
    "Tim nab.rs",
]
EN_LINES = [
    "thank you for registering on nab.rs.",
    "The service is currently in a free trial period.",
    "Using it is completely free of charge.",
    "Please tell us what does not work, what is missing and what you would change.",
    "We are improving the service precisely on the basis of your comments and suggestions.",
    "e-mail: info@nab.rs",
    "Telegram: t.me/nabrs_news",
    "Log in to the service: https://app.nab.rs",
    "Kind regards,",
    "The nab.rs team",
]
ADDRESS = "user@company.rs"


def _settings():
    return CabinetSettings(CABINET_BASE_URL="https://nab.rs", CABINET_SECRET_KEY="t")


def _one(item):
    return FakeResult(items=[item] if item is not None else [])


def _verify_session(user, token):
    return FakeSession(
        results=[_one(token), FakeResult(items=[user.tenant_id])],
        get_map={"User": user},
    )


# --- the text: the choice of the language ----------------------------------------------------


def test_welcome_sr_only_for_sr_locale():
    subject, text, _html = welcome_parts("sr", name=ADDRESS)
    assert subject == SR_SUBJECT
    assert SR_GREETING in text
    for line in SR_LINES:
        assert line in text
    assert "Dear" not in text and "Welcome" not in text


def test_welcome_en_only_for_en_locale():
    subject, text, _html = welcome_parts("en", name=ADDRESS)
    assert subject == EN_SUBJECT
    assert EN_GREETING in text
    for line in EN_LINES:
        assert line in text
    assert "Poštovani" not in text and "Dobrodošli" not in text


@pytest.mark.parametrize("locale", ["ru", None, ""])
def test_welcome_ru_or_empty_is_bilingual_sr_first(locale):
    subject, text, _html = welcome_parts(locale, name=ADDRESS)
    assert SR_SUBJECT in subject and EN_SUBJECT in subject
    assert text.index(SR_GREETING) < text.index(EN_GREETING)
    for line in SR_LINES + EN_LINES:
        assert line in text


def test_welcome_html_escapes_name_and_keeps_text():
    _subject, _text, html = welcome_parts("sr", name="<b>x</b>@a.rs")
    assert "&lt;b&gt;x&lt;/b&gt;@a.rs" in html
    assert "<b>x" not in html
    assert "Srdačan pozdrav" in html


# --- sending: once -----------------------------------------------------


@pytest.mark.asyncio
async def test_verify_sends_welcome_once_in_sr():
    user = make_user(email_verified_at=None, locale="sr")
    token = make_token(purpose="email_verify", user_id=user.id)
    sender = MockEmailSender()
    sess = await svc.verify_email_token(
        _verify_session(user, token),
        raw_token="raw",
        email_sender=sender,
    )
    assert sess is not None
    assert len(sender.outbox) == 1
    sent = sender.outbox[0]
    assert sent.to == ADDRESS
    assert sent.subject == SR_SUBJECT
    assert SR_GREETING in sent.text_body
    assert sent.html_body is not None


@pytest.mark.asyncio
async def test_verify_sends_bilingual_for_ru_user():
    user = make_user(email_verified_at=None, locale="ru")
    token = make_token(purpose="email_verify", user_id=user.id)
    sender = MockEmailSender()
    await svc.verify_email_token(
        _verify_session(user, token), raw_token="raw", email_sender=sender
    )
    assert len(sender.outbox) == 1
    assert EN_GREETING in sender.outbox[0].text_body


@pytest.mark.asyncio
async def test_repeat_verification_with_live_token_does_not_resend():
    """The token is alive, but the account is already confirmed (the transition already happened) → there is no second letter."""
    user = make_user(email_verified_at=datetime.now(timezone.utc), locale="sr")
    token = make_token(purpose="email_verify", user_id=user.id)
    sender = MockEmailSender()
    sess = await svc.verify_email_token(
        _verify_session(user, token), raw_token="raw", email_sender=sender
    )
    assert sess is not None
    assert sender.outbox == []


@pytest.mark.asyncio
async def test_consumed_token_does_not_resend():
    user = make_user(email_verified_at=None, locale="sr")
    token = make_token(
        purpose="email_verify",
        user_id=user.id,
        consumed_at=datetime.now(timezone.utc),
    )
    sender = MockEmailSender()
    sess = await svc.verify_email_token(
        FakeSession(results=[_one(token)]), raw_token="raw", email_sender=sender
    )
    assert sess is None
    assert sender.outbox == []


@pytest.mark.asyncio
async def test_smtp_error_does_not_break_confirmation(caplog):
    class _BrokenSender(MockEmailSender):
        async def send(self, **kwargs):
            raise smtplib.SMTPException("relay refused " + ADDRESS)

    user = make_user(email_verified_at=None, locale="sr")
    token = make_token(purpose="email_verify", user_id=user.id)
    caplog.set_level(logging.DEBUG)
    sess = await svc.verify_email_token(
        _verify_session(user, token), raw_token="raw", email_sender=_BrokenSender()
    )
    # The confirmation took place, despite the SMTP refusal.
    assert sess is not None
    assert user.email_verified_at is not None
    assert token.consumed_at is not None
    # In the log — only the exception type; we do not write the address or the error text.
    assert "SMTPException" in caplog.text
    assert ADDRESS not in caplog.text
    assert "relay refused" not in caplog.text


@pytest.mark.asyncio
async def test_no_address_in_logs_on_success(caplog):
    user = make_user(email_verified_at=None, locale="en")
    token = make_token(purpose="email_verify", user_id=user.id)
    caplog.set_level(logging.DEBUG)
    await svc.verify_email_token(
        _verify_session(user, token), raw_token="raw", email_sender=MockEmailSender()
    )
    assert ADDRESS not in caplog.text


def test_verify_route_passes_email_sender_to_service(monkeypatch):
    """The route gives the service the sender that is connected in deps (the wiring of the letter)."""
    from fastapi.testclient import TestClient

    from src.api import deps
    from src.api.cabinet import routes_auth
    from src.api.main import app

    seen = {}
    sender = MockEmailSender()

    async def _stub(session, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(routes_auth.svc, "verify_email_token", _stub)
    monkeypatch.setattr(routes_auth.svc, "email_verify_already_done", _not_done)
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: FakeSession())
    app.dependency_overrides[deps.get_email_sender] = lambda: sender
    try:
        TestClient(app, follow_redirects=False).get("/auth/verify?token=raw")
    finally:
        app.dependency_overrides.clear()
    assert seen["email_sender"] is sender
    assert seen["raw_token"] == "raw"


async def _not_done(*args, **kwargs):
    return False


# --- the sender: the welcome — from info@nab.rs, the rest — from noreply ---------


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["sr", "en", "ru"])
async def test_welcome_sent_from_and_reply_to_info_inbox(locale):
    user = make_user(email_verified_at=None, locale=locale)
    token = make_token(purpose="email_verify", user_id=user.id)
    sender = MockEmailSender()
    await svc.verify_email_token(
        _verify_session(user, token), raw_token="raw", email_sender=sender
    )
    assert len(sender.outbox) == 1
    rec = sender.outbox[0]
    assert rec.from_email == "nab.rs <info@nab.rs>"
    assert rec.reply_to == "info@nab.rs"
    # the caption “e-mail: info@nab.rs” — both in text and in html, in all locales
    assert "info@nab.rs" in rec.text_body
    assert "info@nab.rs" in rec.html_body


@pytest.mark.asyncio
async def test_magic_link_mail_keeps_configured_sender():
    """A login by magic-link is NOT a welcome: the sender is not overridden (noreply)."""
    user = make_user(email_verified_at=datetime.now(timezone.utc), locale="sr")
    sender = MockEmailSender()
    await svc.request_magic_link(
        FakeSession(results=[_one(user)]),
        email=user.email,
        email_sender=sender,
        settings=_settings(),
    )
    rec = sender.outbox[0]
    assert "/auth/magic?token=" in rec.text_body
    assert rec.from_email is None
    assert rec.reply_to is None
