"""A logged-in client on the showcase pages sees the cabinet header (02.10.2026).

Owner complaint: “I press 'Plans' — and I fall out of the cabinet”. /plans, /help, /tenderi, / inherit public_base.html, whose header drew the showcase menu with one link “Cabinet”. Expectations — literals of href, not via ``t()``.
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
_PATHS = ["/plans", "/help", "/tenderi", "/"]


def _client(*, locale: str | None = None, authed: bool = True) -> TestClient:
    fs = FakeSession(results=[])
    sess = {"uid": _UID, "tid": _TID}
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: (
        sess if authed else None
    )
    client = TestClient(app, follow_redirects=False)
    if authed:
        secret = get_cabinet_settings().cabinet_secret_key
        client.cookies.set(
            "tb_session",
            security.sign_session(_UID, _TID, secret, locale=locale),
        )
    return client


def teardown_function():
    app.dependency_overrides.clear()


def _header(html: str) -> str:
    m = re.search(r"<header.*?</header>", html, re.S)
    assert m, "there is no <header>"
    return m.group(0)


@pytest.mark.parametrize("path", _PATHS)
def test_authed_public_header_has_cabinet_menu(path):
    r = _client(locale="ru").get(path)
    assert r.status_code == 200
    head = _header(r.text)
    for href in ('href="/app/feed"', 'href="/cabinet/account"', 'action="/logout"'):
        assert href in head, (path, href)
    assert 'href="/login"' not in head
    assert 'href="/signup"' not in head


@pytest.mark.parametrize("path", _PATHS)
def test_anon_public_header_unchanged(path):
    r = _client(authed=False).get(path)
    assert r.status_code == 200
    head = _header(r.text)
    assert 'href="/login"' in head and 'href="/signup"' in head
    for href in ('href="/app/feed"', 'href="/cabinet/account"', 'action="/logout"'):
        assert href not in head, (path, href)


@pytest.mark.parametrize("path", _PATHS)
def test_authed_header_logo_leads_to_storefront_root(path):
    """The cabinet header logo leads to `/` for a logged-in user too: the way back to the cabinet is the “Dashboard” item.

    Previously the logo led to `/app`, and from any cabinet page there was no way to the showcase.
    """
    head = _header(_client(locale="ru").get(path).text)
    assert '<a href="/" class="brand">' in head
    assert 'href="/app" class="brand"' not in head


def test_authed_public_lang_switch_is_post_anon_is_get():
    head = _header(_client(locale="ru").get("/plans").text)
    assert 'action="/cabinet/locale"' in head
    assert "?lang=" not in head
    anon = _header(_client(authed=False).get("/plans").text)
    assert 'href="?lang=ru"' in anon
    assert "/cabinet/locale" not in anon


def test_authed_public_page_follows_users_locale():
    """Without a lang cookie and without ?lang= the showcase language = users.locale from the session."""
    html = _client(locale="ru").get("/plans").text
    assert '<html lang="ru">' in html
    assert '<html lang="sr">' in _client(authed=False).get("/plans").text
    # an explicit ?lang= is stronger than the saved language
    assert '<html lang="en">' in _client(locale="ru").get("/plans?lang=en").text


def _link(head: str, href: str) -> str:
    m = re.search(r'<a href="%s"[^>]*>[^<]*</a>' % re.escape(href), head)
    assert m, f"no link {href}"
    return m.group(0)


@pytest.mark.parametrize("path", _PATHS + ["/izvori"])
def test_authed_menu_has_all_tenders_in_tools_and_no_sources(path):
    """owner 10.10: “All tenders” — in the “Tools” dropdown; Sources/Plans/
    News moved from the cabinet header to the footer. Expectation — literals of href and captions.
    """
    head = _header(_client(locale="ru").get(path).text)
    assert "Все тендеры" in _link(head, "/tenderi")
    assert "Мои совпадения" in _link(head, "/app/feed")
    tools = head.split('<details class="nav-more nav-hide-mobile">')[1].split(
        "</details>"
    )[0]
    assert "Инструменты ▾" in tools and 'href="/tenderi"' in tools
    assert 'href="/izvori"' not in head
    assert 'href="/plans"' not in head
    assert 'href="/novosti"' not in head


def test_authed_menu_active_item_on_tenderi_and_izvori():
    tenderi = _header(_client(locale="ru").get("/tenderi").text)
    assert "is-active" in _link(tenderi, "/tenderi")
    assert "is-active" not in _link(tenderi, "/app/feed")
    # the active tool highlights also the “Tools” summary
    assert re.search(r'<summary class="nav-link is-active">Инструменты', tenderi)
    izvori = _header(_client(locale="ru").get("/izvori").text)
    assert "is-active" not in _link(izvori, "/tenderi")
    assert not re.search(r'<summary class="nav-link is-active">Инструменты', izvori)


@pytest.mark.parametrize(
    "locale,all_tenders,tools,matches",
    [
        ("sr", "Svi tenderi", "Alati", "Moja poklapanja"),
        ("en", "All tenders", "Tools", "My matches"),
    ],
)
def test_authed_menu_labels_per_locale(locale, all_tenders, tools, matches):
    head = _header(_client(locale=locale).get("/plans").text)
    assert all_tenders in _link(head, "/tenderi")
    assert f">{tools} ▾</summary>" in head
    assert matches in _link(head, "/app/feed")


def test_anon_public_header_has_sources_link():
    head = _header(_client(authed=False).get("/tenderi").text)
    assert 'href="/izvori"' in head and 'href="/tenderi"' in head
    head_izvori = _header(_client(authed=False).get("/izvori").text)
    assert re.search(r'<a href="/izvori" class="[^"]*is-active', head_izvori)
