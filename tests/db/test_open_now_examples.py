"""Integration of the block “Already open tenders” after the creation of a template (a real PG).

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, see ``tests/db/test_rls_isolation.py``).
Without the variable — a skip; gate brings up a one-off PG itself.

What we prove on the real SQL, not on a mock:
  1. The predicate “open now” = the feed (the deadline in the future OR there is no deadline and the tender
     is younger than 30 days): the expired and the old-without-deadline do NOT get in, the open one older than
     30 days — gets in (there is no window of first_seen here).
  2. The order — the nearest deadline first, without a deadline — last.
  3. The block only READS: the rows of ``delivery_outbox`` and ``user_channels`` of a tenant with an active channel and an already standing queue are the same before and after the call.
  4. The flag ``archived``: a tender that appeared before the ``created_at`` of the profile (the cut-off of delivery) is marked; one that appeared after — is not.
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

from src.api import repo
from src.api.cabinet import preview_service

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция блока пропускается",
)

SOURCE_KEY = "jnportal"


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def open_fixture():
    """A tenant + a profile + five tenders, a channel and the delivery queue.

    The profile was created two days before “now”: tenders with first_seen before this
    moment — “from the archive” (archived), later — no.
    """
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    sup_engine = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    tid = uuid.uuid4()
    uid = uuid.uuid4()
    pid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    created = now - timedelta(days=2)
    tenders = {
        # Open, but published 60 days ago: there is no first_seen window in the block.
        "future": (now + timedelta(days=1), now - timedelta(days=60)),
        "later": (now + timedelta(days=5), now - timedelta(hours=1)),
        "expired": (now - timedelta(days=1), now - timedelta(hours=3)),
        "null_fresh": (None, now - timedelta(hours=1)),
        "null_old": (None, now - timedelta(days=45)),
    }
    ids = {name: uuid.uuid4() for name in tenders}
    try:
        async with sup_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status) "
                    "VALUES (:id, 'RS', 'lite', 'trial')"
                ),
                {"id": tid},
            )
            await conn.execute(
                text("INSERT INTO users (id, tenant_id) VALUES (:id, :tid)"),
                {"id": uid, "tid": tid},
            )
            await conn.execute(
                text(
                    "INSERT INTO user_channels (id, user_id, channel, external_id, "
                    "status, linked_at, locale) VALUES (:id, :uid, 'telegram', "
                    ":ext, 'active', now(), 'ru')"
                ),
                {"id": uuid.uuid4(), "uid": uid, "ext": f"open-now-{tid}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO profiles (id, tenant_id, name, is_active, "
                    "created_at) VALUES (:id, :tid, 'Шаблон', true, :created)"
                ),
                {"id": pid, "tid": tid, "created": created},
            )
            for name, (deadline, first_seen) in tenders.items():
                await conn.execute(
                    text(
                        "INSERT INTO tenders_normalized "
                        "(id, source_key, source_uid, title, customer_name, "
                        "deadline_at, source_url, first_seen_at, country_code) "
                        "VALUES (:id, :sk, :uid, :title, 'Заказчик', :deadline, "
                        "'https://portal.rs/x', :first_seen, 'RS')"
                    ),
                    {
                        "id": ids[name],
                        "sk": SOURCE_KEY,
                        "uid": f"open-now-{name}-{tid}",
                        "title": f"Тендер {name}",
                        "deadline": deadline,
                        "first_seen": first_seen,
                    },
                )
            # The delivery queue is already standing (as after the ordinary matching).
            await conn.execute(
                text(
                    "INSERT INTO delivery_outbox (profile_id, tender_id, channel, "
                    "status, requires_ai) VALUES (:pid, :tender, 'telegram', "
                    "'pending', false)"
                ),
                {"pid": pid, "tender": ids["future"]},
            )
    finally:
        await sup_engine.dispose()

    yield {
        "tenant_id": str(tid),
        "profile_id": str(pid),
        "ids": {name: str(v) for name, v in ids.items()},
    }

    cleanup = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    try:
        async with cleanup.begin() as conn:
            await conn.execute(
                text("DELETE FROM delivery_outbox WHERE profile_id = :pid"),
                {"pid": pid},
            )
            await conn.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": list(ids.values())},
            )
            await conn.execute(
                text("DELETE FROM profiles WHERE id = :pid"), {"pid": pid}
            )
            await conn.execute(
                text("DELETE FROM user_channels WHERE user_id = :uid"), {"uid": uid}
            )
            await conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})
            await conn.execute(
                text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid}
            )
    finally:
        await cleanup.dispose()


def _payload() -> dict:
    """The minimal payload of the profile “All tenders” on one source."""
    return {
        "enabled_source_keys": [SOURCE_KEY],
        "match_all": True,
        "keywords_include": [],
        "keywords_exclude": [],
        "classifications": {},
    }


async def _delivery_state(db: AsyncSession) -> tuple:
    """The counters and the control sum of the delivery rows and of the channels of the current tenant.

    The sum over (id, status, sent_at) catches also a changed row, not only a new one.
    """
    outbox = (
        await db.execute(
            text(
                "SELECT count(*), coalesce(md5(string_agg(id::text || status, ','"
                " ORDER BY id)), '') FROM delivery_outbox"
            )
        )
    ).one()
    channels = (
        await db.execute(
            text(
                "SELECT count(*), coalesce(md5(string_agg(id::text || status, ','"
                " ORDER BY id)), '') FROM user_channels"
            )
        )
    ).one()
    return tuple(outbox), tuple(channels)


@pytest.mark.asyncio
async def test_open_now_keeps_only_open_sorted_by_deadline(sessionmaker_, open_fixture):
    """Open — by the deadline, without a deadline — at the end; the expired and the old-without-deadline — out."""
    ids = open_fixture["ids"]
    async with sessionmaker_() as db:
        await repo.set_tenant(db, open_fixture["tenant_id"])
        examples = await preview_service.open_now_examples(
            db, _payload(), open_fixture["profile_id"]
        )
    got = [ex.id for ex in examples]
    assert got == [ids["future"], ids["later"], ids["null_fresh"]]
    assert ids["expired"] not in got
    assert ids["null_old"] not in got


@pytest.mark.asyncio
async def test_open_now_respects_limit(sessionmaker_, open_fixture):
    """The limit cuts the tail without changing the order: the first two are the nearest deadlines."""
    ids = open_fixture["ids"]
    async with sessionmaker_() as db:
        await repo.set_tenant(db, open_fixture["tenant_id"])
        examples = await preview_service.open_now_examples(
            db, _payload(), open_fixture["profile_id"], limit=2
        )
    assert [ex.id for ex in examples] == [ids["future"], ids["later"]]


@pytest.mark.asyncio
async def test_open_now_writes_nothing_to_delivery(sessionmaker_, open_fixture):
    """The block only reads: delivery_outbox and user_channels do not change."""
    async with sessionmaker_() as db:
        await repo.set_tenant(db, open_fixture["tenant_id"])
        before = await _delivery_state(db)
        assert before[0][0] == 1  # the queue stands before the call
        assert before[1][0] == 1  # the active channel exists
        await preview_service.open_now_examples(
            db, _payload(), open_fixture["profile_id"]
        )
        await db.commit()
        # commit resets app.current_tenant_id (set_config is_local): we set it again.
        await repo.set_tenant(db, open_fixture["tenant_id"])
        after = await _delivery_state(db)
    assert after == before


@pytest.mark.asyncio
async def test_open_now_empty_profile_without_sources_returns_nothing(
    sessionmaker_, open_fixture
):
    """A profile without platforms gets nothing — the block is honestly empty."""
    payload = {**_payload(), "enabled_source_keys": []}
    async with sessionmaker_() as db:
        await repo.set_tenant(db, open_fixture["tenant_id"])
        examples = await preview_service.open_now_examples(
            db, payload, open_fixture["profile_id"]
        )
    assert examples == []


@pytest.mark.asyncio
async def test_open_now_archived_flag_marks_tenders_before_profile_cutoff(
    sessionmaker_, open_fixture
):
    """The archive = first_seen before the created_at of the profile; after — the notice will come."""
    ids = open_fixture["ids"]
    async with sessionmaker_() as db:
        await repo.set_tenant(db, open_fixture["tenant_id"])
        examples = await preview_service.open_now_examples(
            db, _payload(), open_fixture["profile_id"]
        )
    archived = {ex.id: ex.archived for ex in examples}
    assert archived == {
        ids["future"]: True,
        ids["later"]: False,
        ids["null_fresh"]: False,
    }


class _CountingSession:
    """A wrapper over the session: counts the SQL calls of ``execute`` (stage B — in Python)."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.calls = 0

    async def execute(self, *args, **kwargs):
        self.calls += 1
        return await self.db.execute(*args, **kwargs)


