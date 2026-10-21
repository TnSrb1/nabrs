"""Integration test of the real SQL predicates of /app/feed (owner 22.09).

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, see ``tests/db/test_rls_isolation.py``
and the memo in the docs — NEVER the deploy production). Without the variable — a skip (the unit environment, the CI without PG).

Why a separate file from ``tests/api/test_feed_service.py``: those tests mock the execution of the SQL, while the project rules explicitly require checking the PREDICATE (the filter
``?actual=`` and the sort ``?sort=deadline``) by a DB query — a mock may accidentally match by the text, but will not prove that PostgreSQL really understands
``CAST(:actual_only AS boolean) = false OR ...`` and ``ORDER BY deadline_at ASC
NULLS LAST``.

The fixture: one tenant, one profile, three tenders — the deadline yesterday (expired),
tomorrow (current), NULL (unknown, seen an hour ago — current by
the 30-day rule) — all delivered
(``delivery_outbox.status='sent'``).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.cabinet import feed_service

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция /app/feed пропускается",
)


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def engine():
    """NullPool: an asyncpg connection must not outlive its own event loop (see
    ``tests/db/test_rls_isolation.py`` — the same defect was already caught here)."""
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def feed_fixture(engine):
    """A tenant + a profile + four tenders (yesterday/tomorrow/in 5 days/NULL), all delivered.

    The setup/cleanup — under the superuser (TEST_SUPERUSER_DATABASE_URL,
    by default the same URL), so as to bypass the RLS on insertion.
    """
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    sup_engine = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    tid = uuid.uuid4()
    pid = uuid.uuid4()
    t_expired = uuid.uuid4()
    t_future = uuid.uuid4()
    t_later = uuid.uuid4()
    t_null = uuid.uuid4()
    now = datetime.now(timezone.utc)
    try:
        async with sup_engine.begin() as conn:
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
            # ``sent_at`` is spread so that the order “by the date of receipt”
            # (sent_at DESC) is the REVERSE of the order “by the submission deadline”: if
            # ``sort="deadline"`` is silently ignored, the order test
            # turns red, rather than matching by chance on equal ``sent_at``.
            for tender_id, title, deadline, sent_at in (
                (
                    t_expired,
                    "Истёкший",
                    now - timedelta(days=1),
                    now - timedelta(hours=3),
                ),
                (
                    t_future,
                    "Будущий",
                    now + timedelta(days=1),
                    now - timedelta(hours=2),
                ),
                # A second CURRENT tender with a date later than that of “Future”:
                # without it ASC and DESC NULLS LAST give the same order [future, null]
                # and the substitution of the sort direction would not turn the test red (review #587).
                (
                    t_later,
                    "Позднее",
                    now + timedelta(days=5),
                    now - timedelta(minutes=90),
                ),
                (t_null, "Без срока", None, now - timedelta(hours=1)),
            ):
                # first_seen_at — as for any ingest row: without it the tender
                # “No deadline” does not prove freshness, and by the 30-day rule
                # (owner 06.10.2026, src/domain/actuality.py) it is not current.
                await conn.execute(
                    text(
                        "INSERT INTO tenders_normalized "
                        "(id, title, customer_name, deadline_at, source_url, "
                        "first_seen_at) VALUES (:id, :title, 'Заказчик', :deadline, "
                        "'https://portal.rs/x', :first_seen)"
                    ),
                    {
                        "id": tender_id,
                        "title": title,
                        "deadline": deadline,
                        "first_seen": sent_at,
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO delivery_outbox "
                        "(profile_id, tender_id, channel, status, sent_at) "
                        "VALUES (:pid, :tid, 'email', 'sent', :sent_at)"
                    ),
                    {"pid": pid, "tid": tender_id, "sent_at": sent_at},
                )
    finally:
        await sup_engine.dispose()

    yield {
        "tenant_id": str(tid),
        "profile_id": str(pid),
        "expired": str(t_expired),
        "future": str(t_future),
        "later": str(t_later),
        "null": str(t_null),
    }

    sup_engine2 = create_async_engine(superuser_url, future=True)
    try:
        async with sup_engine2.begin() as conn:
            await conn.execute(
                text("DELETE FROM delivery_outbox WHERE profile_id = :pid"),
                {"pid": pid},
            )
            await conn.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": [t_expired, t_future, t_later, t_null]},
            )
            await conn.execute(
                text("DELETE FROM profiles WHERE id = :pid"), {"pid": pid}
            )
            await conn.execute(
                text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid}
            )
    finally:
        await sup_engine2.dispose()


@pytest.mark.asyncio
async def test_actual_only_true_drops_expired_on_real_db(sessionmaker_, feed_fixture):
    """‼️ Counter-fact «actual=1»: the expired tender does NOT come, the other two do."""
    async with sessionmaker_() as db:
        items = await feed_service.get_feed_page(
            db, feed_fixture["tenant_id"], page=1, page_size=10, actual_only=True
        )
        ids = {it.tender_id for it in items}
        assert ids == {
            feed_fixture["future"],
            feed_fixture["later"],
            feed_fixture["null"],
        }
        assert feed_fixture["expired"] not in ids


@pytest.mark.asyncio
async def test_actual_only_false_includes_expired_on_real_db(
    sessionmaker_, feed_fixture
):
    """‼️ Counter-fact «actual=0»: the expired tender APPEARS on a par with the others."""
    async with sessionmaker_() as db:
        items = await feed_service.get_feed_page(
            db, feed_fixture["tenant_id"], page=1, page_size=10, actual_only=False
        )
        ids = {it.tender_id for it in items}
        assert ids == {
            feed_fixture["expired"],
            feed_fixture["future"],
            feed_fixture["later"],
            feed_fixture["null"],
        }


@pytest.mark.asyncio
async def test_count_matches_actual_only_toggle_on_real_db(sessionmaker_, feed_fixture):
    """‼️ COUNT must count with the SAME predicate — the total matches the page."""
    async with sessionmaker_() as db:
        total_on = await feed_service.count_feed_total(
            db, feed_fixture["tenant_id"], actual_only=True
        )
        total_off = await feed_service.count_feed_total(
            db, feed_fixture["tenant_id"], actual_only=False
        )
        assert total_on == 3
        assert total_off == 4


@pytest.mark.asyncio
async def test_sort_deadline_orders_ascending_nulls_last_on_real_db(
    sessionmaker_, feed_fixture
):
    """``sort="deadline"``: the nearest deadline first, NULL — last (a real PG)."""
    async with sessionmaker_() as db:
        items = await feed_service.get_feed_page(
            db,
            feed_fixture["tenant_id"],
            page=1,
            page_size=10,
            sort="deadline",
            actual_only=False,
        )
        assert [it.tender_id for it in items] == [
            feed_fixture["expired"],
            feed_fixture["future"],
            feed_fixture["later"],
            feed_fixture["null"],
        ]


@pytest.mark.asyncio
async def test_sort_date_is_reverse_of_deadline_in_fixture_on_real_db(
    sessionmaker_, feed_fixture
):
    """A control of the fixture: “by the date of receipt” gives the REVERSE order — so
    the test above distinguishes the two sorts, and does not pass on a coincidence."""
    async with sessionmaker_() as db:
        items = await feed_service.get_feed_page(
            db,
            feed_fixture["tenant_id"],
            page=1,
            page_size=10,
            sort="date",
            actual_only=False,
        )
        assert [it.tender_id for it in items] == [
            feed_fixture["null"],
            feed_fixture["later"],
            feed_fixture["future"],
            feed_fixture["expired"],
        ]


@pytest.mark.asyncio
async def test_dashboard_teaser_ignores_actual_toggle_on_real_db(
    sessionmaker_, feed_fixture
):
    """The teaser of the dashboard (get_feed_items) is always actual_only=true — Step 3 is untouched."""
    async with sessionmaker_() as db:
        items = await feed_service.get_feed_items(
            db, feed_fixture["tenant_id"], limit=10
        )
        ids = {it.tender_id for it in items}
        assert feed_fixture["expired"] not in ids
        assert ids == {
            feed_fixture["future"],
            feed_fixture["later"],
            feed_fixture["null"],
        }


@pytest.mark.asyncio
async def test_dashboard_teaser_deadline_sort_on_real_db(sessionmaker_, feed_fixture):
    """The teaser “Deadline soon”: among the CURRENT ones the nearest deadline first, NULL last
    — the order is the REVERSE of the “new” one (see the control of the fixture above).
    """
    async with sessionmaker_() as db:
        by_deadline = await feed_service.get_feed_items(
            db, feed_fixture["tenant_id"], limit=10, sort="deadline"
        )
        by_date = await feed_service.get_feed_items(
            db, feed_fixture["tenant_id"], limit=10
        )
    assert [it.tender_id for it in by_deadline] == [
        feed_fixture["future"],
        feed_fixture["later"],
        feed_fixture["null"],
    ]
    assert [it.tender_id for it in by_date] == [
        feed_fixture["null"],
        feed_fixture["later"],
        feed_fixture["future"],
    ]


@pytest.mark.asyncio
async def test_rls_still_isolates_tenant_under_new_predicate(
    sessionmaker_, feed_fixture
):
    """‼️ A new bind parameter did not open a hole in the RLS: a foreign tenant sees 0 rows."""
    async with sessionmaker_() as db:
        items = await feed_service.get_feed_page(
            db, str(uuid.uuid4()), page=1, page_size=10, actual_only=False
        )
        assert items == []
