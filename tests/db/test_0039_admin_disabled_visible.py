"""Unit: migration 0039 (a disabled subscriber is visible in /admin/users and can be restored).

Without a network/DB (``op.execute`` intercepted), the same trick as 0025/0037 —
the live regression under the role ``tendersrb_app`` is in ``tests/db/test_rls_isolation.py``
(it skips without ``TEST_RLS_DATABASE_URL``).

What we pin:
* the chain of revisions 0038 → 0039;
* ``app_admin_list_users``/``app_admin_get_user`` — DROP + CREATE (not REPLACE,
  the ``RETURNS TABLE`` changes) with the new column ``deleted_at`` and WITHOUT the filter
  ``u.deleted_at IS NULL`` — otherwise the disabled subscriber stays invisible,
  and the “Restore” button on his card is unreachable;
* ``app_admin_users_total`` counts the same set as the list (without the filter) —
  otherwise the pagination would diverge;
* both DROP+CREATE functions — SECURITY DEFINER with a fixed search_path and
  the GRANT to ``tendersrb_app`` again (DROP removes the old grant);
* the downgrade returns verbatim the definitions of 0025 (with the filter), including the grant.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0039_admin_disabled_visible.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("migration_0039", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load()


def _sql(migration, monkeypatch, which="upgrade") -> str:
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))
    getattr(migration, which)()
    return "\n".join(calls)


def test_revision_chain(migration):
    assert migration.revision == "0039"
    assert migration.down_revision == "0038"


def test_list_and_get_user_are_dropped_before_recreate(migration, monkeypatch):
    """RETURNS TABLE changes (+deleted_at) — CREATE OR REPLACE would fall on
    Postgres ('cannot change return type of existing function')."""
    joined = _sql(migration, monkeypatch)
    assert "DROP FUNCTION IF EXISTS app_admin_list_users(int, int)" in joined
    assert "DROP FUNCTION IF EXISTS app_admin_get_user(uuid)" in joined


@pytest.mark.parametrize("fn", ["app_admin_list_users", "app_admin_get_user"])
def test_deleted_at_filter_removed_and_column_added(migration, monkeypatch, fn):
    """‼️ The core of the task: without removing the filter the disabled subscriber stays
    invisible in the list/card, and the “Restore” button is unreachable."""
    joined = _sql(migration, monkeypatch)
    body = joined.split(f"FUNCTION {fn}", 1)[1]
    head = body.split("AS $$", 1)[0]
    sql_body = body.split("AS $$", 1)[1]
    assert "deleted_at timestamptz" in head, f"{fn}: the new column is not declared"
    assert (
        "WHERE u.deleted_at IS NULL" not in sql_body
        and "AND u.deleted_at IS NULL" not in sql_body
    ), f"{fn}: the deleted_at filter still hides the disabled ones"
    assert "u.deleted_at" in sql_body, f"{fn}: the deleted_at column is not selected"


def test_users_total_has_no_deleted_at_filter(migration, monkeypatch):
    """The counter must match what the list really returns — otherwise
    the pagination would diverge."""
    joined = _sql(migration, monkeypatch)
    body = joined.split("FUNCTION app_admin_users_total", 1)[1]
    assert "WHERE deleted_at IS NULL" not in body
    assert "SELECT COUNT(*) FROM users" in body


@pytest.mark.parametrize(
    "fn", ["app_admin_list_users", "app_admin_get_user", "app_admin_users_total"]
)
def test_functions_are_security_definer_with_fixed_search_path(
    migration, monkeypatch, fn
):
    joined = _sql(migration, monkeypatch)
    body = joined.split(f"FUNCTION {fn}", 1)[1]
    head = body.split("$$", 1)[0]
    assert (
        "SECURITY DEFINER" in head
    ), f"{fn}: without DEFINER the app role will not pass the RLS"
    assert "SET search_path = public" in head, f"{fn}: the search_path must be fixed"


@pytest.mark.parametrize(
    "signature",
    [
        "app_admin_list_users(INT, INT)",
        "app_admin_get_user(uuid)",
        "app_admin_users_total()",
    ],
)
def test_execute_regranted_to_app_role(migration, monkeypatch, signature):
    """DROP removes the previous grant — without a repeated GRANT the admin would get
    permission denied already on production."""
    joined = _sql(migration, monkeypatch).replace("\n", " ")
    assert f"GRANT EXECUTE ON FUNCTION {signature} TO tendersrb_app" in " ".join(
        joined.split()
    )


def test_migration_deletes_nothing_and_does_not_touch_rls(migration, monkeypatch):
    joined = _sql(migration, monkeypatch)
    assert "DELETE FROM" not in joined.upper()
    assert "ROW LEVEL SECURITY" not in joined
    assert "CREATE POLICY" not in joined and "DROP POLICY" not in joined
    assert "ALTER TABLE" not in joined


def test_downgrade_restores_0025_definitions_verbatim(migration, monkeypatch):
    joined = _sql(migration, monkeypatch, which="downgrade")
    assert "WHERE u.id = p_uid AND u.deleted_at IS NULL" in joined
    assert "WHERE u.deleted_at IS NULL" in joined
    assert "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL" in joined
    # The previous RETURNS TABLE — without deleted_at.
    get_user_body = joined.split("FUNCTION app_admin_get_user", 1)[1]
    head = get_user_body.split("AS $$", 1)[0]
    assert "deleted_at timestamptz" not in head
    for signature in (
        "app_admin_list_users(INT, INT)",
        "app_admin_get_user(uuid)",
        "app_admin_users_total()",
    ):
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO tendersrb_app" in " ".join(
            joined.split()
        )
