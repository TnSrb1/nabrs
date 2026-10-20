"""The daily delivery cap is visible on the site: /help, /plans, the template form, “Account” (owner 16.10).

Expectations — literals; the number is taken from the env ``DELIVERY_DAILY_CAP`` (one source).
"""

from __future__ import annotations

import pytest

from src.api.public.i18n import HELP, PLANS
from src.api.public.trial_text import fill_trial


@pytest.fixture(autouse=True)
def _cap(monkeypatch):
    monkeypatch.setenv("DELIVERY_DAILY_CAP", "30")


def test_help_telegram_section_mentions_cap_for_all_langs():
    expected = {
        "ru": "В день приходит до 30 тендеров на шаблон — в Telegram и на почту; на сайте видны все тендеры по вашему шаблону.",
        "sr": "Dnevno stiže do 30 tendera po šablonu u Telegram i na e-poštu; na sajtu su vidljivi svi tenderi po vašem šablonu.",
        "en": "Up to 30 tenders a day per template are delivered to Telegram and email; the website shows all tenders matching your template.",
    }
    for lang, line in expected.items():
        filled = fill_trial(HELP[lang], lang)
        section = next(s for s in filled["sections"] if s["id"] == "telegram")
        assert any(line in p for p in section["body"]), lang
        assert "{cap}" not in str(filled)


def test_plans_note_filled_for_all_langs():
    expected = {
        "ru": "Доставка в Telegram и на почту: до 30 тендеров в день на шаблон; на сайте видны все тендеры.",
        "sr": "Isporuka u Telegram i na e-poštu: do 30 tendera dnevno po šablonu; na sajtu su vidljivi svi tenderi.",
        "en": "Delivery to Telegram and email: up to 30 tenders a day per template; the website shows all of them.",
    }
    for lang, line in expected.items():
        assert fill_trial(PLANS[lang], lang)["delivery_cap_note"] == line


def test_cap_follows_env(monkeypatch):
    monkeypatch.setenv("DELIVERY_DAILY_CAP", "45")
    assert "до 45 тендеров" in fill_trial(PLANS["ru"], "ru")["delivery_cap_note"]


def test_profile_form_shows_cap_next_to_channels():
    """Template form: the cap line stands next to the channel selection (render)."""
    from tests.api.test_routes_profiles import SESS, _client

    resp = _client(sess=SESS).get("/profiles/new?manual=1")
    assert resp.status_code == 200
    assert "data-delivery-cap" in resp.text
    assert (
        "До 30 тендеров в день на шаблон в Telegram и на почту; на сайте — все."
        in resp.text
    )


def test_account_page_shows_cap_in_channels_section():
    from tests.api.test_cabinet_hub import _client

    text = _client().get("/cabinet/account").text
    assert (
        "Do 30 tendera dnevno po šablonu u Telegramu i na e-pošti; na sajtu — svi."
        in text
    )


def test_cap_zero_hides_site_hints(monkeypatch):
    monkeypatch.setenv("DELIVERY_DAILY_CAP", "0")
    from tests.api.test_cabinet_hub import _client

    assert "data-delivery-cap" not in _client().get("/cabinet/account").text
