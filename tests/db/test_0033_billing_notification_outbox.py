"""Unit: migration 0033 (billing_notification_outbox) — the chain + SQL (op.execute intercepted).

Without a network/DB. It checks: a platform table WITHOUT RLS, but with an EXPLICIT REVOKE of the direct access of the app role (fail-closed, the pattern admin_audit_log 0030), UNIQUE (provider,event_id), three SECURITY DEFINER functions with GRANT EXECUTE, the downgrade.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0033_billing_notification_outbox.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("migration_0033", _MODULE_PATH)
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
    assert migration.revision == "0033"
    assert migration.down_revision == "0032"


def test_creates_outbox_table_with_dedup_unique(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch))
    assert "CREATE TABLE billing_notification_outbox" in joined
    # (provider, event_id) UNIQUE — one letter per one webhook event.
    assert "billing_notification_outbox_provider_event_uniq" in joined
    assert "UNIQUE (provider, event_id)" in joined
    # sent_at nullable — the mark “not sent yet”.
    assert "sent_at timestamptz" in joined


def test_platform_table_has_no_rls(migration, monkeypatch):
    """The idempotency key is the webhook event, not the tenant → RLS is not hung on it."""
    joined = "\n".join(_calls(migration, monkeypatch))
    assert "ROW LEVEL SECURITY" not in joined
    assert "CREATE POLICY" not in joined


def test_revokes_direct_app_access_failclosed(migration, monkeypatch):
    """0021 granted DEFAULT PRIVILEGES → here we EXPLICITLY revoke (the pattern 0030)."""
    joined = "\n".join(_calls(migration, monkeypatch))
    assert "REVOKE ALL ON billing_notification_outbox FROM tendersrb_app" in joined
    assert (
        "REVOKE ALL ON SEQUENCE billing_notification_outbox_id_seq "
        "FROM tendersrb_app" in joined
    )
    # NO wide GRANT of the direct access of the app role to the table.
    assert "GRANT ALL" not in joined
    assert "GRANT SELECT" not in joined
    assert "GRANT INSERT" not in joined
    assert "GRANT UPDATE" not in joined


def test_security_definer_functions(migration, monkeypatch):
    calls = _calls(migration, monkeypatch)
    creates = [c for c in calls if "CREATE OR REPLACE FUNCTION" in c]
    names = {
        "app_billing_enqueue_notification",
        "app_billing_pending_notifications",
        "app_billing_mark_notification_sent",
    }
    for name in names:
        body = next(c for c in creates if name in c)
        assert "SECURITY DEFINER" in body
        assert "SET search_path = public" in body
    # enqueue/mark write → VOLATILE; pending only reads → STABLE.
    enqueue = next(c for c in creates if "app_billing_enqueue_notification" in c)
    pending = next(c for c in creates if "app_billing_pending_notifications" in c)
    mark = next(c for c in creates if "app_billing_mark_notification_sent" in c)
    assert "VOLATILE" in enqueue
    assert "ON CONFLICT (provider, event_id) DO NOTHING" in enqueue
    assert "STABLE" in pending
    assert "WHERE o.sent_at IS NULL" in pending
    assert "VOLATILE" in mark
    # The atomic claim: only the first NULL→now() returns the id.
    assert "sent_at IS NULL" in mark
    assert "RETURNING id" in mark


def test_grants_execute_to_app_role(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch))
    for name in (
        "app_billing_enqueue_notification",
        "app_billing_pending_notifications",
        "app_billing_mark_notification_sent",
    ):
        assert f"GRANT EXECUTE ON FUNCTION {name}" in joined
    assert "TO tendersrb_app" in joined


def test_downgrade_drops_everything(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch, which="downgrade"))
    assert "DROP TABLE IF EXISTS billing_notification_outbox" in joined
    assert "DROP FUNCTION IF EXISTS app_billing_enqueue_notification" in joined
    assert "DROP FUNCTION IF EXISTS app_billing_pending_notifications" in joined
    assert "DROP FUNCTION IF EXISTS app_billing_mark_notification_sent" in joined
