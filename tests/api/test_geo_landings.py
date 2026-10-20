"""Tests of the ``/tenderi`` landing pages (GEO-2, TASK geo_landings, step 6).

The DB is mocked via dependency_overrides; templates render for real. Aggregates
(:mod:`src.api.public.geo_cache`) are primed DIRECTLY via ``_cpv_cache``/
``_region_cache`` (bypassing SQL) — by default the ``conftest`` gives them empty
(``_empty_geo_cache``, autouse), so unrelated tests in the file do not break the
``FakeResult`` queue. The “not a thin content” threshold — ``MIN_TENDERS_FOR_PAGE=5``.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps, repo
from src.api.main import app
from src.api.public import geo_cache, sources_view
from tests.api.conftest import FakeResult, FakeSession


def _client(session: FakeSession):
    def factory():
        return session

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()
    sources_view.clear_cache()


def _prime_sources() -> None:
    # The frequency from the sources table is a separate cache: we warm it up so that the number of queries
    # per render does not depend on who filled the cache first (the test is about rows).
    sources_view._cache = (
        time.monotonic() + 3600,
        sources_view.SourcesView((), (1800,)),
    )


def _prime_cpv(stats: dict) -> None:
    geo_cache._cpv_cache = (time.monotonic() + 3600, stats)


def _prime_region(stats: dict) -> None:
    geo_cache._region_cache = (time.monotonic() + 3600, stats)


def _row(**kw):
    base = dict(
        id="t-1",
        title="Радови на реконструкцији пута",
        customer_name="ЈП Путеви Србије",
        deadline_at=datetime.now(timezone.utc) + timedelta(days=10),
        amount_eur=50000.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# 200 for a section >= the threshold, with links to the cards
# --------------------------------------------------------------------------- #


def test_cpv_section_200_lists_open_tenders():
    _prime_cpv(
        {
            "45": {
                "total": 10,
                "open": 2,
                "open_sum_eur": 75000.0,
                "label": "Gradđevinski radovi",
            }
        }
    )
    session = FakeSession(
        results=[
            FakeResult(items=[_row(id="t-1"), _row(id="t-2")]),  # open
            FakeResult(items=[_row(id="t-3")]),  # closed (page 1)
        ]
    )
    client = _client(session)
    resp = client.get("/tenderi/cpv/45")
    assert resp.status_code == 200
    assert 'href="/tender/t-1"' in resp.text
    assert 'href="/tender/t-2"' in resp.text
    assert 'href="/tender/t-3"' in resp.text
    assert "Gradđevinski radovi" in resp.text


def test_region_section_200_lists_open_tenders():
    _prime_region({"RS-00": {"total": 20, "open": 1, "open_sum_eur": None}})
    session = FakeSession(
        results=[
            FakeResult(items=[_row(id="t-9")]),
            FakeResult(items=[]),
        ]
    )
    client = _client(session)
    resp = client.get("/tenderi/region/RS-00")
    assert resp.status_code == 200
    assert 'href="/tender/t-9"' in resp.text


# --------------------------------------------------------------------------- #
# 404 — a section below the threshold and a section that does not exist at all (junk)
# --------------------------------------------------------------------------- #


def test_cpv_section_404_below_threshold():
    """A section exists in the aggregates, but total < MIN_TENDERS_FOR_PAGE — 404,
    without a single listing query (mutation: remove the threshold check in routes_geo,
    and this body must start returning 200)."""
    _prime_cpv({"07": {"total": 3, "open": 1, "open_sum_eur": None, "label": "x"}})
    session = FakeSession(results=[])
    client = _client(session)
    resp = client.get("/tenderi/cpv/07")
    assert resp.status_code == 404
    assert session.executed == []


def test_cpv_section_404_garbage_division():
    _prime_cpv({"45": {"total": 10, "open": 2, "open_sum_eur": None, "label": "x"}})
    session = FakeSession(results=[])
    client = _client(session)
    resp = client.get("/tenderi/cpv/xx")
    assert resp.status_code == 404


def test_region_section_404_unknown_code():
    """A code not from OKRUG_CODES is cut off before the aggregates are read (a junk code)."""
    _prime_region({})
    session = FakeSession(results=[])
    client = _client(session)
    resp = client.get("/tenderi/region/ZZ-99")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# PLAN elements (the planned period) do not yield a section — the SQL predicate (text
# guard; mutation: remove the condition ``scheme = 'CPV'`` in repo._CPV_STATS_SQL —
# the test goes red).
# --------------------------------------------------------------------------- #


def test_cpv_stats_sql_excludes_plan_entries():
    assert "scheme' = 'CPV'" in repo._CPV_STATS_SQL.text
    assert "scheme' = 'CPV'" in repo._CPV_LABEL_SQL.text


# --------------------------------------------------------------------------- #
# JSON-LD parses, there are no AI-analysis fields
# --------------------------------------------------------------------------- #


def test_cpv_section_json_ld_has_no_ai_fields():
    _prime_cpv(
        {"45": {"total": 10, "open": 1, "open_sum_eur": None, "label": "Radovi"}}
    )
    session = FakeSession(
        results=[FakeResult(items=[_row(id="t-1")]), FakeResult(items=[])]
    )
    client = _client(session)
    resp = client.get("/tenderi/cpv/45")
    assert resp.status_code == 200
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', resp.text, re.S
    )
    assert len(blocks) >= 2, "a minimum of CollectionPage + BreadcrumbList was expected"
    types_seen = set()
    for raw in blocks:
        data = json.loads(raw)
        types_seen.add(data["@type"])
        dumped = json.dumps(data)
        assert "analysis" not in dumped
        assert "match_score" not in dumped
    assert "CollectionPage" in types_seen
    assert "BreadcrumbList" in types_seen


def test_hub_json_ld_parses():
    session = FakeSession(results=[])
    client = _client(session)
    resp = client.get("/tenderi")
    assert resp.status_code == 200
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', resp.text, re.S
    )
    assert blocks
    for raw in blocks:
        json.loads(raw)  # must not raise


_SR_CPV_03 = "Пољопривредни производи, производи пољопривредних газдинстава, рибарства, шумарства и сродни производи"
_EN_CPV_03 = "Agricultural, farming, fishing, forestry and related products"


def _hub_text(lang: str) -> str:
    # The label in the cache — from the tender data (Serbian Cyrillic), as in production.
    _prime_cpv(
        {"03": {"total": 99, "open": 5, "open_sum_eur": None, "label": _SR_CPV_03}}
    )
    _prime_region({})
    resp = _client(FakeSession(results=[])).get(f"/tenderi?lang={lang}")
    assert resp.status_code == 200
    return resp.text


def test_hub_cpv_label_english_uses_catalog_en_field():
    """``?lang=en`` → the section name from ``cpv_codes.json`` (field ``l``), not the Serbian one.

    Mutation: remove ``_localize_cpv`` in ``geo_hub`` → the test goes red.
    """
    text = _hub_text("en")
    assert _EN_CPV_03 in text
    assert _SR_CPV_03 not in text


def test_hub_cpv_label_serbian_is_latin():
    """sr: the section name from the data, but in Latin (owner 10.10.2026)."""
    text = _hub_text("sr")
    assert (
        "Poljoprivredni proizvodi, proizvodi poljoprivrednih gazdinstava, "
        "ribarstva, šumarstva i srodni proizvodi"
    ) in text
    assert _SR_CPV_03 not in text


def test_localize_cpv_falls_back_to_data_label_and_keeps_cache():
    from src.api.public.routes_geo import _localize_cpv

    stats = {"99": {"total": 1, "label": "Непознато"}}
    out = _localize_cpv(stats, "en")
    assert out["99"]["label"] == "Непознато"  # not in the directory → an explicit fallback
    live = {"03": {"total": 1, "label": _SR_CPV_03}}
    assert _localize_cpv(live, "en")["03"]["label"] == _EN_CPV_03
    assert live["03"]["label"] == _SR_CPV_03  # cache not mutated


def test_cpv_section_title_english():
    _prime_cpv(
        {"03": {"total": 99, "open": 1, "open_sum_eur": None, "label": _SR_CPV_03}}
    )
    session = FakeSession(results=[FakeResult(items=[_row()]), FakeResult(items=[])])
    resp = _client(session).get("/tenderi/cpv/03?lang=en")
    assert resp.status_code == 200
    assert _EN_CPV_03 in resp.text
    assert _SR_CPV_03 not in resp.text


# --------------------------------------------------------------------------- #
# sitemap contains /tenderi
# --------------------------------------------------------------------------- #


def test_sitemap_pages_contains_tenderi_hub_and_sections():
    _prime_cpv({"45": {"total": 10, "open": 1, "open_sum_eur": None, "label": "x"}})
    _prime_region({"RS-00": {"total": 20, "open": 1, "open_sum_eur": None}})
    session = FakeSession(results=[])
    client = _client(session)
    resp = client.get("/sitemap-pages.xml")
    assert resp.status_code == 200
    assert "<loc>https://nab.rs/tenderi</loc>" in resp.text
    assert "<loc>https://nab.rs/tenderi/cpv/45</loc>" in resp.text
    assert "<loc>https://nab.rs/tenderi/region/RS-00</loc>" in resp.text
    # a thin section below the threshold is not listed in the aggregates at all — the sitemap
    # will not see it (the aggregates are already filtered by _cpv_sections/_region_sections).


def test_sitemap_pages_empty_geo_has_no_tenderi_sections():
    """Empty aggregates (the autouse fixture default) — the hub exists, there are no sections."""
    session = FakeSession(results=[])
    client = _client(session)
    resp = client.get("/sitemap-pages.xml")
    assert resp.status_code == 200
    assert "<loc>https://nab.rs/tenderi</loc>" in resp.text
    assert "/tenderi/cpv/" not in resp.text
    assert "/tenderi/region/" not in resp.text


# --------------------------------------------------------------------------- #
# No N+1: the number of queries per render does not grow with the number of rows
# --------------------------------------------------------------------------- #


def test_cpv_section_query_count_is_fixed_regardless_of_row_count():
    _prime_sources()
    _prime_cpv({"45": {"total": 60, "open": 60, "open_sum_eur": None, "label": "x"}})

    session_few = FakeSession(
        results=[FakeResult(items=[_row(id="t-1")]), FakeResult(items=[])]
    )
    client_few = _client(session_few)
    resp_few = client_few.get("/tenderi/cpv/45")
    assert resp_few.status_code == 200
    n_few = len(session_few.executed)
    app.dependency_overrides.clear()

    many_open = [_row(id=f"t-{i}") for i in range(50)]
    many_closed = [_row(id=f"c-{i}") for i in range(20)]
    session_many = FakeSession(
        results=[FakeResult(items=many_open), FakeResult(items=many_closed)]
    )
    client_many = _client(session_many)
    resp_many = client_many.get("/tenderi/cpv/45")
    assert resp_many.status_code == 200
    n_many = len(session_many.executed)

    assert n_few == n_many == 2, (
        "the render of a section must make a FIXED number of queries "
        f"(open+closed listing) regardless of the number of rows: few={n_few} many={n_many}"
    )


# --------------------------------------------------------------------------- #
# The deadline “today” in the row: “Ističe danas”, not “Još 0 dana” (production fact 30.09)
# --------------------------------------------------------------------------- #


def _cpv_page_with_deadline(deadline_at, lang="sr"):
    _prime_cpv(
        {
            "45": {
                "total": 10,
                "open": 1,
                "open_sum_eur": 1000.0,
                "label": "Gradđevinski radovi",
            }
        }
    )
    session = FakeSession(
        results=[
            FakeResult(items=[_row(id="t-today", deadline_at=deadline_at)]),
            FakeResult(items=[]),
        ]
    )
    resp = _client(session).get(f"/tenderi/cpv/45?lang={lang}")
    assert resp.status_code == 200
    return resp.text


def test_geo_row_deadline_today_says_istice_danas():
    # A deadline in one second — not yet expired, the calendar day is the same (days == 0).
    html = _cpv_page_with_deadline(datetime.now(timezone.utc) + timedelta(seconds=2))
    assert "Ističe danas" in html
    assert "Još 0 dana" not in html


def test_geo_row_deadline_in_days_still_counts_days():
    html = _cpv_page_with_deadline(datetime.now(timezone.utc) + timedelta(days=3))
    assert "Još 3 dana" in html
    assert "Ističe danas" not in html


def test_sitemap_pages_contains_izvori_in_all_languages():
    """owner 02.10: “Sources” is a public page, it must be in the sitemap."""
    session = FakeSession(results=[])
    resp = _client(session).get("/sitemap-pages.xml")
    assert "<loc>https://nab.rs/izvori</loc>" in resp.text
    assert "<loc>https://nab.rs/izvori?lang=ru</loc>" in resp.text
    assert "<loc>https://nab.rs/izvori?lang=en</loc>" in resp.text


# --------------------------------------------------------------------------- #
# canonical of pagination: page>1 canonicalises itself (?page=N), page=1 — without it
# --------------------------------------------------------------------------- #


def _cpv_page_client(page_query: str):
    _prime_cpv(
        {"45": {"total": 120, "open": 120, "open_sum_eur": None, "label": "Radovi"}}
    )
    session = FakeSession(results=[FakeResult(items=[_row(id="t-51")])])
    return _client(session).get(f"/tenderi/cpv/45{page_query}")


def test_pagination_page2_canonical_keeps_page_param():
    resp = _cpv_page_client("?page=2")
    assert resp.status_code == 200
    assert 'rel="canonical" href="https://nab.rs/tenderi/cpv/45?page=2"' in resp.text


def test_pagination_page2_canonical_keeps_lang_and_page():
    """lang stays in the canonical (ru/en canonicalise themselves), page too."""
    resp = _cpv_page_client("?page=2&lang=en")
    assert resp.status_code == 200
    assert (
        'rel="canonical" href="https://nab.rs/tenderi/cpv/45?lang=en&amp;page=2"'
        in (resp.text)
    )


def test_pagination_page2_hreflang_carries_page_in_every_language():
    resp = _cpv_page_client("?page=2")
    hreflangs = dict(re.findall(r'hreflang="([^"]+)" href="([^"]+)"', resp.text))
    assert hreflangs["sr"] == "https://nab.rs/tenderi/cpv/45?page=2"
    assert hreflangs["ru"] == "https://nab.rs/tenderi/cpv/45?lang=ru&amp;page=2"
    assert hreflangs["en"] == "https://nab.rs/tenderi/cpv/45?lang=en&amp;page=2"
    assert hreflangs["x-default"] == "https://nab.rs/tenderi/cpv/45?page=2"


def test_pagination_page1_canonical_has_no_page_param():
    for query in ("", "?page=1"):
        resp = _cpv_page_client(query)
        assert resp.status_code == 200
        assert 'rel="canonical" href="https://nab.rs/tenderi/cpv/45"' in resp.text
