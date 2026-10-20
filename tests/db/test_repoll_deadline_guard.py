"""The guard “deadline earlier than publication” in the repeated poll (repoll, 08.10.2026).

The main pass zeroes the broken deadline of the portal if it is earlier than ``published_at``
(Q-29). The repeated poll must do the same, otherwise the zeroed deadline would come back as a “change” and the tender would drop out of the feed. The test goes through the real ``_repoll_apply`` against PostgreSQL: we prove by the ``xmin`` of the row, not by the list of SQL.

Requires ``TEST_RLS_DATABASE_URL`` / ``TEST_SUPERUSER_DATABASE_URL`` (a throwaway PG,
NEVER the deploy production; gate.sh brings it up itself).

The expectations — literals from the fact: the row is published 2026-10-01 09:30 UTC, the card
gives the deadline 2026-09-20 10:00 UTC (earlier than the publication → broken).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.workers.ingest import IngestWorker

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — защита срока в repoll пропускается",
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
# Not midnight: for a midnight stub the guard is deliberately off (see is_midnight).
PUBLISHED = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
BROKEN_CARD_DEADLINE = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def tender():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    source_key = f"rp-guard-{uuid.uuid4().hex[:12]}"
    tid = uuid.uuid4()
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO tenders_raw (id, country_code, source_key, source_uid, "
                "raw_payload, raw_hash, fetched_at) VALUES "
                "(:id, 'RS', :k, 'guard-1', CAST('{}' AS jsonb), 'guard-1', :fa)"
            ),
            {"id": tid, "k": source_key, "fa": NOW},
        )
        await c.execute(
            text(
                "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                "country_code, title, customer_name, status, deadline_at, "
                "published_at, first_seen_at, last_seen_at, last_changed_at) VALUES "
                "(:id, :k, 'guard-1', 'RS', 'guard-1', 'Заказчик', 'active', NULL, "
                ":pub, :fs, :fs, :fs)"
            ),
            {"id": tid, "k": source_key, "pub": PUBLISHED, "fs": PUBLISHED},
        )
    yield {"source_key": source_key, "id": tid, "eng": eng}
    async with eng.begin() as c:
        await c.execute(
            text("DELETE FROM tenders_normalized WHERE source_key = :k"),
            {"k": source_key},
        )
        await c.execute(
            text("DELETE FROM tenders_raw WHERE source_key = :k"),
            {"k": source_key},
        )
    await eng.dispose()


async def _norm_row(world) -> dict:
    async with world["eng"].connect() as conn:
        result = await conn.execute(
            text(
                "SELECT deadline_at, status, published_at, xmin::text AS xmin "
                "FROM tenders_normalized WHERE id = :id"
            ),
            {"id": world["id"]},
        )
        return dict(result.mappings().one())


@pytest.mark.asyncio
async def test_repoll_ignores_deadline_before_publication(tender):
    before = await _norm_row(tender)
    assert before["deadline_at"] is None  # the initial fact: there is no deadline

    factory = async_sessionmaker(
        tender["eng"], class_=AsyncSession, expire_on_commit=False
    )
    changed = await IngestWorker._repoll_apply(
        SimpleNamespace(_session_factory=factory),
        tender["source_key"],
        "guard-1",
        before,
        {"deadline_at": BROKEN_CARD_DEADLINE, "status": "active"},
        NOW,
    )

    assert changed is False
    after = await _norm_row(tender)
    assert after["deadline_at"] is None
    # without a change the row was not updated → and there was no trigger NOTIFY either
    assert after["xmin"] == before["xmin"]
