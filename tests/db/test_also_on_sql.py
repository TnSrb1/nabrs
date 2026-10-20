"""Integration: “the same tender on other sources” — the number in EVERY render selection.

The mark is drawn in the feed/teaser of the cabinet, the cabinet card, the card and the digest of the delivery,
on the public card; the number N is counted by one SQL expression
(:func:`src.dedup.also_on.also_on_count_sql`), embedded into the selection of each point.
On mocks the expression is not checked — the test runs the REAL selections against a
throwaway PostgreSQL.

The cluster: source A — two rows (A1 ``dup``, A2 ``probable``), source B — one
(``dup``); plus a row C outside the cluster. N counts the SourceS, not the rows:
B has two neighbouring rows, but one source → 1 (counting rows would give 2).

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.cabinet.feed_service import _FEED_PAGE_SQL
from src.api.cabinet.tender_ai_service import _TENDER_META_SQL
from src.api.repo import _ALSO_ON_COUNT_SQL
from src.workers.delivery import _LOAD_JOBS_SQL

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция пометки also_on пропускается",
)

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    tid, uid, pid, canon = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sfx = uuid.uuid4().hex[:6]
    key_a, key_b, key_c = f"ao-a-{sfx}", f"ao-b-{sfx}", f"ao-c-{sfx}"
    rows = {
        "a1": (key_a, canon, "dup"),
        "a2": (key_a, canon, "probable"),
        "b": (key_b, canon, "dup"),
        "c": (key_c, None, None),
    }
    ids = {name: uuid.uuid4() for name in rows}
    outbox: dict[str, int] = {}
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
                "VALUES (:id, :uid, 'telegram', '424243', 'active')"
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
        await c.execute(
            text("INSERT INTO canonical_tenders (id) VALUES (:c)"), {"c": canon}
        )
        for name, (key, cluster, conf) in rows.items():
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, status, first_seen_at, deadline_at, "
                    "title_normalized, canonical_tender_id, canonical_confidence) "
                    "VALUES (:id, :k, :uid, 'RS', 'active', now(), :dl, 'nabavka', "
                    ":c, :conf)"
                ),
                {
                    "id": ids[name],
                    "k": key,
                    "uid": ids[name].hex,
                    "dl": NOW + timedelta(days=10),
                    "c": cluster,
                    "conf": conf,
                },
            )
            outbox[name] = (
                await c.execute(
                    text(
                        "INSERT INTO delivery_outbox (profile_id, tender_id, channel, "
                        "status, requires_ai, sent_at) VALUES (:p, :t, 'telegram', "
                        "'sent', false, now()) RETURNING id"
                    ),
                    {"p": pid, "t": ids[name]},
                )
            ).scalar_one()

    class W:
        engine = eng
        tenant_id = tid

    w = W()
    w.ids, w.outbox = ids, outbox
    try:
        yield w
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM delivery_outbox WHERE profile_id = :p"), {"p": pid}
            )
            await c.execute(text("DELETE FROM profiles WHERE id = :p"), {"p": pid})
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:t)"),
                {"t": list(ids.values())},
            )
            await c.execute(
                text("DELETE FROM canonical_tenders WHERE id = :c"), {"c": canon}
            )
            await c.execute(
                text("DELETE FROM user_channels WHERE user_id = :u"), {"u": uid}
            )
            await c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await eng.dispose()


# The expectations — literals from the device of the cluster (the module docstring).
EXPECTED = {"a1": (1, "dup"), "a2": (1, "probable"), "b": (1, "dup"), "c": (0, None)}


@pytest.mark.asyncio
async def test_delivery_load_jobs_counts_other_platforms(world):
    async with world.engine.connect() as c:
        got = {
            name: (row.also_on_n, row.canonical_confidence)
            for name in EXPECTED
            for row in await c.execute(_LOAD_JOBS_SQL, {"ids": [world.outbox[name]]})
        }
    assert got == EXPECTED


@pytest.mark.asyncio
async def test_cabinet_feed_counts_other_platforms_in_one_query(world):
    async with world.engine.connect() as c:
        rows = list(
            await c.execute(
                _FEED_PAGE_SQL,
                {
                    "tid": str(world.tenant_id),
                    "limit": 50,
                    "offset": 0,
                    "profile_id": None,
                    "actual_only": True,
                },
            )
        )
    by_id = {r.tender_id: (r.also_on_n, r.canonical_confidence) for r in rows}
    assert by_id == {str(world.ids[n]): exp for n, exp in EXPECTED.items()}


@pytest.mark.asyncio
async def test_cabinet_card_meta_counts_other_platforms(world):
    async with world.engine.connect() as c:
        got = {
            name: (row.also_on_n, row.canonical_confidence)
            for name in EXPECTED
            for row in await c.execute(
                _TENDER_META_SQL, {"tender_id": str(world.ids[name])}
            )
        }
    assert got == EXPECTED


@pytest.mark.asyncio
async def test_public_card_counts_platforms_not_rows(world):
    async with world.engine.connect() as c:
        got = {
            name: (
                await c.execute(_ALSO_ON_COUNT_SQL, {"tender_id": str(world.ids[name])})
            ).scalar_one()
            for name in EXPECTED
        }
    # B: two neighbouring rows (A1, A2), one source.
    assert got == {"a1": 1, "a2": 1, "b": 1, "c": 0}