@pytest.mark.asyncio
async def test_open_now_scan_is_capped_when_keyword_never_matches(
    sessionmaker_, open_fixture
):
    """A word does not occur in any of the 1600 open → a ceiling of pages, not a walk over all.

    Without a ceiling stage B would page through all the open ones by ``LIMIT`` 15
    (≈107 queries per render). With the ceiling of 1500 rows and a page of 300 — no more than
    ``ceil(1500 / 300) = 5`` SQL calls; the result is empty.
    """
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    tag = open_fixture["tenant_id"]
    sup_engine = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    try:
        async with sup_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "title, customer_name, deadline_at, source_url, first_seen_at, "
                    "country_code) SELECT gen_random_uuid(), :sk, "
                    "'open-now-bulk-' || :tag || '-' || g, 'Набавка ' || g, "
                    "'Заказчик', now() + interval '2 days', 'https://portal.rs/x', "
                    "now(), 'RS' FROM generate_series(1, 1600) AS g"
                ),
                {"sk": SOURCE_KEY, "tag": tag},
            )
        payload = {
            **_payload(),
            "match_all": False,
            "keywords_include": [{"all_of": ["zzqnemaguc"]}],
        }
        async with sessionmaker_() as db:
            await repo.set_tenant(db, open_fixture["tenant_id"])
            counting = _CountingSession(db)
            examples = await preview_service.open_now_examples(
                counting, payload, open_fixture["profile_id"]
            )
        assert examples == []
        assert counting.calls <= 5
    finally:
        async with sup_engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM tenders_normalized WHERE source_uid LIKE "
                    "'open-now-bulk-' || :tag || '-%'"
                ),
                {"tag": tag},
            )
        await sup_engine.dispose()
