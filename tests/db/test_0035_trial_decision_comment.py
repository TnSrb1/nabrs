"""Unit: migration 0035 (trial_requests.decision_comment + the shoulder of the decision letter).

Without a network/DB (op.execute intercepted). It checks: the chain of revisions, ADD COLUMN
decision_comment, the 4-arg decide function writes decision_comment (+ GRANT), the pending function of the payment (0033) gives the letter the decision context (LEFT JOIN trial_requests, the columns plan/decision_comment) WITHOUT a second worker/outbox, the downgrade returns the original signatures and removes the column. The RLS of trial_requests is NOT touched.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0035_trial_decision_comment.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("migration_0035", _MODULE_PATH)
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
    assert migration.revision == "0035"
    assert migration.down_revision == "0034"


def test_adds_decision_comment_column(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch))
    assert "ALTER TABLE trial_requests ADD COLUMN decision_comment text" in joined


def test_does_not_touch_rls(migration, monkeypatch):
    """The RLS of trial_requests stays FORCE (0012/0032) — the migration does not touch it."""
    joined = "\n".join(_calls(migration, monkeypatch))
    assert "ROW LEVEL SECURITY" not in joined
    assert "CREATE POLICY" not in joined


def test_decide_function_is_4arg_and_writes_comment(migration, monkeypatch):
    calls = _calls(migration, monkeypatch)
    joined = "\n".join(calls)
    # The old 3-arg one is dropped, the new 4-arg one is created.
    assert (
        "DROP FUNCTION IF EXISTS app_admin_decide_trial_request(uuid, text, int)"
        in joined
    )
    create = next(
        c
        for c in calls
        if "CREATE OR REPLACE FUNCTION" in c and "app_admin_decide_trial_request" in c
    )
    assert "p_id uuid, p_status text, p_days int, p_comment text" in create
    assert "decision_comment = p_comment" in create
    # the guarded UPDATE status='pending' (idempotency) is kept.
    assert "status = 'pending'" in create
    assert "SECURITY DEFINER" in create
    assert "SET search_path = public" in create
    assert (
        "GRANT EXECUTE ON FUNCTION app_admin_decide_trial_request("
        "uuid, text, int, text) TO tendersrb_app" in joined
    )


def test_pending_function_surfaces_decision_context(migration, monkeypatch):
    calls = _calls(migration, monkeypatch)
    joined = "\n".join(calls)
    # The return signature changes → DROP+CREATE (not CREATE OR REPLACE alone).
    assert "DROP FUNCTION IF EXISTS app_billing_pending_notifications(int)" in joined
    create = next(
        c
        for c in calls
        if "CREATE OR REPLACE FUNCTION" in c
        and "app_billing_pending_notifications" in c
    )
    # The new columns for the decision letter.
    assert "plan text" in create
    assert "decision_comment text" in create
    # The decision context — from trial_requests, only for the manual rows.
    assert "LEFT JOIN trial_requests" in create
    assert "o.provider = 'manual'" in create
    assert "decision:" in create  # the decision key event_id='decision:<rid>'
    # plan from upgrade_request:<plan> (as in 0034).
    assert "upgrade_request:%" in create
    assert "SECURITY DEFINER" in create
    assert (
        "GRANT EXECUTE ON FUNCTION app_billing_pending_notifications(int) "
        "TO tendersrb_app" in joined
    )


def test_no_second_outbox_or_worker(migration, monkeypatch):
    """We reuse the payment letter contour — we do NOT create a second outbox/enqueue."""
    joined = "\n".join(_calls(migration, monkeypatch))
    assert "CREATE TABLE" not in joined
    assert "app_billing_enqueue_notification" not in joined


def test_downgrade_restores_signatures_and_drops_column(migration, monkeypatch):
    joined = "\n".join(_calls(migration, monkeypatch, which="downgrade"))
    # The return of the 7-column pending (without plan/decision_comment).
    assert "DROP FUNCTION IF EXISTS app_billing_pending_notifications(int)" in joined
    # The return of the 3-arg decide.
    assert (
        "DROP FUNCTION IF EXISTS app_admin_decide_trial_request("
        "uuid, text, int, text)" in joined
    )
    assert "p_id uuid, p_status text, p_days int" in joined
    # The removal of the column.
    assert "ALTER TABLE trial_requests DROP COLUMN IF EXISTS decision_comment" in joined
