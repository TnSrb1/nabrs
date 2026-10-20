"""Unit: migration 0029 (workflow) — the chain of revisions + SQL (op.execute intercepted).

Without a network/DB. The live up/down regression — on an ephemeral PG (see REPORT); the RLS regression
under the app role — tests/db/test_rls_isolation.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0029_workflow_folders_notes.py"
)

_TABLES = ("tender_folders", "folder_items", "tender_notes")


def _load():
    spec = importlib.util.spec_from_file_location("migration_0029", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load()


def _calls(migration, monkeypatch, which="upgrade"):
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))
    getattr(migration, which)()
    return calls


def test_revision_chain(migration):
    assert migration.revision == "0029"
    assert migration.down_revision == "0028"


def test_creates_all_three_tables(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch))
    for table in _TABLES:
        assert f"CREATE TABLE {table}" in joined


def test_every_table_has_force_rls_and_policy(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch))
    for table in _TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in joined
        assert f"CREATE POLICY tenant_isolation ON {table}" in joined
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in joined
    # A direct policy by the GUC app.current_tenant_id (without the missing_ok flag).
    assert "current_setting('app.current_tenant_id')::uuid" in joined


def test_tenant_scoped_columns_and_fks(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch))
    # All three — with a direct tenant_id column → tenants(id).
    assert joined.count("tenant_id uuid NOT NULL REFERENCES tenants(id)") == 3
    # folder_items / tender_notes reference tenders_normalized(id).
    assert "REFERENCES tenders_normalized(id)" in joined
    # folder_items references tender_folders(id).
    assert "REFERENCES tender_folders(id)" in joined


def test_uniqueness_constraints(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch))
    # The uniqueness of the folder name within a tenant (case-insensitive).
    assert "tender_folders_tenant_name_uniq" in joined
    assert "lower(name)" in joined
    # One tender in a folder at most once.
    assert "UNIQUE (folder_id, tender_id)" in joined
    # One note per (tenant, tender).
    assert "UNIQUE (tenant_id, tender_id)" in joined


def test_no_security_definer(migration, monkeypatch):
    # A cross-tenant access is not needed → we do not create SECURITY DEFINER.
    joined = "\n".join(_calls(migration, monkeypatch))
    assert "SECURITY DEFINER" not in joined


def test_downgrade_drops_all_tables(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch, which="downgrade"))
    for table in _TABLES:
        assert f"DROP TABLE IF EXISTS {table}" in joined
