"""Migration 0058 + SQL predicates of tender changes on a REAL PG.

Without a DB — the revision chain and the revocation of rights from the app role. On PG (``TEST_SUPERUSER_DATABASE_URL``,
``TEST_RLS_DATABASE_URL``):

* notifier: "no more than once a day per (tender, recipient)", "delivered BEFORE
  the change", events over a day are merged, a closed tender is not sent;
* ``tendersrb_app`` does not see ``tender_change_sent`` and does not write the journal;
* the AI cache does not return an analysis older than ``last_changed_at`` and updates the stale row
  in place (UNIQUE(tender, profile, level) does not allow inserting a second one).
"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.ai.usage import cache_lookup, finalize_with_insert
from src.workers.tender_change_notifier import _PENDING_SQL, WINDOW_DAYS

_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0058_tender_changes.py"
)
APP_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", "")
needs_pg = pytest.mark.skipif(
    not (APP_URL and SUPER_URL), reason="нет TEST_RLS/SUPERUSER_DATABASE_URL"
)


def _load():
    spec = importlib.util.spec_from_file_location("migration_0058", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(monkeypatch, which) -> str:
    m = _load()
    calls: list[str] = []
    monkeypatch.setattr(m.op, "execute", lambda sql: calls.append(sql))
    getattr(m, which)()
    return " ".join("\n".join(calls).split())


def test_revision_chain():
    m = _load()
    assert (m.revision, m.down_revision) == ("0058", "0057")


def test_app_role_rights_revoked(monkeypatch):
    sql = _sql(monkeypatch, "upgrade")
    assert "REVOKE ALL ON tender_change_sent FROM tendersrb_app" in sql
    assert (
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON tender_changes, tender_docs_check "
        "FROM tendersrb_app" in sql
    )
    assert "POLICY" not in sql


def test_downgrade_drops_all_three(monkeypatch):
    sql = _sql(monkeypatch, "downgrade")
    for t in ("tender_docs_check", "tender_change_sent", "tender_changes"):
        assert f"DROP TABLE IF EXISTS {t}" in sql


class _Env:
    """Tenant + profile + tender + "delivered" (sent_at) on the superuser connection."""

    def __init__(self, conn):
        self.c = conn
        self.tenant = None
        self.tender = None

    async def seed(self, *, sent_ago="3 days", deadline="now() + interval '10 days'"):
        c = self.c
        self.tenant = (
            await c.execute(
                text("INSERT INTO tenants (country_code) VALUES ('RS') RETURNING id")
            )
        ).scalar_one()
        profile = (
            await c.execute(
                text(
                    "INSERT INTO profiles (tenant_id, country_code, target_language) "
                    "VALUES (:t, 'RS', 'sr') RETURNING id"
                ),
                {"t": self.tenant},
            )
        ).scalar_one()
        self.tender = (
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (country_code, source_key, source_uid, "
                    "title, deadline_at) VALUES ('RS','jnportal', :u, 'T', "
                    + deadline
                    + ") RETURNING id"
                ),
                {"u": uuid.uuid4().hex},
            )
        ).scalar_one()
        await c.execute(
            text(
                "INSERT INTO delivery_outbox (profile_id, tender_id, channel, status, sent_at) "
                f"VALUES (:p, :t, 'telegram', 'sent', now() - interval '{sent_ago}')"
            ),
            {"p": profile, "t": self.tender},
        )

    async def change(self, diff, ago="1 hour"):
        return (
            await self.c.execute(
                text(
                    "INSERT INTO tender_changes (tender_id, kind, diff, detected_at) "
                    f"VALUES (:t, 'content', CAST(:d AS jsonb), now() - interval '{ago}') "
                    "RETURNING id"
                ),
                {"t": self.tender, "d": json.dumps(diff)},
            )
        ).scalar_one()

    async def pending(self):
        rows = (await self.c.execute(_PENDING_SQL, {"window_days": WINDOW_DAYS})).all()
        return [r for r in rows if r.tender_id == self.tender]

    async def mark_sent(self, last_id, ago="1 hour"):
        await self.c.execute(
            text(
                "INSERT INTO tender_change_sent (tender_id, notified_tenant, last_change_id, sent_at) "
                f"VALUES (:t, :n, :l, now() - interval '{ago}')"
            ),
            {"t": self.tender, "n": self.tenant, "l": last_id},
        )


async def _with_env(fn, **seed_kw):
    sup = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    try:
        async with sup.connect() as conn:
            env = _Env(conn)
            await env.seed(**seed_kw)
            try:
                await fn(env)
            finally:
                await conn.rollback()
    finally:
        await sup.dispose()


@needs_pg
@pytest.mark.asyncio
async def test_pending_pair_appears_and_events_merge_per_pair():
    async def body(env):
        await env.change({"deadline": {"old": "A", "new": "B"}}, ago="2 hours")
        last = await env.change({"deadline": {"old": "B", "new": "C"}}, ago="1 hour")
        [row] = await env.pending()
        assert row.tenant_id == str(env.tenant) and row.last_id == last
        assert row.prev_id is None

    await _with_env(body)


@needs_pg
@pytest.mark.asyncio
async def test_not_more_than_once_a_day_per_pair():
    async def body(env):
        first = await env.change({"deadline": {"old": "A", "new": "B"}}, ago="3 hours")
        await env.mark_sent(first, ago="1 hour")  # already notified an hour ago
        await env.change({"deadline": {"old": "B", "new": "C"}}, ago="30 minutes")
        assert await env.pending() == []  # a day has not passed — accumulate

        await env.c.execute(
            text("UPDATE tender_change_sent SET sent_at = now() - interval '25 hours'")
        )
        [row] = await env.pending()  # a day has passed → ONLY the new event will go
        assert row.prev_id == first

    await _with_env(body)


@needs_pg
@pytest.mark.asyncio
async def test_already_notified_event_is_not_repeated():
    async def body(env):
        ev = await env.change({"deadline": {"old": "A", "new": "B"}}, ago="2 days")
        await env.mark_sent(ev, ago="30 hours")
        assert await env.pending() == []  # there are no new events

    await _with_env(body)


@needs_pg
@pytest.mark.asyncio
async def test_delivered_after_the_change_gets_no_notice():
    async def body(env):
        # delivered 1 hour ago, changed 2 hours ago: the recipient has already seen the new one
        await env.change({"deadline": {"old": "A", "new": "B"}}, ago="2 hours")
        assert await env.pending() == []

    await _with_env(body, sent_ago="1 hour")


@needs_pg
@pytest.mark.asyncio
async def test_closed_tender_and_old_events_are_not_sent():
    async def closed(env):
        await env.change({"deadline": {"old": "A", "new": "B"}})
        assert await env.pending() == []

    await _with_env(closed, deadline="now() - interval '1 day'")

    async def stale(env):
        await env.change({"deadline": {"old": "A", "new": "B"}}, ago="5 days")
        assert await env.pending() == []

    await _with_env(stale, sent_ago="10 days")


@needs_pg
@pytest.mark.asyncio
async def test_app_role_cannot_touch_sent_or_write_journal():
    app = create_async_engine(APP_URL, poolclass=NullPool, future=True)
    try:
        for stmt in (
            "SELECT 1 FROM tender_change_sent",
            "INSERT INTO tender_changes (tender_id, kind, diff) VALUES (gen_random_uuid(), 'x', '{}')",
            "INSERT INTO tender_docs_check (tender_id) VALUES (gen_random_uuid())",
        ):
            async with app.connect() as c:
                with pytest.raises(DBAPIError, match="permission denied"):
                    await c.execute(text(stmt))
    finally:
        await app.dispose()


# --- AI cache: stale analysis ------------------------------------------------- #
async def _ai_env(conn):
    tender = (
        await conn.execute(
            text(
                "INSERT INTO tenders_normalized (country_code, source_key, source_uid, title) "
                "VALUES ('RS','jnportal', :u, 'T') RETURNING id"
            ),
            {"u": uuid.uuid4().hex},
        )
    ).scalar_one()
    tenant = (
        await conn.execute(
            text("INSERT INTO tenants (country_code) VALUES ('RS') RETURNING id")
        )
    ).scalar_one()
    profile = (
        await conn.execute(
            text(
                "INSERT INTO profiles (tenant_id, country_code, target_language) "
                "VALUES (:t, 'RS', 'sr') RETURNING id"
            ),
            {"t": tenant},
        )
    ).scalar_one()
    return str(tender), str(tenant), str(profile)


class _Result(SimpleNamespace):
    pass


def _res():
    return _Result(
        model="m",
        output={"v": 2},
        tokens_input=1,
        tokens_output=1,
        tokens_cached=0,
        cost_usd=0,
    )


@needs_pg
@pytest.mark.asyncio
async def test_cache_lookup_skips_analysis_older_than_last_changed_and_refreshes_in_place():
    """AI cache under role ``tendersrb_app`` (FORCE RLS): the superuser only seeds and cleans."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.api.repo import set_tenant

    sup = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    app = create_async_engine(APP_URL, poolclass=NullPool, future=True)
    tender = tenant = profile = None
    try:
        async with AsyncSession(sup) as s:
            tender, tenant, profile = await _ai_env(s)
            old_id = (
                await s.execute(
                    text(
                        "INSERT INTO tender_ai_analyses (tender_id, profile_id, level, output, created_at) "
                        "VALUES (:t, :p, 'bid', CAST(:o AS jsonb), now() - interval '2 days') "
                        "RETURNING id"
                    ),
                    {"t": tender, "p": profile, "o": '{"v": 1}'},
                )
            ).scalar_one()
            await s.commit()
        try:
            async with AsyncSession(app) as a:
                assert (await a.execute(text("SELECT current_user"))).scalar_one() == (
                    "tendersrb_app"
                )
                await set_tenant(a, tenant)
                # tender not changed → cache hit
                assert await cache_lookup(a, tender, profile, "bid") == str(old_id)
                await a.rollback()
            # tender changed AFTER the analysis → miss (updated by ingest, not by the app)
            async with AsyncSession(sup) as s:
                await s.execute(
                    text(
                        "UPDATE tenders_normalized SET last_changed_at = now() - interval '1 day' "
                        "WHERE id = :t"
                    ),
                    {"t": tender},
                )
                await s.commit()
            async with AsyncSession(app) as a:
                await set_tenant(a, tenant)
                assert await cache_lookup(a, tender, profile, "bid") is None
                # new analysis: UNIQUE does not allow inserting a second one → the row is updated in place
                new_id = await finalize_with_insert(
                    a,
                    tender_id=tender,
                    profile_id=profile,
                    tenant_id=tenant,
                    level="bid",
                    result=_res(),
                )
                assert new_id == str(
                    old_id
                )  # same id: the outbox may reference it
                await a.commit()
            async with AsyncSession(app) as a:
                await set_tenant(a, tenant)
                assert await cache_lookup(a, tender, profile, "bid") == str(old_id)
                out = (
                    await a.execute(
                        text("SELECT output FROM tender_ai_analyses WHERE id = :i"),
                        {"i": old_id},
                    )
                ).scalar_one()
                assert out == {"v": 2}
                await a.rollback()
        finally:
            async with AsyncSession(sup) as s:
                for sql, key, val in (
                    (
                        "DELETE FROM tender_ai_analyses WHERE tender_id = :v",
                        "v",
                        tender,
                    ),
                    ("DELETE FROM ai_usage WHERE tenant_id = :v", "v", tenant),
                    ("DELETE FROM profiles WHERE id = :v", "v", profile),
                    ("DELETE FROM tenders_normalized WHERE id = :v", "v", tender),
                    ("DELETE FROM tenants WHERE id = :v", "v", tenant),
                ):
                    await s.execute(text(sql), {key: val})
                await s.commit()
    finally:
        await sup.dispose()
        await app.dispose()
