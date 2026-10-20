"""Tests of the SMTP transport and of the sender-selection factory (STAGE 2).

We do not touch the network — ``smtplib`` is mocked by substituting the SMTP/SMTP_SSL classes.
"""

from __future__ import annotations

import smtplib

import pytest

from src.api.emailer import (
    MockEmailSender,
    PostmarkSender,
    SmtpSender,
    make_email_sender,
)

# --- make_email_sender: three priority branches SMTP → Postmark → Mock ----------


def test_factory_prefers_smtp_when_host_set():
    sender = make_email_sender(
        smtp_host="smtp.example.com",
        smtp_user="u",
        smtp_password="p",
        smtp_from="from@nab.rs",
        postmark_token="tok",
        email_from="ef@nab.rs",
    )
    assert isinstance(sender, SmtpSender)
    assert sender._from == "from@nab.rs"


def test_factory_smtp_from_falls_back_to_email_from():
    sender = make_email_sender(
        smtp_host="smtp.example.com", smtp_from="", email_from="ef@nab.rs"
    )
    assert isinstance(sender, SmtpSender)
    assert sender._from == "ef@nab.rs"


def test_factory_postmark_when_no_smtp_but_token():
    sender = make_email_sender(postmark_token="tok", email_from="ef@nab.rs")
    assert isinstance(sender, PostmarkSender)


def test_factory_mock_when_nothing_configured():
    sender = make_email_sender(email_from="ef@nab.rs")
    assert isinstance(sender, MockEmailSender)


# --- SmtpSender: mocked smtplib ------------------------------------------


class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent = msg


class _FakeSMTPSSL(_FakeSMTP):
    instances: list = []


@pytest.fixture(autouse=True)
def _reset_instances():
    _FakeSMTP.instances = []
    _FakeSMTPSSL.instances = []
    yield


async def test_smtp_587_uses_starttls_and_login(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)
    sender = SmtpSender("smtp.example.com", 587, "user", "pass", "from@nab.rs")
    await sender.send(to="to@nab.rs", subject="Тема", text_body="Текст")
    assert len(_FakeSMTP.instances) == 1
    assert _FakeSMTPSSL.instances == []
    smtp = _FakeSMTP.instances[0]
    assert smtp.port == 587
    assert smtp.timeout == 10.0
    assert smtp.started_tls is True
    assert smtp.logged_in == ("user", "pass")
    assert smtp.sent["To"] == "to@nab.rs"
    assert smtp.sent["From"] == "from@nab.rs"
    assert smtp.sent["Subject"] == "Тема"


async def test_smtp_465_uses_ssl_no_starttls(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)
    sender = SmtpSender("smtp.example.com", 465, "user", "pass", "from@nab.rs")
    await sender.send(to="to@nab.rs", subject="S", text_body="T")
    assert len(_FakeSMTPSSL.instances) == 1
    assert _FakeSMTP.instances == []
    smtp = _FakeSMTPSSL.instances[0]
    assert smtp.port == 465
    assert smtp.started_tls is False
    assert smtp.logged_in == ("user", "pass")


async def test_smtp_no_login_when_user_empty(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    sender = SmtpSender("smtp.example.com", 587, "", "", "from@nab.rs")
    await sender.send(to="to@nab.rs", subject="S", text_body="T")
    assert _FakeSMTP.instances[0].logged_in is None


async def test_smtp_html_makes_multipart(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    sender = SmtpSender("smtp.example.com", 587, "u", "p", "from@nab.rs")
    await sender.send(to="to@nab.rs", subject="S", text_body="T", html_body="<b>H</b>")
    assert _FakeSMTP.instances[0].sent.is_multipart() is True


async def test_smtp_raises_without_host():
    sender = SmtpSender("", 587, "u", "p", "from@nab.rs")
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        await sender.send(to="to@nab.rs", subject="S", text_body="T")


# --- From / Reply-To: override on sending (the welcome with info@) -------


async def test_smtp_from_and_reply_to_override(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)
    sender = SmtpSender("smtp.example.com", 587, "user", "pass", "noreply@nab.rs")
    await sender.send(
        to="to@nab.rs",
        subject="S",
        text_body="T",
        from_email="nab.rs <info@nab.rs>",
        reply_to="info@nab.rs",
    )
    msg = _FakeSMTP.instances[0].sent
    # EmailMessage takes the display name with a dot in quotes (RFC 5322) — a fact of the run.
    assert msg["From"] == '"nab.rs" <info@nab.rs>'
    assert msg["Reply-To"] == "info@nab.rs"


async def test_smtp_without_override_keeps_configured_from(monkeypatch):
    """Without an override the letter goes out with the configured SMTP_FROM and without Reply-To."""
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)
    sender = SmtpSender("smtp.example.com", 587, "user", "pass", "noreply@nab.rs")
    await sender.send(to="to@nab.rs", subject="S", text_body="T")
    msg = _FakeSMTP.instances[0].sent
    assert msg["From"] == "noreply@nab.rs"
    assert msg["Reply-To"] is None
