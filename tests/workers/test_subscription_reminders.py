"""Unit tests of SubscriptionReminderWorker.

The DB, the TG adapter and the e-mail sender are mocked (as in test_reminders.py / test_summary.py): we check the idempotency of claim-then-send, NULL/expired access_until → skip, the extension (a new snapshot) → the thresholds fire again, the choice of the threshold 7/1, the assembly of the texts — without a real PostgreSQL/network.
"""

from __future__ import annotations

import logging
import re
import smtplib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.api.emailer import MockEmailSender
from src.workers.adapters.base import DeliveryResult
from src.workers.subscription_reminders import (
    EXPIRED_GRACE_HOURS,
    EXPIRED_THRESHOLD,
    THRESHOLDS_DAYS,
    SubscriptionReminderWorker,
    _EXPIRED_TENANTS_SQL,
    build_email_body,
    build_email_expired_body,
    build_email_expired_subject,
    build_email_subject,
    build_tg_expired_text,
    build_tg_text,
    days_until_access,
)
from zoneinfo import ZoneInfo

# A fixed “now” (UTC, 10:00 — after SEND_HOUR=9 by Belgrade UTC+2=12:00).
NOW = datetime(2026, 6, 14, 8, 0, tzinfo=timezone.utc)  # 10:00 Belgrade
BG_TZ = ZoneInfo("Europe/Belgrade")
# The subscriber’s address is PII: the tests below guard that it does not get into the logs.
PII_EMAIL = "user@test.rs"


def _now() -> datetime:
    return NOW


# --------------------------------------------------------------------------- #
#  Mocks of the DB
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return self._results.pop(0) if self._results else _FakeResult([])

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class _FakeTgAdapter:
    channel = "telegram"

    def __init__(self, result=None, raise_exc=None):
        self._result = result or DeliveryResult(ok=True, response_code=200)
        self._raise = raise_exc
        self.sent: list[tuple[str, str]] = []

    def supports_buttons(self) -> bool:
        return True

    async def send_text(self, target, text, **_kwargs) -> DeliveryResult:
        if self._raise is not None:
            raise self._raise
        self.sent.append((target, text))
        return self._result


