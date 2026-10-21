"""Unit: migration 0037 (disabling/restoring a subscriber via SECURITY DEFINER).

Without a network/DB (``op.execute`` intercepted) — the RLS regression under the app role lives in
``tests/db/test_rls_isolation.py`` and skips without ``TEST_RLS_DATABASE_URL``, therefore we check the contract of the functions by the SQL of the migration ITSELF: this is the only check that always works in the CI (the pattern — ``test_rls_force_invariant``).

What we pin:
* the chain of revisions 0036 → 0037;
* both functions — ``SECURITY DEFINER`` with ``SET search_path = public`` and the GRANT of
  the role ``tendersrb_app``: without any of the three the cross-tenant admin either does not
  work under the app role, or becomes a hole;
* disable switches off ALL THREE axes (access, channels, login) — otherwise the defect of
  revoke_access repeats, where the bot went on answering the disabled one;
* enable removes exactly what disable switched off, and does NOT touch the term at ``p_days = 0``;
* the migration does not delete data and does not weaken the RLS.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0037_admin_disable_subscriber.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("migration_0037", _MODULE_PATH)
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
    assert migration.revision == "0037"
    assert migration.down_revision == "0036"


@pytest.mark.parametrize(
    "fn", ["app_admin_disable_subscriber", "app_admin_enable_subscriber"]
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
    ["app_admin_disable_subscriber(uuid)", "app_admin_enable_subscriber(uuid, int)"],
)
def test_execute_granted_to_app_role(migration, monkeypatch, signature):
    joined = _sql(migration, monkeypatch)
    assert f"GRANT EXECUTE ON FUNCTION {signature} TO tendersrb_app" in joined.replace(
        "\n", " "
    ).replace("  ", " ")


def test_disable_closes_all_three_axes(migration, monkeypatch):
    """‼️ The core of the task: one axis is not enough.

    `revoke_access` (0025) moved only `access_until`, and the disabled subscriber went on receiving the bot’s answers by the linked channel and logging into the cabinet
    by a fresh magic-link.
    """
    disable = _sql(migration, monkeypatch).split("app_admin_enable_subscriber", 1)[0]
    assert "UPDATE tenants" in disable and "access_until = now()" in disable
    assert "UPDATE user_channels" in disable and "'disabled'" in disable
    assert "UPDATE users" in disable and "deleted_at = now()" in disable


def test_enable_restores_channels_and_login_without_touching_term_by_default(
    migration, monkeypatch
):
    enable = _sql(migration, monkeypatch).split("app_admin_enable_subscriber", 1)[1]
    assert "deleted_at = NULL" in enable
    assert "status = 'active'" in enable and "'disabled'" in enable
    # The term is touched ONLY at p_days > 0 — otherwise one button would silently decide
    # two different questions (restoring a subscriber ≠ granting a new term).
    assert "IF p_days > 0 THEN" in enable


def test_channels_are_selected_through_users_not_tenant_column(migration, monkeypatch):
    """`user_channels` has no `tenant_id` — the isolation is via JOIN users (0022)."""
    joined = _sql(migration, monkeypatch)
    assert "user_id IN (SELECT id FROM users WHERE tenant_id = p_tenant)" in joined
    assert "user_channels" in joined and "user_channels.tenant_id" not in joined


def test_migration_deletes_nothing_and_does_not_touch_rls(migration, monkeypatch):
    """The disabling is soft: neither DELETE nor edits of the policies/schema."""
    joined = _sql(migration, monkeypatch)
    assert "DELETE FROM" not in joined.upper()
    assert "ROW LEVEL SECURITY" not in joined
    assert "CREATE POLICY" not in joined and "DROP POLICY" not in joined
    assert "ALTER TABLE" not in joined


def test_downgrade_drops_both_functions(migration, monkeypatch):
    joined = _sql(migration, monkeypatch, which="downgrade")
    assert "DROP FUNCTION IF EXISTS app_admin_disable_subscriber(uuid)" in joined
    assert "DROP FUNCTION IF EXISTS app_admin_enable_subscriber(uuid, int)" in joined
