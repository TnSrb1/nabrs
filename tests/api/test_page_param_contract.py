"""The contract of ?page= on the /tenderi showcase: junk → page 1, past the end → the last, 200.

Expectations — literals from the fact (not via pager_pages/_total_pages).
PAGE_SIZE of the showcase = 50: 60 tenders → 2 pages, 120 → 3.
DB — a mock (FakeSession), templates render for real.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from src.api.public import geo_cache
from tests.api.conftest import FakeResult, FakeSession
from tests.api.test_geo_landings import _prime_cpv, _prime_region, _prime_sources, _row

BASE = "https://nab.rs"


def _client(session: FakeSession) -> TestClient:
    app.dependency_overrides[deps.get_session_factory] = lambda: lambda: session
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def _prime_group(total: int) -> None:
    future = time.monotonic() + 3600
    geo_cache._cpv_dir_cache = (
        future,
        {
            "groups": {"453": {"total": total, "open": total, "open_sum_eur": None}},
            "codes": {},
        },
    )


def _prime_all() -> None:
    _prime_cpv({"45": {"total": 120, "open": 120, "open_sum_eur": None, "label": "x"}})
    _prime_region({"RS-00": {"total": 120, "open": 120, "open_sum_eur": None}})
    _prime_sources()


def _unfiltered(path: str):
    """Without filters: the first queue — open (+ closed on page 1, if any)."""
    _prime_all()
    _prime_group(120)
    session = FakeSession(
        results=[
            FakeResult(items=[_row()]),
            FakeResult(items=[]),
            FakeResult(items=[_row()]),
        ]
    )
    return _client(session).get(path)


# All three listing routes (the hub separately: without filters it does not paginate).
_LISTING = ["/tenderi/cpv/45", "/tenderi/cpv/453", "/tenderi/region/RS-00"]
_CANON = {
    "/tenderi/cpv/45": f"{BASE}/tenderi/cpv/45",
    "/tenderi/cpv/453": f"{BASE}/tenderi/cpv/453",
    "/tenderi/region/RS-00": f"{BASE}/tenderi/region/RS-00",
}


@pytest.mark.parametrize("path", _LISTING)
@pytest.mark.parametrize("junk", ["abc", "0", "-5", "1.5", "9999999999999999999999x"])
def test_junk_page_is_first_page_200(path, junk):
    resp = _unfiltered(f"{path}?page={junk}")
    assert resp.status_code == 200
    assert f'rel="canonical" href="{_CANON[path]}"' in resp.text
    assert '<span class="fd-pager__num" aria-current="page">1</span>' in resp.text


@pytest.mark.parametrize("path", _LISTING)
@pytest.mark.parametrize("huge", ["9999", "999999999999999999999"])
def test_page_past_last_is_last_page_200(path, huge):
    # 120 tenders → 3 pages; a request past the end → page 3, canonical = ?page=3
    resp = _unfiltered(f"{path}?page={huge}")
    assert resp.status_code == 200
    assert f'rel="canonical" href="{_CANON[path]}?page=3"' in resp.text
    assert '<span class="fd-pager__num" aria-current="page">3</span>' in resp.text


def test_hub_junk_page_is_200_and_canonical_has_no_page():
    _prime_all()
    resp = _client(FakeSession(results=[])).get("/tenderi?page=abc")
    assert resp.status_code == 200
    assert f'rel="canonical" href="{BASE}/tenderi"' in resp.text


def test_hub_huge_page_is_200_and_canonical_has_no_page():
    _prime_all()
    resp = _client(FakeSession(results=[])).get("/tenderi?page=9999")
    assert resp.status_code == 200
    assert f'rel="canonical" href="{BASE}/tenderi"' in resp.text


def test_filtered_past_last_goes_to_last_page_200():
    # filter: found=120 → 3 pages. Queue: empty page 9999, count, page 3, count.
    _prime_all()
    session = FakeSession(
        results=[
            FakeResult(items=[]),
            FakeResult(scalar=120),
            FakeResult(items=[_row()]),
            FakeResult(scalar=120),
        ]
    )
    resp = _client(session).get("/tenderi/cpv/45?q=skola&page=9999")
    assert resp.status_code == 200
    assert f'rel="canonical" href="{BASE}/tenderi/cpv/45?page=3"' in resp.text
    assert '<span class="fd-pager__num" aria-current="page">3</span>' in resp.text
