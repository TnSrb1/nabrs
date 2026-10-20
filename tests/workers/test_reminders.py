"""Unit tests of ReminderWorker (the port of v6 reminders_loop).

The DB and the channel adapter are mocked (as in test_delivery.py): we check the calculation of the days to the deadline and the choice of the threshold (5/3/1/0 / “not a threshold”), the idempotency of claim-then-send, the cut-off of the overdue ones, the filter of the funnel statuses, the non-rollback of the journal on a channel failure and the assembly of the text of the reminder (a pure function) — without a real PostgreSQL/network.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.workers.adapters.base import DeliveryResult
from src.workers.reminders import (
    ReminderWorker,
    build_reminder_text,
    days_until_deadline,
)

# A fixed “now” for the determinism (UTC). The deadlines are counted relative to it.
NOW = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)


def _now() -> datetime:
    return NOW


# --------------------------------------------------------------------------- #
#  DB mocks (as in test_delivery.py)
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


class _FakeAdapter:
    """An adapter stub: records send_text, gives the set result/exception."""

    channel = "telegram"

    def __init__(
        self, result: DeliveryResult | None = None, raise_exc: Exception | None = None
    ):
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


def _cand(*, days_ahead: int = 5, **over) -> SimpleNamespace:
    """A candidate row (as from _CANDIDATES_SQL), the deadline = NOW + days_ahead days."""
    row = SimpleNamespace(
        tenant_id="ten-1",
        tender_id="t-1",
        user_status="active",
        title="Видеонаблюдение",
        source_url="https://jnportal.rs/t/1",
        deadline_at=NOW + timedelta(days=days_ahead),
        tz="Europe/Belgrade",
        user_channel_id="uc-1",
        target="555",
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row


def _worker(session, adapter=None, **kw) -> ReminderWorker:
    return ReminderWorker(
        adapters={"telegram": adapter or _FakeAdapter()},
        session_factory=_session_factory(session),
        now_factory=_now,
        **kw,
    )


# --------------------------------------------------------------------------- #
#  Pure functions: the days to the deadline + the choice of the threshold
# --------------------------------------------------------------------------- #


def test_days_until_deadline_calendar_floor():
    bg = "Europe/Belgrade"
    assert days_until_deadline(NOW + timedelta(days=5), now=NOW, tz_name=bg) == 5
    assert days_until_deadline(NOW + timedelta(days=3), now=NOW, tz_name=bg) == 3
    assert days_until_deadline(NOW + timedelta(days=1), now=NOW, tz_name=bg) == 1
    assert days_until_deadline(NOW, now=NOW, tz_name=bg) == 0
    assert days_until_deadline(NOW + timedelta(days=4), now=NOW, tz_name=bg) == 4
    # an overdue → negative (it will not get into the worker — the SQL cuts it off)
    assert days_until_deadline(NOW - timedelta(days=2), now=NOW, tz_name=bg) < 0


def test_days_until_deadline_unknown_tz_falls_back_utc():
    # an unknown zone → UTC, the function does not fall
    assert (
        days_until_deadline(NOW + timedelta(days=2), now=NOW, tz_name="No/Where") == 2
    )
    assert days_until_deadline(NOW + timedelta(days=2), now=NOW, tz_name=None) == 2


def test_threshold_for_selects_threshold_else_none():
    worker = _worker(_FakeSession([]))
    assert worker._threshold_for(NOW + timedelta(days=5), "Europe/Belgrade") == 5
    assert worker._threshold_for(NOW + timedelta(days=0), "Europe/Belgrade") == 0
    # 4 days — not a threshold
    assert worker._threshold_for(NOW + timedelta(days=4), "Europe/Belgrade") is None


# --------------------------------------------------------------------------- #
#  The assembly of the text of the reminder (a pure function)
# --------------------------------------------------------------------------- #


def test_build_reminder_text_thresholds_title_deadline_url():
    dl = datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc)
    for n in (5, 3, 1):
        msg = build_reminder_text(
            n, title="Тендер", deadline_at=dl, source_url="https://jnportal.rs/x"
        )
        assert f"до дедлайна {n} дн." in msg
        assert "<b>Тендер</b>" in msg
        assert "🗓 Приём до 10.06.2026 12:00" in msg  # 10:00Z of June → 12:00 Belgrade
        assert "https://jnportal.rs/x" in msg


def test_build_reminder_text_zero_threshold_is_today_and_omits_missing():
    dl = datetime(2026, 6, 5, 10, 0, tzinfo=timezone.utc)
    msg = build_reminder_text(0, title="Тендер", deadline_at=dl, source_url=None)
    assert "дедлайн СЕГОДНЯ" in msg
    assert "до дедлайна" not in msg
    assert "🔗" not in msg  # url is absent → there is no line


def test_build_reminder_text_escapes_html_and_omits_no_deadline():
    msg = build_reminder_text(
        3,
        title="<b>x</b> & y",
        deadline_at=None,
        source_url="https://x?a=1&b=2",
    )
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in msg
    assert "a=1&amp;b=2" in msg
    assert "🗓" not in msg  # deadline None → there is no submission line


# --------------------------------------------------------------------------- #
#  poll_once — the candidates, the thresholds, the idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_candidates_sql_filters_future_deadline_and_funnel_statuses():
    session = _FakeSession([_FakeResult([])])
    worker = _worker(session)
    await worker.poll_once()
    stmt, params = session.calls[0]
    # the overdue ones are not reminded; only the funnel active/watching; the channel telegram
    assert "t.deadline_at >= now()" in stmt
    assert "tus.status = ANY(:statuses)" in stmt
    assert params["statuses"] == ["active", "watching"]
    assert params["channel"] == "telegram"


@pytest.mark.asyncio
async def test_poll_once_happy_path_claims_then_sends():
    # a candidate at the threshold 5 → the claim returned the row → we send
    session = _FakeSession(
        [_FakeResult([_cand(days_ahead=5)]), _FakeResult([SimpleNamespace(id="r-1")])]
    )
    adapter = _FakeAdapter()
    worker = _worker(session, adapter)
    sent = await worker.poll_once()
    assert sent == 1
    assert len(adapter.sent) == 1
    target, text = adapter.sent[0]
    assert target == "555"
    assert "до дедлайна 5 дн." in text
    # the claim passed into reminder_log and is committed BEFORE the send
    assert any("reminder_log" in s for s, _ in session.calls)
    assert session.commits >= 1


@pytest.mark.asyncio
async def test_poll_once_idempotent_conflict_does_not_send():
    # the claim did not return a row (ON CONFLICT — the threshold was already there) → we do NOT send
    session = _FakeSession([_FakeResult([_cand(days_ahead=3)]), _FakeResult([])])
    adapter = _FakeAdapter()
    worker = _worker(session, adapter)
    sent = await worker.poll_once()
    assert sent == 0
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_poll_once_non_threshold_day_skips_claim_and_send():
    # a deadline in 4 days — not a threshold → neither a claim, nor a send
    session = _FakeSession([_FakeResult([_cand(days_ahead=4)])])
    adapter = _FakeAdapter()
    worker = _worker(session, adapter)
    sent = await worker.poll_once()
    assert sent == 0
    assert adapter.sent == []
    assert all("reminder_log" not in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_poll_once_send_failure_does_not_rollback_journal():
    # the claim passed, but the channel fell with an exception — the journal is NOT rolled back, the worker is alive
    session = _FakeSession(
        [_FakeResult([_cand(days_ahead=1)]), _FakeResult([SimpleNamespace(id="r-1")])]
    )
    adapter = _FakeAdapter(raise_exc=RuntimeError("network glitch"))
    worker = _worker(session, adapter)
    sent = await worker.poll_once()  # does not fall
    assert sent == 0
    # the claim was executed and committed; there is no rollback (otherwise it would loop the spam)
    assert any("reminder_log" in s for s, _ in session.calls)
    assert session.commits >= 1
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_poll_once_adapter_not_ok_counts_as_not_sent():
    # send_text returned ok=False → we do not count it as sent (the journal is already committed)
    session = _FakeSession(
        [_FakeResult([_cand(days_ahead=0)]), _FakeResult([SimpleNamespace(id="r-1")])]
    )
    adapter = _FakeAdapter(
        result=DeliveryResult(ok=False, response_code=403, blocked=True)
    )
    worker = _worker(session, adapter)
    sent = await worker.poll_once()
    assert sent == 0
    assert len(adapter.sent) == 1  # the attempt was made


@pytest.mark.asyncio
async def test_poll_once_unknown_channel_adapter_skipped():
    session = _FakeSession([])
    worker = _worker(session, enabled_channels=("viber",))  # no viber adapter
    sent = await worker.poll_once()
    assert sent == 0
    assert session.calls == []  # the candidates were not even queried
