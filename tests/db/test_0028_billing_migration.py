"""Unit: migration 0028 (billing) — the chain of revisions + SQL (op.execute intercepted).

Without a network/DB. The live up/down regression — on an ephemeral PG (see REPORT); the RLS regression
under the app role — tests/db/test_rls_isolation.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0028_billing_subscriptions.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("migration_0028", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load()


def _calls(migration, monkeypatch, which="upgrade"):
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))
    getattr(migration, which)()
    return calls


def test_revision_chain(migration):
    assert migration.revision == "0028"
    assert migration.down_revision == "0027"


def test_creates_both_tables(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch))
    assert "CREATE TABLE billing_subscriptions" in joined
    assert "CREATE TABLE billing_webhook_events" in joined


def test_billing_subscriptions_has_force_rls_and_policy(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch))
    assert "ALTER TABLE billing_subscriptions ENABLE ROW LEVEL SECURITY" in joined
    assert "CREATE POLICY tenant_isolation ON billing_subscriptions" in joined
    assert "current_setting('app.current_tenant_id')" in joined
    assert "ALTER TABLE billing_subscriptions FORCE ROW LEVEL SECURITY" in joined


def test_webhook_events_dedup_unique(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch))
    # (provider, event_id) UNIQUE — the idempotency key.
    assert "billing_webhook_events_provider_event_uniq" in joined
    assert "UNIQUE (provider, event_id)" in joined
    # the platform table — WITHOUT RLS on webhook_events.
    assert "ROW LEVEL SECURITY" not in "".join(
        c for c in _calls(migration, monkeypatch) if "billing_webhook_events" in c
    )


def test_security_definer_functions(migration, monkeypatch):
    calls = _calls(migration, monkeypatch)
    creates = [c for c in calls if "CREATE OR REPLACE FUNCTION" in c]
    names = {
        "app_billing_upsert_subscription",
        "app_billing_resolve_tenant_by_customer",
    }
    for name in names:
        body = next(c for c in creates if name in c)
        assert "SECURITY DEFINER" in body
        assert "SET search_path = public" in body
    # upsert writes → VOLATILE; resolve only reads → STABLE.
    upsert = next(c for c in creates if "app_billing_upsert_subscription" in c)
    resolve = next(c for c in creates if "app_billing_resolve_tenant_by_customer" in c)
    assert "VOLATILE" in upsert
    assert "ON CONFLICT (provider, provider_subscription_id)" in upsert
    assert "STABLE" in resolve


def test_grants_execute_to_app_role(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch))
    assert (
        "GRANT EXECUTE ON FUNCTION app_billing_upsert_subscription" in joined
        and "TO tendersrb_app" in joined
    )
    assert "GRANT EXECUTE ON FUNCTION app_billing_resolve_tenant_by_customer" in joined


def test_downgrade_drops_everything(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch, which="downgrade"))
    assert "DROP TABLE IF EXISTS billing_subscriptions" in joined
    assert "DROP TABLE IF EXISTS billing_webhook_events" in joined
    assert "DROP FUNCTION IF EXISTS app_billing_upsert_subscription" in joined
    assert "DROP FUNCTION IF EXISTS app_billing_resolve_tenant_by_customer" in joined
