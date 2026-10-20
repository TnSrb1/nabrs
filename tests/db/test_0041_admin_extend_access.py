"""Unit: migration 0041 (the access extension + the notice recipient, SECURITY DEFINER).

Without a network/DB (``op.execute`` intercepted) — the RLS regression under the app role lives in
``tests/db/test_rls_isolation.py`` and skips without ``TEST_RLS_DATABASE_URL``, therefore we check the contract of the functions by the SQL of the migration ITSELF: this is the only check that always works in the CI (the pattern — ``test_rls_force_invariant``).

What we pin:
* the chain of revisions 0040 → 0041;
* both functions — ``SECURITY DEFINER`` with ``SET search_path = public`` and the GRANT to the
  role ``tendersrb_app``: without any of the three the bot and the admin under the app role fall
  with ``UndefinedObjectError`` (FORCE RLS), or a hole appears;
* ‼️ the MIRROR of the arithmetic: the body of the extension is VERBATIM
  ``src.domain.access.extend_until_sql("tenants", "p_days")``. There must be no second arithmetic
  of the extension in the system: once diverged, the SQL side would again start
  cutting the access (#497), and no mock test would see it;
* the extension EXTENDS (GREATEST from the effective term), rather than sets;
* the migration is additive: it does not delete other functions, does not touch the RLS/schema;
* the downgrade removes exactly its two functions.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.domain.access import extend_until_sql

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0041_admin_extend_access.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("migration_0041", _MODULE_PATH)
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
    assert migration.revision == "0041"
    assert migration.down_revision == "0040"


@pytest.mark.parametrize(
    "fn", ["app_admin_extend_access", "app_admin_tenant_notify_target"]
)
def test_functions_are_security_definer_with_fixed_search_path(
    migration, monkeypatch, fn
):
    joined = _sql(migration, monkeypatch)
    body = joined.split(f"FUNCTION {fn}", 1)[1]
    head = body.split("$$", 1)[0]
    assert "SECURITY DEFINER" in head, f"{fn}: without DEFINER the app role will not pass the RLS"
    assert (
        "SET search_path = public" in head
    ), f"{fn}: the search_path must be fixed"


@pytest.mark.parametrize(
    "signature",
    ["app_admin_extend_access(uuid, int)", "app_admin_tenant_notify_target(uuid)"],
)
def test_execute_granted_to_app_role(migration, monkeypatch, signature):
    joined = " ".join(_sql(migration, monkeypatch).split())
    assert f"GRANT EXECUTE ON FUNCTION {signature} TO tendersrb_app" in joined


def test_extend_expression_mirrors_domain_helper(migration, monkeypatch):
    """‼️ The core of the task: one arithmetic of the extension for the whole system.

    The Python side (``extend_until``), the SQL brick (``extend_until_sql``) and the body of the function of 0041 are one expression. If someone “fixes” it only here,
    the admin and the bot will again start cutting the access of a tenant whose term is further.
    """
    joined = " ".join(_sql(migration, monkeypatch).split())
    assert extend_until_sql("tenants", "p_days") in joined


def test_extend_extends_and_never_sets(migration, monkeypatch):
    """An extension, not a setting: the base is the larger of the dates, the term only grows."""
    extend = _sql(migration, monkeypatch).split("app_admin_tenant_notify_target", 1)[0]
    assert "GREATEST(COALESCE(tenants.access_until, now()), now())" in extend
    assert "RETURNING access_until" in extend, "the term must return to the caller"
    assert "SET access_until = now()" not in " ".join(
        extend.split()
    ), "a setting instead of an extension would shorten the already granted access"


def test_notify_target_reads_channel_and_email_cross_tenant(migration, monkeypatch):
    """The notice recipient — the Telegram chat + the email of a live user of a FOREIGN tenant.

    Under the app role this same SELECT from the bot fell (users/user_channels under FORCE
    RLS, the operation has no GUC) — because of it the decision buttons did not work at all,
    not only on the access grant.
    """
    target = _sql(migration, monkeypatch).split("app_admin_tenant_notify_target", 1)[1]
    assert "FROM users u" in target and "u.tenant_id = p_tid" in target
    assert "u.deleted_at IS NULL" in target
    assert "uc.channel = 'telegram'" in target and "uc.status = 'active'" in target


def test_migration_is_additive(migration, monkeypatch):
    """It is additive: it does not delete other functions, does not touch the RLS/schema."""
    joined = _sql(migration, monkeypatch)
    assert "DROP" not in joined.upper()
    assert "DELETE FROM" not in joined.upper()
    assert "ROW LEVEL SECURITY" not in joined
    assert "CREATE POLICY" not in joined and "DROP POLICY" not in joined
    assert "ALTER TABLE" not in joined
    # ‼️ The contract of the webhook (the absolute date of the provider) stays as it is.
    assert "app_admin_grant_access" not in joined


def test_downgrade_drops_only_own_functions(migration, monkeypatch):
    joined = _sql(migration, monkeypatch, which="downgrade")
    assert "DROP FUNCTION IF EXISTS app_admin_extend_access(uuid, int)" in joined
    assert "DROP FUNCTION IF EXISTS app_admin_tenant_notify_target(uuid)" in joined
    assert "app_admin_grant_access" not in joined
    assert "trial_request" not in joined
