"""CPV directory: ``/cpv-kodovi`` and the group pages ``/tenderi/cpv/NNN``.

The DB is mocked (as in ``test_geo_landings``), aggregates are primed into the cache. Expectations — literals. The group index threshold — ``INDEX_CPV_GROUP_MIN_TENDERS`` = 10.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from src.api.public import geo_cache
from tests.api.conftest import FakeResult, FakeSession

CYR = re.compile("[Ѐ-ӿ]")
_SR_031 = "Poljoprivredni i hortikulturni proizvodi"
_EN_031 = "Agricultural and horticultural products"


def _client(session: FakeSession) -> TestClient:
    def factory():
        return session

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def _prime(groups: dict, codes: dict | None = None, divisions: dict | None = None):
    future = time.monotonic() + 3600
    geo_cache._cpv_dir_cache = (future, {"groups": groups, "codes": codes or {}})
    geo_cache._cpv_cache = (future, divisions or {})


def _g(total: int, open_: int = 0) -> dict:
    return {"total": total, "open": open_, "open_sum_eur": None}


def _row(**kw):
    base = dict(
        id="t-1",
        title="Радови",
        customer_name="ЈП",
        deadline_at=datetime.now(timezone.utc) + timedelta(days=10),
        amount_eur=1000.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _group_page(path: str, total: int):
    _prime({"031": _g(total, 1)})
    session = FakeSession(results=[FakeResult(items=[_row()]), FakeResult(items=[])])
    return _client(session).get(path)


# --- /cpv-kodovi -----------------------------------------------------------


def test_directory_200_in_three_languages():
    _prime({"031": _g(12)}, {"03111000": 4})
    client = _client(FakeSession(results=[]))
    sr = client.get("/cpv-kodovi")
    ru = client.get("/cpv-kodovi?lang=ru")
    en = client.get("/cpv-kodovi?lang=en")
    assert (sr.status_code, ru.status_code, en.status_code) == (200, 200, 200)
    assert "CPV šifarnik — javne nabavke po šifri" in sr.text
    assert "Справочник CPV — тендеры по коду" in ru.text
    assert "CPV code directory — tenders by code" in en.text


def test_directory_sr_has_no_cyrillic_and_links_group():
    _prime({"031": _g(12)}, {"03111000": 4})
    resp = _client(FakeSession(results=[])).get("/cpv-kodovi")
    assert not CYR.search(resp.text)
    assert _SR_031 in resp.text
    assert 'href="/tenderi/cpv/031"' in resp.text
    assert "tendera: 12" in resp.text


def test_directory_thin_group_listed_without_link():
    _prime({"031": _g(9)})
    resp = _client(FakeSession(results=[])).get("/cpv-kodovi")
    assert _SR_031 in resp.text
    assert 'href="/tenderi/cpv/031"' not in resp.text


def test_directory_ru_falls_back_to_marked_english():
    _prime({})
    resp = _client(FakeSession(results=[])).get("/cpv-kodovi?lang=ru")
    assert _EN_031 in resp.text
    assert '<abbr class="text-xs text-slate-500" title=' in resp.text


def test_directory_canonical_hreflang_json_ld_and_script():
    _prime({"031": _g(12)})
    resp = _client(FakeSession(results=[])).get("/cpv-kodovi?lang=en")
    assert (
        '<link rel="canonical" href="https://nab.rs/cpv-kodovi?lang=en">' in resp.text
    )
    assert 'hreflang="x-default" href="https://nab.rs/cpv-kodovi"' in resp.text
    assert '"@type":"DefinedTermSet"' in resp.text.replace(" ", "")
    assert "noindex" not in resp.text
    assert re.search(
        r'<script src="/static/js/cpv_directory\.js\?v=\w+" integrity="sha384-',
        resp.text,
    )
    assert "onclick=" not in resp.text


def test_directory_data_block_carries_counts_and_linkable_groups():
    _prime({"031": _g(12), "032": _g(3)}, {"03111000": 4})
    resp = _client(FakeSession(results=[])).get("/cpv-kodovi")
    blob = re.search(
        r'<script type="application/json" id="cpv-dir-data">(.*?)</script>', resp.text
    )
    data = json.loads(blob.group(1))
    assert data["n"] == {"03111000": 4}
    assert data["g"] == ["031"]


# --- /tenderi/cpv/NNN ------------------------------------------------------


def test_group_with_10_tenders_is_indexable():
    resp = _group_page("/tenderi/cpv/031", 10)
    assert resp.status_code == 200
    assert 'name="robots"' not in resp.text
    assert _SR_031 in resp.text
    assert 'href="/cpv-kodovi"' in resp.text
    assert 'href="/tenderi/cpv/03"' in resp.text


def test_group_with_9_tenders_is_noindex_but_200():
    resp = _group_page("/tenderi/cpv/031", 9)
    assert resp.status_code == 200
    assert '<meta name="robots" content="noindex,follow">' in resp.text


def test_group_unknown_code_is_404():
    _prime({})
    client = _client(FakeSession(results=[]))
    assert client.get("/tenderi/cpv/999").status_code == 404
    assert client.get("/tenderi/cpv/abc").status_code == 404
    assert client.get("/tenderi/cpv/0311").status_code == 404


def test_group_canonical_and_pagination_follow_710():
    resp = _group_page("/tenderi/cpv/031?lang=en", 12)
    assert (
        '<link rel="canonical" href="https://nab.rs/tenderi/cpv/031?lang=en">'
        in resp.text
    )
    assert 'hreflang="sr" href="https://nab.rs/tenderi/cpv/031"' in resp.text


def test_group_without_stats_row_is_200_noindex():
    _prime({})
    session = FakeSession(results=[FakeResult(items=[]), FakeResult(items=[])])
    resp = _client(session).get("/tenderi/cpv/031")
    assert resp.status_code == 200
    assert "noindex,follow" in resp.text


def test_division_page_links_indexable_child_groups_only():
    _prime(
        {"031": _g(12), "032": _g(3)},
        divisions={"03": {"total": 30, "open": 1, "open_sum_eur": None, "label": "x"}},
    )
    session = FakeSession(results=[FakeResult(items=[_row()]), FakeResult(items=[])])
    resp = _client(session).get("/tenderi/cpv/03")
    assert resp.status_code == 200
    assert 'href="/tenderi/cpv/031"' in resp.text
    assert 'href="/tenderi/cpv/032"' not in resp.text
    assert 'href="/cpv-kodovi"' in resp.text


# --- sitemap ---------------------------------------------------------------


def test_sitemap_has_directory_and_only_groups_over_threshold():
    _prime({"031": _g(10), "032": _g(9), "999": _g(500)})
    resp = _client(FakeSession(results=[])).get("/sitemap-pages.xml")
    assert "<loc>https://nab.rs/cpv-kodovi</loc>" in resp.text
    assert "<loc>https://nab.rs/cpv-kodovi?lang=ru</loc>" in resp.text
    assert "<loc>https://nab.rs/tenderi/cpv/031</loc>" in resp.text
    assert "/tenderi/cpv/032" not in resp.text
    assert "/tenderi/cpv/999" not in resp.text  # not in the directory → 404 → not in sitemap


# --- SQL: prefix length (group ≠ section) ------------------------------------


def test_group_listing_sql_matches_three_digit_prefix_not_two():
    session = _run_group_listing()
    sqls = [s for s, _ in session.executed if "tenders_normalized" in s]
    assert sqls, "the group listing did not touch tenders_normalized"
    for sql in sqls:
        assert "left(e->>'code', 3) = :division" in sql
        assert "left(e->>'code', 2)" not in sql


def _run_group_listing():
    _prime({"031": _g(12, 1)})
    session = FakeSession(results=[FakeResult(items=[_row()]), FakeResult(items=[])])
    assert _client(session).get("/tenderi/cpv/031").status_code == 200
    return session


def test_portal_filter_on_group_uses_three_digit_prefix():
    from src.api import repo
    from src.api.public.portal_filters import PortalFilters

    where, params = repo._portal_where(
        PortalFilters(), cpv_division="031", region_code=None
    )
    assert "left(e->>'code', 3) = CAST(:division AS text)" in where
    assert params["division"] == "031"
