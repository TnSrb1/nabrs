"""Unit: migration 0026 (app_admin_delivery_* SECURITY DEFINER) — the chain + SQL.

Without a network/DB: ``alembic.op`` is substituted by a fake that intercepts execute().
The live regression under the role ``tendersrb_app`` — in tests/db/test_rls_isolation.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0026_admin_delivery_health_functions.py"
)

_FUNCTIONS = (
    ("app_admin_delivery_backlog", "app_admin_delivery_backlog()"),
    ("app_admin_reminder_freshness", "app_admin_reminder_freshness()"),
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0026", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load_migration()


def test_revision_chain(migration) -> None:
    assert migration.revision == "0026"
    assert migration.down_revision == "0025"


def test_upgrade_creates_all_functions_with_security_definer(migration, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.upgrade()

    joined = "\n".join(calls)
    for fn_name, _sig in _FUNCTIONS:
        assert f"FUNCTION {fn_name}" in joined, f"{fn_name} is not created"
    create_calls = [c for c in calls if "CREATE OR REPLACE FUNCTION" in c]
    assert len(create_calls) == len(_FUNCTIONS)
    for c in create_calls:
        assert "SECURITY DEFINER" in c
        assert "SET search_path = public" in c
        # Only reading of aggregates — STABLE, not VOLATILE.
        assert "STABLE" in c


def test_functions_return_aggregates_only_no_tenant_rows(migration, monkeypatch):
    """INVARIANT VG-29.3: the functions return ONLY aggregates (COUNT/MAX),
    no row-level data of tenants / PII (no tenant_id/profile_id
    in RETURNS TABLE)."""
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.upgrade()

    backlog = next(c for c in calls if "app_admin_delivery_backlog" in c)
    assert "COUNT(" in backlog
    assert "MAX(" in backlog
    assert "delivery_outbox" in backlog

    freshness = next(c for c in calls if "app_admin_reminder_freshness" in c)
    assert "MAX(" in freshness
    assert "reminder_log" in freshness
    assert "subscription_reminder_log" in freshness

    # The RETURNS signature must not contain the identifier columns of the tenant/profile.
    for c in (backlog, freshness):
        returns = c.split("AS $$")[0]
        assert "tenant_id" not in returns
        assert "profile_id" not in returns


def test_upgrade_grants_execute_to_app_role(migration, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.upgrade()

    for _fn_name, sig in _FUNCTIONS:
        assert any(
            f"GRANT EXECUTE ON FUNCTION {sig} TO tendersrb_app" in c for c in calls
        ), f"no GRANT EXECUTE for {sig}"


def test_downgrade_drops_all_functions(migration, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.downgrade()

    joined = "\n".join(calls)
    for _fn_name, sig in _FUNCTIONS:
        fn_name = sig.split("(")[0]
        assert (
            f"DROP FUNCTION IF EXISTS {fn_name}(" in joined
        ), f"{fn_name} is not dropped"
    assert len(calls) == len(_FUNCTIONS)
