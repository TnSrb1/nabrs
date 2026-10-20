"""Tests of the trial access in the bot: the decide service + the callback handler (STAGE T).

Without a network and a real DB (ADR-013): the session is a mock (side_effect on ``execute``);
``session_scope``/``decide``/``get_bot_settings`` are patched in the handler. We check
the idempotency of the decision, the shift of the access on approve, and in the handler — the admin guard,
the editing of the message, the notice of the user.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bots.trial_handlers import on_trial_decision, trial_admin_keyboard
from src.bots.trial_service import DecideOutcome, decide, list_pending

_RID = "44444444-4444-4444-4444-444444444444"
_ADMIN = 123456789


def _datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _row(**kw):
    r = MagicMock()
    r.first.return_value = SimpleNamespace(**kw) if kw else None
    return r


def _none_row():
    r = MagicMock()
    r.first.return_value = None
    return r


def _returns(value):
    """The result of a SELECT function with one value (``.scalar()``).

    This is how ``app_admin_extend_access`` (0041) answers — with the new ``access_until``.
    """
    r = MagicMock()
    r.scalar.return_value = value
    return r


def _params_of(session, needle: str) -> dict:
    """The parameters of the first execute whose SQL contains ``needle``."""
    for call in session.execute.await_args_list:
        if needle in str(call.args[0]):
            return call.args[1] if len(call.args) > 1 else {}
    raise AssertionError(f"no call with {needle!r}")


def _assert_decision_enqueued(session, *, kind: str) -> None:
    """Checks that among the executes there was the enqueue of the decision letter of the needed kind.

    Mutation: remove ``enqueue_decision_notification`` from ``decide`` → this assert turns red (the reliable answer to the subscriber disappeared).
    """
    for call in session.execute.await_args_list:
        sql = str(call.args[0])
        params = (
            call.args[1]
            if len(call.args) > 1 and isinstance(call.args[1], dict)
            else {}
        )
        if "app_billing_enqueue_notification" in sql:
            assert params.get("provider") == "manual"
            assert params.get("event_id") == f"decision:{_RID}"
            assert params.get("kind") == kind
            return
    raise AssertionError(f"enqueue of the decision letter ({kind}) was not called")


# --------------------------------------------------------------------------- #
# The decision keyboard — the callback format tr:<id>:<3|15|30|deny>
# --------------------------------------------------------------------------- #


def test_admin_keyboard_callback_format():
    kb = trial_admin_keyboard(_RID)
    assert _datas(kb) == [
        f"tr:{_RID}:3",
        f"tr:{_RID}:15",
        f"tr:{_RID}:30",
        f"tr:{_RID}:deny",
    ]


# --------------------------------------------------------------------------- #
# decide — idempotency + the shift of access
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_decide_approve_grants_access():
    until = datetime.now(timezone.utc)
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _row(tenant_id="t1", comment=None),
            _returns(until),  # app_admin_extend_access (0041)
            MagicMock(),  # set_current_tenant (inside activate_pending_verification)
            MagicMock(),  # UPDATE of the status pending→trial
            _row(chat_id="555", email="a@b.rs"),
            MagicMock(),  # enqueue_decision_notification (the reply letter)
        ]
    )
    session.commit = AsyncMock()
    out = await decide(session, request_id=_RID, approve=True, days=15)
    assert out.status == "approved"
    assert out.days == 15
    assert out.access_until == until
    assert out.chat_id == "555"
    # an ordinary trial access: tier/subscription are not touched, there is no audit →
    # decide + grant + activate(2) + target + enqueue of the reply letter = 6 queries.
    assert out.plan_key is None
    assert session.execute.await_count == 6
    # the reliable answer to the subscriber: the decision letter is queued (manual_approved)
    _assert_decision_enqueued(session, kind="manual_approved")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_decide_approve_extends_via_security_definer_and_never_shrinks():
    """An approval for N days — an EXTENSION via SECURITY DEFINER 0041.

    The current term is unknown in Python here (the bot runs under ``tendersrb_app``,
    a foreign ``tenants`` is not visible to it and a raw UPDATE falls with
    ``UndefinedObjectError``), therefore the base — the larger of the dates — is computed by
    the function ``app_admin_extend_access`` itself. The absolute ``now() + N``
    (``app_admin_grant_access``) would cut the access already granted (the incident of 04.08.2026 on the admin button).
    """
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _row(tenant_id="t1", comment=None),
            _returns(datetime.now(timezone.utc)),
            MagicMock(),  # set_current_tenant (inside activate_pending_verification)
            MagicMock(),  # UPDATE of the status pending→trial
            _row(chat_id="555", email="a@b.rs"),
            MagicMock(),
        ]
    )
    session.commit = AsyncMock()
    await decide(session, request_id=_RID, approve=True, days=3)

    sqls = [" ".join(str(c.args[0]).split()) for c in session.execute.await_args_list]
    grants = [s for s in sqls if "app_admin_extend_access" in s]
    assert len(grants) == 1, "the access grant must be exactly one"
    assert _params_of(session, "app_admin_extend_access")["days"] == 3
    assert not any(
        "app_admin_grant_access" in s for s in sqls
    ), "an absolute date instead of the extension: a term less than the remainder would shorten the access"
    # No raw queries to tenant-scoped tables remain on the bot path — under
    # the app role without the GUC they fail fail-closed (the defect of 06.08.2026).
    # The only exception — the UPDATE of the status pending→trial inside the extension
    # (14.10): it goes after set_current_tenant on THE SAME tenant, FORCE RLS lets through
    # only its row.
    assert not any("FROM trial_requests" in s for s in sqls)
    raw_updates = [s for s in sqls if "UPDATE tenants" in s]
    assert all("status = 'pending_verification'" in s for s in raw_updates)


@pytest.mark.asyncio
async def test_decide_deny_no_grant():
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _row(tenant_id="t1", comment=None),
            _row(chat_id=None, email="a@b.rs"),
            MagicMock(),  # enqueue_decision_notification
        ]
    )
    session.commit = AsyncMock()
    out = await decide(session, request_id=_RID, approve=False, days=None)
    assert out.status == "rejected"
    assert out.access_until is None
    # decide + target + enqueue (without _GRANT_SQL, without an audit — not a subscription) = 3
    assert session.execute.await_count == 3
    _assert_decision_enqueued(session, kind="manual_rejected")


@pytest.mark.asyncio
async def test_decide_approve_upgrade_uses_billing_path():
    """approve of an upgrade request → the billing path: a manual subscription + set_tier +
    grant_access + audit. The tier set = Plan.tier, the plan is visible in the outcome."""
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _row(tenant_id="t1", comment="upgrade_request:pro"),  # decide
            _returns(datetime(2030, 5, 1, tzinfo=timezone.utc)),  # extend_access
            MagicMock(),  # set_current_tenant (inside activate_pending_verification)
            MagicMock(),  # UPDATE of the status pending→trial
            _none_row(),  # upsert subscription
            _none_row(),  # set_tier
            _none_row(),  # write_audit
            _row(chat_id="555", email="a@b.rs"),  # target
            MagicMock(),  # enqueue_decision_notification
        ]
    )
    session.commit = AsyncMock()
    out = await decide(session, request_id=_RID, approve=True, days=30)
    assert out.status == "approved"
    assert out.plan_key == "pro"
    assert out.tier == "pro"
    assert out.access_until == datetime(2030, 5, 1, tzinfo=timezone.utc)
    # 9 queries: decide + (extend+activate(2)+upsert+set_tier) + audit + target + enqueue
    assert session.execute.await_count == 9
    _assert_decision_enqueued(session, kind="manual_approved")
    # among the queries there is an upsert of the subscription provider='manual' with the tier plan pro
    param_dicts = [
        c.args[1]
        for c in session.execute.await_args_list
        if len(c.args) > 1 and isinstance(c.args[1], dict)
    ]
    assert any(
        p.get("provider") == "manual" and p.get("plan_key") == "pro"
        for p in param_dicts
    )
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_decide_reject_upgrade_writes_audit_no_grant():
    """reject of an upgrade request: the audit is there, but the access/tier are NOT touched."""
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _row(tenant_id="t1", comment="upgrade_request:business"),  # decide
            _none_row(),  # write_audit
            _row(chat_id=None, email="a@b.rs"),  # target
            MagicMock(),  # enqueue_decision_notification
        ]
    )
    session.commit = AsyncMock()
    out = await decide(session, request_id=_RID, approve=False, days=None)
    assert out.status == "rejected"
    assert out.plan_key is None
    # decide + audit + target + enqueue (no grant/set_tier at all)
    assert session.execute.await_count == 4
    _assert_decision_enqueued(session, kind="manual_rejected")


@pytest.mark.asyncio
async def test_decide_idempotent_already():
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_none_row()])
    session.commit = AsyncMock()
    out = await decide(session, request_id=_RID, approve=True, days=3)
    assert out.status == "already"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_pending_maps_rows_via_security_definer():
    """``/trials`` takes the list by the cross-tenant function 0034, not by a raw SELECT.

    A raw ``SELECT ... FROM trial_requests`` under the bot role falls with ``UndefinedObjectError`` (FORCE RLS, the operation has no GUC) — the command did not work at all.
    """
    res = MagicMock()
    res.all.return_value = [
        SimpleNamespace(
            id="r1",
            plan="pro",
            requested_at=datetime.now(timezone.utc),
            email="a@b.rs",
        )
    ]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=res)
    pending = await list_pending(session)
    assert pending[0].request_id == "r1"
    assert pending[0].email == "a@b.rs"
    assert pending[0].plan == "pro"
    sql = str(session.execute.await_args.args[0])
    assert "app_admin_list_trial_requests" in sql
    assert "FROM trial_requests" not in sql


# --------------------------------------------------------------------------- #
# The handler on_trial_decision — the admin guard / the editing / the notice
# --------------------------------------------------------------------------- #


def _make_cb(data, *, from_id=_ADMIN):
    cb = AsyncMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.from_user = SimpleNamespace(id=from_id)
    msg = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    cb.message = msg
    cb.bot = AsyncMock()
    cb.bot.send_message = AsyncMock()
    return cb


def _patches(outcome=None):
    session = AsyncMock()

    @asynccontextmanager
    async def _scope():
        yield session

    decide_mock = AsyncMock(return_value=outcome)
    return [
        patch("src.bots.trial_handlers.session_scope", _scope),
        patch("src.bots.trial_handlers.decide", decide_mock),
        patch(
            "src.bots.trial_handlers.get_bot_settings",
            lambda: SimpleNamespace(admin_chat_id=_ADMIN),
        ),
    ], decide_mock


@pytest.mark.asyncio
async def test_handler_foreign_user_denied():
    cb = _make_cb(f"tr:{_RID}:3", from_id=999)
    patches, decide_mock = _patches()
    with patches[0], patches[1], patches[2]:
        await on_trial_decision(cb)
    cb.answer.assert_awaited_with("Нет прав", show_alert=True)
    decide_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_approve_edits_and_notifies():
    outcome = DecideOutcome(
        status="approved",
        days=15,
        access_until=datetime.now(timezone.utc),
        tenant_id="t1",
        chat_id="555",
        email="a@b.rs",
    )
    cb = _make_cb(f"tr:{_RID}:15")
    patches, decide_mock = _patches(outcome)
    with patches[0], patches[1], patches[2]:
        await on_trial_decision(cb)
    decide_mock.assert_awaited_once()
    assert decide_mock.await_args.kwargs["approve"] is True
    assert decide_mock.await_args.kwargs["days"] == 15
    # the admin message is edited, the buttons are removed
    assert "Одобрено" in cb.message.edit_text.await_args.args[0]
    assert cb.message.edit_text.await_args.kwargs["reply_markup"] is None
    # the user is notified in Telegram
    cb.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_already_processed():
    cb = _make_cb(f"tr:{_RID}:3")
    patches, _decide = _patches(DecideOutcome(status="already"))
    with patches[0], patches[1], patches[2]:
        await on_trial_decision(cb)
    cb.answer.assert_awaited_with("Уже обработан", show_alert=True)
    cb.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_deny_edits_message():
    outcome = DecideOutcome(status="rejected", tenant_id="t1", chat_id=None)
    cb = _make_cb(f"tr:{_RID}:deny")
    patches, decide_mock = _patches(outcome)
    with patches[0], patches[1], patches[2]:
        await on_trial_decision(cb)
    assert decide_mock.await_args.kwargs["approve"] is False
    assert "Отклонено" in cb.message.edit_text.await_args.args[0]
    # without a Telegram binding — we do not send to the user
    cb.bot.send_message.assert_not_awaited()


# --------------------------------------------------------------------------- #
# /trials — a re-registration request: without buttons (only the web admin decides it)
# --------------------------------------------------------------------------- #


def _trials_message():
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=_ADMIN)
    return message


@pytest.mark.asyncio
async def test_trials_reregister_request_has_no_buttons():
    """plan='reregister' (0049) — text without a keyboard; the button answered “already decided”."""
    from src.bots.trial_handlers import cmd_trials
    from src.bots.trial_service import PendingRequest

    pending = [
        PendingRequest(
            request_id=_RID,
            email="a@b.rs",
            plan="reregister",
            requested_at=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
        )
    ]
    message = _trials_message()
    session = AsyncMock()

    @asynccontextmanager
    async def _scope():
        yield session

    with (
        patch("src.bots.trial_handlers.session_scope", _scope),
        patch("src.bots.trial_handlers.list_pending", AsyncMock(return_value=pending)),
        patch(
            "src.bots.trial_handlers.get_bot_settings",
            lambda: SimpleNamespace(admin_chat_id=_ADMIN),
        ),
    ):
        await cmd_trials(message)

    message.answer.assert_awaited_once()
    body = message.answer.await_args.args[0]
    assert body.startswith(
        "🆕 Запрос повторной регистрации — решение в /admin/requests"
    )
    assert "Email: a@b.rs" in body
    assert "Reregister" not in body
    assert message.answer.await_args.kwargs.get("reply_markup") is None
