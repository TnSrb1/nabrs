"""Tests of the honest banner “hidden by the region filter” (feed_service, ADR-013).

We check :func:`feed_service.get_region_hidden_notice` on a mock session (without DB):
- the banner is there when the profile narrowed the region AND part of the suitable tenders is hidden;
- the numbers are taken from the preview logic (``hidden_by_region``), not from a new predicate;
- there is no banner with an empty ``regions`` — and the preview is NOT requested at all (a performance invariant: count ONLY when a region is selected). Mutation: remove the ``p.regions`` filter — the “empty region” test goes red (a request/banner appears);
- there is no banner when nothing is hidden (hidden == 0);
- inactive profiles are ignored;
- several profiles with a region → the aggregate (sum), the link to the first.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api.cabinet.feed_service import RegionHiddenNotice, get_region_hidden_notice

_TID = "11111111-1111-1111-1111-111111111111"


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class _Session:
    """Mock AsyncSession: accumulates the results of preview/coverage queries in turn.

    ``queries`` — the really executed SQL (not set_config), to prove the performance invariant: with an empty region the preview is NOT called even once.
    """

    def __init__(self, results):
        self._results = list(results)
        self.queries: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "set_config" in sql or "app.current_tenant_id" in sql:
            return _Result([])
        self.queries.append(sql)
        return self._results.pop(0) if self._results else _Result([])


def _profile(*, pid: str, regions, is_active=True):
    """A stub profile with the fields that payload_from_profile + notice read."""
    return SimpleNamespace(
        id=pid,
        is_active=is_active,
        regions=regions,
        enabled_source_keys=["jnportal"],
        classifications={},
        min_amount_eur=None,
        max_amount_eur=None,
        min_days_until_deadline=None,
        include_no_amount=False,
        keywords_include=[],
        keywords_exclude=[],
        keyword_dict=False,
        # A profile without a topic gets the stream only in the explicit mode (0043);
        # the banner “hidden by the region filter” is checked precisely on it.
        match_all=True,
    )


def _preview_rows(n: int):
    return [
        SimpleNamespace(id=f"p{i}", title_normalized="t", description_normalized="d")
        for i in range(n)
    ]


def _coverage_rows(n: int, with_region: int = 0):
    return [
        SimpleNamespace(
            id=f"c{i}",
            title_normalized="t",
            description_normalized="d",
            has_region=(i < with_region),
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_banner_shown_when_region_filter_hides_tenders():
    # preview: 3 matched the region; coverage: 5 would match without the region → 2 hidden.
    s = _Session([_Result(_preview_rows(3)), _Result(_coverage_rows(5, with_region=3))])
    notice = await get_region_hidden_notice(
        s, _TID, [_profile(pid="A", regions=["12"])]
    )
    assert isinstance(notice, RegionHiddenNotice)
    assert notice.hidden == 2  # max(denom(5) - count(3), 0), from the preview logic
    assert notice.profile_id == "A"


@pytest.mark.asyncio
async def test_no_banner_when_region_empty_and_no_query_run():
    # An empty region: no banner AND the preview is NOT requested (the performance invariant).
    s = _Session([_Result(_preview_rows(3)), _Result(_coverage_rows(9))])
    notice = await get_region_hidden_notice(s, _TID, [_profile(pid="A", regions=[])])
    assert notice is None
    assert s.queries == []  # mutation: remove the p.regions filter → a query would appear here


@pytest.mark.asyncio
async def test_no_banner_when_nothing_hidden():
    # denom == count → nothing to hide → None (we do not show the banner).
    s = _Session([_Result(_preview_rows(5)), _Result(_coverage_rows(5))])
    notice = await get_region_hidden_notice(
        s, _TID, [_profile(pid="A", regions=["12"])]
    )
    assert notice is None


@pytest.mark.asyncio
async def test_inactive_profile_ignored():
    s = _Session([_Result(_preview_rows(3)), _Result(_coverage_rows(9))])
    notice = await get_region_hidden_notice(
        s, _TID, [_profile(pid="A", regions=["12"], is_active=False)]
    )
    assert notice is None
    assert s.queries == []


@pytest.mark.asyncio
async def test_aggregates_across_profiles_links_to_first():
    # Profile A hides 2 (5-3), profile B hides 1 (4-3) → the aggregate 3.
    s = _Session(
        [
            _Result(_preview_rows(3)),
            _Result(_coverage_rows(5)),
            _Result(_preview_rows(3)),
            _Result(_coverage_rows(4)),
        ]
    )
    notice = await get_region_hidden_notice(
        s,
        _TID,
        [_profile(pid="A", regions=["12"]), _profile(pid="B", regions=["21"])],
    )
    assert notice.hidden == 3
    assert notice.profile_id == "A"  # the link to the first profile with a region


@pytest.mark.asyncio
async def test_no_profiles_returns_none():
    s = _Session([])
    assert await get_region_hidden_notice(s, _TID, []) is None
    assert s.queries == []
