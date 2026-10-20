"""Tests of feed_service (ADR-013: mock session without a real DB).

We check:
- the delivery feed: field mapping + AI actions (delivered level / bid / checklist / draft);
- an empty feed → [];
- burning deadlines: mapping + days_left (UTC-aware);
- empty burning deadlines → [].
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.api.cabinet import feed_service
from src.domain.actuality import no_deadline_is_fresh, sql_no_deadline_fresh_tn
from src.api.cabinet.feed_service import (
    AiActions,
    FeedItem,
    count_feed_total,
    get_feed_items,
    get_feed_page,
    get_hot_deadlines,
    split_feed_by_urgency,
)

_TID = "11111111-1111-1111-1111-111111111111"


class _Result:
    def __init__(self, *, rows=None, scalar=None):
        self._rows = list(rows) if rows is not None else []
        self._scalar = scalar

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._scalar


class _Session:
    """Mock AsyncSession: set_config does not touch the results queue."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "set_config" in sql or "app.current_tenant_id" in sql:
            return _Result()
        return self._results.pop(0) if self._results else _Result()


# --------------------------------------------------------------------------- #
# get_feed_items
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_feed_items_maps_fields_and_ai_actions():
    sent_at = datetime.now(timezone.utc) - timedelta(hours=1)
    deadline = datetime.now(timezone.utc) + timedelta(days=5)
    row = SimpleNamespace(
        tender_id="aaaa",
        profile_id="pppp",
        sent_at=sent_at,
        title="Набавка рачунара",
        customer_name="Општина Нови Сад",
        deadline_at=deadline,
        tender_url="https://portal.rs/t/1",
        profile_name="ИТ набавке",
        ai_level="standard",
        delivered_ai_level="standard",
        has_bid_verdict=True,
        has_checklist=False,
        has_draft=False,
    )
    s = _Session([_Result(rows=[row])])
    items = await get_feed_items(s, _TID)
    assert len(items) == 1
    item = items[0]
    assert item.tender_id == "aaaa"
    assert item.title == "Набавка рачунара"
    assert item.customer_name == "Општина Нови Сад"
    assert item.deadline_at == deadline
    assert item.tender_url == "https://portal.rs/t/1"
    assert item.profile_name == "ИТ набавке"
    assert item.ai_level == "standard"
    assert item.ai_actions.delivered_analysis_level == "standard"
    assert item.ai_actions.has_bid_verdict is True
    assert item.ai_actions.has_checklist is False
    assert item.ai_actions.has_draft is False


@pytest.mark.asyncio
async def test_feed_items_defaults_for_missing_names():
    row = SimpleNamespace(
        tender_id="bbbb",
        profile_id="qqqq",
        sent_at=None,
        title=None,
        customer_name=None,
        deadline_at=None,
        tender_url=None,
        profile_name=None,
        ai_level=None,
        delivered_ai_level=None,
        has_bid_verdict=False,
        has_checklist=False,
        has_draft=False,
    )
    s = _Session([_Result(rows=[row])])
    items = await get_feed_items(s, _TID)
    assert items[0].title == "Без названия"
    assert items[0].profile_name == "Без названия"
    assert items[0].ai_actions.delivered_analysis_level is None


@pytest.mark.asyncio
async def test_feed_items_empty():
    s = _Session([_Result(rows=[])])
    items = await get_feed_items(s, _TID)
    assert items == []


# --------------------------------------------------------------------------- #
# get_hot_deadlines
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hot_deadlines_maps_fields_and_days_left():
    deadline = datetime.now(timezone.utc) + timedelta(days=3)
    row = SimpleNamespace(
        tender_id="cccc",
        title="Изградња пута",
        customer_name="ЈП Путеви Србије",
        deadline_at=deadline,
        tender_url="https://portal.rs/t/2",
    )
    s = _Session([_Result(rows=[row])])
    deadlines = await get_hot_deadlines(s, _TID)
    assert len(deadlines) == 1
    d = deadlines[0]
    assert d.tender_id == "cccc"
    assert d.title == "Изградња пута"
    assert d.customer_name == "ЈП Путеви Србије"
    assert d.deadline_at == deadline
    assert d.tender_url == "https://portal.rs/t/2"
    assert d.days_left in (3, 4)  # ceil depends on the current time within the day