def _tenant(
    *,
    days_ahead: int = 7,
    tg_chat_id: str | None = "555",
    email: str | None = PII_EMAIL,
    email_status: str = "active",
    tz: str = "Europe/Belgrade",
    **over,
) -> SimpleNamespace:
    """The row of the tenant (as from _TENANTS_SQL)."""
    row = SimpleNamespace(
        tenant_id="ten-1",
        access_until=NOW + timedelta(days=days_ahead),
        tz=tz,
        email=email,
        email_status=email_status,
        tg_chat_id=tg_chat_id,
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row


def _expired_tenant(
    *,
    hours_ago: int = 1,
    tg_chat_id: str | None = "555",
    email: str | None = PII_EMAIL,
    email_status: str = "active",
    tz: str = "Europe/Belgrade",
    **over,
) -> SimpleNamespace:
    """A tenant row with access_until ALREADY IN THE PAST (as from _EXPIRED_TENANTS_SQL)."""
    row = SimpleNamespace(
        tenant_id="ten-1",
        access_until=NOW - timedelta(hours=hours_ago),
        tz=tz,
        email=email,
        email_status=email_status,
        tg_chat_id=tg_chat_id,
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row


def _worker(session, tg=None, email_sender=None, **kw) -> SubscriptionReminderWorker:
    return SubscriptionReminderWorker(
        tg_adapter=tg or _FakeTgAdapter(),
        email_sender=email_sender or MockEmailSender(),
        session_factory=_session_factory(session),
        now_factory=kw.pop("now_factory", _now),
        **kw,
    )


# --------------------------------------------------------------------------- #
#  Pure functions
# --------------------------------------------------------------------------- #


def test_days_until_access_calendar():
    tz = BG_TZ
    assert days_until_access(NOW + timedelta(days=7), now=NOW, tz=tz) == 7
    assert days_until_access(NOW + timedelta(days=1), now=NOW, tz=tz) == 1
    assert days_until_access(NOW + timedelta(days=0), now=NOW, tz=tz) == 0
    assert days_until_access(NOW - timedelta(days=1), now=NOW, tz=tz) < 0


def test_thresholds_constant():
    assert THRESHOLDS_DAYS == (7, 1)


def test_build_tg_text_contains_days_and_date():
    access = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    msg = build_tg_text(7, access, BG_TZ)
    assert "7 дн." in msg
    assert "21.06.2026" in msg
    assert "support@nab.rs" in msg


def test_build_email_subject():
    assert "7 дн." in build_email_subject(7)
    assert "1 дн." in build_email_subject(1)


def test_build_email_body_contains_date_and_contact():
    access = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    body = build_email_body(1, access, BG_TZ)
    assert "1 дн." in body
    assert "15.06.2026" in body
    assert "support@nab.rs" in body


# --------------------------------------------------------------------------- #
#  poll_once — the idempotency, the skips, the channels
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_null_access_until_skipped():
    """The tenants with access_until IS NULL are filtered by the SQL — the worker does not see them."""
    session = _FakeSession([_FakeResult([])])  # the SQL returned 0 rows
    worker = _worker(session)
    sent = await worker.poll_once()
    assert sent == 0
    # the SQL query contains the condition IS NOT NULL
    assert "IS NOT NULL" in session.calls[0][0]
    assert "> now()" in session.calls[0][0]


@pytest.mark.asyncio
async def test_expired_access_until_skipped():
    """An expired access_until (> now() in the SQL) — the tenant does not get into the selection."""
    session = _FakeSession([_FakeResult([])])
    worker = _worker(session)
    sent = await worker.poll_once()
    assert sent == 0


@pytest.mark.asyncio
async def test_non_threshold_day_skipped():
    """access_until in 5 days — not a threshold (7/1) → the claim is not made."""
    session = _FakeSession([_FakeResult([_tenant(days_ahead=5)])])
    adapter = _FakeTgAdapter()
    worker = _worker(session, tg=adapter)
    sent = await worker.poll_once()
    assert sent == 0
    assert adapter.sent == []
    # the claim was not made — subscription_reminder_log is not mentioned
    assert all("subscription_reminder_log" not in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_threshold_7_happy_path_sends_tg_and_email():
    """7 days before access_until — the claim + TG + email."""
    email_sender = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([_tenant(days_ahead=7)]),
            _FakeResult([SimpleNamespace(id="r-1")]),  # the claim returned a row
        ]
    )
    adapter = _FakeTgAdapter()
    worker = _worker(session, tg=adapter, email_sender=email_sender)
    sent = await worker.poll_once()
    assert sent == 1
    assert len(adapter.sent) == 1
    assert "7 дн." in adapter.sent[0][1]
    assert session.commits >= 1
    assert len(email_sender.outbox) == 1
    assert "7 дн." in email_sender.outbox[0].subject


@pytest.mark.asyncio
async def test_threshold_1_happy_path():
    """1 day before access_until — the claim + the send."""
    email_sender = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([_tenant(days_ahead=1)]),
            _FakeResult([SimpleNamespace(id="r-2")]),
        ]
    )
    adapter = _FakeTgAdapter()
    worker = _worker(session, tg=adapter, email_sender=email_sender)
    sent = await worker.poll_once()
    assert sent == 1
    assert "1 дн." in adapter.sent[0][1]
    assert "1 дн." in email_sender.outbox[0].subject


@pytest.mark.asyncio
async def test_idempotent_conflict_does_not_send():
    """The claim returned an empty RETURNING (ON CONFLICT) → we do not send again."""
    email_sender = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([_tenant(days_ahead=7)]),
            _FakeResult([]),  # conflict — an empty RETURNING
        ]
    )
    adapter = _FakeTgAdapter()
    worker = _worker(session, tg=adapter, email_sender=email_sender)
    sent = await worker.poll_once()
    assert sent == 0
    assert adapter.sent == []
    assert email_sender.outbox == []


