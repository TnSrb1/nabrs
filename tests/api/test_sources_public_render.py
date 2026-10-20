"""SRC-0d (owner 30.09 R7): the showcase does not promise “one portal” — the names of the sources are rendered from the ``sources`` table. Expectations — literals.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from src.api.public import routes_public, sources_view
from src.api.public.sources_view import (
    SourceItem,
    SourcesView,
    fill_sources,
    interval_phrase,
)
from tests.api.conftest import FakeSession

TWO = (SourceItem("Portal javnih nabavki", ("RS",)), SourceItem("TED", ("ME", "HR")))
# intervals: jnportal 1800 s (production fact), TED 10800 s → “30 min – 3 h”
TWO_INTERVALS = (1800, 10800)


def teardown_function():
    app.dependency_overrides.clear()
    sources_view.clear_cache()


def _client(monkeypatch, items=TWO, intervals=TWO_INTERVALS) -> TestClient:
    async def fake_view(_db):
        return SourcesView(tuple(items), intervals)

    monkeypatch.setattr(routes_public, "get_sources_view", fake_view)
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


def test_help_sr_lists_enabled_sources_with_countries(monkeypatch):
    html = _client(monkeypatch).get("/help?lang=sr").text
    assert "Pratimo izvore javnih nabavki: Portal javnih nabavki, TED." in html
    assert "Uključeni izvori:" in html
    assert "— Srbija" in html
    assert "— Crna Gora, Hrvatska" in html
    # the FAQ lists ALL sources, not the single portal
    assert (
        "proverava izvore javnih nabavki (Portal javnih nabavki, TED) na svakih 30 min – 3 h"
        in html
    )


def test_help_ru_and_en_render_sources_section(monkeypatch):
    ru = _client(monkeypatch).get("/help?lang=ru").text
    assert "Мы отслеживаем источники госзакупок: Portal javnih nabavki, TED." in ru
    assert "— Черногория, Хорватия" in ru
    en = _client(monkeypatch).get("/help?lang=en").text
    assert "We track public procurement sources: Portal javnih nabavki, TED." in en
    assert "— Montenegro, Croatia" in en


def test_help_json_ld_faq_uses_table_names(monkeypatch):
    html = _client(monkeypatch).get("/help?lang=sr").text
    assert "{sources}" not in html
    assert "Portal javnih nabavki, TED" in html.split("application/ld+json", 1)[1]


def test_help_without_sources_drops_placeholder_and_list(monkeypatch):
    html = _client(monkeypatch, items=(), intervals=()).get("/help?lang=sr").text
    assert "{sources}" not in html
    assert "Uključeni izvori:" not in html
    # No enabled platforms → a neutral frequency, without an invented number.
    assert "proverava izvore javnih nabavki redovno" in html


def test_landing_hero_names_all_enabled_sources(monkeypatch):
    html = _client(monkeypatch).get("/?lang=sr").text
    assert (
        "Proveravamo izvore javnih nabavki (Portal javnih nabavki, TED) na svakih 30 min – 3 h"
        in html
    )
    assert "{sources}" not in html


def test_fill_sources_does_not_touch_other_braces():
    cat = {"a": ["izvori: {sources}", "otvoreno: {n}"], "b": ("x ({sources})",)}
    assert fill_sources(cat, "A, B") == {
        "a": ["izvori: A, B", "otvoreno: {n}"],
        "b": ("x (A, B)",),
    }
    assert fill_sources(cat, "") == {
        "a": ["izvori: ", "otvoreno: {n}"],
        "b": ("x",),
    }


def _landing_with_facts(monkeypatch, items) -> str:
    async def fake_facts(_db):
        return {
            "open_total": 1234,
            "updated_at": None,
            "example": None,
            "sources_count": len(items),
        }

    monkeypatch.setattr(routes_public.geo_cache, "get_landing_facts", fake_facts)
    return _client(monkeypatch, items=items).get("/?lang=sr").text


def test_landing_facts_show_source_count_not_names(monkeypatch):
    # The list of names is removed (brief 17.10): in the facts row — the counter-link to /izvori.
    html = _landing_with_facts(monkeypatch, TWO)
    facts_line = html.split("data-landing-facts", 1)[1].split("</p>")[0]
    assert "2\u00a0izvora" in facts_line
    assert 'href="/izvori"' in facts_line
    assert "Portal javnih nabavki" not in facts_line and "TED" not in facts_line


def test_landing_facts_without_sources_has_no_dangling_izvori(monkeypatch):
    html = _landing_with_facts(monkeypatch, ())
    assert "data-landing-facts" in html  # the facts block is in place
    assert "izvori:" not in html.split("data-landing-facts", 1)[1].split("</p>")[0]


# --------------------------------------------------------------------------- #
# The check frequency — from sources.polling_interval_sec of the enabled sources
# --------------------------------------------------------------------------- #


def test_interval_phrase_single_value_is_not_a_range():
    assert interval_phrase([1800], "sr") == "na svakih 30 min"
    assert interval_phrase([21600], "sr") == "na svakih 6 h"


def test_interval_phrase_range_from_min_to_max():
    assert interval_phrase([1800, 10800], "sr") == "na svakih 30 min – 3 h"
    assert interval_phrase([10800, 21600, 1800], "en") == "every 30 min – 6 h"


def test_interval_phrase_ru_plurals():
    assert interval_phrase([1800], "ru") == "каждые 30 минут"
    assert interval_phrase([10800, 21600], "ru") == "каждые 3 часа – 6 часов"


def test_interval_phrase_exact_one_hour_is_not_1_h():
    # “na svakih 1 h” / “every 1 hour” / “every 1 h” — ungrammatical for exactly one hour.
    assert interval_phrase([3600], "sr") == "na svakih sat vremena"
    assert interval_phrase([3600], "ru") == "каждый час"
    assert interval_phrase([3600], "en") == "every hour"


def test_interval_phrase_without_data_is_neutral():
    assert interval_phrase([], "sr") == "redovno"
    assert interval_phrase([None, 0], "en") == "regularly"


def test_interval_phrase_sr_is_latin_and_has_no_cyrillic():
    for values in ([1800], [1800, 21600], []):
        phrase = interval_phrase(values, "sr")
        assert not any("\u0400" <= ch <= "\u04ff" for ch in phrase), phrase


def _sources_session(*pairs):
    """The fake registry of the enabled platforms: (source_key, polling_interval_sec)."""
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(
            source_key=key,
            display_name=key,
            country_codes=["RS"],
            available_for_tiers=None,
            polling_interval_sec=sec,
        )
        for key, sec in pairs
    ]
    return FakeSession(enabled_sources=rows)


def _use_sources(monkeypatch_or_none, *pairs) -> TestClient:
    sources_view.clear_cache()
    session = _sources_session(*pairs)
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: session)
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


def test_enabling_source_with_6h_interval_changes_landing_copy():
    client = _use_sources(None, ("jnportal", 1800), ("ted_balkan", 21600))
    html = client.get("/?lang=sr").text
    assert "na svakih 30 min – 6 h" in html
    assert "na svakih 30 min – 3 h" not in html
    assert "Ako je izvor privremeno nedostupan, proveravamo ređe." in html


def test_no_placeholder_leaks_on_public_pages():
    client = _use_sources(None, ("jnportal", 1800))
    for path in (
        "/?lang=sr",
        "/?lang=ru",
        "/?lang=en",
        "/help?lang=sr",
        "/plans?lang=en",
    ):
        html = client.get(path).text
        assert "{interval" not in html, path
        assert "{sources}" not in html, path
    assert "{interval" not in client.get("/llms.txt").text
