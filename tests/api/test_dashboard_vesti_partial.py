"""The Vesti block tabs switch without reloading /app (partial /app/vesti).

Live render (route + Jinja), services substituted. Expectations — literals: the markup of
htmx attributes and the text of the ru catalog, not via ``t()``. Tržište data — the fixture
``test_dashboard_vesti_market.SHARED``.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from src.api import deps
from src.api.cabinet import market_service
from src.api.main import app
from tests.api.test_dashboard_v2_layout import _client, _stub  # noqa: F401
from tests.api.test_dashboard_vesti_market import SHARED, TODAY, _block


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    market_service.clear_cache()
    monkeypatch.setattr(market_service, "local_today", lambda now=None: TODAY)
    yield
    market_service.clear_cache()
    app.dependency_overrides.pop(deps.get_optional_session, None)


def _stub_market(monkeypatch, calls: list[str]):
    _stub(monkeypatch)

    async def get_shared(*a, **k):
        calls.append("shared")
        return SHARED

    async def get_personal(*a, **k):
        calls.append("personal")
        return {"new": 12, "expiring": 4}

    monkeypatch.setattr(market_service, "get_shared", get_shared)
    monkeypatch.setattr(market_service, "get_personal", get_personal)


def test_partial_returns_only_the_block(monkeypatch):
    """The partial is ONLY the `#vesti` section: without the page frame and header."""
    _stub_market(monkeypatch, [])
    r = _client().get("/app/vesti")
    assert r.status_code == 200
    html = r.text
    assert '<section class="card dash-vesti" id="vesti"' in html
    assert html.count('id="vesti"') == 1
    assert "<html" not in html
    assert "site-header" not in html
    assert "dash-cal" not in html


def test_partial_news_tab_is_default(monkeypatch):
    calls: list[str] = []
    _stub_market(monkeypatch, calls)
    b = _block(_client().get("/app/vesti").text)
    assert calls == []
    assert 'data-vesti-tab="news" aria-current="true"' in b
    assert (
        'data-vesti-tab="market"' in b
        and 'data-vesti-tab="market" aria-current' not in b
    )
    assert 'href="/novosti/' in b
    assert "data-vesti-market" not in b


def test_partial_market_tab_renders_market_block(monkeypatch):
    calls: list[str] = []
    _stub_market(monkeypatch, calls)
    b = _block(_client().get("/app/vesti?vesti=trziste").text)
    assert set(calls) == {"shared", "personal"}
    assert 'data-vesti-tab="market" aria-current="true"' in b
    assert 'data-vesti-tab="news" aria-current' not in b
    assert "data-vesti-market" in b
    assert "Вчера опубликовано тендеров: 23 (RS 14, BA 0, ME 0, MK 9)" in b
    assert "В ваших областях: 12 новых, истекает 4" in b


def test_tabs_carry_htmx_swap_and_keep_href_fallback(monkeypatch):
    """Both tabs: hx-get of the partial + swap into #vesti + the address in history; the href remains."""
    _stub_market(monkeypatch, [])
    b = _block(_client().get("/app/vesti").text)
    assert (
        '<a href="/app#vesti" class="ck-teaser__tab" data-vesti-tab="news" '
        'aria-current="true" autofocus hx-get="/app/vesti" hx-target="#vesti" '
        'hx-swap="outerHTML" hx-push-url="/app#vesti" hx-indicator="#vesti">'
    ) in b
    assert (
        '<a href="/app?vesti=trziste#vesti" class="ck-teaser__tab" data-vesti-tab="market" '
        'hx-get="/app/vesti?vesti=trziste" hx-target="#vesti" hx-swap="outerHTML" '
        'hx-push-url="/app?vesti=trziste#vesti" hx-indicator="#vesti">'
    ) in b
    assert " onclick=" not in b


def test_swap_response_focuses_active_tab(monkeypatch):
    """Focus: in the response to a switch, autofocus goes to the ACTIVE tab and only to it."""
    _stub_market(monkeypatch, [])
    b = _block(_client().get("/app/vesti?vesti=trziste").text)
    assert 'data-vesti-tab="market" aria-current="true" autofocus hx-get' in b
    assert b.count("autofocus") == 1


def test_full_page_does_not_autofocus_tab(monkeypatch):
    """On the full /app autofocus is NOT set: otherwise the page load would jump to the block."""
    _stub_market(monkeypatch, [])
    assert "autofocus" not in _block(_client().get("/app?vesti=trziste").text)
    assert "autofocus" not in _block(_client().get("/app").text)


def test_anonymous_gets_303_like_app(monkeypatch):
    _stub_market(monkeypatch, [])
    c = _client()  # sets the SESS-override — set the anonymous user AFTER it
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    partial = c.get("/app/vesti?vesti=trziste")
    full = c.get("/app?vesti=trziste")
    assert partial.status_code == 303
    assert full.status_code == 303
    assert partial.headers["location"] == full.headers["location"]
    assert partial.headers["location"] == "/login?next=%2Fapp%3Fvesti%3Dtrziste"
    assert c.get("/app/vesti").headers["location"] == "/login?next=%2Fapp"


def test_anonymous_htmx_gets_hx_redirect_not_login_html(monkeypatch):
    """htmx would follow the 303 and insert the login page into the block — we send HX-Redirect."""
    _stub_market(monkeypatch, [])
    c = _client()  # sets the SESS-override — set the anonymous user AFTER it
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    r = c.get("/app/vesti?vesti=trziste", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert r.headers["hx-redirect"] == "/login?next=%2Fapp%3Fvesti%3Dtrziste"
    assert "dash-vesti" not in r.text


# --- click modifiers: a new tab, not a block substitution -------------------

_ROOT = Path(__file__).resolve().parents[2]


def test_vesti_tabs_script_is_loaded_with_matching_sri():
    """The script catches Ctrl/⌘/Shift-click; SRI in base.html — the hash of the FILE (otherwise the browser discards it)."""
    js = (_ROOT / "src/api/static/js/vesti_tabs.js").read_bytes()
    digest = base64.b64encode(hashlib.sha384(js).digest()).decode()
    base_html = (_ROOT / "src/api/templates/base.html").read_text(encoding="utf-8")
    assert (
        f'<script src="{{{{ static_url(\'js/vesti_tabs.js\') }}}}" integrity="sha384-{digest}"'
        in base_html
    )


def test_vesti_tabs_script_stops_modifier_clicks_in_capture_phase():
    """A capture on the document + stopImmediatePropagation: the htmx handler on the link does not fire."""
    js = (_ROOT / "src/api/static/js/vesti_tabs.js").read_text(encoding="utf-8")
    assert "addEventListener('click'" in js and "}, true);" in js
    assert "e.ctrlKey || e.metaKey || e.shiftKey" in js
    assert "e.stopImmediatePropagation()" in js
    assert "[data-vesti-tab]" in js
