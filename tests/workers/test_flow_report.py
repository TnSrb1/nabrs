"""The tests of the daily summary of the flow (tmp/TASK_flow_observability.md, Test D).

The expectations are literals: the text of the summary, the hour of the sending, the claim “once a day”.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.workers.flow_report import (
    FlowReportWorker,
    build_daily_summary_text,
    report_hour_utc,
)


# --------------------------------------------------------------------------- #
#  Fakes (the same trick as test_ingest_silence_alert.py/test_ai_spend_report.py)
# --------------------------------------------------------------------------- #
class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, completeness_row):
        self._row = completeness_row

    async def execute(self, statement, params=None):
        return _FakeResult([self._row] if self._row is not None else [])

    async def commit(self):
        pass


def _session_factory(row):
    @asynccontextmanager
    async def factory():
        yield _FakeSession(row)

    return factory


class _FakeRedis:
    """``SET NX`` semantics — like a real Redis: a second SET with the same key returns falsy if the key already exists."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def hincrby(self, key, field, amount):
        return amount

    async def hget(self, key, field):
        return None

    async def hset(self, key, field, value):
        pass

    async def hgetall(self, key):
        return {}

    async def expire(self, key, ttl):
        pass


class _BrokenRedis:
    async def set(self, *a, **k):
        raise ConnectionError("redis down")


@dataclass
class _Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def _row(total=173, no_deadline=14, no_amount=69, no_description=16):
    return SimpleNamespace(
        total=total,
        no_deadline=no_deadline,
        no_amount=no_amount,
        no_description=no_description,
    )


def _make_worker(*, row, redis, clock, alerts):
    async def _alert(text: str) -> None:
        alerts.append(text)

    return FlowReportWorker(
        session_factory=_session_factory(row),
        redis=redis,
        alert_sender=_alert,
        now_factory=clock,
    )


# --------------------------------------------------------------------------- #
#  The hour of the sending
# --------------------------------------------------------------------------- #
def test_report_hour_default_is_6_utc():
    """‼️ The literal from the REPORT/docstring: 06:00 UTC — after the night window of the portal’s publications 22:00-01:00 UTC."""
    assert report_hour_utc() == 6


def test_report_hour_env_override(monkeypatch):
    monkeypatch.setenv("FLOW_REPORT_HOUR_UTC", "9")
    assert report_hour_utc() == 9


def test_report_hour_env_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("FLOW_REPORT_HOUR_UTC", "25")  # outside [0,23]
    assert report_hour_utc() == 6


# --------------------------------------------------------------------------- #
#  build_daily_summary_text — all four indicators as literals
# --------------------------------------------------------------------------- #
def test_summary_text_contains_all_four_indicators():
    completeness = {
        "total": 173,
        "no_deadline": 14,
        "no_amount": 69,
        "no_description": 16,
    }
    flow = {
        "requests_total": 1246,
        "requests_429": 397,
        "peak_rps": 13,
        "docs_lost": 14,
        "budget_over_count": 2,
        "budget_max": 16,
        "by_endpoint_429": {"GetDocument.ashx": 14, "TenderNotices": 8},
    }
    text = build_daily_summary_text(
        day="2026-09-20", completeness=completeness, flow=flow
    )

    assert (
        "1) Темп/отказы источника: запросов 1246, 429 397 (31.9%), пик 13 req/s."
        in text
    )
    assert "GetDocument.ashx=14, TenderNotices=8" in text
    assert "2) Полнота обогащения (n=173): без дедлайна 14 (8.1%" in text
    assert "без описания 16 (9.2%" in text
    assert "без суммы 69 (39.9%" in text
    assert "3) Документов потеряно молча (429 без ретрая): 14." in text
    assert "5) Приток за 24ч: 173 тендеров." in text
    assert (
        "Бюджет запросов на карточку: превышений 2, самая дорогая карточка 16 "
        "запр. (потолок 16)." in text
    )


def test_summary_text_budget_line_literal_when_cards_present():
    completeness = {"total": 5}
    flow = {"budget_over_count": 0, "budget_max": 9}
    text = build_daily_summary_text(
        day="2026-09-27", completeness=completeness, flow=flow
    )
    assert (
        "Бюджет запросов на карточку: превышений 0, самая дорогая карточка 9 "
        "запр. (потолок 16)." in text
    )


def test_summary_text_budget_line_literal_when_no_cards():
    completeness = {"total": 0}
    flow = {"budget_over_count": 0, "budget_max": 0}
    text = build_daily_summary_text(
        day="2026-09-27", completeness=completeness, flow=flow
    )
    assert (
        "Бюджет запросов на карточку: превышений 0, карточек за сутки не было." in text
    )


