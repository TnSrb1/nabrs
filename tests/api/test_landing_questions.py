"""Landing: the block “Šta vas zanima?” and the coverage “K izvora · M zemalja” (17.10.2026).

Real render (templates, the LANDING catalog); DB — a fake, facts and sources substituted by a fixture. The number of sources in the test — ``len(fixture)``, not a literal.
Anonymous and logged-in (signed cookie) users get the same block.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import pytest

from src.api.public import geo_cache, routes_public
from src.api.public.i18n import LANDING
from src.api.public.sources_view import SourceItem, SourcesView
from src.domain.sources import SourceInfo
from tests.api.test_public_chrome_authed import _client as _authed_client
from tests.api.test_landing import _client as _anon_client

#: Fixture of the “enabled sources” of the test DB: three countries, four sources.
FIXTURE_SOURCES = [
    SourceInfo("a", "Portal A", ("RS",), None),
    SourceInfo("b", "Portal B", ("RS", "BA"), None),
    SourceInfo("c", "Portal C", ("ME",), None),
    SourceInfo("d", "Portal D", ("MK", "RS"), None),
]
FIXTURE_COUNTRIES = {c for s in FIXTURE_SOURCES for c in s.country_codes}


def _block(html: str) -> str:
    m = re.search(
        r"<section[^>]*data-landing-questions[^>]*>(.*?)</section>", html, re.S
    )
    assert m, "the questions block is not rendered"
    return m.group(1)


def _facts(**extra) -> dict:
    base = {
        "open_total": 1234,
        "updated_at": datetime.now(timezone.utc),
        "example": None,
        "sources_count": len(FIXTURE_SOURCES),
        "countries_count": len(FIXTURE_COUNTRIES),
    }
    base.update(extra)
    return base


def _patch(monkeypatch, facts: dict) -> None:
    async def fake_facts(_db):
        return facts

    async def fake_view(_db):
        return SourcesView(
            tuple(SourceItem(s.label, s.country_codes) for s in FIXTURE_SOURCES)
        )

    monkeypatch.setattr(geo_cache, "get_landing_facts", fake_facts)
    monkeypatch.setattr(routes_public, "get_sources_view", fake_view)


# --------------------------------------------------------------------------- #
# The questions block: three cards, each — the whole link, no interactive element inside <a>
# --------------------------------------------------------------------------- #


def test_anon_landing_has_three_question_cards(monkeypatch):
    _patch(monkeypatch, _facts())
    block = _block(_anon_client().get("/?lang=sr").text)
    assert 'href="/tenderi"' in block
    assert 'href="/statistika"' in block
    assert "Ko pobeđuje" in block
    assert block.count("<a ") == 3
    assert "<button" not in block and "<input" not in block


def test_open_count_shown_once_in_facts_line_not_in_card(monkeypatch):
    # The number of open tenders — only in the facts row; card 1 without a number.
    _patch(monkeypatch, _facts(open_total=987))
    html = _anon_client().get("/?lang=sr").text
    block = _block(html)
    assert "987" not in block
    assert "Otvorenih tendera sada: 987" in html


def test_question_cards_render_without_facts(monkeypatch):
    # No facts (DB failure) — the cards stay, the caption with the number disappears.
    async def boom(_db):
        raise RuntimeError("db down")

    monkeypatch.setattr(geo_cache, "get_landing_facts", boom)
    block = _block(_anon_client().get("/?lang=sr").text)
    assert 'href="/statistika"' in block and "Ko pobeđuje" in block


@pytest.mark.parametrize(
    ("lang", "title"),
    [
        ("sr", "Tenderi po oblasti i regionu"),
        ("ru", "Тендеры по отраслям и регионам"),
        ("en", "Tenders by sector and region"),
    ],
)
def test_soon_card_is_honest_hub_link(monkeypatch, lang, title):
    # Card 2 leads to the hub /tenderi (categories and regions), not to “expiring soon”.
    _patch(monkeypatch, _facts())
    block = _block(_anon_client().get(f"/?lang={lang}").text)
    assert 'href="/tenderi"' in block
    assert title in block
    assert LANDING[lang]["q_soon_title"] == title


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_question_cards_present_in_every_locale(monkeypatch, lang):
    _patch(monkeypatch, _facts())
    html = _anon_client().get(f"/?lang={lang}").text
    block = _block(html)
    assert LANDING[lang]["q_win_title"] in block
    assert LANDING[lang]["q_open_title"] in block
    assert LANDING[lang]["q_soon_title"] in block


def test_question_cards_block_precedes_facts_block(monkeypatch):
    _patch(monkeypatch, _facts())
    html = _anon_client().get("/?lang=sr").text
    assert html.index("data-landing-questions") < html.index("data-landing-facts")


def test_authed_landing_sees_same_question_cards(monkeypatch):
    _patch(monkeypatch, _facts())
    block = _block(_authed_client(locale="sr").get("/").text)
    assert 'href="/statistika"' in block
    assert "Ko pobeđuje" in block


# --------------------------------------------------------------------------- #
# Coverage: “K izvora · M zemalja” from the DB, link to /izvori
# --------------------------------------------------------------------------- #


def test_facts_line_shows_sources_and_countries_from_fixture(monkeypatch):
    _patch(monkeypatch, _facts())
    html = _anon_client().get("/?lang=sr").text
    facts_line = html.split("data-landing-facts", 1)[1].split("</p>", 1)[0]
    # A non-breaking space between the number and the word (otherwise it breaks at 1280).
    assert f"{len(FIXTURE_SOURCES)}\u00a0izvora" in facts_line
    # The fixture — 4 countries: the Serbian form for 2–4 — “zemlje” (for 5+ — “zemalja”).
    assert f"{len(FIXTURE_COUNTRIES)}\u00a0zemlje" in facts_line
    assert 'href="/izvori"' in facts_line


def test_facts_line_has_no_source_names_list(monkeypatch):
    # The list of names (“izvori: Portal A, …”) is removed: only the counter-link remains.
    _patch(monkeypatch, _facts())
    html = _anon_client().get("/?lang=sr").text
    facts_line = html.split("data-landing-facts", 1)[1].split("</p>", 1)[0]
    assert "izvori:" not in facts_line
    assert "Portal A" not in facts_line


def test_sources_count_matches_enabled_sources_in_db(monkeypatch):
    # The counter is from the same selection as the “Izvori” tab: enabled ones and only them.
    geo_cache.clear_cache()

    async def fake_enabled(_db):
        return FIXTURE_SOURCES

    async def fake_cpv(_db):
        return {"45": {"total": 3, "open": 2, "open_sum_eur": None, "label": "x"}}

    async def fake_changed(_db):
        return None

    async def fake_example(_db):
        return None

    from src.api import repo

    monkeypatch.setattr(geo_cache, "list_enabled_sources", fake_enabled)
    monkeypatch.setattr(repo, "cpv_division_stats", fake_cpv)
    monkeypatch.setattr(repo, "latest_tender_change_at", fake_changed)
    monkeypatch.setattr(repo, "latest_open_example", fake_example)
    try:
        facts = asyncio.run(geo_cache.get_landing_facts(object()))
    finally:
        geo_cache.clear_cache()
    assert facts["sources_count"] == len(FIXTURE_SOURCES)
    assert facts["countries_count"] == len(FIXTURE_COUNTRIES)


@pytest.mark.parametrize(
    ("lang", "word"),
    [("sr", "zemalja"), ("ru", "стран"), ("en", "countries")],
)
def test_countries_word_per_locale(monkeypatch, lang, word):
    _patch(monkeypatch, _facts(countries_count=5))
    html = _anon_client().get(f"/?lang={lang}").text
    assert f"5\u00a0{word}" in html


@pytest.mark.parametrize(
    ("n", "sr", "ru", "en"),
    [
        (1, "1\u00a0izvor", "1\u00a0источник", "1\u00a0source"),
        (2, "2\u00a0izvora", "2\u00a0источника", "2\u00a0sources"),
        (4, "4\u00a0izvora", "4\u00a0источника", "4\u00a0sources"),
        (5, "5\u00a0izvora", "5\u00a0источников", "5\u00a0sources"),
        (11, "11\u00a0izvora", "11\u00a0источников", "11\u00a0sources"),
        (21, "21\u00a0izvor", "21\u00a0источник", "21\u00a0sources"),
        (22, "22\u00a0izvora", "22\u00a0источника", "22\u00a0sources"),
    ],
)
def test_sources_plural_forms(n, sr, ru, en):
    # 21 — “izvor”/“source”: the last digit is 1, except 11–14.
    # en: one form only when n == 1 ("21 sources").
    from src.api.public.routes_public import _count_label

    assert _count_label(n, "sr", "facts_sources") == sr
    assert _count_label(n, "ru", "facts_sources") == ru
    assert _count_label(n, "en", "facts_sources") == en
