"""Integration: the delivery cut-off by the moment of subscription and the silent first pass.

The owner’s decisions 30.09.2026 (SRC-0a): R4 — the first pass of a new source is not sent to anyone; R5 — a profile gets only the tenders that arrived AFTER it enabled the source (the fallback — the creation of the profile). Plus the country/region: a foreign tender must not be cut by the Serbian ``profiles.country_code``.

‼️ The predicate lives in the SQL (``STAGE_A_SQL``) — it is not proved on mocks
(``.claude/rules/tests-known-defects.md``). Therefore the test runs the REAL
``RoutingWorker.route_tender`` against a throwaway PostgreSQL and counts the rows of
``delivery_outbox``. The routing in production goes under the superuser ``tendersrb``
(checked by ``SELECT current_user`` in deploy-routing-1 on 30.09) — here too
``TEST_SUPERUSER_DATABASE_URL``.

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production) —
without it a skip, like the other ``tests/db`` integrations.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.workers.routing import RoutingWorker

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция routing-отсечки пропускается",
)

NOW = datetime.now(timezone.utc)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def world():
    """A tenant + a user with a live telegram + a source; the profiles/tenders — by the test."""
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    tid, uid, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = f"src0a-{uuid.uuid4().hex[:8]}"
    created: dict[str, list] = {"profiles": [], "tenders": []}
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
            {"id": cid, "uid": uid},
        )

    class W:
        engine = eng
        source_key = key

        async def source(self, *, enabled_at, baseline_at, key=key):
            async with eng.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO sources (source_key, enabled, enabled_at, "
                        "baseline_at, country_codes) VALUES (:k, true, :ea, :ba, "
                        "ARRAY['RS']::char(2)[])"
                    ),
                    {"k": key, "ea": enabled_at, "ba": baseline_at},
                )

        async def profile(
            self,
            *,
            created_at,
            opt_in=None,
            keys=None,
            regions=(),
            country="RS",
        ):
            pid = uuid.uuid4()
            async with eng.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO profiles (id, tenant_id, country_code, is_active, "
                        "match_all, enabled_source_keys, regions, delivery_channels, "
                        "ai_analysis_level, created_at, source_opt_in) VALUES "
                        "(:id, :tid, :cc, true, true, :keys, :regions, "
                        "ARRAY['telegram'], 'none', :ca, CAST(:opt AS jsonb))"
                    ),
                    {
                        "id": pid,
                        "tid": tid,
                        "cc": country,
                        "keys": list(keys if keys is not None else [key]),
                        "regions": list(regions),
                        "ca": created_at,
                        "opt": json.dumps(
                            {k: v.isoformat() for k, v in (opt_in or {}).items()}
                        ),
                    },
                )
            created["profiles"].append(pid)
            return pid

        async def tender(self, *, first_seen, country="RS", regions=(), key=key):
            tender_id = uuid.uuid4()
            async with eng.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                        "country_code, regions, status, deadline_at, first_seen_at, "
                        "title_normalized) VALUES (:id, :k, :uid, :cc, :regions, "
                        "'active', :dl, :fs, 'nabavka')"
                    ),
                    {
                        "id": tender_id,
                        "k": key,
                        "uid": tender_id.hex,
                        "cc": country,
                        "regions": list(regions),
                        "dl": NOW + timedelta(days=20),
                        "fs": first_seen,
                    },
                )
            created["tenders"].append(tender_id)
            return tender_id

        async def route(self, tender_id, change_kind="INSERT") -> int:
            worker = RoutingWorker(
                session_factory=async_sessionmaker(
                    eng, class_=AsyncSession, expire_on_commit=False
                )
            )
            await worker.route_tender(
                {"tender_id": str(tender_id), "change_kind": change_kind}
            )
            async with eng.connect() as c:
                return int(
                    (
                        await c.execute(
                            text(
                                "SELECT count(*) FROM delivery_outbox "
                                "WHERE tender_id = :t"
                            ),
                            {"t": tender_id},
                        )
                    ).scalar_one()
                )

    w = W()
    try:
        yield w
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM delivery_outbox WHERE profile_id = ANY(:p)"),
                {"p": created["profiles"]},
            )
            await c.execute(
                text("DELETE FROM profiles WHERE tenant_id = :t"), {"t": tid}
            )
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:t)"),
                {"t": created["tenders"]},
            )
            await c.execute(
                text("DELETE FROM sources WHERE source_key LIKE :k"),
                {"k": f"{key}%"},
            )
            await c.execute(
                text("DELETE FROM user_channels WHERE user_id = :u"), {"u": uid}
            )
            await c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await eng.dispose()


# --------------------------------------------------------------------------- #
#  (ii) R5: an UPDATE of an old tender does not go to a profile created later
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_update_of_tender_seen_before_profile_creation_is_not_delivered(world):
    await world.source(enabled_at=NOW - timedelta(days=60), baseline_at=EPOCH)
    await world.profile(created_at=NOW - timedelta(hours=1))
    tender = await world.tender(first_seen=NOW - timedelta(days=1))

    assert await world.route(tender, "UPDATE") == 0


@pytest.mark.asyncio
async def test_tender_seen_after_profile_creation_is_delivered(world):
    """Positive: the standard jnportal path is not broken."""
    await world.source(enabled_at=NOW - timedelta(days=60), baseline_at=EPOCH)
    await world.profile(created_at=NOW - timedelta(days=2))
    tender = await world.tender(first_seen=NOW - timedelta(days=1))

    assert await world.route(tender, "INSERT") == 1


@pytest.mark.asyncio
async def test_opt_in_moment_beats_profile_creation(world):
    """The profile is old, but the source was enabled an hour ago → a tender of two hours ago — no."""
    await world.source(enabled_at=NOW - timedelta(days=60), baseline_at=EPOCH)
    await world.profile(
        created_at=NOW - timedelta(days=10),
        opt_in={world.source_key: NOW - timedelta(hours=1)},
    )
    old = await world.tender(first_seen=NOW - timedelta(hours=2))
    fresh = await world.tender(first_seen=NOW - timedelta(minutes=10))

    assert await world.route(old, "UPDATE") == 0
    assert await world.route(fresh, "INSERT") == 1


# --------------------------------------------------------------------------- #
#  (iii) R4: the silent first pass of a new source
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_first_pass_of_new_source_is_not_delivered(world):
    """baseline_at IS NULL (the first pass is still running) → to no one, even the old profiles."""
    await world.source(enabled_at=NOW - timedelta(minutes=30), baseline_at=None)
    await world.profile(created_at=NOW - timedelta(days=30))
    tender = await world.tender(first_seen=NOW - timedelta(minutes=5))

    assert await world.route(tender, "INSERT") == 0


@pytest.mark.asyncio
async def test_tenders_after_baseline_are_delivered(world):
    await world.source(
        enabled_at=NOW - timedelta(hours=3), baseline_at=NOW - timedelta(hours=2)
    )
    await world.profile(created_at=NOW - timedelta(days=30))
    quiet = await world.tender(first_seen=NOW - timedelta(hours=2, minutes=30))
    fresh = await world.tender(first_seen=NOW - timedelta(hours=1))

    assert await world.route(quiet, "UPDATE") == 0
    assert await world.route(fresh, "INSERT") == 1


# --------------------------------------------------------------------------- #
#  Country / region: a foreign source is not dead because of profiles.country_code
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_foreign_tender_reaches_rs_profile_with_its_source_and_rs_regions(
    world,
):
    """A BA tender, regions=[] → the profile RS (with the region RS-00) with the key of the source — yes."""
    await world.source(enabled_at=NOW - timedelta(days=5), baseline_at=EPOCH)
    await world.profile(created_at=NOW - timedelta(days=2), regions=["RS-00"])
    tender = await world.tender(first_seen=NOW - timedelta(hours=1), country="BA")

    assert await world.route(tender, "INSERT") == 1


@pytest.mark.asyncio
async def test_foreign_tender_not_delivered_without_source_key(world):
    await world.source(enabled_at=NOW - timedelta(days=5), baseline_at=EPOCH)
    await world.profile(created_at=NOW - timedelta(days=2), keys=["jnportal"])
    tender = await world.tender(first_seen=NOW - timedelta(hours=1), country="BA")

    assert await world.route(tender, "INSERT") == 0


@pytest.mark.asyncio
async def test_rs_region_filter_still_cuts_other_rs_regions(world):
    """A region of the same country — the filter works as before."""
    await world.source(enabled_at=NOW - timedelta(days=5), baseline_at=EPOCH)
    await world.profile(created_at=NOW - timedelta(days=2), regions=["RS-00"])
    other = await world.tender(first_seen=NOW - timedelta(hours=1), regions=["RS-10"])
    same = await world.tender(first_seen=NOW - timedelta(hours=1), regions=["RS-00"])
    none = await world.tender(first_seen=NOW - timedelta(hours=1), regions=[])

    assert await world.route(other, "INSERT") == 0
    assert await world.route(same, "INSERT") == 1
    assert await world.route(none, "INSERT") == 0
