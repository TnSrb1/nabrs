"""Integration: ``delivery._LOAD_JOBS_SQL`` does not give the delivery to a deleted user.

10.10: ``users.deleted_at`` closed the login (the cabinet gate), but a live row of
``user_channels`` of the deleted subscriber still got into the delivery selection.
The test runs the REAL SQL against a throwaway PostgreSQL: two users of one
tenant, both with an active telegram channel; the deleted one must not give a row, the live one
— must (a control that the filter did not wipe the tenant entirely).

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
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
    reason="TEST_RLS_DATABASE_URL не задан — интеграция deleted_at в доставке пропускается",
)


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    tid, pid = uuid.uuid4(), uuid.uuid4()
    live_uid, dead_uid = uuid.uuid4(), uuid.uuid4()
    live_uc, dead_uc = uuid.uuid4(), uuid.uuid4()
    tender_id = uuid.uuid4()
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
                "INSERT INTO users (id, tenant_id, locale, role, deleted_at) VALUES "
                "(:l, :tid, 'ru', 'admin', NULL), "
                "(:d, :tid, 'ru', 'user', now())"
            ),
            {"l": live_uid, "d": dead_uid, "tid": tid},
        )
        await c.execute(
            text(
                # DISTINCT ON (o.id) gives ONE row per job, choosing by
                # linked_at DESC: we make the deleted channel the freshest, so that without the
                # filter it surely wins (otherwise the test would pass by chance).
                "INSERT INTO user_channels (id, user_id, channel, external_id, status, "
                "linked_at) VALUES "
                "(:lc, :lu, 'telegram', '111', 'active', now() - interval '1 day'), "
                "(:dc, :du, 'telegram', '222', 'active', now())"
            ),
            {"lc": live_uc, "lu": live_uid, "dc": dead_uc, "du": dead_uid},
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
            text(
                "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                "country_code, status, first_seen_at, title_normalized) "
                "VALUES (:id, 'deleted-user-test', :uid, 'RS', 'active', now(), 'x')"
            ),
            {"id": tender_id, "uid": tender_id.hex},
        )
        oid = (
            await c.execute(
                text(
                    "INSERT INTO delivery_outbox (profile_id, tender_id, channel, "
                    "status, requires_ai) VALUES (:p, :t, 'telegram', 'pending', "
                    "false) RETURNING id"
                ),
                {"p": pid, "t": tender_id},
            )
        ).scalar_one()

    try:
        yield {"oid": int(oid), "live_uc": live_uc, "dead_uc": dead_uc, "eng": eng}
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM delivery_outbox WHERE id = :o"), {"o": oid}
            )
            await c.execute(text("DELETE FROM profiles WHERE id = :p"), {"p": pid})
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = :t"),
                {"t": tender_id},
            )
            await c.execute(
                text("DELETE FROM user_channels WHERE user_id = ANY(:u)"),
                {"u": [live_uid, dead_uid]},
            )
            await c.execute(
                text("DELETE FROM users WHERE id = ANY(:u)"),
                {"u": [live_uid, dead_uid]},
            )
            await c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await eng.dispose()


@pytest.mark.asyncio
async def test_load_jobs_skips_deleted_user_channel(world):
    async with world["eng"].connect() as c:
        rows = list(await c.execute(_LOAD_JOBS_SQL, {"ids": [world["oid"]]}))
    channels = [r.user_channel_id for r in rows]
    assert world["dead_uc"] not in channels
    assert channels == [world["live_uc"]]
