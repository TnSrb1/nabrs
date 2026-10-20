"""Unit tests of BillingNotificationsWorker.

The DB and the e-mail sender are mocked (as in test_channel_dead_notify / test_subscription_reminders): we check claim-then-send and idempotency (a double run = one letter), the texts by kind (past_due/canceled report “access until <date>”, NOT “disabled”), “no e-mail → skip without a claim”, and that the subscriber’s address does not leak into the logs (PII). Without a real PostgreSQL/network.
"""

from __future__ import annotations

import logging
import smtplib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from zoneinfo import ZoneInfo

from src.api.emailer import MockEmailSender
from src.workers.billing_notifications import (
    BillingNotificationsWorker,
    build_cabinet_url,
    build_email_body,
    build_email_subject,
)

# The subscriber’s address is PII: the tests below guard that it does not get into the logs.
PII_EMAIL = "payer@test.rs"
ACCESS_UNTIL = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
BG_TZ = "Europe/Belgrade"


# --------------------------------------------------------------------------- #
#  Mocks of the DB
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Answers by a heuristic: a pending query → the rows of the queue; mark → claim.

    ``claims`` — the list of the results of mark in the order of the calls (True = the id came back, False = the row was already taken/sent).
    """

    def __init__(self, *, pending, claims):
        self._pending = list(pending)
        self._claims = list(claims)
        self.calls: list[str] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append(sql)
        if "app_billing_pending_notifications" in sql:
            return _FakeResult(self._pending)
        if "app_billing_mark_notification_sent" in sql:
            claimed = self._claims.pop(0) if self._claims else False
            # The function always returns one row: an id or NULL.
            return _FakeResult([(1,) if claimed else (None,)])
        return _FakeResult([])

    async def commit(self):
        self.commits += 1


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class _RaisingEmailSender(MockEmailSender):
    """Falls on send — emulates a refusal of SMTP (to check the PII leak)."""

    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    async def send(self, *, to, subject, text_body, html_body=None, **kwargs):
        raise self._exc


def _row(**over):
    base = dict(
        id=1,
        tenant_id="tenant-1",
        kind="activated",
        email=PII_EMAIL,
        email_status="ok",
        access_until=ACCESS_UNTIL,
        tz=BG_TZ,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _worker(session, email_sender):
    return BillingNotificationsWorker(
        email_sender=email_sender,
        session_factory=_session_factory(session),
    )


# --------------------------------------------------------------------------- #
#  The texts
# --------------------------------------------------------------------------- #


def test_subjects_per_kind():
    assert "оплата прошла" in build_email_subject("activated").lower()
    assert "продлена" in build_email_subject("renewed").lower()
    assert "не прошёл" in build_email_subject("past_due").lower()
    assert "отменена" in build_email_subject("canceled").lower()


def test_past_due_body_says_access_kept_until_date_not_cut():
    body = build_email_body(
        "past_due",
        access_until=ACCESS_UNTIL,
        tz=ZoneInfo(BG_TZ),
        cabinet_url="https://nab.rs/app",
    )
    assert "доступ сохраняется" in body.lower()
    assert "15.08.2026" in body  # access_until in the tenant’s tz
    # ‼️ past_due does NOT cut the access — there must not be “the access is disabled”.
    assert "отключён" not in body.lower()
    assert "отключен" not in body.lower()


def test_canceled_body_says_access_until_date_not_cut():
    body = build_email_body(
        "canceled",
        access_until=ACCESS_UNTIL,
        tz=ZoneInfo(BG_TZ),
        cabinet_url="https://nab.rs/app",
    )
    assert "сохраняется до 15.08.2026" in body
    assert "отключён" not in body.lower()


def test_body_has_cabinet_link():
    body = build_email_body(
        "activated",
        access_until=ACCESS_UNTIL,
        tz=ZoneInfo(BG_TZ),
        cabinet_url="https://nab.rs/app",
    )
    assert "https://nab.rs/app" in body


def test_no_raw_placeholders_in_texts():
    """No [[owner:]] stubs in the letters."""
    for kind in ("activated", "renewed", "past_due", "canceled"):
        body = build_email_body(
            kind,
            access_until=ACCESS_UNTIL,
            tz=ZoneInfo(BG_TZ),
            cabinet_url="https://nab.rs/app",
        )
        assert "[[" not in body and "]]" not in body
        assert "[[" not in build_email_subject(kind)


def test_build_cabinet_url_appends_app_path():
    assert build_cabinet_url("https://app.nab.rs/") == "https://app.nab.rs/app"
    assert build_cabinet_url("") == "https://nab.rs/app"


# --------------------------------------------------------------------------- #
#  The reply letters about the decision on a request (manual_approved / manual_rejected, 0035)
# --------------------------------------------------------------------------- #


def test_manual_decision_subjects():
    assert "одобрена" in build_email_subject("manual_approved").lower()
    assert "решение" in build_email_subject("manual_rejected").lower()


def test_manual_approved_body_names_plan_and_access_date():
    body = build_email_body(
        "manual_approved",
        access_until=ACCESS_UNTIL,
        tz=ZoneInfo(BG_TZ),
        cabinet_url="https://nab.rs/app",
        plan="pro",
    )
    assert "одобрена" in body.lower()
    assert "Pro" in body  # the plan is named
    assert "15.08.2026" in body  # access until <date> in the tenant’s tz


def test_manual_approved_body_plain_trial_without_plan():
    body = build_email_body(
        "manual_approved",
        access_until=ACCESS_UNTIL,
        tz=ZoneInfo(BG_TZ),
        cabinet_url="https://nab.rs/app",
        plan=None,
    )
    assert "тестовый доступ" in body.lower()
    assert "15.08.2026" in body


def test_manual_rejected_body_includes_admin_comment():
    body = build_email_body(
        "manual_rejected",
        access_until=None,
        tz=ZoneInfo(BG_TZ),
        cabinet_url="https://nab.rs/app",
        decision_comment="Профиль компании не заполнен",
    )
    assert "решение" in body.lower()
    # the operator’s comment is visible to the subscriber
    assert "Профиль компании не заполнен" in body


def test_manual_rejected_body_without_comment_has_no_empty_block():
    body = build_email_body(
        "manual_rejected",
        access_until=None,
        tz=ZoneInfo(BG_TZ),
        cabinet_url="https://nab.rs/app",
        decision_comment=None,
    )
    assert "Комментарий:" not in body


def test_manual_decision_texts_have_no_placeholders():
    for kind in ("manual_approved", "manual_rejected"):
        body = build_email_body(
            kind,
            access_until=ACCESS_UNTIL,
            tz=ZoneInfo(BG_TZ),
            cabinet_url="https://nab.rs/app",
            plan="business",
            decision_comment="ок",
        )
        assert "[[" not in body and "]]" not in body
        assert "[[" not in build_email_subject(kind)


@pytest.mark.asyncio
async def test_worker_sends_manual_approved_with_plan_and_comment():
    """poll_once carries plan/decision_comment from the queue into the body of the letter."""
    row = _row(
        kind="manual_approved",
        plan="pro",
        decision_comment="Рады видеть вас на Pro",
    )
    session = _FakeSession(pending=[row], claims=[True])
    email = MockEmailSender()
    assert await _worker(session, email).poll_once() == 1
    sent = email.outbox[0]
    assert "одобрена" in sent.subject.lower()
    assert "Pro" in sent.text_body
    assert "Рады видеть вас на Pro" in sent.text_body


# --------------------------------------------------------------------------- #
#  poll_once: claim-then-send + idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sends_email_and_claims():
    session = _FakeSession(pending=[_row(kind="activated")], claims=[True])
    email = MockEmailSender()
    sent = await _worker(session, email).poll_once()
    assert sent == 1
    assert len(email.outbox) == 1
    assert email.outbox[0].to == PII_EMAIL
    assert "оплата прошла" in email.outbox[0].subject.lower()
    # the claim is made (mark) before the send.
    assert any("app_billing_mark_notification_sent" in c for c in session.calls)


@pytest.mark.asyncio
async def test_claim_lost_to_other_run_skips_send():
    """A double run of the worker: the row was taken by another → the letter does NOT go out."""
    session = _FakeSession(pending=[_row()], claims=[False])
    email = MockEmailSender()
    sent = await _worker(session, email).poll_once()
    assert sent == 0
    assert email.outbox == []


@pytest.mark.asyncio
async def test_double_poll_sends_once():
    """The first pass claims and sends; on the second the rows are already gone (sent_at is set)."""
    email = MockEmailSender()

    s1 = _FakeSession(pending=[_row()], claims=[True])
    assert await _worker(s1, email).poll_once() == 1

    # The second pass: pending is empty (the SQL filters sent_at IS NULL).
    s2 = _FakeSession(pending=[], claims=[])
    assert await _worker(s2, email).poll_once() == 0

    assert len(email.outbox) == 1


@pytest.mark.asyncio
async def test_no_email_skips_without_claim():
    """There is no valid e-mail → we do not claim, do not fall, there is no letter."""
    session = _FakeSession(pending=[_row(email=None)], claims=[])
    email = MockEmailSender()
    sent = await _worker(session, email).poll_once()
    assert sent == 0
    assert email.outbox == []
    # claim was NOT called (the address check is before the mark).
    assert not any("mark_notification_sent" in c for c in session.calls)


@pytest.mark.asyncio
async def test_bounced_email_skips_without_claim():
    session = _FakeSession(pending=[_row(email_status="bounced")], claims=[])
    email = MockEmailSender()
    assert await _worker(session, email).poll_once() == 0
    assert not any("mark_notification_sent" in c for c in session.calls)


@pytest.mark.asyncio
async def test_send_failure_does_not_rollback_claim():
    """A channel failure: the claim is already committed (sent_at is set) — we do not roll back."""
    session = _FakeSession(pending=[_row()], claims=[True])
    email = _RaisingEmailSender(RuntimeError("smtp down"))
    sent = await _worker(session, email).poll_once()
    assert sent == 0
    # the commit of the mark took place (the claim is not rolled back by anti-spam).
    assert session.commits == 1


@pytest.mark.asyncio
async def test_smtp_refusal_does_not_leak_email_into_logs(caplog):
    """PII: the subscriber’s address must NOT get into the logs on an SMTP refusal."""
    session = _FakeSession(pending=[_row()], claims=[True])
    exc = smtplib.SMTPRecipientsRefused({PII_EMAIL: (550, b"blocked")})
    email = _RaisingEmailSender(exc)
    with caplog.at_level(logging.ERROR):
        await _worker(session, email).poll_once()
    assert PII_EMAIL not in caplog.text
    # but the class of the error for the diagnostics is there.
    assert "SMTPRecipientsRefused" in caplog.text