@pytest.mark.asyncio
async def test_hot_deadlines_empty():
    s = _Session([_Result(rows=[])])
    deadlines = await get_hot_deadlines(s, _TID)
    assert deadlines == []


# --------------------------------------------------------------------------- #
# get_feed_page / count_feed_total (web archive, pagination)
# --------------------------------------------------------------------------- #


def _make_row(i: int):
    return SimpleNamespace(
        tender_id=f"t{i:04d}",
        profile_id="pppp",
        sent_at=datetime.now(timezone.utc) - timedelta(hours=i),
        title=f"Тендер {i}",
        customer_name=f"Заказчик {i}",
        deadline_at=None,
        tender_url=f"https://portal.rs/t/{i}",
        profile_name="Профиль",
        ai_level=None,
        delivered_ai_level=None,
        has_bid_verdict=False,
        has_checklist=False,
        has_draft=False,
    )


class _PagingSession:
    """Mock AsyncSession, SIMULATING DB pagination by the params limit/offset.

    So the test sees the real work of the window: get_feed_page must compute
    offset=(page-1)*page_size and pass it to the SQL — otherwise page 2 would return
    the same rows as page 1 (a mutation guard of the offset formula). The COUNT SQL
    returns the full size of the set (for total/total_pages)."""

    def __init__(self, dataset):
        self._dataset = list(dataset)

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "set_config" in sql or "app.current_tenant_id" in sql:
            return _Result()
        if "COUNT(*)" in sql:
            return _Result(scalar=len(self._dataset))
        limit = params["limit"]
        offset = params["offset"]
        return _Result(rows=self._dataset[offset : offset + limit])


@pytest.mark.asyncio
async def test_feed_page_returns_distinct_rows_per_page():
    dataset = [_make_row(i) for i in range(25)]
    s = _PagingSession(dataset)

    p1 = await get_feed_page(s, _TID, page=1, page_size=10)
    p2 = await get_feed_page(s, _TID, page=2, page_size=10)
    p3 = await get_feed_page(s, _TID, page=3, page_size=10)

    assert [it.tender_id for it in p1] == [f"t{i:04d}" for i in range(0, 10)]
    assert [it.tender_id for it in p2] == [f"t{i:04d}" for i in range(10, 20)]
    assert [it.tender_id for it in p3] == [f"t{i:04d}" for i in range(20, 25)]
    # The pages do NOT overlap — direct proof of the correct offset.
    assert not ({it.tender_id for it in p1} & {it.tender_id for it in p2})


@pytest.mark.asyncio
async def test_count_feed_total_returns_scalar():
    dataset = [_make_row(i) for i in range(7)]
    s = _PagingSession(dataset)
    assert await count_feed_total(s, _TID) == 7


@pytest.mark.asyncio
async def test_count_feed_total_empty_is_zero():
    s = _Session([_Result(rows=[], scalar=None)])
    assert await count_feed_total(s, _TID) == 0


@pytest.mark.asyncio
async def test_feed_page_maps_item_fields():
    s = _PagingSession([_make_row(3)])
    items = await get_feed_page(s, _TID, page=1, page_size=10)
    assert len(items) == 1
    assert items[0].tender_id == "t0003"
    assert items[0].title == "Тендер 3"
    assert items[0].customer_name == "Заказчик 3"


# --------------------------------------------------------------------------- #
# Stage 4: the working feed = only CURRENT + a filter by profile
#
# There is no DB here (ADR-013), so the predicates are checked by a mock that EXECUTES
# exactly those conditions that actually stand in the SQL text (see _PredicateSession).
# This is what makes the tests mutational: remove the predicate from the body or from COUNT —
# the mock will stop filtering too, exactly where the DB would have stopped.
# --------------------------------------------------------------------------- #


def _row(i: int, *, profile_id="p-a", days=None, seen_days_ago=0):
    """A row of the feed selection: profile ``profile_id``, the deadline in ``days`` days,
    for us for the first time ``seen_days_ago`` days ago (``first_seen_at``)."""
    now = datetime.now(timezone.utc)
    row = _make_row(i)
    row.profile_id = profile_id
    row.deadline_at = None if days is None else now + timedelta(days=days)
    row.published_at = None
    row.first_seen_at = now - timedelta(days=seen_days_ago)
    return row


