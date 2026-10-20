"""Integration: “no deadline → the tender is current for 30 days” (the owner’s decision 06.10.2026).

The predicate lives in the SQL (`src/domain/actuality.py::sql_no_deadline_fresh`) and is substituted into the feed of the cabinet (`feed_service._CURRENT_ONLY_PREDICATE`) and into stage A of the matching (`STAGE_A_SQL`, the bind parameters of the tender). It is not proved on mocks (`.claude/rules/tests-known-defects.md`) — the test runs the real ``feed_service.get_feed_page``/``count_feed_total`` and ``RoutingWorker.route_tender`` against a throwaway PostgreSQL.

The expectations — literals from the owner’s decision: no deadline, first seen 31 days ago → NOT current; 29 days → current; a deadline in the future → current at any age; “all” (``actual_only=false``) shows everything.

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
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
from src.workers.routing import RoutingWorker

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция «без срока 30 дней» пропускается",
)

NOW = datetime.now(timezone.utc)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# (name, deadline_at, published_at, first_seen_at) — the whole matrix in one fixture.
_CASES = {
    "nodl_seen_31d": (None, None, NOW - timedelta(days=31)),
    "nodl_seen_29d": (None, None, NOW - timedelta(days=29)),
    # published_at is primary over first_seen_at: published 40 days ago, it reached us
    # yesterday (a backfill) — it is no longer current.
    "nodl_pub_40d_seen_1d": (None, NOW - timedelta(days=40), NOW - timedelta(days=1)),
    "nodl_pub_10d_seen_1d": (None, NOW - timedelta(days=10), NOW - timedelta(days=1)),
    "dl_future_seen_100d": (
        NOW + timedelta(days=3),
        NOW - timedelta(days=100),
        NOW - timedelta(days=100),
    ),
    "dl_past_seen_1d": (NOW - timedelta(days=1), None, NOW - timedelta(days=1)),
}
_ACTUAL = {"nodl_seen_29d", "nodl_pub_10d_seen_1d", "dl_future_seen_100d"}


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    tid, pid = uuid.uuid4(), uuid.uuid4()
    ids = {name: uuid.uuid4() for name in _CASES}
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
                "INSERT INTO profiles (id, tenant_id, name, is_active) "
                "VALUES (:id, :tid, 'Профиль', true)"
            ),
            {"id": pid, "tid": tid},
        )
        for name, (deadline, published, first_seen) in _CASES.items():
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, title, customer_name, "
                    "deadline_at, published_at, first_seen_at, source_url) VALUES "
                    "(:id, :title, 'Заказчик', :dl, :pub, :fs, 'https://portal.rs/x')"
                ),
                {
                    "id": ids[name],
                    "title": name,
                    "dl": deadline,
                    "pub": published,
                    "fs": first_seen,
                },
            )
            await c.execute(
                text(
                    "INSERT INTO delivery_outbox "
                    "(profile_id, tender_id, channel, status, sent_at) "
                    "VALUES (:pid, :tid, 'email', 'sent', :sent)"
                ),
                {"pid": pid, "tid": ids[name], "sent": NOW - timedelta(hours=1)},
            )

    yield {"tenant_id": str(tid), "ids": {k: str(v) for k, v in ids.items()}}

    async with eng.begin() as c:
        await c.execute(
            text("DELETE FROM delivery_outbox WHERE profile_id = :p"), {"p": pid}
        )
        await c.execute(
            text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
            {"ids": list(ids.values())},
        )
        await c.execute(text("DELETE FROM profiles WHERE id = :p"), {"p": pid})
        await c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
    await eng.dispose()


@pytest_asyncio.fixture
async def app_session():
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


def _names(world, items) -> set[str]:
    by_id = {v: k for k, v in world["ids"].items()}
    return {by_id[it.tender_id] for it in items}


@pytest.mark.asyncio
async def test_feed_actual_drops_no_deadline_older_than_30_days(world, app_session):
    async with app_session() as db:
        items = await feed_service.get_feed_page(
            db, world["tenant_id"], page=1, page_size=50, actual_only=True
        )
        total = await feed_service.count_feed_total(
            db, world["tenant_id"], actual_only=True
        )
    assert _names(world, items) == {
        "nodl_seen_29d",
        "nodl_pub_10d_seen_1d",
        "dl_future_seen_100d",
    }
    assert total == 3


@pytest.mark.asyncio
async def test_feed_all_still_shows_everything(world, app_session):
    async with app_session() as db:
        items = await feed_service.get_feed_page(
            db, world["tenant_id"], page=1, page_size=50, actual_only=False
        )
        total = await feed_service.count_feed_total(
            db, world["tenant_id"], actual_only=False
        )
    assert _names(world, items) == set(_CASES)
    assert total == 6


@pytest.mark.asyncio
async def test_dashboard_teaser_uses_same_rule(world, app_session):
    async with app_session() as db:
        items = await feed_service.get_feed_items(db, world["tenant_id"], limit=50)
    assert _names(world, items) == _ACTUAL


# --------------------------------------------------------------------------- #
#  Stage A of the matching (STAGE_A_SQL) — the same predicate on the bind parameters
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def routing_world():
    """A tenant + a live telegram + a source + a profile “everything”."""
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    tid, uid, cid, pid = (uuid.uuid4() for _ in range(4))
    key = f"nodl30-{uuid.uuid4().hex[:8]}"
    tenders: list[uuid.UUID] = []
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
            {"id": cid, "uid": uid},
        )
        await c.execute(
            text(
                "INSERT INTO sources (source_key, enabled, enabled_at, baseline_at, "
                "country_codes) VALUES (:k, true, :ea, :ba, ARRAY['RS']::char(2)[])"
            ),
            {"k": key, "ea": NOW - timedelta(days=200), "ba": EPOCH},
        )
        await c.execute(
            text(
                "INSERT INTO profiles (id, tenant_id, country_code, is_active, "
                "match_all, enabled_source_keys, delivery_channels, "
                "ai_analysis_level, created_at) VALUES "
                "(:id, :tid, 'RS', true, true, ARRAY[:k], ARRAY['telegram'], "
                "'none', :ca)"
            ),
            {"id": pid, "tid": tid, "k": key, "ca": NOW - timedelta(days=200)},
        )

    async def route(name: str) -> int:
        deadline, published, first_seen = _CASES[name]
        tender_id = uuid.uuid4()
        tenders.append(tender_id)
        async with eng.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, deadline_at, published_at, first_seen_at, "
                    "title_normalized) VALUES (:id, :k, :uid, 'RS', :dl, :pub, :fs, "
                    "'nabavka')"
                ),
                {
                    "id": tender_id,
                    "k": key,
                    "uid": tender_id.hex,
                    "dl": deadline,
                    "pub": published,
                    "fs": first_seen,
                },
            )
        worker = RoutingWorker(
            session_factory=async_sessionmaker(
                eng, class_=AsyncSession, expire_on_commit=False
            )
        )
        await worker.route_tender(
            {"tender_id": str(tender_id), "change_kind": "INSERT"}
        )
        async with eng.connect() as c:
            return int(
                (
                    await c.execute(
                        text(
                            "SELECT count(*) FROM delivery_outbox WHERE tender_id = :t"
                        ),
                        {"t": tender_id},
                    )
                ).scalar_one()
            )

    try:
        yield route
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM delivery_outbox WHERE profile_id = :p"), {"p": pid}
            )
            await c.execute(text("DELETE FROM profiles WHERE id = :p"), {"p": pid})
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:t)"),
                {"t": tenders},
            )
            await c.execute(
                text("DELETE FROM sources WHERE source_key = :k"), {"k": key}
            )
            await c.execute(
                text("DELETE FROM user_channels WHERE user_id = :u"), {"u": uid}
            )
            await c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await eng.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,delivered",
    [
        ("nodl_seen_31d", 0),
        ("nodl_seen_29d", 1),
        ("nodl_pub_40d_seen_1d", 0),
        ("nodl_pub_10d_seen_1d", 1),
        ("dl_future_seen_100d", 1),
    ],
)
async def test_stage_a_applies_30_day_rule(routing_world, name, delivered):
    assert await routing_world(name) == delivered
