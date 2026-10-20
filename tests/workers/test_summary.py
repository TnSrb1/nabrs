"""Unit tests of SummaryWorker (the port of v6 daily_summary).

The DB and the channel adapter are mocked (as in test_reminders.py / test_delivery.py): we check the window of the sending (before SUMMARY_HOUR we do not send; after — we send), the idempotency of claim-then-send (a repeat within the same day does not send), the assembly of the text of the summary (the counters + the top deadlines + the branch “no deadlines”), the branch “everything is empty → do not send” and the non-rollback of the journal on a channel failure — without a real PostgreSQL/network.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.workers.adapters.base import DeliveryResult
from src.workers.summary import SummaryWorker, build_summary_text

# Fixed “now” (UTC). The candidates have tz=“UTC” → local == UTC, the hour
# of the summary is controlled directly (SUMMARY_HOUR = 8).
NOW_LATE = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)  # 09:00 ≥ 8 → we send
NOW_EARLY = datetime(2026, 6, 5, 5, 0, tzinfo=timezone.utc)  # 05:00 < 8 → too early


def _now_late() -> datetime:
    return NOW_LATE


def _now_early() -> datetime:
    return NOW_EARLY


# --------------------------------------------------------------------------- #
#  DB mocks (as in test_reminders.py)
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


def _tenant(**over) -> SimpleNamespace:
    """A candidate row of the tenant (as from _TENANTS_SQL)."""
    row = SimpleNamespace(tenant_id="ten-1", tz="UTC", target="555")
    for key, value in over.items():
        setattr(row, key, value)
    return row


def _counts(active=0, watching=0, submitted=0, won=0) -> _FakeResult:
    return _FakeResult(
        [
            SimpleNamespace(
                active=active, watching=watching, submitted=submitted, won=won
            )
        ]
    )


def _dl(title="Видеонаблюдение", days_ahead=2) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        deadline_at=NOW_LATE + timedelta(days=days_ahead),
        source_url="https://jnportal.rs/t/1",
    )


def _worker(session, adapter=None, now_factory=_now_late, **kw) -> SummaryWorker:
    return SummaryWorker(
        adapters={"telegram": adapter or _FakeAdapter()},
        session_factory=_session_factory(session),
        now_factory=now_factory,
        **kw,
    )


# --------------------------------------------------------------------------- #
#  The assembly of the text of the summary (a pure function)
# --------------------------------------------------------------------------- #


def test_build_summary_text_counts_and_deadlines():
    msg = build_summary_text(
        active=2,
        watching=1,
        submitted=3,
        won=0,
        deadlines=[
            SimpleNamespace(
                title="Тендер А",
                deadline_at=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                title="Тендер Б",
                deadline_at=datetime(2026, 6, 12, 9, 30, tzinfo=timezone.utc),
            ),
        ],
    )
    assert "📊 Ежедневная сводка" in msg
    assert "✅ Участвуем: 2" in msg
    assert "👀 Изучаем: 1" in msg
    assert "📨 Подано: 3" in msg
    assert "🏆 Выиграно: 0" in msg
    assert "⏳ Ближайшие дедлайны:" in msg
    assert "<b>Тендер А</b>" in msg
    assert "🗓 10.06.2026 12:00" in msg  # 10:00Z of June → 12:00 Belgrade time
    assert "<b>Тендер Б</b>" in msg


def test_build_summary_text_no_deadlines_branch():
    msg = build_summary_text(active=1, watching=0, submitted=0, won=0, deadlines=[])
    assert "нет ближайших дедлайнов" in msg
    assert "• <b>" not in msg


def test_build_summary_text_renders_up_to_five():
    deadlines = [_dl(title=f"T{i}", days_ahead=i + 1) for i in range(5)]
    msg = build_summary_text(
        active=5, watching=0, submitted=0, won=0, deadlines=deadlines
    )
    assert msg.count("• <b>") == 5


def test_build_summary_text_escapes_title():
    msg = build_summary_text(
        active=1,
        watching=0,
        submitted=0,
        won=0,
        deadlines=[SimpleNamespace(title="<b>x</b> & y", deadline_at=None)],
    )
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in msg
    assert "🗓 —" in msg  # deadline None → a dash


# --------------------------------------------------------------------------- #
#  The window of the sending + the candidates
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tenants_sql_filters_active_channel():
    session = _FakeSession([_FakeResult([])])
    worker = _worker(session)
    await worker.poll_once()
    stmt, params = session.calls[0]
    assert "uc.status = 'active'" in stmt
    assert params["channel"] == "telegram"


@pytest.mark.asyncio
async def test_before_summary_hour_does_not_send_or_claim():
    # 05:00 local < SUMMARY_HOUR(8) → neither the aggregates, nor a claim, nor a send
    session = _FakeSession([_FakeResult([_tenant()])])
    adapter = _FakeAdapter()
    worker = _worker(session, adapter, now_factory=_now_early)
    sent = await worker.poll_once()
    assert sent == 0
    assert adapter.sent == []
    # only the query of the candidates; the aggregates were not queried
    assert len(session.calls) == 1
    assert all("summary_log" not in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_after_summary_hour_claims_then_sends():
    # 09:00 ≥ 8, there is a funnel + a deadline → the claim returned the row → we send
    session = _FakeSession(
        [
            _FakeResult([_tenant()]),
            _counts(active=2, watching=1),
            _FakeResult([_dl()]),
            _FakeResult([SimpleNamespace(id="s-1")]),
        ]
    )
    adapter = _FakeAdapter()
    worker = _worker(session, adapter)
    sent = await worker.poll_once()
    assert sent == 1
    assert len(adapter.sent) == 1
    target, text = adapter.sent[0]
    assert target == "555"
    assert "✅ Участвуем: 2" in text
    # the claim passed into summary_log and is committed BEFORE the send
    assert any("summary_log" in s for s, _ in session.calls)
    assert session.commits >= 1


@pytest.mark.asyncio
async def test_idempotent_conflict_does_not_send():
    # the claim did not return a row (ON CONFLICT — the summary of the day was already there) → we do NOT send
    session = _FakeSession(
        [
            _FakeResult([_tenant()]),
            _counts(active=1),
            _FakeResult([_dl()]),
            _FakeResult([]),  # claim: conflict
        ]
    )
    adapter = _FakeAdapter()
    worker = _worker(session, adapter)
    sent = await worker.poll_once()
    assert sent == 0
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_empty_funnel_and_no_deadlines_skips_claim_and_send():
    # all the counters are 0 AND there are no deadlines → neither a claim, nor a send (the anti-spam of emptiness)
    session = _FakeSession(
        [
            _FakeResult([_tenant()]),
            _counts(),  # all zeros
            _FakeResult([]),  # there are no deadlines
        ]
    )
    adapter = _FakeAdapter()
    worker = _worker(session, adapter)
    sent = await worker.poll_once()
    assert sent == 0
    assert adapter.sent == []
    assert all("summary_log" not in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_empty_funnel_but_has_deadline_still_sends():
    # counters 0, but there is a nearest deadline → we send the summary
    session = _FakeSession(
        [
            _FakeResult([_tenant()]),
            _counts(),
            _FakeResult([_dl()]),
            _FakeResult([SimpleNamespace(id="s-1")]),
        ]
    )
    adapter = _FakeAdapter()
    worker = _worker(session, adapter)
    sent = await worker.poll_once()
    assert sent == 1
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_send_failure_does_not_rollback_journal():
    # the claim passed, but the channel fell with an exception — the journal is NOT rolled back, the worker is alive
    session = _FakeSession(
        [
            _FakeResult([_tenant()]),
            _counts(active=1),
            _FakeResult([_dl()]),
            _FakeResult([SimpleNamespace(id="s-1")]),
        ]
    )
    adapter = _FakeAdapter(raise_exc=RuntimeError("network glitch"))
    worker = _worker(session, adapter)
    sent = await worker.poll_once()  # does not fall
    assert sent == 0
    assert any("summary_log" in s for s, _ in session.calls)
    assert session.commits >= 1
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_adapter_not_ok_counts_as_not_sent():
    # send_text returned ok=False → we do not count it as sent (the journal is already committed)
    session = _FakeSession(
        [
            _FakeResult([_tenant()]),
            _counts(won=1),
            _FakeResult([]),
            _FakeResult([SimpleNamespace(id="s-1")]),
        ]
    )
    adapter = _FakeAdapter(
        result=DeliveryResult(ok=False, response_code=403, blocked=True)
    )
    worker = _worker(session, adapter)
    sent = await worker.poll_once()
    assert sent == 0
    assert len(adapter.sent) == 1  # the attempt was made


@pytest.mark.asyncio
async def test_unknown_channel_adapter_skipped():
    session = _FakeSession([])
    worker = _worker(session, enabled_channels=("viber",))  # no viber adapter
    sent = await worker.poll_once()
    assert sent == 0
    assert session.calls == []  # the candidates were not even queried
