"""SRC-0d (owner 30.09 R7): the public ``/tenderi*`` are the “Srbija” pages, the selections must cut by the country ``HOME_COUNTRY`` (otherwise the BA/MK tenders from other sources would land on the Serbian SEO pages).

The test on the SQL interception checks the TEXT and the parameters of the query, not the data: the real behavior of the predicate on PG — in TESTING (a measurement on a throwaway PG).
"""

from __future__ import annotations

import pytest

from src.api import repo
from tests.api.conftest import FakeResult, FakeSession

PREDICATE = "country_code = CAST(:home_country AS text)"

_CALLS = [
    ("cpv_division_stats", ("RS",)),
    ("region_stats", ()),
    ("list_open_tenders_by_cpv", ("72", 0, 10)),
    ("list_closed_tenders_by_cpv", ("72", 10)),
    ("list_open_tenders_by_region", ("RS-00", 0, 10)),
    ("list_closed_tenders_by_region", ("RS-00", 10)),
    ("latest_open_example", ()),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("fn,args", _CALLS, ids=[c[0] for c in _CALLS])
async def test_public_geo_queries_are_limited_to_home_country(fn, args):
    if fn == "cpv_division_stats":
        args = ()
    session = FakeSession(results=[FakeResult(), FakeResult()])
    await getattr(repo, fn)(session, *args)

    assert session.executed, fn
    for text, params in session.executed:
        assert PREDICATE in text, (fn, text)
        assert params and params["home_country"] == "RS", (fn, params)
