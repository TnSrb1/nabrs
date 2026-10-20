"""Tests of the email senders (ADR-013): MockEmailSender and PostmarkSender.

We do not touch the network — the httpx client in PostmarkSender is mocked.
"""

from __future__ import annotations

import sys
import types

import pytest

from src.api import emailer
from src.api.emailer import (
    POSTMARK_SEND_URL,
    EmailSender,
    MockEmailSender,
    PostmarkSender,
    SentEmail,
)

# --- SentEmail / interface ---------------------------------------------------


def test_sent_email_defaults_html_none():
    rec = SentEmail(to="a@b.rs", subject="S", text_body="T")
    assert rec.to == "a@b.rs"
    assert rec.subject == "S"
    assert rec.text_body == "T"
    assert rec.html_body is None


def test_email_sender_is_abstract():
    with pytest.raises(TypeError):
        EmailSender()  # type: ignore[abstract]


# --- MockEmailSender ---------------------------------------------------------


async def test_mock_starts_with_empty_outbox():
    sender = MockEmailSender()
    assert sender.outbox == []


async def test_mock_accumulates_text_only():
    sender = MockEmailSender()
    await sender.send(to="user@nab.rs", subject="Привет", text_body="Тело")
    assert len(sender.outbox) == 1
    rec = sender.outbox[0]
    assert rec == SentEmail(to="user@nab.rs", subject="Привет", text_body="Тело")
    assert rec.html_body is None


async def test_mock_accumulates_html_and_order_preserved():
    sender = MockEmailSender()
    await sender.send(to="one@nab.rs", subject="S1", text_body="T1")
    await sender.send(
        to="two@nab.rs", subject="S2", text_body="T2", html_body="<b>T2</b>"
    )
    assert [r.to for r in sender.outbox] == ["one@nab.rs", "two@nab.rs"]
    assert sender.outbox[1].html_body == "<b>T2</b>"


# --- PostmarkSender: contract without network ---------------------------------------


async def test_postmark_raises_without_token():
    sender = PostmarkSender(server_token="", from_email="no-reply@nab.rs")
    with pytest.raises(RuntimeError, match="POSTMARK_SERVER_TOKEN"):
        await sender.send(to="x@nab.rs", subject="S", text_body="T")


class _FakeResponse:
    def __init__(self) -> None:
        self.raise_called = False

    def raise_for_status(self) -> None:
        self.raise_called = True


class _FakeAsyncClient:
    """Substitutes httpx.AsyncClient: remembers the arguments of post, without network."""

    last_instance: "_FakeAsyncClient | None" = None

    def __init__(self, *, timeout: float | None = None) -> None:
        self.timeout = timeout
        self.posted: dict | None = None
        self.response = _FakeResponse()
        _FakeAsyncClient.last_instance = self

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def post(self, url, *, json, headers):  # noqa: A002 - httpx signature
        self.posted = {"url": url, "json": json, "headers": headers}
        return self.response


@pytest.fixture
def fake_httpx(monkeypatch):
    """The lazily imported httpx is substituted with a fake in sys.modules."""
    module = types.ModuleType("httpx")
    module.AsyncClient = _FakeAsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", module)
    _FakeAsyncClient.last_instance = None
    return module


async def test_postmark_send_builds_payload_text_only(fake_httpx):
    sender = PostmarkSender(
        server_token="tok-123", from_email="no-reply@nab.rs", timeout=5.0
    )
    await sender.send(to="dst@nab.rs", subject="Тема", text_body="Текст")

    client = _FakeAsyncClient.last_instance
    assert client is not None
    assert client.timeout == 5.0
    assert client.posted["url"] == POSTMARK_SEND_URL
    payload = client.posted["json"]
    assert payload == {
        "From": "no-reply@nab.rs",
        "To": "dst@nab.rs",
        "Subject": "Тема",
        "TextBody": "Текст",
        "MessageStream": "outbound",
    }
    assert "HtmlBody" not in payload
    assert client.posted["headers"]["X-Postmark-Server-Token"] == "tok-123"
    assert client.posted["headers"]["Accept"] == "application/json"
    assert client.response.raise_called is True


async def test_postmark_send_includes_html_when_given(fake_httpx):
    sender = PostmarkSender(server_token="tok", from_email="from@nab.rs")
    await sender.send(
        to="dst@nab.rs",
        subject="S",
        text_body="T",
        html_body="<p>Hi</p>",
    )
    payload = _FakeAsyncClient.last_instance.posted["json"]
    assert payload["HtmlBody"] == "<p>Hi</p>"


async def test_postmark_default_timeout_is_ten(fake_httpx):
    sender = PostmarkSender(server_token="tok", from_email="from@nab.rs")
    await sender.send(to="dst@nab.rs", subject="S", text_body="T")
    assert _FakeAsyncClient.last_instance.timeout == 10.0


def test_module_constant_send_url():
    assert emailer.POSTMARK_SEND_URL == "https://api.postmarkapp.com/email"


async def test_mock_records_from_and_reply_to():
    sender = MockEmailSender()
    await sender.send(
        to="u@nab.rs",
        subject="S",
        text_body="T",
        from_email="nab.rs <info@nab.rs>",
        reply_to="info@nab.rs",
    )
    rec = sender.outbox[0]
    assert rec.from_email == "nab.rs <info@nab.rs>"
    assert rec.reply_to == "info@nab.rs"


async def test_mock_default_from_and_reply_to_are_none():
    sender = MockEmailSender()
    await sender.send(to="u@nab.rs", subject="S", text_body="T")
    rec = sender.outbox[0]
    assert rec.from_email is None
    assert rec.reply_to is None


async def test_postmark_from_and_reply_to_override(fake_httpx):
    sender = PostmarkSender(server_token="tok", from_email="noreply@nab.rs")
    await sender.send(
        to="dst@nab.rs",
        subject="S",
        text_body="T",
        from_email="nab.rs <info@nab.rs>",
        reply_to="info@nab.rs",
    )
    payload = _FakeAsyncClient.last_instance.posted["json"]
    assert payload["From"] == "nab.rs <info@nab.rs>"
    assert payload["ReplyTo"] == "info@nab.rs"


async def test_postmark_without_override_has_no_reply_to(fake_httpx):
    sender = PostmarkSender(server_token="tok", from_email="noreply@nab.rs")
    await sender.send(to="dst@nab.rs", subject="S", text_body="T")
    payload = _FakeAsyncClient.last_instance.posted["json"]
    assert payload["From"] == "noreply@nab.rs"
    assert "ReplyTo" not in payload
