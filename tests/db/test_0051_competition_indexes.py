"""Migration 0051: the indexes for the «Konkurencija» block (issue #686).

* without a DB — the chain of revisions and the SQL (CONCURRENTLY, DROP in reverse order);
* on a throwaway PG (``TEST_SUPERUSER_DATABASE_URL``) — both indexes and the function exist and are valid
  after ``upgrade head``; the REAL query of the service (``_BY_NAME_SQL``/``_BY_CPV_SQL``) uses the new indexes in the plan (Index, not Seq Scan); the predicate ``cpv_group3 @> ARRAY[g]`` gives the same
  set of tenders as the previous ``EXISTS (… left(code,3) = g)``.
"""

from __future__ import annotations

import importlib.util
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.cabinet.competition_service import _BY_CPV_SQL, _BY_NAME_SQL

_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0051_competition_indexes.py"
)
_NAME_IDX = "tender_awards_customer_key_idx"
_CPV_IDX = "tenders_normalized_cpv_group3_gin"
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", "")
APP_URL = os.getenv("TEST_RLS_DATABASE_URL", "")


def _load():
    spec = importlib.util.spec_from_file_location("migration_0051", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeCtx:
    @contextmanager
    def autocommit_block(self):
        yield


def _sql(migration, monkeypatch, which) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))
    monkeypatch.setattr(migration.op, "get_context", lambda: _FakeCtx())
    getattr(migration, which)()
    return [" ".join(c.split()) for c in calls]


def test_revision_chain():
    m = _load()
    assert (m.revision, m.down_revision) == ("0051", "0050")


def test_upgrade_creates_function_and_concurrent_indexes(monkeypatch):
    calls = _sql(_load(), monkeypatch, "upgrade")
    assert any("CREATE OR REPLACE FUNCTION cpv_group3(j jsonb)" in c for c in calls)
    assert any(
        f"CREATE INDEX CONCURRENTLY {_NAME_IDX} ON tender_awards "
        "(lower(btrim(regexp_replace(customer_name, '\\s+', ' ', 'g'))))" in c
        for c in calls
    ), calls
    assert any(
        f"CREATE INDEX CONCURRENTLY {_CPV_IDX} ON tenders_normalized "
        "USING gin (cpv_group3(classifications))" in c
        for c in calls
    ), calls


def test_downgrade_drops_indexes_then_function(monkeypatch):
    calls = _sql(_load(), monkeypatch, "downgrade")
    joined = " | ".join(calls)
    assert f"DROP INDEX CONCURRENTLY IF EXISTS {_CPV_IDX}" in joined
    assert f"DROP INDEX CONCURRENTLY IF EXISTS {_NAME_IDX}" in joined
    assert "DROP FUNCTION IF EXISTS cpv_group3(jsonb)" in joined
    # the function — the last: the index depends on it
    assert joined.index("DROP FUNCTION") > joined.index(
        f"DROP INDEX CONCURRENTLY IF EXISTS {_CPV_IDX}"
    )


needs_pg = pytest.mark.skipif(not SUPER_URL, reason="нет TEST_SUPERUSER_DATABASE_URL")
needs_app = pytest.mark.skipif(not APP_URL, reason="нет TEST_RLS_DATABASE_URL")


@needs_pg
@pytest.mark.asyncio
async def test_indexes_and_function_exist_after_head():
    """After ``upgrade head`` both indexes exist and are VALID, the function exists."""
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    try:
        async with eng.connect() as c:
            for name in (_NAME_IDX, _CPV_IDX):
                n = (
                    await c.execute(
                        text(
                            "SELECT count(*) FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
                            "WHERE ic.relname = :n AND i.indisvalid"
                        ),
                        {"n": name},
                    )
                ).scalar_one()
                assert n == 1, name
            fn = (
                await c.execute(
                    text("SELECT count(*) FROM pg_proc WHERE proname = 'cpv_group3'")
                )
            ).scalar_one()
            assert fn == 1
    finally:
        await eng.dispose()


@needs_app
@pytest.mark.asyncio
async def test_service_queries_use_new_indexes():
    """The real SQL of the service uses the new indexes. A seq scan on a tiny table beats
    any index — we disable it to check the AVAILABILITY of the index, not the statistics of an empty DB.
    """
    eng = create_async_engine(APP_URL, poolclass=NullPool, future=True)
    base = {
        "tender_id": "00000000-0000-0000-0000-000000000000",
        "cutoff": date(2024, 10, 13),
        "today": date(2026, 10, 13),
        "limit": 5,
    }
    try:
        async with eng.connect() as c:
            await c.execute(text("SET enable_seqscan = off"))
            name_plan = "\n".join(
                r[0]
                for r in (
                    await c.execute(
                        text("EXPLAIN " + _BY_NAME_SQL.text),
                        {**base, "name": "x"},
                    )
                ).all()
            )
            cpv_plan = "\n".join(
                r[0]
                for r in (
                    await c.execute(
                        text("EXPLAIN " + _BY_CPV_SQL.text),
                        {**base, "group": "331"},
                    )
                ).all()
            )
            await c.rollback()
    finally:
        await eng.dispose()
    assert _NAME_IDX in name_plan, name_plan
    assert _CPV_IDX in cpv_plan, cpv_plan


@needs_app
@pytest.mark.asyncio
async def test_cpv_predicate_matches_old_exists_semantics():
    """``cpv_group3 @> [g]`` == the previous ``EXISTS (… scheme='CPV' AND left(code,3)=g)``.

    The values are arrays of classifications (as in production; NULL too). Non-array forms
    (not an array) are not included here: the previous ``jsonb_array_elements`` would fall on them itself.
    """
    eng = create_async_engine(APP_URL, poolclass=NullPool, future=True)
    cases = [
        '[{"scheme": "CPV", "code": "33141000-0"}]',
        '[{"scheme": "CPV", "code": "30192000"}, {"scheme": "CPV", "code": "33100000"}]',
        '[{"scheme": "CPV", "code": "33"}]',
        '[{"scheme": "NACE", "code": "331"}]',
        '[{"scheme": "CPV", "code": "30192000"}]',
        "[]",
        None,
    ]
    rows_sql = " UNION ALL ".join(
        f"SELECT CAST(:v{i} AS jsonb) AS v" for i in range(len(cases))
    )
    params = {f"v{i}": v for i, v in enumerate(cases)}
    try:
        async with eng.connect() as c:
            rows = (
                await c.execute(
                    text(
                        "SELECT v, cpv_group3(v) @> ARRAY['331']::text[] AS new_m, "
                        "EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE(v, '[]'::jsonb)) e "
                        "WHERE e->>'scheme' = 'CPV' AND left(e->>'code', 3) = '331') AS old_m "
                        f"FROM ({rows_sql}) s"
                    ),
                    params,
                )
            ).all()
            await c.rollback()
    finally:
        await eng.dispose()
    assert len(rows) == len(cases)
    mismatch = [r for r in rows if r.new_m != r.old_m]
    assert not mismatch, mismatch
    # matches exist in both directions — otherwise the equivalence proves nothing
    assert sum(1 for r in rows if r.new_m) == 2
