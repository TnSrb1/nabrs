"""The guards of the i18n of the e-mail (Phase 4): the chrome of the letters by the recipient’s language (sr/ru/en).

Two surfaces of the letters:

- the delivery of tenders (:mod:`src.workers.adapters.email`): the subject prefixes, the body-card, the digest — the language from ``DeliveryJob.locale`` (users.locale);
- the transactional ones (:mod:`src.api.cabinet.auth_service`): verify/magic-link — the language from the parameter ``locale`` (the router resolves it from the request).

We check: under en/sr the chrome is localised (without Cyrillic where it must not be), and the DATA of the tender (the title) and the links remain “as is”. The parity of the catalog keys is held by a separate test — otherwise a new key in one language would silently drop a KeyError only under this language in production.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from src.api.cabinet import auth_service as svc
from src.api.config import CabinetSettings
from src.api.emailer import MockEmailSender
from src.i18n_email import EMAIL, digest_count_phrase, email_t
from src.workers.adapters.email import (
    EmailAdapter,
    build_digest_subject,
    build_subject,
)
from tests.api.conftest import FakeSession, good_email, ok_turnstile

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _job(adapter_cls=None, **over):
    from src.workers.adapters.base import DeliveryJob

    base = dict(
        outbox_id=7,
        tender_id="abc-123",
        channel="email",
        requires_ai=False,
        user_channel_id="uc-1",
        target="buyer@example.com",
        channel_status="active",
        title="Видеонаблюдение & монтаж",  # the data — we do not translate
        source_url="https://jnportal.rs/tender/1",
        amount_eur=1234567.0,
        deadline_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc),
        source_uid="374138",
        source_key="jnportal",
        regions=["Београдски округ"],
        profile_name="Видеонаблюдение",
    )
    base.update(over)
    return DeliveryJob(**base)


def _adapter():
    return EmailAdapter(MockEmailSender(), cabinet_base_url="https://nab.rs")


# -- the catalog: parity of the keys ----------------------------------------------- #


def test_email_catalog_key_parity():
    ru_keys = set(EMAIL["ru"])
    for lang in ("sr", "en"):
        assert set(EMAIL[lang]) == ru_keys, f"a divergence of the keys ru↔{lang}"


# -- the delivery: the subject prefix is localised, the name is kept ------------- #


@pytest.mark.parametrize("lang", ["en", "sr"])
def test_build_subject_prefix_localized_title_preserved(lang):
    subject = build_subject(_job(locale=lang))
    # the prefix-chrome — in the recipient’s language, without Cyrillic
    prefix = email_t(lang, "subject_new")
    assert subject.startswith(prefix)
    assert not CYRILLIC.search(prefix)
    # the NAME of the tender (data, Cyrillic) is kept as is
    assert "Видеонаблюдение & монтаж" in subject


def test_build_subject_ru_unchanged():
    assert build_subject(_job(locale="ru")).startswith("Новый тендер:")
    # NULL locale → ru (backward compatibility)
    assert build_subject(_job(locale=None)).startswith("Новый тендер:")


@pytest.mark.parametrize("lang", ["en", "sr"])
def test_build_digest_subject_prefix_localized(lang):
    jobs = [_job(outbox_id=1, locale=lang), _job(outbox_id=2, locale=lang)]
    subject = build_digest_subject(jobs)
    assert subject.startswith(email_t(lang, "digest_prefix"))
    assert not CYRILLIC.search(subject)


def test_digest_count_phrase_plurals():
    assert digest_count_phrase(1, "en") == "1 new tender"
    assert digest_count_phrase(3, "en") == "3 new tenders"
    assert digest_count_phrase(1, "ru") == "1 новый тендер"
    assert digest_count_phrase(3, "ru") == "3 новых тендера"
    assert digest_count_phrase(1, "sr") == "1 novi tender"
    assert digest_count_phrase(3, "sr") == "3 nova tendera"


@pytest.mark.parametrize("lang", ["en", "sr"])
def test_delivery_body_chrome_localized_no_cyrillic_chrome(lang):
    msg = _adapter().format(_job(locale=lang, title="City CCTV project"))
    # greeting/intro/footer — in the recipient’s language (without Cyrillic)
    assert email_t(lang, "greeting") in msg.text
    assert email_t(lang, "intro_single") in msg.text
    assert email_t(lang, "footer_actions") in msg.text
    assert email_t(lang, "open_card") in msg.text
    # the labels of the rows of the card — localised
    assert email_t(lang, "lbl_region") in msg.text
    # the data (title, link) — as is
    assert "City CCTV project" in msg.text
    assert "https://nab.rs/tender/abc-123" in msg.text


# -- the transactional: verify/magic under the locale ------------------------------- #


def _settings():
    return CabinetSettings(CABINET_BASE_URL="https://nab.rs", CABINET_SECRET_KEY="t")


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "sr"])
async def test_signup_verify_email_localized(lang):
    sender = MockEmailSender()
    result = await svc.signup(
        FakeSession(),
        email="user@company.rs",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=good_email(),
        email_sender=sender,
        settings=_settings(),
        locale=lang,
    )
    assert result.outcome == svc.TRIAL_STARTED
    sent = sender.outbox[0]
    assert sent.subject == email_t(lang, "verify_subject")
    assert not CYRILLIC.search(sent.subject)
    # the body is localised, but the link (data) is kept
    assert email_t(lang, "greeting") in sent.text_body
    assert "/auth/verify?token=" in sent.text_body


@pytest.mark.asyncio
async def test_signup_default_locale_is_ru():
    sender = MockEmailSender()
    await svc.signup(
        FakeSession(),
        email="user@company.rs",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=good_email(),
        email_sender=sender,
        settings=_settings(),
    )
    assert sender.outbox[0].subject == "Подтвердите регистрацию — TenderSRB"
