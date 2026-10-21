"""Sending transactional mail (ARCHITECTURE §9).

The :class:`EmailSender` interface with three implementations: :class:`SmtpSender` (the stdlib ``smtplib``, STAGE 2), :class:`PostmarkSender` (the Postmark Email API) and :class:`MockEmailSender` (dev / tests, ADR-013). The choice of transport is :func:`make_email_sender` (priority SMTP → Postmark → Mock).
"""

from __future__ import annotations

import asyncio
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage

from src.domain.text_norm import sr_display

POSTMARK_SEND_URL = "https://api.postmarkapp.com/email"


@dataclass
class SentEmail:
    """A record of a sent email (for MockEmailSender / tests)."""

    to: str
    subject: str
    text_body: str
    html_body: str | None = None
    from_email: str | None = None
    reply_to: str | None = None


def localize_email(
    locale: str | None, subject: str, text_body: str, html_body: str | None
) -> tuple[str, str, str | None]:
    """The subject / text / HTML of the email at the send boundary: sr → Latin, otherwise as is."""
    return (
        sr_display(subject, locale),
        sr_display(text_body, locale),
        sr_display(html_body, locale),
    )


class EmailSender(ABC):
    """The sender of transactional emails."""

    @abstractmethod
    async def send(
        self,
        *,
        to: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
        locale: str | None = None,
        from_email: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        """``locale`` — the recipient’s language; ``“sr”`` → the email only in Latin
        (owner 10.10.2026: the tender data is in Cyrillic, at the display boundary — Latin). A missing language = “as is”: the caller passes ``users.locale``.
        ``from_email`` — an override of ``From`` (otherwise the configured sender); ``reply_to`` — ``Reply-To`` (if not given, it is not set).
        """
        raise NotImplementedError


class SmtpSender(EmailSender):
    """Sending via SMTP (the stdlib ``smtplib``) — without external dependencies.

    Port 465 → implicit TLS (``SMTP_SSL``); otherwise plain SMTP + ``starttls()``. The blocking ``smtplib`` is moved to a thread via ``asyncio.to_thread``, so as not to block the event loop. Authentication (``login``) — only if a ``username`` is set; an empty ``username`` → sending without login (for example, a local relay).
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        from_email: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._user = username
        self._password = password
        self._from = from_email
        self._timeout = timeout

    async def send(
        self,
        *,
        to: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
        locale: str | None = None,
        from_email: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        if not self._host:
            raise RuntimeError("SMTP_HOST is not set")
        subject, text_body, html_body = localize_email(
            locale, subject, text_body, html_body
        )
        await asyncio.to_thread(
            self._send_sync, to, subject, text_body, html_body, from_email, reply_to
        )

    def _send_sync(
        self,
        to: str,
        subject: str,
        text_body: str,
        html_body: str | None,
        from_email: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        msg = EmailMessage()
        msg["From"] = from_email or self._from
        msg["To"] = to
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(text_body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        if self._port == 465:
            with smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout
            ) as smtp:
                self._auth_and_send(smtp, msg)
        else:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
                smtp.starttls()
                self._auth_and_send(smtp, msg)

    def _auth_and_send(self, smtp: smtplib.SMTP, msg: EmailMessage) -> None:
        if self._user:
            smtp.login(self._user, self._password)
        smtp.send_message(msg)


class PostmarkSender(EmailSender):
    """A real send via the Postmark Email API."""

    def __init__(
        self, server_token: str, from_email: str, *, timeout: float = 10.0
    ) -> None:
        self._token = server_token
        self._from = from_email
        self._timeout = timeout

    async def send(
        self,
        *,
        to: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
        locale: str | None = None,
        from_email: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        if not self._token:
            raise RuntimeError("POSTMARK_SERVER_TOKEN is not set")
        import httpx  # lazy

        subject, text_body, html_body = localize_email(
            locale, subject, text_body, html_body
        )

        payload = {
            "From": from_email or self._from,
            "To": to,
            "Subject": subject,
            "TextBody": text_body,
            "MessageStream": "outbound",
        }
        if html_body:
            payload["HtmlBody"] = html_body
        if reply_to:
            payload["ReplyTo"] = reply_to
        headers = {
            "X-Postmark-Server-Token": self._token,
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(POSTMARK_SEND_URL, json=payload, headers=headers)
            resp.raise_for_status()


class MockEmailSender(EmailSender):
    """A stub: accumulates the emails in ``outbox`` (for dev / tests)."""

    def __init__(self) -> None:
        self.outbox: list[SentEmail] = []

    async def send(
        self,
        *,
        to: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
        locale: str | None = None,
        from_email: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        subject, text_body, html_body = localize_email(
            locale, subject, text_body, html_body
        )
        self.outbox.append(
            SentEmail(to, subject, text_body, html_body, from_email, reply_to)
        )


def make_email_sender(
    *,
    smtp_host: str = "",
    smtp_port: int = 587,
    smtp_user: str = "",
    smtp_password: str = "",
    smtp_from: str = "",
    postmark_token: str = "",
    email_from: str = "",
) -> EmailSender:
    """The choice of transport by priority SMTP → Postmark → Mock.

    - ``SMTP_HOST`` set → :class:`SmtpSender` (the sender — ``SMTP_FROM``, or a fallback to ``EMAIL_FROM``).
    - otherwise ``POSTMARK_SERVER_TOKEN`` set → :class:`PostmarkSender`.
    - otherwise (dev) → :class:`MockEmailSender` (the emails pile up in ``outbox``).
    """
    if smtp_host:
        return SmtpSender(
            smtp_host,
            smtp_port,
            smtp_user,
            smtp_password,
            smtp_from or email_from,
        )
    if postmark_token:
        return PostmarkSender(postmark_token, email_from)
    return MockEmailSender()
