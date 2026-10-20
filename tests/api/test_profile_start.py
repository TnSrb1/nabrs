"""The first screen “Create template” — “Čime se bavite?” (template_wizard_2, step 3).

Before step 3 it was impossible to get from “Create template” (/profiles/new) to the package library: straight to the questionnaire. Now first — the package tiles of the showcase (the same cards and numbers as /library) + “Sam ću podesiti” → the previous wizard.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_library
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


def teardown_function():
    app.dependency_overrides.clear()


def _client(user_locale="sr"):
    def factory():
        return FakeSession(user_locale=user_locale)

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    return TestClient(app, follow_redirects=False)


def _async(value):
    async def _stub(*args, **kwargs):
        return value

    return _stub


def test_new_without_prefill_shows_business_chooser():
    resp = _client().get("/profiles/new")
    assert resp.status_code == 200
    html = resp.text
    assert "Čime se bavite?" in html
    # A package tile — the same link as on /library.
    assert 'href="/profiles/new/confirm?from_package=gradjevina"' in html
    assert "Građevina" in html
    # The exit to the manual wizard and to the library.
    assert "Sam ću podesiti" in html
    assert 'href="/profiles/new?manual=1"' in html
    # There is no questionnaire on this screen.
    assert 'name="keywords_include"' not in html


def test_chooser_links_to_library_when_templates_exist(monkeypatch):
    tpl = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        name="Putevi",
        description=None,
        activity_kind="Građevina",
        keywords_include=[{"all_of": ["asfalt"]}],
        classifications=None,
        i18n=None,
    )
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([tpl]))
    html = _client().get("/profiles/new").text
    assert 'href="/library"' in html
    assert "Uži šabloni po delatnostima — u biblioteci" in html


def test_manual_opens_the_wizard():
    html = _client().get("/profiles/new?manual=1").text
    assert 'name="keywords_include"' in html
    assert "Čime se bavite?" not in html


def test_package_prefill_skips_chooser():
    html = _client().get("/profiles/new?from_package=gradjevina").text
    assert 'name="keywords_include"' in html
    assert "Čime se bavite?" not in html


def test_chooser_respects_profile_limit(monkeypatch):
    monkeypatch.setattr(
        routes_library.subscription_service,
        "get_tenant_tier",
        _async(("lite", "active")),
    )
    monkeypatch.setattr(routes_library.tiers, "profile_limit", lambda tier: 1)
    monkeypatch.setattr(
        routes_library.profile_service, "list_profiles", _async([object()])
    )
    html = _client().get("/profiles/new").text
    assert "/profiles/new/confirm?from_package=" not in html
    assert 'href="/profiles/new?manual=1"' not in html
    assert 'href="/cabinet/account"' in html


def test_chooser_speaks_russian_for_ru_user():
    html = _client(user_locale="ru").get("/profiles/new").text
    assert "Чем вы занимаетесь?" in html
    assert "Настрою вручную" in html
