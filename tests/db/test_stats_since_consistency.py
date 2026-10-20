"""Integration: the cabinet dashboard and ``/statistika`` name ONE start date of the data.

Both surfaces take the start from one constant ``REGULAR_SINCE`` (02.06.2026, Europe/Belgrade; ``src/domain/live_tender.py``) and one predicate of the population. Earlier it was ``min(published_at)`` of the live tenders (30.05, the tail of the initial load); before that the showcase took ``min(first_seen_at)`` and showed “from 04.06” against “from 30.05” on the dashboard (the owner’s diagnosis, 10.10.2026).

The fixture: two live tenders with publication 30.05 and 02.06, but seen on 04.06 and 03.06 (the first appearance ≠ the publication), plus an archive row of 2019, seen on 01.06. The expectation as a literal: both surfaces give 02.06.2026 (REGULAR_SINCE), the tender of 30.05 does not count.

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.cabinet import stats_service
from src.api.public import stats_page
from src.domain.tz import to_local

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — дата начала данных пропускается",
)

COUNTRY = "XQ"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# name: (published_at, first_seen_at)
_CASES = {
    "live_early": (_ts("2026-05-30T12:00"), _ts("2026-06-04T09:00")),
    "live_later": (_ts("2026-06-02T08:00"), _ts("2026-06-03T09:00")),
    "archive_2019": (_ts("2019-03-01T08:00"), _ts("2026-06-01T08:00")),
}


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    ids = {name: uuid.uuid4() for name in _CASES}
    async with eng.begin() as c:
        # get_system_stats counts over the whole table: foreign rows would break the literal.
        left = (
            await c.execute(text("SELECT count(*) FROM tenders_normalized"))
        ).scalar()
        assert (
            left == 0
        ), f"tenders_normalized is not empty ({left} rows) — the test is not isolated"
        for name, (published, first_seen) in _CASES.items():
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, title, published_at, first_seen_at, source_url) "
                    "VALUES (:id, 'since-test', :uid, :cc, :title, :pub, :fs, "
                    "'https://portal.rs/x')"
                ),
                {
                    "id": ids[name],
                    "uid": f"since-{name}-{uuid.uuid4().hex[:8]}",
                    "cc": COUNTRY,
                    "title": name,
                    "pub": published,
                    "fs": first_seen,
                },
            )

    yield ids

    async with eng.begin() as c:
        await c.execute(
            text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
            {"ids": list(ids.values())},
        )
    await eng.dispose()


@pytest_asyncio.fixture
async def app_session():
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


@pytest.mark.asyncio
async def test_dashboard_and_statistika_share_start_date(world, app_session):
    async with app_session() as db:
        dashboard = await stats_service.get_system_stats(db)
    async with app_session() as db:
        page = await stats_page.compute_stats(db, country=COUNTRY)

    assert page is not None
    # Dashboard: “N tenders from 30.05.2026”.
    assert dashboard.since == date(2026, 6, 2)
    # Showcase: «Podaci od 02.06.2026» — the same date in Belgrade.
    assert to_local(page.period_start).date() == date(2026, 6, 2)
    # 30.05 (the tail of the initial load) is not in the population.
    assert page.tenders_total == 1
    assert dashboard.all_time == 1