class _PredicateSession:
    """Mock AsyncSession, EXECUTING the feed predicates over an in-memory dataset.

    It filters ONLY by the conditions present in the text of the incoming SQL, and
    separately for each query — so a divergence of the body and COUNT shows up
    as a divergence of ``total`` with the number of rows on the pages, not as a check
    by a constant string.
    """

    def __init__(self, dataset):
        self._dataset = list(dataset)

    def _visible(self, sql: str, params: dict):
        rows = self._dataset
        if "tn.deadline_at >= now()" in sql and params.get("actual_only", True):
            now = datetime.now(timezone.utc)
            # The branch “no deadline” — also by the SQL text: there is a fragment of the rule
            # 30 days — we execute it with its Python mirror; if not — “NULL = forever”.
            rule_30d = sql_no_deadline_fresh_tn("tn") in sql
            rows = [
                r
                for r in rows
                if (r.deadline_at is not None and r.deadline_at >= now)
                or (
                    r.deadline_at is None
                    and (
                        not rule_30d
                        or no_deadline_is_fresh(r.published_at, r.first_seen_at, now)
                    )
                )
            ]
        if "o.profile_id = CAST(:profile_id AS uuid)" in sql:
            wanted = params.get("profile_id")
            if wanted is not None:
                rows = [r for r in rows if r.profile_id == wanted]
        return rows

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if "set_config" in sql or "app.current_tenant_id" in sql:
            return _Result()
        rows = self._visible(sql, params)
        if "COUNT(*)" in sql:
            return _Result(scalar=len(rows))
        offset = params.get("offset", 0)
        return _Result(rows=rows[offset : offset + params["limit"]])


def _mixed_deadlines():
    """Expired, future, fresh without a deadline, and old without a deadline."""
    return [
        _row(1, days=-3),  # the submission deadline has passed
        _row(2, days=5),  # still possible to submit
        _row(3, days=None, seen_days_ago=2),  # no deadline, seen 2 days ago
        _row(4, days=None, seen_days_ago=31),  # no deadline, 31 days — outdated
    ]


@pytest.mark.asyncio
async def test_feed_page_drops_expired_and_stale_no_deadline():
    """The working feed = only current: expired goes away; without a deadline it lives 30 days.

    Owner decision 06.10.2026 (`src/domain/actuality.py`): before it, a NULL deadline
    stayed in the feed forever — 6351 of 6529 “current” pairs were older than 30 days.
    The SQL predicate itself is proven on PG: tests/db/test_no_deadline_30d.py.
    """
    s = _PredicateSession(_mixed_deadlines())
    items = await get_feed_page(s, _TID, page=1, page_size=10)
    assert [it.tender_id for it in items] == ["t0002", "t0003"]


@pytest.mark.asyncio
async def test_count_feed_total_drops_expired_too():
    """‼️ The same predicate in COUNT: otherwise total would promise pages with no rows."""
    s = _PredicateSession(_mixed_deadlines())
    assert await count_feed_total(s, _TID) == 2


@pytest.mark.asyncio
async def test_dashboard_teaser_is_current_only():
    """The dashboard teaser reads the same body — so there will be no expired one on the cockpit either."""
    s = _PredicateSession(_mixed_deadlines())
    items = await get_feed_items(s, _TID, limit=10)
    assert [it.tender_id for it in items] == ["t0002", "t0003"]


# --------------------------------------------------------------------------- #
# owner 22.09: mode “all / only current” (?actual=0|1 on /app/feed)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_feed_page_actual_only_false_keeps_expired_too():
    """``actual_only=False`` — expired and outdated without a deadline are RETURNED."""
    s = _PredicateSession(_mixed_deadlines())
    items = await get_feed_page(s, _TID, page=1, page_size=10, actual_only=False)
    assert {it.tender_id for it in items} == {"t0001", "t0002", "t0003", "t0004"}


@pytest.mark.asyncio
async def test_feed_page_actual_only_true_is_still_the_default():
    """Without an explicit ``actual_only`` — the previous behavior (expired is cut off)."""
    s = _PredicateSession(_mixed_deadlines())
    items = await get_feed_page(s, _TID, page=1, page_size=10)
    assert [it.tender_id for it in items] == ["t0002", "t0003"]


@pytest.mark.asyncio
async def test_count_feed_total_actual_only_false_counts_expired_too():
    """‼️ The same bind parameter in COUNT — otherwise total would diverge from the page."""
    s = _PredicateSession(_mixed_deadlines())
    assert await count_feed_total(s, _TID, actual_only=False) == 4
    assert await count_feed_total(s, _TID, actual_only=True) == 2


