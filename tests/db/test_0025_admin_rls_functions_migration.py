"""Unit: migration 0025 (app_admin_* SECURITY DEFINER functions) — the chain of revisions + SQL.

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
    / "0025_admin_rls_functions.py"
)

_FUNCTIONS = (
    ("app_admin_list_users", "app_admin_list_users(INT, INT)"),
    ("app_admin_users_total", "app_admin_users_total()"),
    ("app_admin_get_user", "app_admin_get_user(uuid)"),
    ("app_admin_list_profiles", "app_admin_list_profiles()"),
    ("app_admin_user_delivered_count", "app_admin_user_delivered_count(uuid)"),
    ("app_admin_grant_access", "app_admin_grant_access(uuid, timestamptz)"),
    ("app_admin_revoke_access", "app_admin_revoke_access(uuid)"),
    ("app_admin_set_tier", "app_admin_set_tier(uuid, text)"),
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0025", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load_migration()


def test_revision_chain(migration) -> None:
    assert migration.revision == "0025"
    assert migration.down_revision == "0024"


def test_upgrade_creates_all_functions_with_security_definer(migration, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.upgrade()

    joined = "\n".join(calls)
    for fn_name, _sig in _FUNCTIONS:
        assert f"FUNCTION {fn_name}" in joined, f"{fn_name} is not created"
    # Each CREATE line contains SECURITY DEFINER + the search_path guard.
    create_calls = [c for c in calls if "CREATE OR REPLACE FUNCTION" in c]
    assert len(create_calls) == len(_FUNCTIONS)
    for c in create_calls:
        assert "SECURITY DEFINER" in c
        assert "SET search_path = public" in c


def test_list_profiles_uses_delivery_outbox_not_tender_user_status(
    migration, monkeypatch
) -> None:
    """A regression guard (moved from tests/api/test_admin.py): app_admin_list_profiles
    must join delivery_outbox by profile_id, not tender_user_status
    (it has no profile_id column)."""
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.upgrade()

    body = next(c for c in calls if "app_admin_list_profiles" in c)
    assert "delivery_outbox" in body
    assert "dox.profile_id" in body
    assert "tender_user_status" not in body


def test_upgrade_grants_execute_to_app_role(migration, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.upgrade()

    for _fn_name, sig in _FUNCTIONS:
        assert any(
            f"GRANT EXECUTE ON FUNCTION {sig} TO tendersrb_app" in c for c in calls
        ), f"no GRANT EXECUTE for {sig}"


def test_upgrade_write_functions_are_volatile_others_stable(migration, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.upgrade()

    write_fns = {
        "app_admin_grant_access",
        "app_admin_revoke_access",
        "app_admin_set_tier",
    }
    for c in calls:
        if "CREATE OR REPLACE FUNCTION" not in c:
            continue
        fn_name = c.split("FUNCTION")[1].split("(")[0].strip()
        if fn_name in write_fns:
            assert "VOLATILE" in c, f"{fn_name} must be VOLATILE (it writes tenants)"
            assert "UPDATE tenants" in c
        else:
            assert "STABLE" in c, f"{fn_name} must be STABLE (read only)"


def test_downgrade_drops_all_functions(migration, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))

    migration.downgrade()

    joined = "\n".join(calls)
    for _fn_name, sig in _FUNCTIONS:
        # The downgrade uses lowercase int/uuid/text — we check by the function name.
        fn_name = sig.split("(")[0]
        assert (
            f"DROP FUNCTION IF EXISTS {fn_name}(" in joined
        ), f"{fn_name} is not dropped"
    assert len(calls) == len(_FUNCTIONS)
