"""Unit tests of AiSpendReportWorker — the daily summary of the AI spend.

PostgreSQL and Telegram are mocked (as in test_subscription_reminders.py): instead of the DB — a tiny in-memory model of three tables (``ai_usage``, ``ai_spend_daily``, ``ai_spend_alert_log``), which answers the same queries as the real one. This is not a “mock repeating the code”: the daily delta, the change of month and the claim-then-send are checked on the STATE, not on the strings of SQL.

What we pin:
* the daily spend = today’s cumulative total − yesterday’s snapshot;
* ‼️ the change of month: yesterday’s snapshot refers to the PAST period → the delta equals today’s total (without this the first of the month gives a negative figure);
* a repeated run on the same day overwrites the snapshot and does NOT send a second alert;
* the threshold is not set → no alert, the snapshot is still written;
* a Telegram failure does not drop the worker, does not roll back the snapshot and does not roll back the journal (otherwise the next cycle would send the same alert again);
* a DB failure → a silent no-op, no exception goes out;
* the message has no PII — only the tenant id and the sums.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.workers.ai_spend_report import (
    AiSpendReportWorker,
    _env_decimal,
    build_report_text,
)

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"


def _at(text_ts: str) -> datetime:
    return datetime.fromisoformat(text_ts).replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
#  The DB mock: three tables in memory, answering the worker’s queries
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    """ai_usage (read only) + ai_spend_daily + ai_spend_alert_log."""

    def __init__(self, usage=(), snapshots=(), alerts=()):
        # usage: (tenant_id, period, cost, tokens, calls)
        self.usage = [tuple(row) for row in usage]
        # snapshots: {day: dict}
        self.snapshots: dict[date, dict] = {row["day"]: dict(row) for row in snapshots}
        self.alerts: set[tuple[date, str]] = set(alerts)
        self.commits = 0
        self.fail = False

    async def execute(self, statement, params=None):
        if self.fail:
            raise RuntimeError("the DB is unavailable")
        sql = " ".join(str(statement).split())
        params = params or {}
        if "INSERT INTO ai_spend_daily" in sql:
            return self._upsert_snapshot(params)
        if "INSERT INTO ai_spend_alert_log" in sql:
            return self._claim_alert(params)
        if "FROM ai_spend_daily" in sql:
            return self._prev_snapshot(sql, params)
        if "GROUP BY u.tenant_id" in sql:
            return self._top_tenants(params)
        if "FROM ai_usage" in sql:
            return self._totals(params)
        raise AssertionError(f"an unexpected query: {sql}")

    async def commit(self):
        self.commits += 1

    # -- ai_usage -------------------------------------------------------- #

    def _period_rows(self, period):
        return [row for row in self.usage if row[1] == period]

    def _totals(self, params):
        rows = self._period_rows(params["period"])
        return _FakeResult(
            [
                SimpleNamespace(
                    cost_usd_total=sum((row[2] for row in rows), Decimal(0)),
                    tokens_total=sum(row[3] for row in rows),
                    calls=sum(row[4] for row in rows),
                    tenants_count=len({row[0] for row in rows}),
                )
            ]
        )

    def _top_tenants(self, params):
        by_tenant: dict[str, Decimal] = {}
        for tenant_id, _period, cost, _tokens, _calls in self._period_rows(
            params["period"]
        ):
            by_tenant[tenant_id] = by_tenant.get(tenant_id, Decimal(0)) + cost
        ordered = sorted(by_tenant.items(), key=lambda item: item[1], reverse=True)
        return _FakeResult(
            SimpleNamespace(tenant_id=tenant_id, cost_usd_total=cost)
            for tenant_id, cost in ordered[: params["limit"]]
        )

    # -- ai_spend_daily -------------------------------------------------- #

    def _prev_snapshot(self, sql, params):
        row = self.snapshots.get(params["day"])
        if row is None:
            return _FakeResult([])
        # ‼️ We apply the filter by period only if it IS in the query — otherwise
        # the mock would “fix” the change of month for the worker, and the test of it would be blind to
        # a condition dropped from the SQL.
        if (
            "period = CAST(:period AS text)" in sql
            and row["period"] != params["period"]
        ):
            return _FakeResult([])
        return _FakeResult([SimpleNamespace(**row)])

    def _upsert_snapshot(self, params):
        self.snapshots[params["day"]] = {
            "day": params["day"],
            "period": params["period"],
            "cost_usd_total": params["cost"],
            "tokens_total": params["tokens"],
            "calls": params["calls"],
            "tenants_count": params["tenants"],
        }
        return _FakeResult([])

    # -- ai_spend_alert_log ---------------------------------------------- #

    def _claim_alert(self, params):
        key = (params["day"], params["kind"])
        if key in self.alerts:
            return _FakeResult([])  # ON CONFLICT DO NOTHING → RETURNING is empty
        self.alerts.add(key)
        return _FakeResult([SimpleNamespace(day=params["day"])])


def _session_factory(db):
    @asynccontextmanager
    async def factory():
        yield db

    return factory


class _FakeSender:
    def __init__(self, raise_exc=None):
        self.sent: list[str] = []
        self._raise = raise_exc

    async def __call__(self, text: str) -> None:
        if self._raise is not None:
            raise self._raise
        self.sent.append(text)


def _worker(db, *, now, sender=None, daily=None, month=None):
    return AiSpendReportWorker(
        session_factory=_session_factory(db),
        alert_sender=sender,
        daily_threshold_usd=daily,
        month_threshold_usd=month,
        now_factory=lambda: now,
    )


def _snapshot(day, period, cost, tokens=0, calls=0, tenants=1):
    return {
        "day": day,
        "period": period,
        "cost_usd_total": Decimal(cost),
        "tokens_total": tokens,
        "calls": calls,
        "tenants_count": tenants,
    }


# --------------------------------------------------------------------------- #
#  The daily delta
# --------------------------------------------------------------------------- #


async def test_daily_spend_is_today_total_minus_yesterday_snapshot():
    """The spend for the day = the cumulative total of the month − yesterday’s snapshot."""
    db = _FakeDb(
        usage=[(TENANT_A, "2026-08", Decimal("10.00"), 5000, 12)],
        snapshots=[_snapshot(date(2026, 8, 14), "2026-08", "7.50", 3000, 9)],
    )
    summary = await _worker(db, now=_at("2026-08-15T06:00:00")).poll_once()

    assert summary["daily_cost"] == Decimal("2.50")
    assert summary["daily_calls"] == 3
    assert summary["month_cost"] == Decimal("10.00")
    # Today’s snapshot is a cumulative total, not a delta.
    assert db.snapshots[date(2026, 8, 15)]["cost_usd_total"] == Decimal("10.00")


async def test_first_run_ever_reports_full_period_total():
    """There is no yesterday’s snapshot at all → the whole total of the period is counted for the day."""
    db = _FakeDb(usage=[(TENANT_A, "2026-08", Decimal("0.64"), 35736, 7)])
    summary = await _worker(db, now=_at("2026-08-03T06:00:00")).poll_once()

    assert summary["daily_cost"] == Decimal("0.64")
    assert summary["month_cost"] == Decimal("0.64")


async def test_month_rollover_does_not_subtract_previous_period():
    """‼️ The first of the month: yesterday’s snapshot — of ANOTHER period.

    Without the filter by ``period`` the subtraction would give a negative figure (yesterday’s total of July is larger than today’s total of August, started from zero).
    """
    db = _FakeDb(
        usage=[
            (TENANT_A, "2026-07", Decimal("50.00"), 90000, 200),
            (TENANT_A, "2026-08", Decimal("1.25"), 900, 3),
        ],
        snapshots=[_snapshot(date(2026, 7, 31), "2026-07", "50.00", 90000, 200)],
    )
    summary = await _worker(db, now=_at("2026-08-01T06:00:00")).poll_once()

    assert summary["period"] == "2026-08"
    assert summary["daily_cost"] == Decimal("1.25")
    assert summary["daily_calls"] == 3
    assert summary["daily_cost"] > 0, "the delta at the change of month went negative"
    # The July snapshot is untouched — it is still needed as history.
    assert db.snapshots[date(2026, 7, 31)]["cost_usd_total"] == Decimal("50.00")


# --------------------------------------------------------------------------- #
#  Thresholds and the idempotency of the alert
# --------------------------------------------------------------------------- #


async def test_alert_sent_once_per_day_on_repeat_run():
    """A repeated run for the same day: the snapshot is updated, there is no second alert."""
    db = _FakeDb(usage=[(TENANT_A, "2026-08", Decimal("5.00"), 4000, 10)])
    sender = _FakeSender()
    worker = _worker(
        db, now=_at("2026-08-15T06:00:00"), sender=sender, daily=Decimal("1.00")
    )

    first = await worker.poll_once()
    second = await worker.poll_once()

    assert first["alerts"] == ["daily"]
    assert second["alerts"] == [], "the repeated run sent the alert again"
    assert len(sender.sent) == 1
    assert len(db.snapshots) == 1, "the repeated run created a second row of the day"


async def test_no_thresholds_writes_snapshot_without_alert():
    """The thresholds are not set → the alert is off, but the snapshot is needed for tomorrow’s delta."""
    db = _FakeDb(usage=[(TENANT_A, "2026-08", Decimal("99.00"), 4000, 10)])
    sender = _FakeSender()
    summary = await _worker(
        db, now=_at("2026-08-15T06:00:00"), sender=sender
    ).poll_once()

    assert sender.sent == []
    assert summary["alerts"] == []
    assert db.snapshots[date(2026, 8, 15)]["cost_usd_total"] == Decimal("99.00")


async def test_below_threshold_is_silent():
    db = _FakeDb(usage=[(TENANT_A, "2026-08", Decimal("0.50"), 400, 2)])
    sender = _FakeSender()
    await _worker(
        db,
        now=_at("2026-08-15T06:00:00"),
        sender=sender,
        daily=Decimal("1.00"),
        month=Decimal("10.00"),
    ).poll_once()
    assert sender.sent == []
    assert db.alerts == set()


async def test_both_thresholds_exceeded_give_one_message_two_claims():
    """Two thresholds — two claims, but one message: a duplicate of the figures is not needed by the operator."""
    db = _FakeDb(usage=[(TENANT_A, "2026-08", Decimal("20.00"), 4000, 10)])
    sender = _FakeSender()
    summary = await _worker(
        db,
        now=_at("2026-08-15T06:00:00"),
        sender=sender,
        daily=Decimal("1.00"),
        month=Decimal("5.00"),
    ).poll_once()

    assert summary["alerts"] == ["daily", "month"]
    assert len(sender.sent) == 1
    assert "за сутки" in sender.sent[0] and "за месяц" in sender.sent[0]


async def test_month_threshold_alerts_even_when_daily_is_quiet():
    """The monthly threshold is independent: the day is quiet, the accumulated for the month is not."""
    db = _FakeDb(
        usage=[(TENANT_A, "2026-08", Decimal("30.00"), 4000, 10)],
        snapshots=[_snapshot(date(2026, 8, 14), "2026-08", "29.90", 3990, 10)],
    )
    sender = _FakeSender()
    summary = await _worker(
        db,
        now=_at("2026-08-15T06:00:00"),
        sender=sender,
        daily=Decimal("1.00"),
        month=Decimal("25.00"),
    ).poll_once()

    assert summary["alerts"] == ["month"]
    assert len(sender.sent) == 1


# --------------------------------------------------------------------------- #
#  The contract of reliability: the observation does not break the observed
# --------------------------------------------------------------------------- #


async def test_telegram_failure_keeps_snapshot_and_journal():
    """A failure of the channel: the worker is alive, the snapshot is in place, the journal is NOT rolled back (anti-spam)."""
    db = _FakeDb(usage=[(TENANT_A, "2026-08", Decimal("5.00"), 4000, 10)])
    sender = _FakeSender(raise_exc=RuntimeError("telegram timeout"))
    worker = _worker(
        db, now=_at("2026-08-15T06:00:00"), sender=sender, daily=Decimal("1.00")
    )

    summary = await worker.poll_once()

    assert summary is not None, "a failure of the channel broke the pass"
    assert db.snapshots[date(2026, 8, 15)]["cost_usd_total"] == Decimal("5.00")
    assert (date(2026, 8, 15), "daily") in db.alerts
    # A second run does not repeat the alert: a rollback of the journal = spam to the operator.
    sender_ok = _FakeSender()
    worker._alert = sender_ok
    assert (await worker.poll_once())["alerts"] == []
    assert sender_ok.sent == []


async def test_db_failure_is_silent_noop():
    """the DB is unavailable → None and a WARNING, no exception goes out."""
    db = _FakeDb(usage=[(TENANT_A, "2026-08", Decimal("5.00"), 4000, 10)])
    db.fail = True
    sender = _FakeSender()

    summary = await _worker(
        db, now=_at("2026-08-15T06:00:00"), sender=sender, daily=Decimal("0.01")
    ).poll_once()

    assert summary is None
    assert sender.sent == []


async def test_db_failure_logs_only_exception_class(caplog):
    """PII/the details of the request do not go into the log: only the class of the error (without the traceback)."""
    db = _FakeDb()
    db.fail = True
    with caplog.at_level(logging.WARNING):
        await _worker(db, now=_at("2026-08-15T06:00:00")).poll_once()
    record = next(r for r in caplog.records if "ai_spend_report" in r.message)
    assert "RuntimeError" in record.getMessage()
    assert record.exc_info is None, "the traceback drags the arguments of the exception into the log"


async def test_alert_sender_absent_is_noop():
    """The channel is not configured (no ADMIN_BOT_TOKEN) → the snapshot is there, no shout."""
    db = _FakeDb(usage=[(TENANT_A, "2026-08", Decimal("5.00"), 4000, 10)])
    summary = await _worker(
        db, now=_at("2026-08-15T06:00:00"), daily=Decimal("1.00")
    ).poll_once()

    assert summary["alerts"] == ["daily"]  # the claim was made
    assert db.snapshots[date(2026, 8, 15)]["cost_usd_total"] == Decimal("5.00")


# --------------------------------------------------------------------------- #
#  The thresholds from the ENV and the text of the message.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [("5", Decimal("5")), ("0.25", Decimal("0.25")), ("", None), ("  ", None)],
)
def test_env_decimal_parses_threshold(monkeypatch, raw, expected):
    monkeypatch.setenv("AI_SPEND_DAILY_ALERT_USD", raw)
    assert _env_decimal("AI_SPEND_DAILY_ALERT_USD") == expected


def test_env_decimal_garbage_disables_threshold_with_warning(monkeypatch, caplog):
    """A typo in the threshold does not drop the worker, but does not stay silent either."""
    monkeypatch.setenv("AI_SPEND_DAILY_ALERT_USD", "десять")
    with caplog.at_level(logging.WARNING):
        assert _env_decimal("AI_SPEND_DAILY_ALERT_USD") is None
    assert any("AI_SPEND_DAILY_ALERT_USD" in r.getMessage() for r in caplog.records)


def test_env_decimal_absent_is_none(monkeypatch):
    monkeypatch.delenv("AI_SPEND_MONTH_ALERT_USD", raising=False)
    assert _env_decimal("AI_SPEND_MONTH_ALERT_USD") is None


def test_report_text_has_figures_and_no_pii():
    """In the message to the operator — the tenant id and the sums; neither the e-mail, nor the names."""
    text = build_report_text(
        day=date(2026, 8, 15),
        period="2026-08",
        daily_cost=Decimal("2.5"),
        month_cost=Decimal("10"),
        daily_calls=3,
        month_calls=12,
        tenants_count=2,
        top_tenants=[(TENANT_A, Decimal("9")), (TENANT_B, Decimal("1"))],
        reasons=["за сутки $2.5000 > $1.0000"],
    )
    assert "2026-08-15" in text and "2026-08" in text
    assert "$2.5000" in text and "$10.0000" in text
    assert TENANT_A in text and TENANT_B in text
    assert "@" not in text, "the summary must not contain addresses"
