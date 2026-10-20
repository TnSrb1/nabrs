"""The link to the project’s Telegram channel: the showcase footer, “Help”, the cabinet hub (14.10.2026).

Expectations — LITERALS from the fact (the channel address, the captions of the three locales), not via ``contact_ui``/``CONTACT``: otherwise the test is tautological. The render — the real path through the TestClient; the cabinet hub — under a signed cookie (as in test_cabinet_hub).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"

_HREF = 'href="https://t.me/nabrs_news"'
_TEXT = {
    "sr": "Telegram kanal: novosti i beta test",
    "ru": "Telegram-канал: новости и бета-тест",
    "en": "Telegram channel: news and beta test",
}


def teardown_function():
    app.dependency_overrides.clear()


def _anon_client():
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: FakeSession())
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


def _hub_client(locale):
    fs = FakeSession(
        results=[],
        tenant_tier="pro",
        tenant_status="active",
        access_until=None,
        user_locale=locale,
        answers_tenant_tier=True,
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": _UID,
        "tid": _TID,
    }
    c = TestClient(app, follow_redirects=False)
    secret = get_cabinet_settings().cabinet_secret_key
    c.cookies.set(
        "tb_session", security.sign_session(_UID, _TID, secret, locale=locale)
    )
    return c


def _anchor(html: str) -> str:
    """The opening <a> tag with the channel address — to check target/rel."""
    m = re.search(r"<a [^>]*" + re.escape(_HREF) + r"[^>]*>", html)
    assert m, "the link to the channel not found"
    return m.group(0)


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
@pytest.mark.parametrize("path", ["/", "/help"])
def test_public_pages_show_channel_link(path, lang):
    resp = _anon_client().get(path, cookies={"lang": lang})
    assert resp.status_code == 200
    assert _TEXT[lang] in resp.text
    tag = _anchor(resp.text)
    assert 'target="_blank"' in tag
    assert 'rel="noopener"' in tag


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_cabinet_hub_shows_channel_link(lang):
    resp = _hub_client(lang).get("/cabinet/account")
    assert resp.status_code == 200
    assert _TEXT[lang] in resp.text
    assert _HREF in resp.text


def test_channel_link_is_not_inline_handler():
    """CSP script-src 'self': the link is an ordinary <a>, without onclick/inline scripts."""
    html = _anon_client().get("/help", cookies={"lang": "ru"}).text
    assert "onclick" not in _anchor(html)
