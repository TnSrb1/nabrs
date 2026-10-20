"""The error page 500/404: the language from the session, the “to home” link by the presence of a session.

Previously the handler unconditionally set ``cabinet_user_locale = None`` → a 500 for a logged-in Russian-speaking user always came out in Serbian, and the link led to
``/login`` (audit 07.10.2026, TECH-07). Expectations — LITERALS from the catalog, not via
``t()`` (otherwise a tautology).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import get_cabinet_settings
from src.api.main import app

_BOOM = "/__test_boom__"


@pytest.fixture
def client():
    async def boom():
        raise RuntimeError("boom")

    before = list(app.router.routes)
    app.add_api_route(_BOOM, boom, methods=["GET"])
    yield TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    app.router.routes[:] = before


def _session_cookie(locale: str | None) -> dict:
    settings = get_cabinet_settings()
    token = security.sign_session(
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        settings.cabinet_secret_key,
        locale=locale,
    )
    return {deps.SESSION_COOKIE: token}


def _get(client, path, cookies=None, headers=None):
    h = {"accept": "text/html"}
    h.update(headers or {})
    return client.get(path, cookies=cookies or {}, headers=h)


def test_500_anon_uses_storefront_language_and_login_link(client):
    r = _get(client, _BOOM)
    assert r.status_code == 500
    assert "Greška servera" in r.text
    assert 'href="/login"' in r.text


@pytest.mark.parametrize(
    ("locale", "title", "home"),
    [
        ("ru", "Ошибка сервера", "На главную"),
        ("en", "Server error", "To the home page"),
        ("sr", "Greška servera", "Na početnu"),
    ],
)
def test_500_authed_uses_session_locale_and_cabinet_link(client, locale, title, home):
    r = _get(client, _BOOM, cookies=_session_cookie(locale))
    assert r.status_code == 500
    assert title in r.text
    assert 'href="/app"' in r.text
    assert home in r.text
    assert 'href="/login"' not in r.text.split("<main", 1)[-1]


def test_404_authed_uses_session_locale_and_cabinet_link(client):
    r = _get(client, "/no-such-page-xyz", cookies=_session_cookie("ru"))
    assert r.status_code == 404
    assert 'href="/app"' in r.text
    assert "На главную" in r.text


def test_500_json_for_programmatic_clients_unchanged(client):
    r = client.get(_BOOM, headers={"accept": "application/json"})
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal Server Error"}
