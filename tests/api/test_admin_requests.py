"""Unit tests of the web list/decision of requests by admin (sub-approval).

No DB (ADR-013): a mock session. We check the routing of approve of an upgrade request to the
billing path (provider='manual' + tier from the catalog + audit), the ordinary trial access
(only grant, without tier/audit), idempotency (already), and the audit of
a subscription reject. The billing SQL behavior itself is checked by test_manual_activation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.admin import admin_service as svc


def _first(obj):
    r = MagicMock()
    r.first.return_value = obj
    return r


def _all(rows):
    r = MagicMock()
    r.all.return_value = rows
    return r


def _scalar(n):
    r = MagicMock()
    r.scalar_one.return_value = n
    return r


def _returns(value):
    """Result of a SELECT function returning one value (``.scalar()``).

    This is how ``app_admin_extend_access`` (0041) answers — with the new ``access_until``.
    """
    r = MagicMock()
    r.scalar.return_value = value
    return r


# The term that the extension “returns”: the request was approved for 15/30 days, but the
# tenant’s access already ran further — the extension is computed by the DB, Python only reads it.
_EXTENDED_UNTIL = datetime(2030, 5, 1, tzinfo=timezone.utc)


def _params(call):
    return call.args[1] if len(call.args) > 1 else {}


# --------------------------------------------------------------------------- #
# list_trial_requests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_trial_requests_maps_rows_and_total():
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(
            id="r1",
            tenant_id="t1",
            email="a@b.rs",
            plan="pro",
            requested_at=now,
            days=None,
        )
    ]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_all(rows), _scalar(1)])
    out, total = await svc.list_trial_requests(session, page=1)
    assert total == 1
    assert out[0].id == "r1"
    assert out[0].plan == "pro"
    assert out[0].email == "a@b.rs"


# --------------------------------------------------------------------------- #
# approve_request
# --------------------------------------------------------------------------- #


def _assert_decision_enqueued(session, *, request_id: str, kind: str) -> None:
    """Among the executes there is an enqueue of the decision email of the needed kind (a reliable answer).

    Mutation: remove ``enqueue_decision_notification`` from approve/reject → red.
    """
    for call in session.execute.await_args_list:
        sql = str(call.args[0])
        params = _params(call)
        if "app_billing_enqueue_notification" in sql:
            assert params.get("provider") == "manual"
            assert params.get("event_id") == f"decision:{request_id}"
            assert params.get("kind") == kind
            return
    raise AssertionError(f"enqueue of the decision letter ({kind}) was not called")


@pytest.mark.asyncio
async def test_approve_upgrade_routes_through_billing_and_audits():
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _first(SimpleNamespace(tenant_id="t1", comment="upgrade_request:pro")),
            MagicMock(),  # enqueue_decision_notification (the reply letter)
            _returns(_EXTENDED_UNTIL),  # app_admin_extend_access (0041)
            MagicMock(),  # set_current_tenant (inside activate_pending_verification)
            MagicMock(),  # UPDATE of the status pending→trial
            MagicMock(),  # upsert subscription
            MagicMock(),  # set_tier
            MagicMock(),  # write_audit
        ]
    )
    session.commit = AsyncMock()
    outcome = await svc.approve_request(
        session,
        admin_uid="admin",
        request_id="r1",
        days=15,
        comment="Добро пожаловать!",
        actor_email="op@nab.rs",
    )
    assert outcome == "approved_subscription"
    param_dicts = [_params(c) for c in session.execute.await_args_list if _params(c)]
    # billing path: upsert provider='manual' plan pro; audit action=approve_subscription
    assert any(
        p.get("provider") == "manual" and p.get("plan_key") == "pro"
        for p in param_dicts
    )
    assert any(p.get("action") == "approve_subscription" for p in param_dicts)
    # the operator’s comment goes into decide (→ decision_comment) and the reliable answer
    assert any(p.get("comment") == "Добро пожаловать!" for p in param_dicts)
    _assert_decision_enqueued(session, request_id="r1", kind="manual_approved")
    # The subscription and the audit name the SAME term that the extension returned (not now()+N):
    # otherwise the client would see one date on /cabinet/subscription and a different access.
    assert any(p.get("period_end") == _EXTENDED_UNTIL for p in param_dicts)
    audit = next(p for p in param_dicts if p.get("action") == "approve_subscription")
    assert _EXTENDED_UNTIL.isoformat() in audit["details"]
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_plain_trial_only_grants_no_tier_no_audit():
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _first(SimpleNamespace(tenant_id="t1", comment=None)),
            MagicMock(),  # enqueue_decision_notification
            _returns(_EXTENDED_UNTIL),  # app_admin_extend_access (0041)
            MagicMock(),  # set_current_tenant (inside activate_pending_verification)
            MagicMock(),  # UPDATE of the status pending→trial
        ]
    )
    session.commit = AsyncMock()
    outcome = await svc.approve_request(
        session,
        admin_uid="admin",
        request_id="r1",
        days=30,
        actor_email="op@nab.rs",
    )
    assert outcome == "approved"
    # exactly 5 queries: decide + enqueue + extension + status activation (2) (neither set_tier, nor audit, nor subscription)
    assert session.execute.await_count == 5
    param_dicts = [_params(c) for c in session.execute.await_args_list if _params(c)]
    assert not any(p.get("action") for p in param_dicts)
    # provider='manual' exists ONLY in enqueue (not in the subscription upsert — there is none)
    assert not any(p.get("plan_key") for p in param_dicts)
    # An EXTENSION, not a set: access is granted by the SECURITY DEFINER function 0041,
    # which computes the base itself; an absolute date now()+N must not be here —
    # it would cut off the access already granted (gap #497).
    sqls = [str(c.args[0]) for c in session.execute.await_args_list]
    assert any("app_admin_extend_access" in s for s in sqls)
    assert not any("app_admin_grant_access" in s for s in sqls)
    assert _params(session.execute.await_args_list[2])["days"] == 30
    _assert_decision_enqueued(session, request_id="r1", kind="manual_approved")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_already_decided_is_idempotent():
    session = AsyncMock()
    # The shared decide function of the DB and the function of the repeated registration request (0049)
    # both returned 0 rows → “already”.
    session.execute = AsyncMock(side_effect=[_first(None), _first(None)])
    session.commit = AsyncMock()
    outcome = await svc.approve_request(
        session, admin_uid="admin", request_id="r1", days=30, actor_email="op@nab.rs"
    )
    assert outcome == "already"
    assert session.execute.await_count == 2
    session.commit.assert_not_awaited()


# --------------------------------------------------------------------------- #
# reject_request
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reject_upgrade_writes_audit():
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _first(SimpleNamespace(tenant_id="t1", comment="upgrade_request:business")),
            MagicMock(),  # enqueue_decision_notification
            MagicMock(),  # write_audit
        ]
    )
    session.commit = AsyncMock()
    outcome = await svc.reject_request(
        session,
        admin_uid="admin",
        request_id="r1",
        comment="Профиль неполный",
        actor_email="op@nab.rs",
    )
    assert outcome == "rejected"
    param_dicts = [_params(c) for c in session.execute.await_args_list if _params(c)]
    assert any(p.get("action") == "reject_subscription" for p in param_dicts)
    assert any(p.get("comment") == "Профиль неполный" for p in param_dicts)
    _assert_decision_enqueued(session, request_id="r1", kind="manual_rejected")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_reject_plain_trial_no_audit():
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _first(SimpleNamespace(tenant_id="t1", comment=None)),
            MagicMock(),  # enqueue_decision_notification
        ]
    )
    session.commit = AsyncMock()
    outcome = await svc.reject_request(
        session, admin_uid="admin", request_id="r1", actor_email="op@nab.rs"
    )
    assert outcome == "rejected"
    # decide + enqueue of the reply email, no audit (not a subscription)
    assert session.execute.await_count == 2
    _assert_decision_enqueued(session, request_id="r1", kind="manual_rejected")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_reject_already_decided_idempotent():
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_first(None), _first(None)])
    session.commit = AsyncMock()
    outcome = await svc.reject_request(
        session, admin_uid="admin", request_id="r1", actor_email="op@nab.rs"
    )
    assert outcome == "already"
    session.commit.assert_not_awaited()
