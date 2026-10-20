"""Integration: after the access expires the Telegram reminders and the summary stay silent.

The owner’s decision 13.10.2026 (the showcase/notifications by the fact of the code, item 6): the workers ``reminders`` (the deadline reminders) and ``summary`` (the daily summary) go privileged, without ``SET tenant``, and earlier did not look at the axis ``tenants.access_until``. An expired tenant received messages, although the matching mailing for it had already been stopped. The predicate is the same as in ``matching`` (``access_active_sql``): ``access_until IS NULL OR access_until > now()``. NULL = permanent, such tenants send as before.

We run the REAL ``_CANDIDATES_SQL`` and ``_TENANTS_SQL`` against a throwaway PostgreSQL: the predicate is not proved on mocks (``tests-known-defects.md``).
Requires ``TEST_RLS_DATABASE_URL`` / ``TEST_SUPERUSER_DATABASE_URL``; gate.sh brings the PG up itself.
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

from src.workers.reminders import _CANDIDATES_SQL, _WATCH_STATUSES
from src.workers.summary import _TENANTS_SQL

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция гейта доступа пропускается",
)

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def world():
    """Three tenants with a live telegram and a tracked tender: expired, NULL, future."""
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    tender_id = uuid.uuid4()
    tenants = {
        "expired": NOW - timedelta(days=1),
        "forever": None,
        "future": NOW + timedelta(days=10),
    }
    ids: dict[str, uuid.UUID] = {}
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                "country_code, title, customer_name, status, deadline_at, "
                "published_at, first_seen_at, last_seen_at, last_changed_at) VALUES "
                "(:id, 'gate-test', :uid, 'RS', 'gate-test', 'Заказчик', 'active', "
                ":dl, :fs, :fs, :fs, :fs)"
            ),
            {
                "id": tender_id,
                "uid": str(tender_id),
                "dl": NOW + timedelta(days=5),
                "fs": NOW - timedelta(days=2),
            },
        )
        for name, access_until in tenants.items():
            tid, uid, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            ids[name] = tid
            await c.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status, access_until) "
                    "VALUES (:id, 'RS', 'lite', 'active', :au)"
                ),
                {"id": tid, "au": access_until},
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
                    "VALUES (:id, :uid, 'telegram', :ext, 'active')"
                ),
                {"id": cid, "uid": uid, "ext": f"gate-{name}-{uid.hex[:8]}"},
            )
            await c.execute(
                text(
                    "INSERT INTO tender_user_status (tenant_id, tender_id, status) "
                    "VALUES (:tid, :tender, 'active')"
                ),
                {"tid": tid, "tender": tender_id},
            )
    yield {"ids": ids, "eng": eng}

    async with eng.begin() as c:
        for tid in ids.values():
            await c.execute(
                text("DELETE FROM tender_user_status WHERE tenant_id = :tid"),
                {"tid": tid},
            )
            await c.execute(
                text(
                    "DELETE FROM user_channels WHERE user_id IN "
                    "(SELECT id FROM users WHERE tenant_id = :tid)"
                ),
                {"tid": tid},
            )
            await c.execute(
                text("DELETE FROM users WHERE tenant_id = :tid"), {"tid": tid}
            )
            await c.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid})
        await c.execute(
            text("DELETE FROM tenders_normalized WHERE source_key = 'gate-test'")
        )
    await eng.dispose()


async def _tenant_ids(world, sql, params) -> set[uuid.UUID]:
    async with world["eng"].connect() as conn:
        rows = (await conn.execute(sql, params)).mappings().all()
    return {r["tenant_id"] for r in rows}


@pytest.mark.asyncio
async def test_reminders_skip_expired_tenant_and_send_to_null_and_future(world):
    got = await _tenant_ids(
        world,
        _CANDIDATES_SQL,
        {
            "channel": "telegram",
            "statuses": list(_WATCH_STATUSES),
            "default_tz": "Europe/Belgrade",
        },
    )
    ids = world["ids"]
    assert ids["expired"] not in got
    assert ids["forever"] in got
    assert ids["future"] in got


@pytest.mark.asyncio
async def test_summary_skips_expired_tenant_and_sends_to_null_and_future(world):
    got = await _tenant_ids(
        world,
        _TENANTS_SQL,
        {"channel": "telegram", "default_tz": "Europe/Belgrade"},
    )
    ids = world["ids"]
    assert ids["expired"] not in got
    assert ids["forever"] in got
    assert ids["future"] in got
