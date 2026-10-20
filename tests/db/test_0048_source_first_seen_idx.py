"""Migration 0048: the index for the “collecting since” of the /izvori showcase.

* without a DB — the chain of revisions and the SQL (CREATE/DROP INDEX);
* on a throwaway PG (``TEST_SUPERUSER_DATABASE_URL``) — the index exists after
  ``upgrade head``, the downgrade/upgrade of 0048 are symmetric, and the query of the showcase
  ``_ENABLED_SOURCES_PAGE_SQL`` uses EXACTLY it in the plan (Index, not Seq Scan).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.public.sources_page import _ENABLED_SOURCES_PAGE_SQL

_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0048_tenders_source_first_seen_idx.py"
)
_INDEX = "tenders_normalized_source_key_first_seen_at_idx"
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", "")


def _load():
    spec = importlib.util.spec_from_file_location("migration_0048", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(migration, monkeypatch, which) -> str:
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))
    getattr(migration, which)()
    return " ".join(" ".join(calls).split())


def test_revision_chain():
    m = _load()
    assert (m.revision, m.down_revision) == ("0048", "0047")


def test_upgrade_creates_index(monkeypatch):
    sql = _sql(_load(), monkeypatch, "upgrade")
    assert f"CREATE INDEX IF NOT EXISTS {_INDEX}" in sql
    assert "ON tenders_normalized (source_key, first_seen_at)" in sql
    assert "CONCURRENTLY" not in sql  # inside an alembic transaction it is not allowed


def test_downgrade_drops_index(monkeypatch):
    assert f"DROP INDEX IF EXISTS {_INDEX}" in _sql(_load(), monkeypatch, "downgrade")


needs_pg = pytest.mark.skipif(not SUPER_URL, reason="нет TEST_SUPERUSER_DATABASE_URL")


@needs_pg
@pytest.mark.asyncio
async def test_index_exists_and_downgrade_roundtrip():
    """The index exists after head; the REAL downgrade()/upgrade() of 0048 remove it and return it."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    m = _load()

    def _run(sync_conn, fn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            fn()

    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    q = text("SELECT count(*) FROM pg_indexes WHERE indexname = :n")
    try:
        async with eng.connect() as c:
            assert (await c.execute(q, {"n": _INDEX})).scalar_one() == 1
            try:
                await c.run_sync(_run, m.downgrade)
                assert (await c.execute(q, {"n": _INDEX})).scalar_one() == 0
            finally:
                await c.run_sync(_run, m.upgrade)
                await c.commit()
            assert (await c.execute(q, {"n": _INDEX})).scalar_one() == 1
    finally:
        await eng.dispose()


@needs_pg
@pytest.mark.asyncio
async def test_sources_page_query_uses_index():
    """The plan of the showcase query contains an Index Scan on the new index (not a seq scan)."""
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    try:
        async with eng.connect() as c:
            # A seq scan on a tiny table beats any index — we disable it,
            # so as to check the AVAILABILITY of the index to the query, not the statistics of an empty DB.
            await c.execute(text("SET enable_seqscan = off"))
            await c.execute(
                text(
                    "INSERT INTO sources (source_key, display_name, enabled) "
                    "VALUES ('zz-idx-probe','p',true) ON CONFLICT DO NOTHING"
                )
            )
            plan = "\n".join(
                r[0]
                for r in (
                    await c.execute(text("EXPLAIN " + _ENABLED_SOURCES_PAGE_SQL.text))
                ).all()
            )
            await c.rollback()
    finally:
        await eng.dispose()
    assert _INDEX in plan, plan
