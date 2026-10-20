"""Link to the directory /cpv-kodovi: the cabinet menu (desktop “Alati ▾” + phone “Meni”)
and a prominent link to /tenderi (page body, outside the header and footer).

Expectations — literals from the fact (``nav_cpv`` captions in three locales), not via ``t()``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import get_cabinet_settings
from src.api.main import app
from src.api.public import geo_cache, sources_view
from tests.api.conftest import FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_NOW = datetime.now(timezone.utc)

# locale → link caption in the cabinet menu (literals from src/api/cabinet/i18n.py)
CABINET_LABELS = {
    "sr": "CPV šifre",
    "ru": "Коды CPV",
    "en": "CPV codes",
}


@pytest.fixture(autouse=True)
def _reset_state():
    yield
    app.dependency_overrides.clear()
    sources_view.clear_cache()
    geo_cache._cpv_cache = None
    geo_cache._region_cache = None


def _cabinet_client(lang: str) -> TestClient:
    fs = FakeSession(
        results=[],
        tenant_tier="pro",
        tenant_status="active",
        access_until=_NOW + timedelta(days=12) - timedelta(hours=1),
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
    c.cookies.set("tb_session", security.sign_session(_UID, _TID, secret, locale=lang))
    return c


def _header_html(lang: str) -> str:
    r = _cabinet_client(lang).get(f"/help?lang={lang}")
    assert r.status_code == 200
    return re.search(r"<header.*?</header>", r.text, re.S).group(0)


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_cabinet_menu_links_cpv_directory_in_both_menus(lang):
    header = _header_html(lang)
    # desktop “Alati ▾” + phone menu “Meni” — one link each
    assert header.count('href="/cpv-kodovi"') == 2
    assert f">{CABINET_LABELS[lang]}</a>" in header


def test_cabinet_cpv_label_is_active_on_directory_page():
    r = _cabinet_client("sr").get("/cpv-kodovi?lang=sr")
    assert r.status_code == 200
    header = re.search(r"<header.*?</header>", r.text, re.S).group(0)
    assert re.search(r'<a href="/cpv-kodovi" class="nav-link[^"]*is-active', header)


def _hub_body(lang: str) -> str:
    # empty geo cache — the hub renders without queries (as in test_geo_landings)
    fs = FakeSession(results=[])
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: fs)
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    r = TestClient(app).get(f"/tenderi?lang={lang}")
    assert r.status_code == 200
    # page body: between the closing header and the footer
    m = re.search(r"</header>(.*?)<footer", r.text, re.S)
    assert m, "did not find the header/footer boundary"
    return m.group(1)


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_tenderi_body_links_cpv_directory(lang):
    body = _hub_body(lang)
    assert 'href="/cpv-kodovi"' in body
    assert "→" in body
