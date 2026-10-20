"""Unit: migration 0045 (SRC-0a) — without a DB, by the SQL/operations of the migration itself.

The live up/down/up — on a throwaway PG (TESTING_src0a_registry.md); here what
works in the CI always:

* the chain 0044 → 0045;
* the backfill of the live source keeps its behavior (the cursor is the same, the silent-pass cut-off does not cut) and selects the rows by ``enabled``, not by a literal;
* the backfill of profiles — ``created_at`` for each key, writes ONLY ``profiles``
  (``tenders_normalized`` wakes routing by a trigger — it must not be touched);
* the downgrade is symmetric.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0045_source_registry_optin.py"
)


@pytest.fixture
def migration():
    spec = importlib.util.spec_from_file_location("migration_0045", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ops(migration, monkeypatch, which):
    calls: list[tuple[str, tuple]] = []
    for name in ("execute", "add_column", "drop_column"):
        monkeypatch.setattr(
            migration.op,
            name,
            lambda *a, _n=name, **k: calls.append((_n, a)),
        )
    getattr(migration, which)()
    return calls


def test_revision_chain(migration):
    assert migration.revision == "0045"
    assert migration.down_revision == "0044"


def test_upgrade_adds_three_columns(migration, monkeypatch):
    added = [
        (a[0], a[1].name)
        for n, a in _ops(migration, monkeypatch, "upgrade")
        if n == "add_column"
    ]
    assert added == [
        ("sources", "enabled_at"),
        ("sources", "baseline_at"),
        ("profiles", "source_opt_in"),
    ]


def test_live_source_backfill_keeps_behaviour(migration, monkeypatch):
    sql = " ".join(
        " ".join(str(a[0]).split())
        for n, a in _ops(migration, monkeypatch, "upgrade")
        if n == "execute"
    )
    assert "SET enabled_at = COALESCE(last_run_at, now())" in sql
    assert "baseline_at = TIMESTAMPTZ '1970-01-01 00:00:00+00'" in sql
    assert "WHERE enabled = true" in sql
    assert "jnportal" not in sql


def test_profile_backfill_uses_created_at_and_touches_no_tenders(
    migration, monkeypatch
):
    sql = " ".join(
        " ".join(str(a[0]).split())
        for n, a in _ops(migration, monkeypatch, "upgrade")
        if n == "execute"
    )
    assert "jsonb_object_agg(k, to_jsonb(p.created_at))" in sql
    assert "FROM unnest(p.enabled_source_keys) AS k" in sql
    assert "tenders_normalized" not in sql


def test_downgrade_is_symmetric(migration, monkeypatch):
    dropped = [a for n, a in _ops(migration, monkeypatch, "downgrade")]
    assert dropped == [
        ("profiles", "source_opt_in"),
        ("sources", "baseline_at"),
        ("sources", "enabled_at"),
    ]
