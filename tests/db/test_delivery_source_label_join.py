"""Integration: ``delivery._LOAD_JOBS_SQL`` gives ``source_label`` via a LEFT JOIN.

The owner’s decision 30.09: the platform’s name — in every alert. The predicate lives in the SQL,
therefore the test runs the REAL ``_LOAD_JOBS_SQL`` against a throwaway PostgreSQL
(it cannot be proved on mocks). Two facts:

* the source is in ``sources`` with ``display_name`` → ``source_label`` = this name;
* there is no row in ``sources`` (or the name is empty) → ``source_label`` IS NULL, and the tender
  is NOT lost (it would have been an INNER JOIN — the row would have dropped out of the output).

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
The delivery in production runs under the superuser — here ``TEST_SUPERUSER_DATABASE_URL``.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.workers.delivery import _LOAD_JOBS_SQL

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция LEFT JOIN sources пропускается",
)


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    tid, uid, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    created = {"tenders": [], "outbox": [], "sources": []}
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO tenants (id, country_code, tier, status) "
                "VALUES (:id, 'RS', 'lite', 'trial')"
            ),
            {"id": tid},
        )
        await c.execute(
            text(
                "INSERT INTO users (id, tenant_id, locale, role) "
                "VALUES (:id, :tid, 'ru', 'admin')"
            ),
            {"id": uid, "tid": tid},
        )
        await c.execute(
            text(
                "INSERT INTO user_channels (id, user_id, channel, external_id, status) "
                "VALUES (:id, :uid, 'telegram', '424242', 'active')"
            ),
            {"id": uuid.uuid4(), "uid": uid},
        )
        await c.execute(
            text(
                "INSERT INTO profiles (id, tenant_id, country_code, is_active, "
                "match_all, delivery_channels, ai_analysis_level) VALUES "
                "(:id, :tid, 'RS', true, true, ARRAY['telegram'], 'none')"
            ),
            {"id": pid, "tid": tid},
        )

    class W:
        engine = eng

        async def tender_with_outbox(
            self, *, source_row: str | None, display_name: str | None = None
        ) -> int:
            """A tender + an outbox row; ``source_row`` — the key in ``sources`` or None."""
            key = f"w2tails-{suffix}-{len(created['tenders'])}"
            tender_id = uuid.uuid4()
            async with eng.begin() as c:
                if source_row is not None:
                    key = f"{key}-{source_row}"
                    await c.execute(
                        text(
                            "INSERT INTO sources (source_key, enabled, display_name, "
                            "country_codes) VALUES (:k, true, :n, "
                            "ARRAY['RS']::char(2)[])"
                        ),
                        {"k": key, "n": display_name},
                    )
                    created["sources"].append(key)
                await c.execute(
                    text(
                        "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                        "country_code, status, first_seen_at, title_normalized) "
                        "VALUES (:id, :k, :uid, 'RS', 'active', now(), 'nabavka')"
                    ),
                    {"id": tender_id, "k": key, "uid": tender_id.hex},
                )
                created["tenders"].append(tender_id)
                oid = (
                    await c.execute(
                        text(
                            "INSERT INTO delivery_outbox (profile_id, tender_id, "
                            "channel, status, requires_ai) VALUES (:p, :t, "
                            "'telegram', 'pending', false) RETURNING id"
                        ),
                        {"p": pid, "t": tender_id},
                    )
                ).scalar_one()
            created["outbox"].append(oid)
            return int(oid)

        async def load(self, outbox_id: int):
            async with eng.connect() as c:
                return list(await c.execute(_LOAD_JOBS_SQL, {"ids": [outbox_id]}))

    try:
        yield W()
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM delivery_outbox WHERE id = ANY(:o)"),
                {"o": created["outbox"]},
            )
            await c.execute(text("DELETE FROM profiles WHERE id = :p"), {"p": pid})
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:t)"),
                {"t": created["tenders"]},
            )
            await c.execute(
                text("DELETE FROM sources WHERE source_key = ANY(:k)"),
                {"k": created["sources"]},
            )
            await c.execute(
                text("DELETE FROM user_channels WHERE user_id = :u"), {"u": uid}
            )
            await c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await eng.dispose()


@pytest.mark.asyncio
async def test_load_jobs_sql_returns_display_name_for_registered_source(world):
    oid = await world.tender_with_outbox(
        source_row="reg", display_name="Portal javnih nabavki (UJN)"
    )
    rows = await world.load(oid)
    assert len(rows) == 1
    assert rows[0].source_label == "Portal javnih nabavki (UJN)"


@pytest.mark.asyncio
async def test_load_jobs_sql_keeps_tender_without_sources_row_label_null(world):
    """No row in ``sources`` → the tender IS in the output, ``source_label`` NULL."""
    oid = await world.tender_with_outbox(source_row=None)
    rows = await world.load(oid)
    assert len(rows) == 1
    assert rows[0].source_label is None


@pytest.mark.asyncio
async def test_load_jobs_sql_empty_display_name_is_null(world):
    oid = await world.tender_with_outbox(source_row="empty", display_name="")
    rows = await world.load(oid)
    assert len(rows) == 1
    assert rows[0].source_label is None
