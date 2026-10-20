"""Integration: “The source statistics” count only the live tenders (owner 10.10.2026).

The predicate lives in the SQL (`stats_service._SYSTEM_STATS_SQL`). The archive — tenders without ``published_at`` and published a year+ before we saw them (the import of 2019–2020 in production) — does not get into the counter. The predicate is not proved on mocks (`.claude/rules/tests-known-defects.md`), therefore the test runs the real ``get_system_stats`` against a throwaway PostgreSQL under the application role.

Three rows: a live one; one without ``published_at``; one published 2 years before we saw it. The expectation as literals: ``all_time == 1``, ``since`` = the date of the live one.

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

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — предикат «живой тендер» пропускается",
)

_FIRST_SEEN = datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc)
# (name, published_at, first_seen_at)
_CASES = {
    "live": (datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc), _FIRST_SEEN),
    # Live, but before the start of the regular collection (02.06): it goes into neither the counters nor the archive.
    "pre_regular": (datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc), _FIRST_SEEN),
    "no_published": (None, _FIRST_SEEN),
    "archive_2y": (datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc), _FIRST_SEEN),
}


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    ids = {name: uuid.uuid4() for name in _CASES}
    async with eng.begin() as c:
        # The table must be empty: otherwise the literal all_time/since lie.
        left = (
            await c.execute(text("SELECT count(*) FROM tenders_normalized"))
        ).scalar()
        assert (
            left == 0
        ), f"tenders_normalized is not empty ({left} rows) — the test is not isolated"
        for name, (published, first_seen) in _CASES.items():
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, title, customer_name, "
                    "published_at, first_seen_at, source_url) VALUES "
                    "(:id, :title, 'Заказчик', :pub, :fs, 'https://portal.rs/x')"
                ),
                {"id": ids[name], "title": name, "pub": published, "fs": first_seen},
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
async def test_system_stats_counts_only_live_tenders(world, app_session):
    async with app_session() as db:
        stats = await stats_service.get_system_stats(db)
    assert stats.all_time == 1
    assert stats.since == date(2026, 6, 2)
    # The archive (without published_at + published 2 years before first_seen) — a separate row.
    assert stats.archive == 2
