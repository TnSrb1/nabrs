"""Cabinet — the third subscription channel (owner 18.10.2026, issue #781).

A tenant without a live Telegram/e-mail must see in the cabinet feed the same matches
of the template that would have gone to the messenger. Routing writes an outbox row for it of channel
``web`` with status ``web_only`` (delivery does not take it); the feed reads it with the
same predicate as the telegram/email rows.

‼️ The predicate is SQL (``STAGE_A_SQL`` + ``FEED_VISIBLE_STATUSES``) and cannot be proven on mocks:
the test runs the REAL ``RoutingWorker.route_tender`` and ``feed_service`` against a
throwaway PG (``TEST_RLS_DATABASE_URL``; routing — superuser, as in prod).
Without the variable — skip.
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
    reason="TEST_RLS_DATABASE_URL не задан — интеграция третьего канала пропускается",
)

NOW = datetime.now(timezone.utc)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    key = f"web3-{uuid.uuid4().hex[:8]}"
    made: dict[str, list] = {"tenants": [], "profiles": [], "tenders": []}
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO sources (source_key, enabled, enabled_at, baseline_at, "
                "country_codes) VALUES (:k, true, :ea, :ba, ARRAY['RS']::char(2)[])"
            ),
            {"k": key, "ea": NOW - timedelta(days=60), "ba": EPOCH},
        )

    class W:
        engine = eng
        source_key = key

        async def tenant(self, *, channel: str | None = None) -> dict:
            """Tenant + user; ``channel`` — a live channel ('telegram'/'email') or None."""
            tid, uid = uuid.uuid4(), uuid.uuid4()
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
                if channel:
                    await c.execute(
                        text(
                            "INSERT INTO user_channels (id, user_id, channel, "
                            "external_id, status) VALUES (:id, :u, :ch, '424242', "
                            "'active')"
                        ),
                        {"id": uuid.uuid4(), "u": uid, "ch": channel},
                    )
            made["tenants"].append((tid, uid))
            return {"tid": str(tid), "uid": uid}

        async def profile(
            self, tenant: dict, *, active=True, channels=(), ai="none"
        ) -> str:
            pid = uuid.uuid4()
            async with eng.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO profiles (id, tenant_id, country_code, "
                        "is_active, match_all, enabled_source_keys, "
                        "delivery_channels, ai_analysis_level, created_at, name) "
                        "VALUES (:id, :tid, 'RS', :act, true, :keys, :chs, :ai, "
                        ":ca, 'Клининг и уборка')"
                    ),
                    {
                        "id": pid,
                        "tid": tenant["tid"],
                        "act": active,
                        "keys": [key],
                        "chs": list(channels),
                        "ai": ai,
                        "ca": NOW - timedelta(days=2),
                    },
                )
            made["profiles"].append(pid)
            return str(pid)

        async def tender(self) -> str:
            tid = uuid.uuid4()
            async with eng.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                        "country_code, regions, status, title, deadline_at, "
                        "first_seen_at, title_normalized) VALUES (:id, :k, :uid, "
                        "'RS', '{}', 'active', 'Уборка помещений', :dl, :fs, "
                        "'ubor')"
                    ),
                    {
                        "id": tid,
                        "k": key,
                        "uid": tid.hex,
                        "dl": NOW + timedelta(days=20),
                        "fs": NOW - timedelta(hours=1),
                    },
                )
            made["tenders"].append(tid)
            return str(tid)

        async def route(self, tender_id: str) -> None:
            worker = RoutingWorker(
                session_factory=async_sessionmaker(
                    eng, class_=AsyncSession, expire_on_commit=False
                )
            )
            await worker.route_tender({"tender_id": tender_id, "change_kind": "INSERT"})

        async def outbox(self, tender_id: str) -> list[tuple[str, str, bool]]:
            async with eng.connect() as c:
                rows = await c.execute(
                    text(
                        "SELECT channel, status, requires_ai FROM delivery_outbox "
                        "WHERE tender_id = :t ORDER BY channel"
                    ),
                    {"t": tender_id},
                )
                return [(r.channel, r.status, r.requires_ai) for r in rows]

    w = W()
    try:
        yield w
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM delivery_outbox WHERE profile_id = ANY(:p)"),
                {"p": made["profiles"]},
            )
            await c.execute(
                text("DELETE FROM profiles WHERE id = ANY(:p)"),
                {"p": made["profiles"]},
            )
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:t)"),
                {"t": made["tenders"]},
            )
            for tid, uid in made["tenants"]:
                await c.execute(
                    text("DELETE FROM user_channels WHERE user_id = :u"), {"u": uid}
                )
                await c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
                await c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
            await c.execute(
                text("DELETE FROM sources WHERE source_key = :k"), {"k": key}
            )
        await eng.dispose()


@pytest_asyncio.fixture
async def app_sessionmaker():
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


async def _feed_ids(sm, tenant: dict) -> set[str]:
    async with sm() as db:
        items = await feed_service.get_feed_page(
            db, tenant["tid"], page=1, page_size=50
        )
    return {it.tender_id for it in items}


@pytest.mark.asyncio
async def test_tenant_without_channels_sees_match_in_cabinet(world, app_sessionmaker):
    """Live case 0fa6cb74: template active, delivery_channels='{}', 0 channels."""
    tenant = await world.tenant()
    await world.profile(tenant, channels=())
    tender = await world.tender()

    await world.route(tender)

    assert await world.outbox(tender) == [("web", "web_only", False)]
    assert await _feed_ids(app_sessionmaker, tenant) == {tender}


@pytest.mark.asyncio
async def test_count_and_teaser_agree_with_page(world, app_sessionmaker):
    tenant = await world.tenant()
    await world.profile(tenant, channels=("telegram",))  # configured, but not connected
    tender = await world.tender()
    await world.route(tender)

    async with app_sessionmaker() as db:
        total = await feed_service.count_feed_total(db, tenant["tid"])
        teaser = await feed_service.get_feed_items(db, tenant["tid"], limit=10)
        hot = await feed_service.get_hot_deadlines(db, tenant["tid"], window_days=30)
    assert total == 1
    assert [it.tender_id for it in teaser] == [tender]
    assert [h.tender_id for h in hot] == [tender]


@pytest.mark.asyncio
async def test_repeated_routing_does_not_duplicate_web_row(world):
    """A repeated routing of the same tender — not a second web_only row (ON CONFLICT)."""
    tenant = await world.tenant()
    await world.profile(tenant, channels=())
    tender = await world.tender()

    await world.route(tender)
    await world.route(tender)

    assert await world.outbox(tender) == [("web", "web_only", False)]


@pytest.mark.asyncio
async def test_web_row_never_requires_ai(world):
    """A paid AI analysis is not started for a row without sending."""
    tenant = await world.tenant()
    await world.profile(tenant, channels=(), ai="quick")
    tender = await world.tender()

    await world.route(tender)

    assert await world.outbox(tender) == [("web", "web_only", False)]


@pytest.mark.asyncio
async def test_other_tenant_does_not_see_it(world, app_sessionmaker):
    owner = await world.tenant()
    stranger = await world.tenant()
    await world.profile(owner, channels=())
    tender = await world.tender()
    await world.route(tender)

    assert await _feed_ids(app_sessionmaker, owner) == {tender}
    assert await _feed_ids(app_sessionmaker, stranger) == set()


@pytest.mark.asyncio
async def test_inactive_profile_gives_no_cards(world, app_sessionmaker):
    tenant = await world.tenant()
    await world.profile(tenant, active=False, channels=())
    tender = await world.tender()

    await world.route(tender)

    assert await world.outbox(tender) == []
    assert await _feed_ids(app_sessionmaker, tenant) == set()


@pytest.mark.asyncio
async def test_tenant_with_live_channel_behaves_as_before(world, app_sessionmaker):
    """Live channel: exactly the channel row, no web row, the feed — one card."""
    tenant = await world.tenant(channel="telegram")
    await world.profile(tenant, channels=("telegram",))
    tender = await world.tender()

    await world.route(tender)

    assert await world.outbox(tender) == [("telegram", "pending", False)]
    assert await _feed_ids(app_sessionmaker, tenant) == {tender}


@pytest.mark.asyncio
async def test_channel_connected_later_does_not_resend_old_card(
    world, app_sessionmaker
):
    """Telegram connected AFTER the showing: the old card stays in the feed and is not
    sent to Telegram in a batch (a repeated routing of the same tender — a no-op for
    delivery: the web row exists, a telegram row appears only if
    the tender arrives again)."""
    tenant = await world.tenant()
    await world.profile(tenant, channels=("telegram",))
    old = await world.tender()
    await world.route(old)

    async with world.engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO user_channels (id, user_id, channel, external_id, "
                "status) VALUES (:id, :u, 'telegram', '424242', 'active')"
            ),
            {"id": uuid.uuid4(), "u": tenant["uid"]},
        )

    # already-shown old items do not turn into delivery: claim takes only
    # pending/ai_done, and the old one has a single row — web_only
    assert await world.outbox(old) == [("web", "web_only", False)]
    fresh = await world.tender()
    await world.route(fresh)
    assert await world.outbox(fresh) == [("telegram", "pending", False)]
    assert await _feed_ids(app_sessionmaker, tenant) == {old, fresh}
