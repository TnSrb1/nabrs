"""The address confirmation email — in the cabinet language, not always in Russian.

Defect: ``_send_confirmation`` sent a hard-coded Russian text ("Confirm the
address for emails“, ”Hello! This address was entered…") to any client. The locale did not come into
the function at all — unlike the registration email, which already chose the language via ``email_t``.

A transactional email is part of the product: a Serb binding an address in the Serbian
cabinet got a Russian email.
"""

from __future__ import annotations

import re

import pytest

from src.api.cabinet import channel_service as svc
from src.i18n_email import EMAIL

_CYR = re.compile(r"[а-яА-ЯёЁ]")


class _Sender:
    def __init__(self) -> None:
        self.outbox: list[dict] = []

    async def send(self, *, to: str, subject: str, text_body: str, **kwargs) -> None:
        self.outbox.append({"to": to, "subject": subject, "text_body": text_body})


class _Settings:
    email_verify_ttl = 7200
    cabinet_base_url = "https://app.nab.rs"
    public_base_url = "https://app.nab.rs"


def test_channel_verify_keys_parity():
    """The keys of the email exist in all three languages (otherwise a KeyError for the client)."""
    for key in ("channel_verify_subject", "channel_verify_body"):
        for lang in ("ru", "sr", "en"):
            assert key in EMAIL[lang], f"{key} is missing in {lang}"


@pytest.mark.parametrize("locale", ["sr", "en"])
@pytest.mark.asyncio
async def test_confirmation_email_speaks_client_language(locale):
    """The email in the cabinet language: without Russian letters, with the link and the expiry.

    Mutation: return a literal in ``_send_confirmation`` → the test turns red.
    """
    sender = _Sender()
    await svc._send_confirmation(
        sender,
        settings=_Settings(),
        address="user@company.rs",
        raw_token="tok123",
        locale=locale,
    )
    assert len(sender.outbox) == 1
    msg = sender.outbox[0]
    assert not _CYR.search(msg["subject"]), msg["subject"]
    assert not _CYR.search(msg["text_body"]), msg["text_body"]
    # The substitutions were not lost: the link and the validity period are in place.
    assert "tok123" in msg["text_body"]
    assert "2" in msg["text_body"]  # email_verify_ttl 7200s → 2 h


@pytest.mark.asyncio
async def test_confirmation_email_ru_keeps_original_text():
    """A Russian client gets the former text — the canon has not changed."""
    sender = _Sender()
    await svc._send_confirmation(
        sender,
        settings=_Settings(),
        address="user@company.ru",
        raw_token="tok",
        locale="ru",
    )
    msg = sender.outbox[0]
    assert msg["subject"] == EMAIL["ru"]["channel_verify_subject"]
    assert "Этот адрес указали в личном кабинете" in msg["text_body"]


@pytest.mark.asyncio
async def test_confirmation_email_unknown_locale_falls_back_to_ru():
    """None/unknown language → ru (the norm_locale contract), not a KeyError."""
    sender = _Sender()
    await svc._send_confirmation(
        sender,
        settings=_Settings(),
        address="user@company.rs",
        raw_token="tok",
        locale=None,
    )
    assert sender.outbox[0]["subject"] == EMAIL["ru"]["channel_verify_subject"]
