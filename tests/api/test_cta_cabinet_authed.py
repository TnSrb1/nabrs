"""CTA of statistics and of the tender card for a logged-in user: “Moj kabinet → /app” (17.10.2026).

The same principle as on the main `/` (`test_landing_authed_cta.py`): for a logged-in user the sign-up button makes no sense. A logged-in user is identified by a SIGNED cookie (the real path `_auth_context`). A guest — without a cookie. Expectations — literals, not `t()`.
`plans.html` is NOT checked here: there the logged-in user has a different meaning; it is outside the brief.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import get_cabinet_settings
from src.api.main import app
from src.api.public import stats_page
from tests.api.conftest import FakeResult, FakeSession
from tests.api.test_newcomer_ux import _tender_row
from tests.api.test_statistika_page import STATS

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_TENDER = "/tender/11111111-1111-4111-8111-111111111111"

# (locale, cabinet caption, sign-up caption on statistics, on the tender card)
LABELS = {
    "sr": ("Moj kabinet", "Registrujte se", "Počnite besplatno"),
    "ru": ("Мой кабинет", "Зарегистрироваться", "Начать бесплатно"),
    "en": ("My cabinet", "Sign up", "Start for free"),
}


def teardown_function():
    app.dependency_overrides.clear()
    stats_page.clear_cache()


def _client(*, authed: bool, locale: str, session_factory) -> TestClient:
    app.dependency_overrides[deps.get_session_factory] = lambda: session_factory
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    client = TestClient(app, follow_redirects=False)
    if authed:
        secret = get_cabinet_settings().cabinet_secret_key
        client.cookies.set(
            "tb_session",
            security.sign_session(_UID, _TID, secret, locale=locale),
        )
    return client


@pytest.mark.parametrize("locale", ["sr", "ru", "en"])
def test_statistika_authed_shows_cabinet_cta(monkeypatch, locale):
    async def fake_stats(_factory):
        return STATS

    monkeypatch.setattr(stats_page, "get_public_stats", fake_stats)
    cabinet, signup, _ = LABELS[locale]
    client = _client(authed=True, locale=locale, session_factory=FakeSession)
    html = client.get("/statistika").text
    assert f'<a href="/app" class="btn-primary">{cabinet}</a>' in html
    assert 'href="/signup"' not in html


@pytest.mark.parametrize("locale", ["sr", "ru", "en"])
def test_statistika_guest_keeps_signup_cta(monkeypatch, locale):
    async def fake_stats(_factory):
        return STATS

    monkeypatch.setattr(stats_page, "get_public_stats", fake_stats)
    cabinet, signup, _ = LABELS[locale]
    client = _client(authed=False, locale=locale, session_factory=FakeSession)
    html = client.get(f"/statistika?lang={locale}").text
    assert f'<a href="/signup" class="btn-primary">{signup}</a>' in html
    assert cabinet not in html


def _tender_session():
    row = _tender_row()
    return lambda: FakeSession(results=[FakeResult(items=[row])])


@pytest.mark.parametrize("locale", ["sr", "ru", "en"])
def test_tender_authed_shows_cabinet_cta(locale):
    cabinet, _, _ = LABELS[locale]
    client = _client(authed=True, locale=locale, session_factory=_tender_session())
    html = client.get(_TENDER).text
    assert f'<a href="/app" class="btn-primary">{cabinet}</a>' in html
    assert 'href="/signup" class="btn-primary"' not in html


@pytest.mark.parametrize("locale", ["sr", "ru", "en"])
def test_tender_guest_keeps_signup_cta(locale):
    _, _, signup = LABELS[locale]
    client = _client(authed=False, locale=locale, session_factory=_tender_session())
    html = client.get(f"{_TENDER}?lang={locale}").text
    assert f'<a href="/signup" class="btn-primary">{signup}</a>' in html
