"""Unit: migration 0040 (the AI spend snapshot — ai_spend_daily + ai_spend_alert_log).

Without a network/DB (``op.execute`` intercepted): we check the contract of the tables by the SQL of the migration ITSELF — the only check that always works in the CI (the pattern — ``test_0038_user_channel_locale``). The semantics of the rollout on a live PostgreSQL is proved by a run of `alembic upgrade/downgrade` on a one-off database (REPORT).

What we pin:
* the chain of revisions 0039 → 0040 (a single head — the guard is
  ``test_0038_user_channel_locale::test_single_alembic_head``);
* ‼️ the tables are PLATFORM ones: neither ``tenant_id``, nor a policy, nor RLS. A tenant column would drag
  ``FORCE ROW LEVEL SECURITY`` in its wake (the CLAUDE.md invariant), and FORCE applies also to the OWNER — the privileged worker
  (which goes without ``SET app.current_tenant_id``) would read 0 rows from its own table.
  The test guards exactly this: “a tenant_id will not appear here”;
* ``day`` — the PRIMARY KEY of the snapshot: the target of ``ON CONFLICT (day) DO UPDATE``, a repeated
  run for the same day overwrites the row rather than multiplying them;
* ``period`` in the snapshot is NOT NULL: the daily delta is computed only within
  one accounting period (a change of month → there is no yesterday’s snapshot of the same period
  → the delta = today’s total);
* the alert journal with the PK ``(day, kind)`` — “one alert of a kind per day, once”;
* ``REVOKE ALL ... FROM tendersrb_app`` on both tables (0021 granted DEFAULT
  PRIVILEGES to future tables; these are only for the owner worker);
* the downgrade removes both tables.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = REPO_ROOT / "alembic" / "versions" / "0040_ai_spend_daily.py"


def _load():
    spec = importlib.util.spec_from_file_location("migration_0040", _MODULE_PATH)
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


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def test_revision_chain(migration):
    assert migration.revision == "0040"
    assert migration.down_revision == "0039"


def test_snapshot_table_columns(migration, monkeypatch):
    upgrade = _flat(_sql(migration, monkeypatch))
    assert "CREATE TABLE ai_spend_daily" in upgrade
    for column in (
        "day date PRIMARY KEY",
        "period text NOT NULL",
        "cost_usd_total numeric NOT NULL",
        "tokens_total bigint NOT NULL",
        "calls integer NOT NULL",
        "tenants_count integer NOT NULL",
        "captured_at timestamptz NOT NULL DEFAULT now()",
    ):
        assert column in upgrade, f"no snapshot column: {column}"


def test_tables_are_platform_scoped_without_rls(migration, monkeypatch):
    """‼️ tenant_id + FORCE RLS = the privileged worker reads 0 rows."""
    upgrade = _flat(_sql(migration, monkeypatch))
    assert "tenant_id" not in upgrade, (
        "ai_spend_* — a platform aggregate. tenant_id would require FORCE ROW "
        "LEVEL SECURITY, and under FORCE a cross-tenant worker (without SET "
        "app.current_tenant_id) would not see its own rows. The breakdown by "
        "tenants is computed on the fly from ai_usage."
    )
    assert "CREATE POLICY" not in upgrade
    assert "ROW LEVEL SECURITY" not in upgrade.upper()


def test_alert_log_is_claim_key_per_day_and_kind(migration, monkeypatch):
    """The day's snapshot is overwritten → it cannot serve as the claim, the journal is separate."""
    upgrade = _flat(_sql(migration, monkeypatch))
    assert "CREATE TABLE ai_spend_alert_log" in upgrade
    assert "PRIMARY KEY (day, kind)" in upgrade
    assert "sent_at timestamptz NOT NULL DEFAULT now()" in upgrade


def test_revokes_app_role_access(migration, monkeypatch):
    """0021 granted DEFAULT PRIVILEGES to future tables — here we revoke."""
    upgrade = _flat(_sql(migration, monkeypatch))
    assert "REVOKE ALL ON ai_spend_daily FROM tendersrb_app" in upgrade
    assert "REVOKE ALL ON ai_spend_alert_log FROM tendersrb_app" in upgrade


def test_migration_is_additive(migration, monkeypatch):
    """Rolls out on the live production: no DROP, no ALTER of existing objects."""
    upgrade = _flat(_sql(migration, monkeypatch)).upper()
    assert "DROP TABLE" not in upgrade
    assert "ALTER TABLE" not in upgrade
    assert "DELETE FROM" not in upgrade


def test_downgrade_drops_both_tables(migration, monkeypatch):
    downgrade = _flat(_sql(migration, monkeypatch, which="downgrade"))
    assert "DROP TABLE IF EXISTS ai_spend_daily CASCADE" in downgrade
    assert "DROP TABLE IF EXISTS ai_spend_alert_log CASCADE" in downgrade
