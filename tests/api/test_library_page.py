"""Tests of the render of the template library /library (web-ux-5 — presentation polish).

We check: anonymous → /login; a filled library renders cards, grouping by sector of activity, keyword/CPV tags, the “Use” CTA on the design system (btn-primary, without a raw bg-slate-900); empty → empty state.
The service logic is not touched — we substitute svc.list_public_templates with a stub.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_library
from src.api.main import app

SESS = {"uid": "u-1", "tid": "t-1"}


def _client(*, sess=None):
    def factory():
        from tests.api.conftest import FakeSession

        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _tpl(**kw):
    base = dict(
        id="11111111-1111-1111-1111-111111111111",
        name="Строительство дорог",
        description="Асфальт, ремонт, инфраструктура",
        activity_kind="Строительство",
        keywords_include=[{"all_of": ["асфальт", "дорога"]}],
        classifications={"include": [{"system_code": "CPV", "code": "45233"}]},
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #


def test_library_anonymous_redirects_to_login():
    resp = _client(sess=None).get("/library")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# --------------------------------------------------------------------------- #
# Filled library
# --------------------------------------------------------------------------- #


def test_library_renders_cards_and_group(monkeypatch):
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([_tpl()]))
    resp = _client(sess=SESS).get("/library")
    assert resp.status_code == 200
    # grouping by sector of activity + card
    assert "tpl-group" in resp.text
    assert "Строительство" in resp.text
    assert "tpl-card" in resp.text
    assert "Строительство дорог" in resp.text
    assert "Асфальт, ремонт, инфраструктура" in resp.text


def test_library_renders_keyword_tags_without_raw_cpv_codes(monkeypatch):
    """Tags are SEARCH words; the raw CPV codes are removed from the card on purpose.

    “eu_cpv:45233000” tells the subscriber nothing and took half the card; what exactly gets substituted (how many words and codes) they see in the form — in the banner “Assembled from the template …”, where it can be corrected.
    """
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([_tpl()]))
    resp = _client(sess=SESS).get("/library")
    assert "tpl-tag" in resp.text
    assert "асфальт" in resp.text
    assert "CPV:45233" not in resp.text
    assert "tpl-tag--cpv" not in resp.text


def test_library_cta_uses_design_system(monkeypatch):
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([_tpl()]))
    resp = _client(sess=SESS).get("/library")
    assert "btn-primary" in resp.text
    assert (
        "/profiles/new/confirm?from_template=11111111-1111-1111-1111-111111111111"
        in resp.text
    )
    # The CTAs are moved to the design system — without the raw dark Tailwind background
    assert "bg-slate-900" not in resp.text


def test_library_caps_keyword_tags_with_overflow(monkeypatch):
    many = _tpl(
        keywords_include=[{"all_of": [f"kw{i}" for i in range(9)]}],
        classifications=None,
    )
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([many]))
    resp = _client(sess=SESS).get("/library")
    # we show 6 tags + the “+3” overflow
    assert "tpl-tag--more" in resp.text
    assert "+3" in resp.text


# --------------------------------------------------------------------------- #
# Empty library → empty state
# --------------------------------------------------------------------------- #


def test_library_empty_shows_empty_state_cta(monkeypatch):
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([]))
    resp = _client(sess=SESS).get("/library")
    assert resp.status_code == 200
    assert "empty-state" in resp.text
    assert "Библиотека шаблонов пока пуста" in resp.text
    assert 'href="/profiles/new"' in resp.text
    assert "Создайте первый шаблон поиска" in resp.text


# --------------------------------------------------------------------------- #
# VG-14: showcase of vertical packages
# --------------------------------------------------------------------------- #


def test_library_renders_vertical_packages(monkeypatch):
    # Packages — data-as-code, shown even with an empty template DB.
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([]))
    resp = _client(sess=SESS).get("/library")
    assert resp.status_code == 200
    # One showcase by sectors: the package is the first card of its sector.
    assert "Пакеты по вертикалям" not in resp.text
    assert "Вся отрасль" in resp.text
    assert "Строительство" in resp.text
    # The CTA “Apply package” leads to the profile-creation flow from the package
    assert "/profiles/new/confirm?from_package=gradjevina" in resp.text
    assert "Применить пакет" in resp.text
    # Raw CPV codes are removed from the cards (see the test on tags above)
    assert "eu_cpv:45000000" not in resp.text
    assert "btn-primary" in resp.text
