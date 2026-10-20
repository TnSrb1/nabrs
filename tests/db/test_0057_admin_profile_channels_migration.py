"""Unit: migration 0057 (template connection date, template and subscriber channels).

Without DB (``op.execute`` intercepted), the same technique as for 0039. A live run under the
role ``tendersrb_app`` — in ``tests/db/test_0057_admin_profile_channels_rls.py``.

What we pin down:
* the revision chain 0056 → 0057;
* ``app_admin_list_profiles`` — DROP + CREATE (``RETURNS TABLE`` changes) with the
  columns ``created_at`` and ``delivery_channels``, the predicate ``p.deleted_at IS NULL``
  preserved;
* ``app_admin_user_channels(uid)`` — SECURITY DEFINER, filter by ``user_id``
  (not by tenant_id), GRANT ``tendersrb_app``;
* both functions — with a fixed search_path and a re-issued GRANT;
* the migration does not touch data or RLS;
* downgrade restores verbatim the 0025 definition (without the new columns).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0057_admin_profile_dates_channels.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("migration_0057", _MODULE_PATH)
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
    assert migration.revision == "0057"
    assert migration.down_revision == "0056"


def test_list_profiles_dropped_before_recreate(migration, monkeypatch):
    """RETURNS TABLE changes — CREATE OR REPLACE would fail on Postgres."""
    joined = _sql(migration, monkeypatch)
    assert "DROP FUNCTION IF EXISTS app_admin_list_profiles()" in joined


def test_list_profiles_returns_created_at_and_delivery_channels(migration, monkeypatch):
    joined = _sql(migration, monkeypatch)
    body = joined.split("FUNCTION app_admin_list_profiles", 1)[1]
    head = body.split("AS $$", 1)[0]
    sql_body = body.split("AS $$", 1)[1].split("$$", 1)[0]
    assert "created_at timestamptz" in head
    assert "delivery_channels text[]" in head
    assert "p.created_at" in sql_body
    assert "p.delivery_channels" in sql_body
    # The selection predicate for templates is not weakened.
    assert "WHERE p.deleted_at IS NULL" in sql_body


def test_user_channels_function_filters_by_user_id(migration, monkeypatch):
    """Channels are tied to the user (user_channels.user_id), not to the tenant."""
    joined = _sql(migration, monkeypatch)
    body = joined.split("FUNCTION app_admin_user_channels", 1)[1]
    head = body.split("AS $$", 1)[0]
    sql_body = body.split("AS $$", 1)[1].split("$$", 1)[0]
    assert "p_uid uuid" in head
    assert "channel text" in head and "status text" in head
    assert "linked_at timestamptz" in head
    assert "WHERE uc.user_id = p_uid" in sql_body
    assert "tenant_id" not in sql_body


@pytest.mark.parametrize(
    "fn", ["app_admin_list_profiles()", "app_admin_user_channels(uuid)"]
)
def test_functions_security_definer_fixed_search_path_and_grant(
    migration, monkeypatch, fn
):
    """Without DEFINER the app role will not pass FORCE RLS; DROP removes the previous GRANT."""
    joined = _sql(migration, monkeypatch)
    name = fn.split("(")[0]
    body = joined.split(f"FUNCTION {name}", 1)[1]
    head = body.split("$$", 1)[0]
    assert "SECURITY DEFINER" in head, f"{fn}: without DEFINER it will not pass RLS"
    assert "SET search_path = public" in head
    flat = " ".join(joined.split())
    assert f"GRANT EXECUTE ON FUNCTION {fn} TO tendersrb_app" in flat


def test_migration_touches_no_data_and_no_rls(migration, monkeypatch):
    joined = _sql(migration, monkeypatch)
    assert "DELETE FROM" not in joined.upper()
    assert "UPDATE " not in joined.upper()
    assert "ROW LEVEL SECURITY" not in joined
    assert "CREATE POLICY" not in joined and "DROP POLICY" not in joined
    assert "ALTER TABLE" not in joined


def test_downgrade_restores_0025_definition_verbatim(migration, monkeypatch):
    joined = _sql(migration, monkeypatch, which="downgrade")
    body = joined.split("FUNCTION app_admin_list_profiles", 1)[1]
    head = body.split("AS $$", 1)[0]
    assert "match_7d bigint" in head
    assert "created_at" not in head and "delivery_channels" not in head
    assert "DROP FUNCTION IF EXISTS app_admin_user_channels(uuid)" in joined
    assert "GRANT EXECUTE ON FUNCTION app_admin_list_profiles() TO tendersrb_app" in (
        " ".join(joined.split())
    )
