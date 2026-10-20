"""The trial 30 days (owner decision 05.10) and the tails of the registration path.

Expectations — LITERALS from the fact (the render of the route), not via ``t()``/the catalog.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import auth_service as svc
from src.api.config import CabinetSettings, get_cabinet_settings
from src.api.main import app
from src.api.public.trial_text import fill_trial
from tests.api.conftest import FakeSession


def _client() -> TestClient:
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


_PAGES = ["/", "/plans", "/help", "/signup", "/legal/terms"]
_PHRASE = {"sr": "30 dana", "ru": "30 дней", "en": "30 days"}
_STALE = re.compile(r"\b14[ -](dana|дн|days?)", re.IGNORECASE)


def test_default_trial_days_is_30():
    assert CabinetSettings.model_fields["trial_days"].default == 30


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
@pytest.mark.parametrize("path", _PAGES)
def test_pages_state_30_and_no_14(path, lang):
    html = _client().get(path, params={"lang": lang}).text
    assert _PHRASE[lang] in html or "30-day" in html, (path, lang)
    assert not _STALE.search(html), (path, lang, _STALE.search(html))


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("sr", "Svaki novi nalog dobija 30 dana Pro besplatno."),
        ("ru", "Каждый новый аккаунт получает 30 дней Pro бесплатно."),
        ("en", "Every new account gets 30 days of Pro for free."),
    ],
)
def test_plans_structured_data_in_page_language(lang, expected):
    html = _client().get("/plans", params={"lang": lang}).text
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    )
    assert json.loads(blocks[0])["description"] == expected


def test_term_comes_from_settings_with_plural_forms(monkeypatch):
    monkeypatch.setattr(get_cabinet_settings(), "trial_days", 21)
    # 21: sr “21 dan”, ru (Cyrillic form), en “21 days”; 21-day — a bare number.
    assert fill_trial("{trial}", "sr") == "21 dan"
    assert fill_trial("{trial}", "ru") == "21 день"
    assert fill_trial("{trial}", "en") == "21 days"
    assert fill_trial("{trial_n}-day", "en") == "21-day"
    html = _client().get("/plans", params={"lang": "ru"}).text
    assert "Каждый новый аккаунт получает 21 день Pro бесплатно." in html


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("sr", "30 dana Pro besplatno · samo email, bez kartice"),
        ("ru", "30 дней Pro бесплатно · только email, без карты"),
        ("en", "30 days of Pro free · email only, no card"),
    ],
)
def test_signup_form_has_promise_line(lang, expected):
    html = _client().get("/signup", params={"lang": lang}).text
    assert expected in html


@pytest.mark.parametrize(
    "lang,label,expected",
    [
        ("sr", "Nije stigla poruka za potvrdu? ", "Pošalji ponovo"),
        ("ru", "Не пришло письмо подтверждения? ", "Отправить повторно"),
        ("en", "No confirmation email? ", "Send again"),
    ],
)
def test_login_links_to_resend(lang, label, expected):
    # resend sends only the CONFIRMATION e-mail — the caption must not promise a login link
    html = _client().get("/login", params={"lang": lang}).text
    assert f'{label}<a href="/auth/resend" class="underline">{expected}</a>' in html


@pytest.mark.parametrize(
    "lang,text,resend",
    [
        (
            "sr",
            "Probni period za ovaj email je već aktiviran",
            "Pošalji ponovo",
        ),
        (
            "ru",
            "Пробный период для этого email уже был активирован",
            "Отправить повторно",
        ),
    ],
)
def test_duplicate_screen_has_resend_and_no_english_trial(
    monkeypatch, lang, text, resend
):
    async def _dup(*a, **kw):
        return svc.SignupResult(outcome=svc.REJECTED_DUPLICATE, message="x")

    monkeypatch.setattr(svc, "signup", _dup)
    resp = _client().post(
        f"/signup?lang={lang}", data={"email": "u@company.rs", "consent": "yes"}
    )
    assert text in resp.text
    assert "Trial" not in resp.text
    assert f'<a href="/auth/resend" class="underline">{resend}</a>' in resp.text
