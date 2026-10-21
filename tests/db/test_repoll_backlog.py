"""One-off poll of the overdue deadlines (scripts/repoll_backlog.py, 08.10.2026) against PostgreSQL.

Requires ``TEST_RLS_DATABASE_URL`` / ``TEST_SUPERUSER_DATABASE_URL`` (a throwaway PG,
NEVER the deploy production; gate.sh brings it up itself). The adapter is a stub returning
prepared cards; no network is needed. We prove by the ``xmin`` of the row (any UPDATE
shifts it, and the NOTIFY trigger hangs on every UPDATE) and by literals from the fact.

The fact: “today” = 2026-10-08 12:00 UTC; the overdue jnportal ones: deadline 05.10, 01.10 and
25.09 (all older than a day), plus an active one with a deadline of 20.10 (not overdue).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from scripts.repoll_backlog import run_backlog

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — repoll_backlog пропускается",
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
PUBLISHED = datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc)
FETCHED_OLD = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
EXTENDED = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
BROKEN = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)  # before the publication

# uid: the deadline in the DB
_ROWS = {
    "ext": datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc),  # card: extended
    "same": datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc),  # card: the same
    "broken": datetime(
        2026, 9, 25, 10, 0, tzinfo=timezone.utc
    ),  # card: a broken deadline
    "open": datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc),  # not overdue
}
# what the portal gives
_CARDS = {
    "ext": {"deadline_at": EXTENDED, "status": "active"},
    "same": {"deadline_at": _ROWS["same"], "status": "active"},
    "broken": {"deadline_at": BROKEN, "status": "active"},
    "open": {"deadline_at": EXTENDED, "status": "active"},
}


class FakeAdapter:
    def __init__(self):
        self.asked: list[str] = []

    async def repoll_cards(self, tender_ids):
        for uid in tender_ids:
            self.asked.append(uid)
            yield uid, dict(_CARDS[uid])


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    source_key = f"rb-test-{uuid.uuid4().hex[:12]}"
    ids = {uid: uuid.uuid4() for uid in _ROWS}
    async with eng.begin() as c:
        for uid, deadline in _ROWS.items():
            await c.execute(
                text(
                    "INSERT INTO tenders_raw (id, country_code, source_key, source_uid, "
                    "raw_payload, raw_hash, fetched_at) VALUES "
                    "(:id, 'RS', :k, :uid, CAST('{}' AS jsonb), :uid, :fa)"
                ),
                {"id": ids[uid], "k": source_key, "uid": uid, "fa": FETCHED_OLD},
            )
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, title, customer_name, status, deadline_at, "
                    "published_at, first_seen_at, last_seen_at, last_changed_at) VALUES "
                    "(:id, :k, :uid, 'RS', :uid, 'Заказчик', 'active', :dl, :pub, "
                    ":pub, :pub, :pub)"
                ),
                {
                    "id": ids[uid],
                    "k": source_key,
                    "uid": uid,
                    "dl": deadline,
                    "pub": PUBLISHED,
                },
            )
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    yield {"source_key": source_key, "ids": ids, "eng": eng, "factory": factory}
    async with eng.begin() as c:
        await c.execute(
            text("DELETE FROM tenders_normalized WHERE source_key = :k"),
            {"k": source_key},
        )
        await c.execute(
            text("DELETE FROM tenders_raw WHERE source_key = :k"), {"k": source_key}
        )
    await eng.dispose()


async def _snapshot(world) -> dict[str, dict]:
    async with world["eng"].connect() as conn:
        res = await conn.execute(
            text(
                "SELECT n.source_uid, n.deadline_at, n.status, n.xmin::text AS xmin, "
                "r.fetched_at, r.xmin::text AS raw_xmin "
                "FROM tenders_normalized n JOIN tenders_raw r "
                "ON r.source_key = n.source_key AND r.source_uid = n.source_uid "
                "WHERE n.source_key = :k"
            ),
            {"k": world["source_key"]},
        )
        return {r["source_uid"]: dict(r) for r in res.mappings().all()}


async def _run(world, adapter, **kw):
    async def no_sleep(_):
        return None

    return await run_backlog(
        adapter,
        world["factory"],
        source_key=world["source_key"],
        clock=lambda: NOW,
        sleep=no_sleep,
        **kw,
    )


@pytest.mark.asyncio
async def test_dry_run_changes_nothing_and_counts_classes(world):
    before = await _snapshot(world)
    adapter = FakeAdapter()
    report = await _run(world, adapter, apply=False)
    after = await _snapshot(world)
    assert after == before  # neither a row, nor the fetched_at mark, nor xmin
    # only the overdue ones were polled, in the order “deadline descending”
    assert adapter.asked == ["ext", "same", "broken"]
    assert (report.selected, report.checked) == (3, 3)
    assert report.extended_future == 1
    assert (
        report.unchanged == 2
    )  # same + broken (the broken deadline dropped by the guard)
    assert report.status_changed == 0
    assert report.errors == 0
    # the delivery estimate ran on the real SQL (there are no profiles in the throwaway DB → 0)
    assert (report.est_profiles, report.est_messages_max) == (0, 0)


@pytest.mark.asyncio
async def test_apply_writes_extended_deadline_and_skips_unchanged(world):
    before = await _snapshot(world)
    report = await _run(world, FakeAdapter(), apply=True)
    after = await _snapshot(world)
    assert report.written == 1
    assert after["ext"]["deadline_at"] == EXTENDED
    # a card without changes: there was no UPDATE of tenders_normalized (the same xmin) …
    assert after["same"]["xmin"] == before["same"]["xmin"]
    # … the guard “deadline earlier than publication”: the broken deadline is not written
    assert after["broken"]["deadline_at"] == _ROWS["broken"]
    assert after["broken"]["xmin"] == before["broken"]["xmin"]
    # the check mark moves for all the checked ones, not for «open»
    assert after["same"]["fetched_at"] == NOW
    assert after["broken"]["fetched_at"] == NOW
    assert after["open"] == before["open"]


@pytest.mark.asyncio
async def test_apply_rerun_with_since_skips_checked(world):
    await _run(world, FakeAdapter(), apply=True, since=NOW)
    adapter = FakeAdapter()
    report = await _run(world, adapter, apply=True, since=NOW)
    # ext is no longer overdue (deadline 25.10), same and broken checked (fetched_at == NOW)
    assert adapter.asked == []
    assert report.selected == 0


@pytest.mark.asyncio
async def test_limit_takes_freshest_overdue_first(world):
    adapter = FakeAdapter()
    await _run(world, adapter, apply=False, limit=2)
    assert adapter.asked == ["ext", "same"]
