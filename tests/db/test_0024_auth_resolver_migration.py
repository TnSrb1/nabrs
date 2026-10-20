"""Unit: migration 0024 (app_resolve_user_for_auth) — the chain of revisions + SQL.

Without a network/DB: ``alembic.op`` is substituted by a fake that intercepts execute().
The live regression under the role ``tendersrb_app`` — in tests/db/test_rls_isolation.py
(``test_email_auth_flow_under_app_role``, requires TEST_RLS_DATABASE_URL).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0024_auth_resolver.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0024", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load_migration()


def test_revision_chain(migration) -> None:
    assert migration.revision == "0024"
    assert migration.down_revision == "0023"


def test_upgrade_creates_resolver_and_grants(migration, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.upgrade()

    joined = "\n".join(calls)
    assert (
        "CREATE OR REPLACE FUNCTION app_resolve_user_for_auth(p_email TEXT)" in joined
    )
    assert "SECURITY DEFINER" in joined
    assert "FROM users" in joined
    assert "WHERE email = p_email AND deleted_at IS NULL" in joined
    assert any(
        "GRANT EXECUTE ON FUNCTION app_resolve_user_for_auth(TEXT) TO tendersrb_app"
        in c
        for c in calls
    )


def test_downgrade_drops_resolver(migration, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.downgrade()

    assert calls == ["DROP FUNCTION IF EXISTS app_resolve_user_for_auth(TEXT)"]