def test_current_only_predicate_is_gated_by_actual_only_bind():
    """Guard: the predicate reads ``:actual_only``, not only the fact of the deadline.

    Without this branch ``actual_only=False`` would be useless — the SQL would keep
    cutting off the expired unconditionally. Mutation: remove the branch
    ``CAST(:actual_only AS boolean) = false`` — the test must go red.
    """
    assert (
        "CAST(:actual_only AS boolean) = false" in feed_service._CURRENT_ONLY_PREDICATE
    )
    for sql in (feed_service._FEED_SELECT_BODY, str(feed_service._FEED_COUNT_SQL)):
        assert feed_service._CURRENT_ONLY_PREDICATE in sql


@pytest.mark.asyncio
async def test_profile_filter_narrows_page_to_one_profile():
    dataset = [_row(1, profile_id="p-a"), _row(2, profile_id="p-b")]
    s = _PredicateSession(dataset)
    items = await get_feed_page(s, _TID, page=1, page_size=10, profile_id="p-b")
    assert [it.tender_id for it in items] == ["t0002"]


@pytest.mark.asyncio
async def test_profile_filter_none_shows_every_profile():
    dataset = [_row(1, profile_id="p-a"), _row(2, profile_id="p-b")]
    s = _PredicateSession(dataset)
    items = await get_feed_page(s, _TID, page=1, page_size=10, profile_id=None)
    assert [it.tender_id for it in items] == ["t0001", "t0002"]


@pytest.mark.asyncio
async def test_total_matches_rows_actually_paged_under_profile_filter():
    """‼️ SELECT and COUNT — one set: total == the sum of rows over all pages.

    Mutation guard of synchrony: remove the profile (or “current”) predicate in
    ONE of the two queries — total will stop converging with what the pagination
    actually returns, and the test goes red.
    """
    dataset = [_row(i, profile_id="p-a" if i % 2 else "p-b") for i in range(1, 13)]
    dataset += [_row(90 + i, profile_id="p-a", days=-1) for i in range(3)]
    s = _PredicateSession(dataset)

    total = await count_feed_total(s, _TID, profile_id="p-a")
    paged = []
    for page in (1, 2, 3):
        paged += await get_feed_page(s, _TID, page=page, page_size=5, profile_id="p-a")

    assert total == len(paged)
    assert {it.profile_id for it in paged} == {"p-a"}


def test_both_feed_queries_carry_both_predicates():
    """The predicates are ONE set of constants, substituted both into the body and into COUNT.

    A direct guard of “one selection per meaning”: the feed body and its counter cannot
    diverge through an oversight, because the predicate text in the code is one.
    """
    for sql in (feed_service._FEED_SELECT_BODY, str(feed_service._FEED_COUNT_SQL)):
        assert feed_service._CURRENT_ONLY_PREDICATE in sql
        assert feed_service._PROFILE_FILTER_PREDICATE in sql
    # The profile filter does not replace tenant isolation, it stands next to it.
    assert "p.tenant_id = CAST(:tid AS uuid)" in feed_service._FEED_SELECT_BODY
    assert "p.tenant_id = CAST(:tid AS uuid)" in str(feed_service._FEED_COUNT_SQL)


# --------------------------------------------------------------------------- #
# split_feed_by_urgency (presentation for /app/feed, a pure function — no DB)
# --------------------------------------------------------------------------- #


def _feed_item(tid: str, *, days: int | None):
    """FeedItem with a deadline in ``days`` days (None — no deadline).

    Plus a minute — so that ``days=0`` is “today, not yet passed” and not expired:
    ``_days_left`` counts calendar dates in Belgrade time, a past moment is always
    negative.
    """
    deadline = (
        None
        if days is None
        else datetime.now(timezone.utc) + timedelta(days=days, minutes=1)
    )
    return FeedItem(
        tender_id=tid,
        title=f"Тендер {tid}",
        customer_name=None,
        deadline_at=deadline,
        tender_url=None,
        profile_id="p-1",
        profile_name="Профиль",
        ai_level=None,
        sent_at=None,
        ai_actions=AiActions(
            delivered_analysis_level=None,
            has_bid_verdict=False,
            has_checklist=False,
            has_draft=False,
        ),
    )


