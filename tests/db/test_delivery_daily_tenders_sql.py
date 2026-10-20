"""The daily delivery limit counts TENDERS, not the outbox rows — on a real PG (owner 16.10).

The mock tests do not prove that PostgreSQL understands ``ANY(CAST(:pids AS uuid[]))`` with asyncpg and that ``DISTINCT`` collapses the channels. Without ``TEST_RLS_DATABASE_URL`` — a skip.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.workers.adapters.base import DeliveryJob
from src.workers.delivery import DeliveryWorker

SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not SUPER_URL,
    reason="TEST_SUPERUSER_DATABASE_URL не задан — интеграция лимита пропускается",
)


@pytest_asyncio.fixture
async def fx():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    tid, pid = uuid.uuid4(), uuid.uuid4()
    tenders = [uuid.uuid4() for _ in range(3)]
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
                "VALUES (:id, :tid, 'P', true)"
            ),
            {"id": pid, "tid": tid},
        )
        for i, t in enumerate(tenders):
            await conn.execute(
                text("INSERT INTO tenders_normalized (id, title) VALUES (:id, 'x')"),
                {"id": t},
            )
            # tenders 0 and 1 — in TWO channels (sent), tender 2 — only pending
            chans = ("telegram", "email") if i < 2 else ("email",)
            for ch in chans:
                await conn.execute(
                    text(
                        "INSERT INTO delivery_outbox (profile_id, tender_id, channel,"
                        " status, sent_at) VALUES (:p, :t, :c, :s, "
                        "CASE WHEN :s = 'sent' THEN now() END)"
                    ),
                    {"p": pid, "t": t, "c": ch, "s": "sent" if i < 2 else "pending"},
                )
    yield {"pid": str(pid), "tenders": {str(t) for t in tenders[:2]}}
    async with eng.begin() as conn:
        await conn.execute(
            text("DELETE FROM delivery_outbox WHERE profile_id=:p"), {"p": pid}
        )
        await conn.execute(
            text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
            {"ids": tenders},
        )
        await conn.execute(text("DELETE FROM profiles WHERE id=:p"), {"p": pid})
        await conn.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tid})
    await eng.dispose()


@pytest.mark.asyncio
async def test_two_channels_count_as_one_tender_each(fx):
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    maker = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        async with maker() as s:
            yield s

    worker = DeliveryWorker(adapters={}, session_factory=factory, daily_cap=30)
    job = DeliveryJob(
        outbox_id=1,
        tender_id="x",
        channel="telegram",
        requires_ai=False,
        user_channel_id=None,
        target=None,
        channel_status=None,
        title=None,
        source_url=None,
        amount_eur=None,
        deadline_at=None,
        profile_id=fx["pid"],
    )
    sent = await worker._load_profile_tenders([job])
    await eng.dispose()
    # 4 rows sent (2 tenders × 2 channels) → 2 tenders; pending is not counted
    assert sent == {fx["pid"]: fx["tenders"]}