@pytest.mark.asyncio
async def test_renewal_new_snapshot_re_sends():
    """After the extension (a new access_until) — a new snapshot → both claims are valid.

    The first claim: (tenant, threshold=7, snapshot=T1) — fires. After the extension: access_until = T2 (another date, also in the threshold 7). The second claim: (tenant, threshold=7, snapshot=T2) — also fires (another UNIQUE key).
    """
    original_access = NOW + timedelta(days=7)
    # After the extension by 30 days, “now” shifts so that T2 is again 7 days
    renewed_access = NOW + timedelta(days=37)
    now_after_renewal = renewed_access - timedelta(
        days=7
    )  # 14:00 UTC = 16:00 Belgrade ≥ SEND_HOUR

    # Cycle 1: the original end date
    row1 = _tenant(days_ahead=7)
    session1 = _FakeSession(
        [_FakeResult([row1]), _FakeResult([SimpleNamespace(id="r-1")])]
    )
    worker1 = _worker(session1)
    sent1 = await worker1.poll_once()
    assert sent1 == 1
    # the claim passed the original access_until as the snapshot
    params1 = next(p for s, p in session1.calls if "subscription_reminder_log" in s)
    assert params1["access_until"] == original_access

    # Cycle 2: after the extension — a new access_until, another snapshot
    row2 = SimpleNamespace(
        tenant_id="ten-1",
        access_until=renewed_access,
        tz="Europe/Belgrade",
        email="user@test.rs",
        email_status="active",
        tg_chat_id="555",
    )
    session2 = _FakeSession(
        [_FakeResult([row2]), _FakeResult([SimpleNamespace(id="r-2")])]
    )
    worker2 = _worker(session2, now_factory=lambda: now_after_renewal)
    sent2 = await worker2.poll_once()
    assert sent2 == 1  # a new snapshot → a new UNIQUE → the claim passes
    params2 = next(p for s, p in session2.calls if "subscription_reminder_log" in s)
    assert params2["access_until"] == renewed_access
    # The key point: the snapshots are different → different UNIQUE → both claims are independent
    assert params1["access_until"] != params2["access_until"]


@pytest.mark.asyncio
async def test_no_tg_only_email_still_sends():
    """There is no TG channel, only email — the send goes through email."""
    email_sender = MockEmailSender()
    row = _tenant(days_ahead=7, tg_chat_id=None)
    session = _FakeSession(
        [_FakeResult([row]), _FakeResult([SimpleNamespace(id="r-1")])]
    )
    worker = _worker(session, tg=None, email_sender=email_sender)
    sent = await worker.poll_once()
    assert sent == 1
    assert len(email_sender.outbox) == 1


@pytest.mark.asyncio
async def test_no_email_only_tg_still_sends():
    """There is no email, only TG — the send through TG."""
    email_sender = MockEmailSender()
    row = _tenant(days_ahead=1, email=None)
    session = _FakeSession(
        [_FakeResult([row]), _FakeResult([SimpleNamespace(id="r-1")])]
    )
    adapter = _FakeTgAdapter()
    worker = _worker(session, tg=adapter, email_sender=email_sender)
    sent = await worker.poll_once()
    assert sent == 1
    assert len(adapter.sent) == 1
    assert email_sender.outbox == []


@pytest.mark.asyncio
async def test_no_channels_skips_claim():
    """Neither TG, nor email — the claim is not made (a future channel will be able to catch the threshold)."""
    row = _tenant(days_ahead=7, tg_chat_id=None, email=None)
    session = _FakeSession([_FakeResult([row])])
    adapter = _FakeTgAdapter()
    email_sender = MockEmailSender()
    worker = _worker(session, tg=adapter, email_sender=email_sender)
    sent = await worker.poll_once()
    assert sent == 0
    assert all("subscription_reminder_log" not in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_bounced_email_skips_email_channel():
    """email_status=bounced → we do not send the email, but TG may pass."""
    email_sender = MockEmailSender()
    row = _tenant(days_ahead=7, email_status="bounced")
    session = _FakeSession(
        [_FakeResult([row]), _FakeResult([SimpleNamespace(id="r-1")])]
    )
    adapter = _FakeTgAdapter()
    worker = _worker(session, tg=adapter, email_sender=email_sender)
    sent = await worker.poll_once()
    assert sent == 1
    assert len(adapter.sent) == 1
    assert email_sender.outbox == []


@pytest.mark.asyncio
async def test_tg_failure_does_not_rollback_journal():
    """TG fell with an exception — the journal is NOT rolled back, we try the email."""
    email_sender = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([_tenant(days_ahead=7)]),
            _FakeResult([SimpleNamespace(id="r-1")]),
        ]
    )
    adapter = _FakeTgAdapter(raise_exc=RuntimeError("TG down"))
    worker = _worker(session, tg=adapter, email_sender=email_sender)
    sent = await worker.poll_once()  # does not fall
    # the email was still tried
    assert len(email_sender.outbox) == 1
    assert session.commits >= 1
    assert session.rollbacks == 0
    assert sent == 1  # the email passed


