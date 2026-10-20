"""Migration 0055: ``source_reconcile`` — the global table of reconciliation with the source.

* without a DB — the chain of revisions, no tenant policy (category U+0432 of RLS_MATRIX), the revocation of the write for the app role;
* on PG (``TEST_SUPERUSER_DATABASE_URL`` — the write as ingest, ``TEST_RLS_DATABASE_URL`` — the app role):
  ``tendersrb_app`` reads, but INSERT/UPDATE/DELETE → ``permission denied``;
  the real upsert of the worker is idempotent by (source_key, day).
"""

from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.workers.source_reconcile import _UPSERT_SQL

_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0055_source_reconcile.py"
)
APP_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", "")
needs_pg = pytest.mark.skipif(
    not (APP_URL and SUPER_URL), reason="нет TEST_RLS/SUPERUSER_DATABASE_URL"
)


def _load():
    spec = importlib.util.spec_from_file_location("migration_0055", _PATH)
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
    assert (m.revision, m.down_revision) == ("0055", "0054")


def test_global_table_no_tenant_policy_and_write_revoked(monkeypatch):
    sql = _sql(monkeypatch, "upgrade")
    assert "CREATE TABLE source_reconcile" in sql
    assert "tenant_id" not in sql and "POLICY" not in sql
    assert "PRIMARY KEY (source_key, day)" in sql
    assert (
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON source_reconcile "
        "FROM tendersrb_app" in sql
    )


def test_downgrade_drops_table(monkeypatch):
    assert "DROP TABLE IF EXISTS source_reconcile" in _sql(monkeypatch, "downgrade")


_ROW = {
    "source_key": "jnportal_test",
    "day": datetime(2026, 10, 14).date(),
    "source_count": 176,
    "our_count": 175,
    "checked_at": datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc),
}


@needs_pg
@pytest.mark.asyncio
async def test_app_role_reads_but_cannot_write_and_upsert_is_idempotent():
    sup = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    app = create_async_engine(APP_URL, poolclass=NullPool, future=True)
    try:
        async with sup.begin() as c:
            await c.execute(
                text("DELETE FROM source_reconcile WHERE source_key = 'jnportal_test'")
            )
            await c.execute(_UPSERT_SQL, _ROW)
            await c.execute(_UPSERT_SQL, {**_ROW, "our_count": 176})
        async with app.connect() as c:
            row = (
                await c.execute(
                    text(
                        "SELECT count(*) AS n, max(our_count) AS o FROM source_reconcile "
                        "WHERE source_key = 'jnportal_test'"
                    )
                )
            ).one()
            assert (row.n, row.o) == (1, 176)
        for stmt in (
            "INSERT INTO source_reconcile (source_key, day, source_count, our_count) "
            "VALUES ('jnportal_test', '2026-10-15', 1, 1)",
            "UPDATE source_reconcile SET our_count = 0",
            "DELETE FROM source_reconcile",
        ):
            async with app.connect() as c:
                with pytest.raises(DBAPIError, match="permission denied"):
                    await c.execute(text(stmt))
    finally:
        async with sup.begin() as c:
            await c.execute(
                text("DELETE FROM source_reconcile WHERE source_key = 'jnportal_test'")
            )
        await sup.dispose()
        await app.dispose()