def test_summary_text_shows_zeros_when_all_healthy():
    """‼️ The summary goes out EVERY day, even when everything is normal (the brief: “not silencing, but a regular reporting”)."""
    completeness = {
        "total": 150,
        "no_deadline": 3,
        "no_amount": 40,
        "no_description": 4,
    }
    flow = {"requests_total": 400, "requests_429": 0, "peak_rps": 1}
    text = build_daily_summary_text(
        day="2026-09-15", completeness=completeness, flow=flow
    )
    assert "429 0 (0.0%)" in text
    assert "3) Документов потеряно молча (429 без ретрая): 0." in text


# --------------------------------------------------------------------------- #
#  poll_once — the hour/the claim/the channel
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_poll_once_waits_until_report_hour():
    alerts: list[str] = []
    clock = _Clock(datetime(2026, 9, 20, 5, 59, tzinfo=timezone.utc))
    worker = _make_worker(row=_row(), redis=_FakeRedis(), clock=clock, alerts=alerts)
    result = await worker.poll_once()
    assert result is None
    assert alerts == []


@pytest.mark.asyncio
async def test_poll_once_sends_after_report_hour():
    alerts: list[str] = []
    clock = _Clock(datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc))
    worker = _make_worker(row=_row(), redis=_FakeRedis(), clock=clock, alerts=alerts)
    result = await worker.poll_once()
    assert result is not None
    assert len(alerts) == 1
    assert "2026-09-20" in alerts[0]


@pytest.mark.asyncio
async def test_poll_once_sends_only_once_per_day():
    """‼️ MUTATION: remove the SET NX claim → a repeated poll on the same day would send the summary on EVERY hourly polling after 06:00 (spam, not reporting)."""
    alerts: list[str] = []
    redis = _FakeRedis()
    clock = _Clock(datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc))
    worker = _make_worker(row=_row(), redis=redis, clock=clock, alerts=alerts)

    await worker.poll_once()
    clock.now = datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc)  # the same day
    result = await worker.poll_once()

    assert result is None
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_poll_once_sends_again_next_day():
    alerts: list[str] = []
    redis = _FakeRedis()
    clock = _Clock(datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc))
    worker = _make_worker(row=_row(), redis=redis, clock=clock, alerts=alerts)

    await worker.poll_once()
    clock.now = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    await worker.poll_once()

    assert len(alerts) == 2


@pytest.mark.asyncio
async def test_poll_once_no_redis_sends_every_call():
    """The contract of reliability: without Redis the claim always succeeds (better twice than never) — the same trick as SilenceAlertGate/PendingEnrichStore."""
    alerts: list[str] = []
    clock = _Clock(datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc))
    worker = _make_worker(row=_row(), redis=None, clock=clock, alerts=alerts)

    await worker.poll_once()
    await worker.poll_once()

    assert len(alerts) == 2


@pytest.mark.asyncio
async def test_poll_once_broken_redis_defaults_to_permissive_claim():
    alerts: list[str] = []
    clock = _Clock(datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc))
    worker = _make_worker(row=_row(), redis=_BrokenRedis(), clock=clock, alerts=alerts)

    await worker.poll_once()

    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_poll_once_db_failure_returns_none_and_no_alert():
    alerts: list[str] = []
    clock = _Clock(datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc))
    worker = _make_worker(row=_row(), redis=_FakeRedis(), clock=clock, alerts=alerts)

    async def _boom():
        raise ConnectionError("db down")

    worker._load_completeness = _boom  # type: ignore[method-assign]
    result = await worker.poll_once()

    assert result is None
    assert alerts == []


def test_summary_text_shows_failed_passes_and_longest_streak():
    """‼️ A single failure of a pass does not ring Telegram (the threshold of the series in src.workers.ingest), therefore in the daily summary it must be VISIBLE — otherwise “to distinguish” would degenerate into “to hide” (the direct directive of the owner of 20.09.2026).

    The figures — from the fact of production of 22.09.2026: one broken pass, a series of length 1.
    """
    completeness = {
        "total": 173,
        "no_deadline": 14,
        "no_amount": 69,
        "no_description": 16,
    }
    flow = {
        "requests_total": 1246,
        "requests_429": 0,
        "peak_rps": 2,
        "docs_lost": 0,
        "budget_over_count": 0,
        "budget_max": 16,
        "passes_failed": 1,
        "fail_streak_max": 1,
        "by_endpoint_429": {},
    }

    text = build_daily_summary_text(
        day="2026-09-22", completeness=completeness, flow=flow
    )

    assert "4) Сорванных проходов: 1, максимум подряд 1." in text
