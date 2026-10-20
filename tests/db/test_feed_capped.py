"""The feed shows ALL the matches, incl. those cut by the daily limit (owner 16.10).

An integration on a throwaway PG (``TEST_RLS_DATABASE_URL``, see ``test_feed_actual_and_
deadline_sort.py``); without the variable — a skip. An outbox row is written for EACH channel,
therefore a tender — two rows; the feed must show it ONCE.

The fixture (one tenant, one profile, the deadline of all — in 5 days):
* ``both_capped``  — telegram + email, both ``suppressed``  → 1 card, capped;
* ``mixed``        — telegram ``sent`` + email ``suppressed`` → 1 card, NOT capped;
* ``queued``       — email ``pending``                       → visible, NOT capped;
* ``failed``       — email ``failed``                        → NOT visible (a failure, not a match).
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
    reason="TEST_RLS_DATABASE_URL не задан — интеграция ленты пропускается",
)

_ROWS = (
    ("both_capped", (("telegram", "suppressed"), ("email", "suppressed"))),
    ("mixed", (("telegram", "sent"), ("email", "suppressed"))),
    ("queued", (("email", "pending"),)),
    ("failed", (("email", "failed"),)),
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
    ids = {name: uuid.uuid4() for name, _ in _ROWS}
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
            for name, chans in _ROWS:
                await conn.execute(
                    text(
                        "INSERT INTO tenders_normalized (id, title, customer_name, "
                        "deadline_at, source_url, first_seen_at) VALUES (:id, :t, "
                        "'Заказчик', :dl, 'https://portal.rs/x', :fs)"
                    ),
                    {
                        "id": ids[name],
                        "t": name,
                        "dl": now + timedelta(days=5),
                        "fs": now,
                    },
                )
                for channel, status in chans:
                    await conn.execute(
                        text(
                            "INSERT INTO delivery_outbox (profile_id, tender_id, "
                            "channel, status, sent_at, failed_at) VALUES (:pid, "
                            ":tid, :ch, :st, "
                            "CASE WHEN :st = 'sent' THEN now() END, "
                            "CASE WHEN :st IN ('suppressed','failed') THEN now() END)"
                        ),
                        {"pid": pid, "tid": ids[name], "ch": channel, "st": status},
                    )
    finally:
        await eng.dispose()

    yield {"tenant_id": str(tid), **{k: str(v) for k, v in ids.items()}}

    eng = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text("DELETE FROM delivery_outbox WHERE profile_id = :p"), {"p": pid}
            )
            await conn.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": list(ids.values())},
            )
            await conn.execute(text("DELETE FROM profiles WHERE id = :p"), {"p": pid})
            await conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_feed_shows_each_matched_tender_once_with_capped_flag(sessionmaker_, fx):
    async with sessionmaker_() as db:
        items = await feed_service.get_feed_page(
            db, fx["tenant_id"], page=1, page_size=20
        )
    by_id = {it.tender_id: it for it in items}
    assert len(items) == 3  # one card per tender, not per channel
    assert set(by_id) == {fx["both_capped"], fx["mixed"], fx["queued"]}
    assert fx["failed"] not in by_id
    assert by_id[fx["both_capped"]].capped is True
    assert by_id[fx["mixed"]].capped is False  # at least one channel sent it
    assert by_id[fx["queued"]].capped is False


@pytest.mark.asyncio
async def test_feed_count_and_teaser_agree_with_page(sessionmaker_, fx):
    async with sessionmaker_() as db:
        total = await feed_service.count_feed_total(db, fx["tenant_id"])
        teaser = await feed_service.get_feed_items(db, fx["tenant_id"], limit=10)
    assert total == 3
    assert {it.tender_id for it in teaser} == {
        fx["both_capped"],
        fx["mixed"],
        fx["queued"],
    }


@pytest.mark.asyncio
async def test_hot_deadlines_include_capped_tender(sessionmaker_, fx):
    async with sessionmaker_() as db:
        hot = await feed_service.get_hot_deadlines(db, fx["tenant_id"], window_days=7)
    assert fx["both_capped"] in {h.tender_id for h in hot}
    assert fx["failed"] not in {h.tender_id for h in hot}
