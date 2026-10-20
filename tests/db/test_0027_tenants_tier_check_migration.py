"""Unit: migration 0027 (the canon of tenants.tier — backfill + CHECK/DEFAULT/NOT NULL).

Without a network/DB: ``alembic.op`` is substituted by a fake that intercepts execute().
The live up/down on an ephemeral PG is run separately (see REPORT of task R8).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.domain.tiers import TIER_ORDER

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0027_tenants_tier_check.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0027", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load_migration()


def test_revision_chain(migration) -> None:
    assert migration.revision == "0027"
    assert migration.down_revision == "0026"


def test_canon_matches_domain_tier_order(migration) -> None:
    """The canon literal in the migration MUST match the SSOT TIER_ORDER —
    otherwise the CHECK would diverge from the gating (the normalizer)."""
    assert tuple(migration._CANON_TIERS) == TIER_ORDER


def test_upgrade_backfills_before_constraints(migration, monkeypatch) -> None:
    """The order MATTERS: the backfill (UPDATE) goes BEFORE NOT NULL/CHECK, otherwise ALTER
    would fail on the existing 'free'/NULL."""
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.upgrade()

    joined = "\n".join(calls)
    # The backfill normalizes free/NULL/non-canon → lite.
    backfill = next(c for c in calls if "UPDATE tenants" in c)
    assert "SET tier = 'lite'" in backfill
    assert "tier IS NULL" in backfill
    assert "NOT IN" in backfill

    idx_update = next(i for i, c in enumerate(calls) if "UPDATE tenants" in c)
    idx_notnull = next(i for i, c in enumerate(calls) if "SET NOT NULL" in c)
    idx_check = next(i for i, c in enumerate(calls) if "ADD CONSTRAINT" in c)
    assert idx_update < idx_notnull < idx_check

    # All parts of the invariant are present.
    assert "SET DEFAULT 'lite'" in joined
    assert "ck_tenants_tier" in joined
    for t in TIER_ORDER:
        assert f"'{t}'" in joined


def test_downgrade_drops_in_reverse_order(migration, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.downgrade()

    joined = "\n".join(calls)
    assert "DROP CONSTRAINT IF EXISTS ck_tenants_tier" in joined
    assert "DROP NOT NULL" in joined
    assert "DROP DEFAULT" in joined
    idx_check = next(i for i, c in enumerate(calls) if "DROP CONSTRAINT" in c)
    idx_notnull = next(i for i, c in enumerate(calls) if "DROP NOT NULL" in c)
    idx_default = next(i for i, c in enumerate(calls) if "DROP DEFAULT" in c)
    assert idx_check < idx_notnull < idx_default
