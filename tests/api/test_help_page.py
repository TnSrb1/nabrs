"""Tests of the help center /help and multilingual keys (VG-30a).

We check: 200 on sr/ru/en, the presence of i18n keys in all three locales
(structural parity of the HELP dictionary — there must be no "forgotten" translation),
and that the empty state with the list of profiles/template library shows
an explanation + an explicit CTA instead of an emptiness.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_library, routes_profiles
from src.api.main import app
from src.api.public.i18n import HELP, SUPPORTED, UI

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


# --------------------------------------------------------------------------- #
# GET /help — render in three locales
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "lang,expected_title",
    [
        ("sr", "Pomoć"),
        ("ru", "Помощь"),
        ("en", "Help"),
    ],
)
def test_help_page_renders_per_locale(lang, expected_title):
    client = _client()
    resp = client.get("/help", cookies={"lang": lang})
    assert resp.status_code == 200
    assert expected_title in resp.text


def test_help_page_default_locale_is_sr():
    client = _client()
    resp = client.get("/help")
    assert resp.status_code == 200
    assert "Pomoć" in resp.text


def test_help_page_lists_all_sections():
    client = _client()
    resp = client.get("/help", cookies={"lang": "ru"})
    assert resp.status_code == 200
    for anchor in (
        "#start",
        "#ai",
        "#telegram",
        "#plans",
        "#card",
        "#bidnobid",
        "#trial",
        "#glossary",
        "#faq",
    ):
        assert anchor in resp.text


def test_help_page_links_to_plans():
    client = _client()
    resp = client.get("/help", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert 'href="/plans"' in resp.text


def test_help_page_quick_start_steps_rendered():
    resp = _client().get("/help", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "help-step" in resp.text
    assert "Создайте шаблон поиска" in resp.text
    assert "Подключите Telegram" in resp.text
    assert "Получите первый алерт" in resp.text


def test_help_page_ai_features_with_tier_badges():
    resp = _client().get("/help", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "help-feature" in resp.text
    # real AI features (none invented) + the plan badge
    assert "Глубокий разбор ТЗ" in resp.text
    assert "Чек-лист документов" in resp.text
    assert "Черновик заявки" in resp.text
    assert "Pro+" in resp.text
    # The feature’s plan is METADATA, not “time still left”/score: blue is pinned
    # to these two roles (the color-role canon), the labels live in .badge-muted.
    assert "badge badge-muted" in resp.text


def test_help_page_opens_bot_and_uses_design_system_cta():
    resp = _client().get("/help", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert 'href="https://t.me/nabrs_bot"' in resp.text
    # The CTAs are moved to the design system (btn-primary), without a raw bg-slate-900
    assert "btn-primary" in resp.text
    assert "bg-slate-900" not in resp.text


def test_help_page_faq_accordion_rendered():
    resp = _client().get("/help", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "faq-item" in resp.text
    assert "<details" in resp.text


def test_help_page_tender_card_guide_rendered():
    resp = _client().get("/help", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "Как читать карточку тендера" in resp.text
    assert "Дедлайн подачи" in resp.text
    assert "CPV и предмет" in resp.text


def test_help_page_bid_no_bid_guide_rendered():
    resp = _client().get("/help", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "стоит ли участвовать" in resp.text
    assert "No-bid" in resp.text


def test_help_page_trial_section_replaces_free_vs_pro():
    """There is no public Free (owner decision 30.09): a section about the trial, without a Free/Pro comparison."""
    resp = _client().get("/help", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "Пробный период 30 дней" in resp.text
    assert "Free" not in resp.text
    assert "Pro+:" not in resp.text


def test_help_page_glossary_rendered():
    resp = _client().get("/help", cookies={"lang": "ru"})
    assert resp.status_code == 200
    # facts only from REGISTRY.md — the portal and its domain are not invented
    assert "Portal javnih nabavki" in resp.text
    assert "jnportal.src-ujn.example.invalid" in resp.text
    assert "Okvirni sporazum" in resp.text
    assert "Извещения о закупке и о заключении договора" in resp.text


def test_help_linked_from_landing():
    landing = _client().get("/", cookies={"lang": "ru"})
    assert 'href="/help"' in landing.text


def test_help_linked_from_cabinet_dashboard(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )
    dashboard = _client(sess=SESS).get("/app")
    assert 'href="/help"' in dashboard.text


# --------------------------------------------------------------------------- #
# i18n parity: not a single new key is forgotten for one of the three locales
# --------------------------------------------------------------------------- #


def test_help_i18n_all_locales_present():
    assert set(HELP.keys()) == set(SUPPORTED)


def test_help_i18n_section_ids_match_across_locales():
    ids_by_lang = {
        lang: [s["id"] for s in HELP[lang]["sections"]] for lang in SUPPORTED
    }
    reference = ids_by_lang[SUPPORTED[0]]
    for lang in SUPPORTED[1:]:
        assert ids_by_lang[lang] == reference, f"section ids mismatch for {lang}"


def test_help_i18n_section_keys_match_across_locales():
    reference_sections = HELP[SUPPORTED[0]]["sections"]
    for idx, ref_section in enumerate(reference_sections):
        for lang in SUPPORTED[1:]:
            other_section = HELP[lang]["sections"][idx]
            assert set(other_section.keys()) == set(
                ref_section.keys()
            ), f"keys mismatch in section '{ref_section['id']}' for {lang}"


def test_help_i18n_faq_item_count_matches_across_locales():
    counts = {
        lang: len(
            next(s for s in HELP[lang]["sections"] if s["id"] == "faq")["faq_items"]
        )
        for lang in SUPPORTED
    }
    assert len(set(counts.values())) == 1, counts


def test_help_i18n_command_count_matches_across_locales():
    counts = {
        lang: len(
            next(s for s in HELP[lang]["sections"] if s["id"] == "telegram")["commands"]
        )
        for lang in SUPPORTED
    }
    assert len(set(counts.values())) == 1, counts


def test_help_i18n_step_count_matches_across_locales():
    counts = {
        lang: len(
            next(s for s in HELP[lang]["sections"] if s["id"] == "start")["steps"]
        )
        for lang in SUPPORTED
    }
    assert len(set(counts.values())) == 1, counts


def test_help_i18n_feature_count_matches_across_locales():
    counts = {
        lang: len(
            next(s for s in HELP[lang]["sections"] if s["id"] == "ai")["features"]
        )
        for lang in SUPPORTED
    }
    assert len(set(counts.values())) == 1, counts


def test_help_i18n_feature_keys_match_across_locales():
    reference = [
        set(f.keys())
        for f in next(s for s in HELP[SUPPORTED[0]]["sections"] if s["id"] == "ai")[
            "features"
        ]
    ]
    for lang in SUPPORTED[1:]:
        feats = next(s for s in HELP[lang]["sections"] if s["id"] == "ai")["features"]
        assert [set(f.keys()) for f in feats] == reference, f"feature keys {lang}"


def _section(lang, sid):
    return next(s for s in HELP[lang]["sections"] if s["id"] == sid)


@pytest.mark.parametrize("sid", ["card", "bidnobid"])
def test_help_i18n_block_count_matches_across_locales(sid):
    counts = {lang: len(_section(lang, sid)["blocks"]) for lang in SUPPORTED}
    assert len(set(counts.values())) == 1, counts


@pytest.mark.parametrize("sid", ["card", "bidnobid"])
def test_help_i18n_block_keys_match_across_locales(sid):
    reference = [set(b.keys()) for b in _section(SUPPORTED[0], sid)["blocks"]]
    for lang in SUPPORTED[1:]:
        blocks = _section(lang, sid)["blocks"]
        assert [set(b.keys()) for b in blocks] == reference, f"block keys {lang}"


def test_help_i18n_glossary_count_matches_across_locales():
    counts = {lang: len(_section(lang, "glossary")["glossary"]) for lang in SUPPORTED}
    assert len(set(counts.values())) == 1, counts


def test_help_i18n_glossary_keys_are_term_and_desc():
    for lang in SUPPORTED:
        for g in _section(lang, "glossary")["glossary"]:
            assert set(g.keys()) == {"term", "desc"}


def test_ui_nav_help_present_in_all_locales():
    for lang in SUPPORTED:
        assert "nav_help" in UI[lang]
        assert UI[lang]["nav_help"]


# --------------------------------------------------------------------------- #
# Empty states: an explanation + an explicit CTA instead of an emptiness
# --------------------------------------------------------------------------- #


def test_dashboard_empty_profiles_shows_cta(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )
    resp = _client(sess=SESS).get("/app")
    assert resp.status_code == 200
    assert "Шаблонов пока нет" in resp.text
    # The first step of an empty dashboard is a CHOICE on the showcase (there are live numbers there) or
    # an honest entry “show all”; manual setup is the button in the section header.
    assert 'href="/library"' in resp.text
    assert "Выберите готовый шаблон" in resp.text
    assert 'href="/profiles/new?preset=all"' in resp.text


def test_library_empty_shows_cta(monkeypatch):
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([]))
    resp = _client(sess=SESS).get("/library")
    assert resp.status_code == 200
    assert "Библиотека шаблонов пока пуста" in resp.text
    assert 'href="/profiles/new"' in resp.text
