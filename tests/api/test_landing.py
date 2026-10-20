"""Landing tests / (VG-28.2): design system + AI block, multilingual.

DB and session are mocked via dependency_overrides; templates render for real.
We check: 200 on sr/ru/en, i18n parity of the new LANDING keys (including
ai_features), the presence of brand components and the absence of a bare slate CTA.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from src.api.public.i18n import LANDING, SUPPORTED
from tests.api.conftest import FakeSession


def _client() -> TestClient:
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 200 on all locales
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_landing_renders_200(lang):
    resp = _client().get("/", cookies={"lang": lang})
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Brand components of the VG-28.1 design system on the landing
# --------------------------------------------------------------------------- #


def test_landing_uses_brand_button_not_bare_slate():
    resp = _client().get("/", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert 'class="btn-primary"' in resp.text
    assert "bg-slate-900" not in resp.text


def test_landing_shows_ai_block_with_all_features():
    resp = _client().get("/", cookies={"lang": "ru"})
    assert resp.status_code == 200
    for feature in LANDING["ru"]["ai_features"]:
        assert feature["title"] in resp.text
        assert feature["text"] in resp.text


# --------------------------------------------------------------------------- #
# i18n parity: not a single new key is forgotten for one of the three locales
# --------------------------------------------------------------------------- #


def test_landing_i18n_all_locales_present():
    assert set(LANDING.keys()) == set(SUPPORTED)


def test_landing_i18n_top_level_keys_match_across_locales():
    reference = set(LANDING[SUPPORTED[0]].keys())
    for lang in SUPPORTED[1:]:
        assert set(LANDING[lang].keys()) == reference, f"key mismatch for {lang}"


def test_landing_i18n_ai_feature_count_and_keys_match_across_locales():
    reference = LANDING[SUPPORTED[0]]["ai_features"]
    for lang in SUPPORTED[1:]:
        other = LANDING[lang]["ai_features"]
        assert len(other) == len(reference), f"ai_features count mismatch for {lang}"
        for ref_item, other_item in zip(reference, other):
            assert set(other_item.keys()) == set(ref_item.keys())


# --------------------------------------------------------------------------- #
# Hero CTA “View plans” → /plans (i18n parity of the key + rendering of the link)
# --------------------------------------------------------------------------- #


def test_landing_hero_cta_plans_key_present_in_all_locales():
    for lang in SUPPORTED:
        assert LANDING[lang].get("hero_cta_plans"), f"hero_cta_plans missing for {lang}"


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_landing_hero_renders_plans_cta(lang):
    resp = _client().get("/", cookies={"lang": lang})
    assert resp.status_code == 200
    assert LANDING[lang]["hero_cta_plans"] in resp.text
    # The CTA leads to the plan showcase.
    assert 'href="/plans"' in resp.text


# The landing AI block: “a ready answer” for each match is untrue at the plan limit (16.10).
# Literals — from the render fact, not via LANDING[...]: otherwise the assertion is tautological.
@pytest.mark.parametrize(
    "lang, fresh",
    [
        ("sr", "Za poklapanja možete zatražiti gotov AI-odgovor"),
        ("ru", "По совпадениям можно запросить готовый AI-разбор"),
        ("en", "For matches you can request a finished AI review"),
    ],
)
def test_landing_ai_subtitle_no_per_match_promise(lang, fresh):
    resp = _client().get("/", cookies={"lang": lang})
    assert resp.status_code == 200
    assert fresh in resp.text
    assert "gotov odgovor, ne" not in resp.text
    assert "готовый ответ, а не" not in resp.text
    assert "Matches come with a finished answer" not in resp.text
