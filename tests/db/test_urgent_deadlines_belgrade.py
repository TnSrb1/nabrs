"""SQL predicate of "upcoming deadlines" and the tile count (issue #810) on a throwaway PG.

Without ``TEST_RLS_DATABASE_URL`` — skip (the gate spins up a throwaway PG itself).
The predicate is checked at a fixed ``now`` (18.10.2026 07:00 and 14:00 CEST):
day+7 23:00 — inside the window, day+8 00:30 — outside.

Fixture: one tenant, one profile, 60 delivered open tenders with a deadline
in 5 days (calendar ≤ 7 for any launch date).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.cabinet import feed_service
from src.domain.tz import belgrade_days_within_sql

BELGRADE = ZoneInfo("Europe/Belgrade")


def _day7_2330_utc() -> datetime:
    """A deadline "day+7, 23:30 Belgrade time" from TODAY's date.

    Always in the future and always in the calendar window ≤7 days; with 7×24 h it falls out
    before 23:30 Belgrade time. So the test catches a return to the interval regardless of
    the launch time (for the fixed code the expectation does not depend on the hour).
    """
    day = datetime.now(BELGRADE).date() + timedelta(days=7)
    return datetime.combine(day, time(23, 30), tzinfo=BELGRADE).astimezone(timezone.utc)


TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция ближайших сроков пропускается",
)

N_TENDERS = 60

_PREDICATE_SQL = text(
    f"SELECT {belgrade_days_within_sql('CAST(:dl AS timestamptz)', '7', 'CAST(:now AS timestamptz)')} AS ok"
)


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def fx():
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    eng = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    tid, pid = uuid.uuid4(), uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(N_TENDERS)]
    now = datetime.now(timezone.utc)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status) "
                    "VALUES (:id, 'RS', 'lite', 'trial')"
                ),
                {"id": tid},
            )
            await conn.execute(
                text(
                    "INSERT INTO profiles (id, tenant_id, name, is_active) "
                    "VALUES (:id, :tid, 'Профиль', true)"
                ),
                {"id": pid, "tid": tid},
            )
            for i, tender in enumerate(ids):
                await conn.execute(
                    text(
                        "INSERT INTO tenders_normalized (id, title, customer_name, "
                        "deadline_at, source_url, first_seen_at) VALUES (:id, :t, "
                        "'Заказчик', :dl, 'https://portal.rs/x', :fs)"
                    ),
                    {
                        "id": tender,
                        "t": f"t{i}",
                        # The last one — "day+7, 23:30" (the window boundary), the rest +5 days.
                        "dl": (
                            _day7_2330_utc()
                            if i == N_TENDERS - 1
                            else now + timedelta(days=5)
                        ),
                        "fs": now,
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO delivery_outbox (profile_id, tender_id, channel, "
                        "status, sent_at) VALUES (:pid, :tid, 'telegram', 'sent', now())"
                    ),
                    {"pid": pid, "tid": tender},
                )
    finally:
        await eng.dispose()

    yield {"tenant_id": str(tid), "tenders": [str(t) for t in ids]}

    eng = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text("DELETE FROM delivery_outbox WHERE profile_id = :p"), {"p": pid}
            )
            await conn.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": ids},
            )
            await conn.execute(text("DELETE FROM profiles WHERE id = :p"), {"p": pid})
            await conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
    finally:
        await eng.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now_utc", "deadline_utc", "expected"),
    [
        # 18.10 07:00 CEST — deadline 25.10 23:00 CET = day+7: in the window.
        ("2026-10-18T05:00:00Z", "2026-10-25T22:00:00Z", True),
        # 18.10 14:00 CEST — the same deadline: in the window.
        ("2026-10-18T12:00:00Z", "2026-10-25T22:00:00Z", True),
        # 18.10 07:00 CEST — deadline 26.10 00:30 CET = day+8: outside the window.
        ("2026-10-18T05:00:00Z", "2026-10-25T23:30:00Z", False),
    ],
)
async def test_belgrade_day_window_sql_boundary(
    sessionmaker_, now_utc, deadline_utc, expected
):
    async with sessionmaker_() as db:
        ok = (
            await db.execute(
                _PREDICATE_SQL,
                {
                    "now": datetime.fromisoformat(now_utc),
                    "dl": datetime.fromisoformat(deadline_utc),
                },
            )
        ).scalar_one()
    assert ok is expected


@pytest.mark.asyncio
async def test_urgent_count_is_not_capped_by_calendar_limit(sessionmaker_, fx):
    async with sessionmaker_() as db:
        count = await feed_service.count_urgent_deadlines(db, fx["tenant_id"], [])
        items = await feed_service.get_calendar_deadlines(db, fx["tenant_id"])
    assert count == 60
    assert len(items) == 60


@pytest.mark.asyncio
async def test_urgent_count_dedupes_work_stage_tender(sessionmaker_, fx):
    # A tender in "follow" is counted ONCE (as an action event), not twice.
    watched = SimpleNamespace(
        tender_id=fx["tenders"][0],
        profile_id=None,
        title="t0",
        customer_name="Заказчик",
        deadline_at=datetime.now(timezone.utc) + timedelta(days=5),
        source_key=None,
        tender_url=None,
        status="watching",
        note=None,
    )
    async with sessionmaker_() as db:
        count = await feed_service.count_urgent_deadlines(
            db, fx["tenant_id"], [watched]
        )
    assert count == 60