@pytest.mark.asyncio
async def test_email_failure_does_not_rollback_journal():
    """The email fell — the journal is NOT rolled back, TG is counted."""

    class _BadEmail(MockEmailSender):
        async def send(self, **_kw):
            raise RuntimeError("SMTP down")

    session = _FakeSession(
        [
            _FakeResult([_tenant(days_ahead=1)]),
            _FakeResult([SimpleNamespace(id="r-1")]),
        ]
    )
    adapter = _FakeTgAdapter()
    worker = _worker(session, tg=adapter, email_sender=_BadEmail())
    sent = await worker.poll_once()
    assert sent == 1  # TG passed
    assert session.commits >= 1
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_before_send_hour_skips():
    """Before SEND_HOUR (9:00 by tz) we do not send (NOW=8:00 UTC = 10:00 Belgrade — OK, but if now=6:00 UTC = 8:00 Belgrade → a skip)."""
    early_now = datetime(2026, 6, 14, 4, 0, tzinfo=timezone.utc)  # 06:00 Belgrade

    session = _FakeSession([_FakeResult([_tenant(days_ahead=7)])])
    adapter = _FakeTgAdapter()
    worker = _worker(session, tg=adapter, now_factory=lambda: early_now)
    sent = await worker.poll_once()
    assert sent == 0
    assert adapter.sent == []
    assert all("subscription_reminder_log" not in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_email_never_leaks_into_logs(caplog):
    """The address of the subscriber — PII: only tenant_id/threshold in the logs."""
    session = _FakeSession(
        [
            _FakeResult([_tenant(days_ahead=7)]),
            _FakeResult([SimpleNamespace(id="r-1")]),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="src.workers.subscription_reminders"):
        assert await _worker(session, tg=_FakeTgAdapter()).poll_once() == 1

    assert caplog.records, "the worker must log the fact of the send"
    assert PII_EMAIL not in caplog.text


@pytest.mark.asyncio
async def test_smtp_refusal_does_not_leak_email_into_logs(caplog):
    """‼️ SMTP puts the address INTO THE EXCEPTION ITSELF — the traceback would leak the PII.

    `SMTPRecipientsRefused` carries the recipient in its args, and `logger.exception` prints the traceback in full. The subscription reminder goes also to the expired addresses, and `make_email_sender` chooses SMTP FIRST (emailer.py) — on production this is the main path. A mirror of the test in test_channel_dead_notify.py.
    """

    class _RefusingSender(MockEmailSender):
        async def send(self, **_kw):
            raise smtplib.SMTPRecipientsRefused({PII_EMAIL: (550, b"User unknown")})

    session = _FakeSession(
        [
            _FakeResult([_tenant(days_ahead=1)]),
            _FakeResult([SimpleNamespace(id="r-1")]),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="src.workers.subscription_reminders"):
        # TG passes → the journal is counted; the email fell, but the address did not get into the log.
        assert (
            await _worker(
                session, tg=_FakeTgAdapter(), email_sender=_RefusingSender()
            ).poll_once()
            == 1
        )

    assert PII_EMAIL not in caplog.text  # neither in the message, nor in the traceback


# --------------------------------------------------------------------------- #
#  The notice “the access has already expired” (EXPIRED_THRESHOLD) — pure functions
# --------------------------------------------------------------------------- #


def test_build_tg_expired_text_names_consequence():
    """The text must name the CONSEQUENCE (the delivery is stopped), not only the fact."""
    access = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    msg = build_tg_expired_text(EXPIRED_THRESHOLD, access, BG_TZ)
    assert "10.06.2026" in msg
    assert "остановлена" in msg
    assert "app.nab.rs" in msg
    assert "support@nab.rs" in msg


def test_build_email_expired_subject_and_body_names_consequence():
    access = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    subject = build_email_expired_subject(EXPIRED_THRESHOLD)
    body = build_email_expired_body(EXPIRED_THRESHOLD, access, BG_TZ)
    assert "приостановлен" in subject
    assert "10.06.2026" in body
    assert "остановлена" in body
    assert "app.nab.rs" in body
    assert "support@nab.rs" in body


# --------------------------------------------------------------------------- #
#  The notice “the access has already expired” — SQL: the main safety test
# --------------------------------------------------------------------------- #


def test_expired_tenants_sql_has_grace_window_guard():
    """‼️ The main safety test of the cut-off window EXPIRED_GRACE_HOURS.

    Without the condition «access_until > now() - grace_hours*INTERVAL» the first run in production would send the notice to ALL the tenants with an expired access, including those expired months ago — the same pitfall that gave 765 extra messages at the backfill in July (see the docstring of EXPIRED_GRACE_HOURS).

    Mutation (see TESTING_expired_access_notice.md): remove the window line from ``_EXPIRED_TENANTS_SQL`` — the test must turn red.
    """
    sql = re.sub(r"\s+", " ", str(_EXPIRED_TENANTS_SQL))
    assert "access_until IS NOT NULL" in sql
    assert "access_until <= now()" in sql
    assert (
        "access_until > now() - (CAST(:grace_hours AS int) * INTERVAL '1 hour')" in sql
    )


@pytest.mark.asyncio
async def test_expired_query_passes_grace_hours_constant():
    """The worker passes exactly EXPIRED_GRACE_HOURS (48) into the query, and not a hard-code."""
    session = _FakeSession([_FakeResult([]), _FakeResult([])])
    worker = _worker(session)
    await worker.poll_once()
    params = next(p for s, p in session.calls if p and "grace_hours" in p)
    assert params["grace_hours"] == EXPIRED_GRACE_HOURS == 48


# --------------------------------------------------------------------------- #
#  The notice “the access has already expired” — poll_once
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_expired_1h_ago_sends_and_claims_threshold_zero():
    """access_until expired 1 hour ago (inside the 48-hour window) → the notice goes out, threshold_days = EXPIRED_THRESHOLD (0) is written into the journal."""
    email_sender = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([]),  # the threshold selection (7/1) — empty
            _FakeResult([_expired_tenant(hours_ago=1)]),  # the selection “already expired”
            _FakeResult([SimpleNamespace(id="r-1")]),  # the claim passed
        ]
    )
    adapter = _FakeTgAdapter()
    worker = _worker(session, tg=adapter, email_sender=email_sender)
    sent = await worker.poll_once()
    assert sent == 1
    assert len(adapter.sent) == 1
    assert "остановлена" in adapter.sent[0][1]
    assert len(email_sender.outbox) == 1
    assert email_sender.outbox[0].subject == build_email_expired_subject(
        EXPIRED_THRESHOLD
    )
    claim_params = next(p for s, p in session.calls if "subscription_reminder_log" in s)
    assert claim_params["threshold"] == EXPIRED_THRESHOLD == 0


@pytest.mark.asyncio
async def test_expired_empty_selection_sends_nothing():
    """An empty selection of the expired → the worker sends nothing and does not make a claim.

    ‼️ This test does NOT check the 48-hour window itself: the mock does not execute the SQL, the rows are set by hand. The window is proved by two other facts: test_expired_tenants_sql_has_grace_window_guard (the condition is present in the query, checked by a mutation) and the measurement of the predicate on a live DB on 06.09.2026 — at 48 h 0 tenants are selected, at 1000 h — 3."""
    email_sender = MockEmailSender()
    session = _FakeSession([_FakeResult([]), _FakeResult([])])
    adapter = _FakeTgAdapter()
    worker = _worker(session, tg=adapter, email_sender=email_sender)
    sent = await worker.poll_once()
    assert sent == 0
    assert adapter.sent == []
    assert email_sender.outbox == []


@pytest.mark.asyncio
async def test_expired_repeat_run_same_access_until_not_resent():
    """A repeated run with the same access_until → claim conflict → the second time we do not send (the idempotency, as for the thresholds 7/1)."""
    email_sender = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([]),
            _FakeResult([_expired_tenant(hours_ago=1)]),
            _FakeResult([]),  # ON CONFLICT DO NOTHING — an empty RETURNING
        ]
    )
    adapter = _FakeTgAdapter()
    worker = _worker(session, tg=adapter, email_sender=email_sender)
    sent = await worker.poll_once()
    assert sent == 0
    assert adapter.sent == []
    assert email_sender.outbox == []


@pytest.mark.asyncio
async def test_expired_no_channels_skips_claim():
    """Neither TG, nor email for the expired tenant — the claim is not made."""
    session = _FakeSession(
        [_FakeResult([]), _FakeResult([_expired_tenant(tg_chat_id=None, email=None)])]
    )
    adapter = _FakeTgAdapter()
    email_sender = MockEmailSender()
    worker = _worker(session, tg=adapter, email_sender=email_sender)
    sent = await worker.poll_once()
    assert sent == 0
    assert all("subscription_reminder_log" not in s for s, _ in session.calls)