def test_split_feed_puts_week_window_into_urgent():
    """“Burning” = the deadline has not passed yet and is ≤ 7 days away (the badge window)."""
    groups = split_feed_by_urgency(
        [
            _feed_item("today", days=0),
            _feed_item("d3", days=3),
            _feed_item("d7", days=7),
        ]
    )
    assert [it.tender_id for it in groups.urgent] == ["today", "d3", "d7"]
    assert groups.rest == []


def test_split_feed_leaves_far_expired_and_dateless_in_rest():
    """Outside the week window, expired and without a deadline are not “burning”.

    An expired one deliberately does not get into “burning”: the application can no longer be submitted,
    a red accent on it would be a false alarm.
    """
    groups = split_feed_by_urgency(
        [
            _feed_item("far", days=30),
            _feed_item("expired", days=-3),
            _feed_item("nodate", days=None),
        ]
    )
    assert groups.urgent == []
    assert [it.tender_id for it in groups.rest] == ["far", "expired", "nodate"]


def test_split_feed_boundary_day_8_is_not_urgent(freeze_feed_clock):
    """The window boundary is exactly the same as for the colored badge: 7 — yes, 8 — no.

    The clocks are frozen: the deadline is calendar (8 days in Belgrade), not “+8×24h”,
    otherwise the boundary floats with the hour of the day and with the switch to winter time.
    """
    deadline_in = freeze_feed_clock(datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    item = replace(_feed_item("d8", days=None), deadline_at=deadline_in(8))
    groups = split_feed_by_urgency([item])
    assert groups.urgent == []
    assert [it.tender_id for it in groups.rest] == ["d8"]


def test_split_feed_preserves_server_order_inside_groups():
    """‼️ The selection order (sent_at DESC) is preserved INSIDE each group.

    This is what makes the grouping safe for pagination: the function only
    splits the list that has already arrived, it does not sort and loses nothing.
    """
    # ‼️ The ids are deliberately NOT alphabetical: if the function starts to sort (and not only
    # split), the order will diverge from the server one, and the test will see it.
    items = [
        _feed_item("z", days=2),
        _feed_item("y", days=40),
        _feed_item("m", days=1),
        _feed_item("b", days=None),
        _feed_item("a", days=5),
    ]
    groups = split_feed_by_urgency(items)
    assert [it.tender_id for it in groups.urgent] == ["z", "m", "a"]
    assert [it.tender_id for it in groups.rest] == ["y", "b"]
    # Not a single row was lost or duplicated.
    assert len(groups.urgent) + len(groups.rest) == len(items)


def test_split_feed_empty_page_gives_empty_groups():
    groups = split_feed_by_urgency([])
    assert groups.urgent == []
    assert groups.rest == []


# --------------------------------------------------------------------------- #
# owner 22.09: feed sorting by template (?sort=profile on /app/feed)
# --------------------------------------------------------------------------- #


class _SqlCaptureSession:
    """Mock AsyncSession: remembers the TEXT of the last SQL, executes nothing."""

    def __init__(self):
        self.last_sql: str | None = None

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "set_config" in sql or "app.current_tenant_id" in sql:
            return _Result()
        self.last_sql = sql
        return _Result(rows=[])


@pytest.mark.asyncio
async def test_get_feed_page_default_sort_orders_by_date():
    """Without ``sort=`` (and with a junk value) — the previous “by date” order."""
    s = _SqlCaptureSession()
    await get_feed_page(s, _TID, page=1, page_size=10)
    assert "ORDER BY sent_at DESC NULLS LAST" in s.last_sql
    assert "profile_name," not in s.last_sql.split("ORDER BY")[-1]


@pytest.mark.asyncio
async def test_get_feed_page_sort_profile_orders_by_profile_name_first():
    """``sort=“profile”`` — the profile name is primary, the order is FULLY deterministic.

    ``outbox_id DESC`` is a tie-breaker: without it, rows with equal
    (profile_name, sent_at) would have no stable order between requests
    for the same page, and the pagination would “drift”.
    """
    s = _SqlCaptureSession()
    await get_feed_page(s, _TID, page=1, page_size=10, sort="profile")
    tail = s.last_sql.rsplit("ORDER BY", 1)[-1]
    assert "profile_name" in tail
    assert tail.index("profile_name") < tail.index("sent_at")
    assert "outbox_id DESC" in tail


@pytest.mark.asyncio
async def test_get_feed_page_sort_profile_does_not_change_where_or_dedup():
    """Sorting — ONLY the trailing ORDER BY; WHERE/JOIN/dedup are not rewritten."""
    s_date = _SqlCaptureSession()
    s_profile = _SqlCaptureSession()
    await get_feed_page(s_date, _TID, page=1, page_size=10, sort="date")
    await get_feed_page(s_profile, _TID, page=1, page_size=10, sort="profile")
    body_date = s_date.last_sql.rsplit("ORDER BY", 1)[0]
    body_profile = s_profile.last_sql.rsplit("ORDER BY", 1)[0]
    assert body_date == body_profile


# --------------------------------------------------------------------------- #
# owner 22.09: feed sorting by the application deadline (?sort=deadline on /app/feed)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_feed_page_sort_deadline_orders_by_deadline_first():
    """``sort=“deadline”`` — nearest deadline first, the order is FULLY deterministic.

    ``outbox_id DESC`` — the same tie-breaker as for sorting by template: without it,
    rows with an equal (incl. all NULL) ``deadline_at`` would have no
    stable order between requests for one page, and pagination would “drift”.
    """
    s = _SqlCaptureSession()
    await get_feed_page(s, _TID, page=1, page_size=10, sort="deadline")
    tail = s.last_sql.rsplit("ORDER BY", 1)[-1]
    assert "deadline_at ASC NULLS LAST" in tail
    assert "outbox_id DESC" in tail


@pytest.mark.asyncio
async def test_get_feed_page_sort_deadline_does_not_change_where_or_dedup():
    """Sorting — ONLY the trailing ORDER BY; WHERE/JOIN/dedup are not rewritten."""
    s_date = _SqlCaptureSession()
    s_deadline = _SqlCaptureSession()
    await get_feed_page(s_date, _TID, page=1, page_size=10, sort="date")
    await get_feed_page(s_deadline, _TID, page=1, page_size=10, sort="deadline")
    body_date = s_date.last_sql.rsplit("ORDER BY", 1)[0]
    body_deadline = s_deadline.last_sql.rsplit("ORDER BY", 1)[0]
    assert body_date == body_deadline


def _row_deadline(i: int, *, days: int | None, profile_id="p-a"):
    """A selection row with its own ``outbox_id`` (sorting/tie-breaker by it)."""
    row = _row(i, profile_id=profile_id, days=days)
    row.outbox_id = i
    return row


class _OrderingSession:
    """Mock AsyncSession: really SORTS the dataset by the ORDER BY of the incoming SQL.

    Unlike ``_SqlCaptureSession`` (text only) and ``_PredicateSession``
    (filter only), here both are needed at once — to prove the order of
    rows that actually pass the actuality predicate.
    """

    def __init__(self, dataset, *, actual_only=True):
        self._dataset = list(dataset)
        self._actual_only = actual_only

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if "set_config" in sql or "app.current_tenant_id" in sql:
            return _Result()
        rows = self._dataset
        if self._actual_only and "tn.deadline_at >= now()" in sql:
            now = datetime.now(timezone.utc)
            rows = [r for r in rows if r.deadline_at is None or r.deadline_at >= now]
        if "ORDER BY deadline_at ASC NULLS LAST" in sql:
            rows = sorted(
                rows,
                key=lambda r: (
                    r.deadline_at is None,
                    r.deadline_at or datetime.max.replace(tzinfo=timezone.utc),
                    -r.outbox_id,
                ),
            )
        if "COUNT(*)" in sql:
            return _Result(scalar=len(rows))
        offset = params.get("offset", 0)
        return _Result(rows=rows[offset : offset + params["limit"]])


@pytest.mark.asyncio
async def test_feed_page_sort_deadline_nulls_last_and_stable():
    """Nearest deadline first, NULL at the very end, the tie-breaker keeps the order."""
    dataset = [
        _row_deadline(1, days=10),
        _row_deadline(2, days=None),
        _row_deadline(3, days=1),
        _row_deadline(4, days=5),
    ]
    s = _OrderingSession(dataset, actual_only=False)
    items = await get_feed_page(
        s, _TID, page=1, page_size=10, sort="deadline", actual_only=False
    )
    assert [it.tender_id for it in items] == ["t0003", "t0004", "t0001", "t0002"]
