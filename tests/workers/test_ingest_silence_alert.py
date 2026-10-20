"""The alert of the silent stop of the inflow (REV-06, tmp/TASK_ingest_silence_alert.md).

The symptom of 07-09.09.2026: the ingest pass finished WITHOUT exceptions (the portal gave emptiness/changed the markup so that the parser does not fall), therefore heartbeat_ok() stayed silent as readily as on a healthy stream, and nobody learned about the inflow that had sunk threefold. `IngestWorker._check_silence()` measures the ACTUAL freshness of `tenders_normalized` after a clean pass and sends a Telegram alert via `WorkerHealth.alert()` if the silence exceeded the threshold (the anti-bounce — Redis, `SilenceAlertGate`).

The assertions — a literal from the text of the alert (not through the same text-assembling function as the code) where this is the only way to prove the contents; where the formula/format itself (`build_silence_alert_text`) is checked, it is covered separately in `test_health.py`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.workers.ingest import IngestWorker

FIXED_NOW = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
#  Fakes
# --------------------------------------------------------------------------- #
class _FreshnessResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSilenceSession:
    """Answers exactly two requests of the silence check; the rest — empty."""

    def __init__(self, *, last_at, cnt_24h, sources_rows, boom=False, fresh_rows=None):
        self.last_at = last_at
        self.cnt_24h = cnt_24h
        self.sources_rows = sources_rows
        self.boom = boom
        # The freshness over the enabled sources (SRC-0a). By default — one
        # enabled jnportal, as in production on 30.09.2026.
        self.fresh_rows = fresh_rows
        self.executed_sql: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed_sql.append(sql)
        if self.boom:
            raise ConnectionError("the DB is unavailable")
        if "MAX(first_seen_at)" in sql:
            if self.fresh_rows is not None:
                return _FreshnessResult(self.fresh_rows)
            return _FreshnessResult(
                [
                    SimpleNamespace(
                        source_key="jnportal",
                        enabled_at=None,
                        last_at=self.last_at,
                        cnt_24h=self.cnt_24h,
                    )
                ]
            )
        if "ORDER BY source_key" in sql:
            return _FreshnessResult(self.sources_rows)
        return _FreshnessResult([])

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class _FakeHealth:
    """Records the calls of alert()/heartbeat_ok() — without the real network."""

    def __init__(self):
        self.alert_calls: list[str] = []
        self.heartbeat_ok_calls = 0

    async def heartbeat_ok(self) -> None:
        self.heartbeat_ok_calls += 1

    async def heartbeat_fail(self, reason=None) -> None:
        raise AssertionError("heartbeat_fail must not ring in these tests")

    async def alert(self, text: str) -> None:
        self.alert_calls.append(text)


class _FakeRedis:
    """In-memory Redis: only get/set, as in SilenceAlertGate."""

    def __init__(self):
        self.kv: dict[str, str] = {}

    async def get(self, key):
        v = self.kv.get(key)
        return v.encode() if v is not None else None

    async def set(self, key, value, ex=None):
        self.kv[key] = value


class _BrokenRedis:
    async def get(self, key):
        raise ConnectionError("Redis is unavailable")

    async def set(self, key, value, ex=None):
        raise ConnectionError("Redis is unavailable")


@dataclass
class _Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


class _Mails:
    """Records the letters to the administrators (instead of the real SMTP)."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, subject: str, body: str) -> int:
        self.sent.append((subject, body))
        return 1


def _make_worker(session, *, health=None, redis=None, clock=None, mails=None):
    return IngestWorker(
        admin_emailer=mails or _Mails(),
        session_factory=_session_factory(session),
        source_key="fake",
        adapter_factory=lambda key: None,
        clock=clock or (lambda: FIXED_NOW),
        health=health or _FakeHealth(),
        redis=redis,
    )


_STALE_SOURCES = [
    SimpleNamespace(
        source_key="jnportal",
        last_run_at=datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc),
        error_count=2,
    ),
    SimpleNamespace(source_key="enabavke", last_run_at=None, error_count=0),
]


# --------------------------------------------------------------------------- #
#  Red/green: a silence above the threshold sends the alert with the figures
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_silence_alert_fires_when_stale():
    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),  # 40 h ago
        cnt_24h=3,
        sources_rows=_STALE_SOURCES,
    )
    health = _FakeHealth()
    worker = _make_worker(session, health=health, redis=_FakeRedis())

    await worker._check_silence()

    assert len(health.alert_calls) == 1
    text = health.alert_calls[0]
    assert "приток тендеров молчит 40.0ч (порог 30ч)" in text
    assert "Последний тендер: 2026-05-28 20:00 UTC. За 24ч новых: 3." in text
    assert "jnportal: last_run_at=2026-05-30 10:00 UTC error_count=2" in text
    assert "enabavke: last_run_at=никогда error_count=0" in text


@pytest.mark.asyncio
async def test_single_source_alert_text_is_unchanged_literal():
    """the behavior of the alert for jnportal does not change (SRC-0a): the full text as a literal."""
    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
        cnt_24h=3,
        sources_rows=_STALE_SOURCES,
    )
    health = _FakeHealth()
    redis = _FakeRedis()
    worker = _make_worker(session, health=health, redis=redis)

    await worker._check_silence()

    assert health.alert_calls == [
        "⚠️ TenderSRB ingest: приток тендеров молчит 40.0ч (порог 30ч).\n"
        "Последний тендер: 2026-05-28 20:00 UTC. За 24ч новых: 3.\n"
        "Источники:\n"
        "  jnportal: last_run_at=2026-05-30 10:00 UTC error_count=2\n"
        "  enabavke: last_run_at=никогда error_count=0"
    ]
    # The anti-bounce key is the previous one — the rollout does not reset the effective cooldown.
    assert list(redis.kv) == ["ingest:silence_alert_sent_at"]


@pytest.mark.asyncio
async def test_live_second_source_does_not_mask_dead_first():
    """A global MAX would mask the death of jnportal by a live second source."""
    session = _FakeSilenceSession(
        last_at=None,
        cnt_24h=0,
        sources_rows=_STALE_SOURCES,
        fresh_rows=[
            SimpleNamespace(
                source_key="jnportal",
                enabled_at=None,
                last_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
                cnt_24h=0,
            ),
            SimpleNamespace(
                source_key="ted_balkan",
                enabled_at=None,
                last_at=datetime(2026, 5, 30, 11, 0, tzinfo=timezone.utc),
                cnt_24h=90,
            ),
        ],
    )
    health = _FakeHealth()
    redis = _FakeRedis()
    worker = _make_worker(session, health=health, redis=redis)

    await worker._check_silence()

    assert len(health.alert_calls) == 1
    assert health.alert_calls[0].startswith(
        "⚠️ TenderSRB ingest: приток тендеров «jnportal» молчит 40.0ч (порог 30ч)."
    )
    assert list(redis.kv) == ["ingest:silence_alert_sent_at:jnportal"]


@pytest.mark.asyncio
async def test_just_enabled_source_without_tenders_is_measured_from_enabled_at():
    session = _FakeSilenceSession(
        last_at=None,
        cnt_24h=0,
        sources_rows=[],
        fresh_rows=[
            SimpleNamespace(
                source_key="jnportal",
                enabled_at=None,
                last_at=datetime(2026, 5, 30, 11, 0, tzinfo=timezone.utc),
                cnt_24h=90,
            ),
            SimpleNamespace(
                source_key="ted_balkan",
                enabled_at=datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc),
                last_at=None,
                cnt_24h=0,
            ),
        ],
    )
    health = _FakeHealth()
    worker = _make_worker(session, health=health, redis=_FakeRedis())

    await worker._check_silence()

    assert health.alert_calls == []


# --------------------------------------------------------------------------- #
#  Counter-fact: fresh data — the alert does NOT go out
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_alert_when_data_is_fresh():
    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 30, 11, 0, tzinfo=timezone.utc),  # 1 h ago
        cnt_24h=150,
        sources_rows=_STALE_SOURCES,
    )
    health = _FakeHealth()
    worker = _make_worker(session, health=health, redis=_FakeRedis())

    await worker._check_silence()

    assert health.alert_calls == []


# --------------------------------------------------------------------------- #
#  The anti-bounce: a repeated pass within 24 h does not send a second alert
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_debounce_suppresses_repeat_within_cooldown():
    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
        cnt_24h=0,
        sources_rows=_STALE_SOURCES,
    )
    health = _FakeHealth()
    redis = _FakeRedis()
    clock = _Clock(FIXED_NOW)
    worker = _make_worker(session, health=health, redis=redis, clock=clock)

    await worker._check_silence()
    assert len(health.alert_calls) == 1

    clock.now = FIXED_NOW + timedelta(hours=23, minutes=59)
    await worker._check_silence()
    assert len(health.alert_calls) == 1  # did not grow


@pytest.mark.asyncio
async def test_alert_resumes_after_cooldown_expires():
    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
        cnt_24h=0,
        sources_rows=_STALE_SOURCES,
    )
    health = _FakeHealth()
    redis = _FakeRedis()
    clock = _Clock(FIXED_NOW)
    worker = _make_worker(session, health=health, redis=redis, clock=clock)

    await worker._check_silence()
    assert len(health.alert_calls) == 1

    clock.now = FIXED_NOW + timedelta(hours=24, minutes=1)
    await worker._check_silence()
    assert len(health.alert_calls) == 2


# --------------------------------------------------------------------------- #
#  The threshold from the ENV (without editing docker-compose)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_custom_threshold_env_overrides_default(monkeypatch):
    monkeypatch.setenv("INGEST_SILENCE_ALERT_HOURS", "2")
    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),  # 3 h ago
        cnt_24h=10,
        sources_rows=_STALE_SOURCES,
    )
    health = _FakeHealth()
    worker = _make_worker(session, health=health, redis=_FakeRedis())

    await worker._check_silence()

    assert len(health.alert_calls) == 1
    assert "молчит 3.0ч (порог 2ч)" in health.alert_calls[0]


# --------------------------------------------------------------------------- #
#  The contract of reliability: a DB/Redis error — a silent no-op, the worker does not fall
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_db_error_during_silence_check_is_swallowed():
    session = _FakeSilenceSession(last_at=None, cnt_24h=0, sources_rows=[], boom=True)
    health = _FakeHealth()
    worker = _make_worker(session, health=health, redis=_FakeRedis())

    await worker._check_silence()  # must not throw an exception

    assert health.alert_calls == []


@pytest.mark.asyncio
async def test_broken_redis_gate_defaults_to_permissive_send():
    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
        cnt_24h=0,
        sources_rows=_STALE_SOURCES,
    )
    health = _FakeHealth()
    worker = _make_worker(session, health=health, redis=_BrokenRedis())

    await worker._check_silence()  # Redis is unavailable → it sends all the same

    assert len(health.alert_calls) == 1


@pytest.mark.asyncio
async def test_no_redis_configured_still_sends_alert():
    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
        cnt_24h=0,
        sources_rows=_STALE_SOURCES,
    )
    health = _FakeHealth()
    worker = _make_worker(session, health=health, redis=None)

    await worker._check_silence()

    assert len(health.alert_calls) == 1


# --------------------------------------------------------------------------- #
#  The wiring in run_once(): only after a CLEAN pass
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_run_once_checks_silence_after_clean_pass():
    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
        cnt_24h=0,
        sources_rows=[],
    )
    health = _FakeHealth()
    worker = _make_worker(session, health=health, redis=_FakeRedis())

    async def _keys():
        return ["fake"]

    worker.source_keys = _keys

    async def fake_ingest_source(source_key):
        return 900  # success — _pass_had_failure stays False

    worker.ingest_source = fake_ingest_source

    await worker.run_once()

    assert health.heartbeat_ok_calls == 1
    assert len(health.alert_calls) == 1  # the silence alert did fire


@pytest.mark.asyncio
async def test_run_once_skips_silence_check_after_failed_pass():
    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),  # also silence
        cnt_24h=0,
        sources_rows=[],
    )
    health = _FakeHealth()
    worker = _make_worker(session, health=health, redis=_FakeRedis())

    async def _keys():
        return ["fake"]

    worker.source_keys = _keys

    async def fake_ingest_source(source_key):
        worker._pass_had_failure = True
        return 900

    worker.ingest_source = fake_ingest_source

    await worker.run_once()

    assert health.heartbeat_ok_calls == 0
    assert health.alert_calls == []  # a broken pass alerts by its own path


# --------------------------------------------------------------------------- #
#  SRC-0d (the owner 30.09 evening, item 4): “check the source <name>” in Telegram AND by the mail
# --------------------------------------------------------------------------- #
def _named_fresh_rows():
    return [
        SimpleNamespace(
            source_key="jnportal",
            display_name="Portal javnih nabavki",
            enabled_at=None,
            last_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
            cnt_24h=0,
        )
    ]


@pytest.mark.asyncio
async def test_silence_alert_names_source_in_telegram_and_email():
    session = _FakeSilenceSession(
        last_at=None,
        cnt_24h=0,
        sources_rows=_STALE_SOURCES,
        fresh_rows=_named_fresh_rows(),
    )
    health, mails = _FakeHealth(), _Mails()
    worker = _make_worker(session, health=health, redis=_FakeRedis(), mails=mails)

    await worker._check_silence()

    assert health.alert_calls[0].startswith(
        "🔎 Проверить источник «Portal javnih nabavki»\n"
    )
    assert len(mails.sent) == 1
    subject, body = mails.sent[0]
    assert subject == "TenderSRB: проверить источник «Portal javnih nabavki»"
    assert "молчит 40.0ч (порог 30ч)" in body


@pytest.mark.asyncio
async def test_email_alert_is_debounced_together_with_telegram():
    session = _FakeSilenceSession(
        last_at=None,
        cnt_24h=0,
        sources_rows=_STALE_SOURCES,
        fresh_rows=_named_fresh_rows(),
    )
    health, mails = _FakeHealth(), _Mails()
    clock = _Clock(FIXED_NOW)
    worker = _make_worker(
        session, health=health, redis=_FakeRedis(), clock=clock, mails=mails
    )
    await worker._check_silence()
    clock.now = FIXED_NOW + timedelta(hours=12)
    await worker._check_silence()
    assert (len(health.alert_calls), len(mails.sent)) == (1, 1)
    clock.now = FIXED_NOW + timedelta(hours=24, minutes=1)
    await worker._check_silence()
    assert (len(health.alert_calls), len(mails.sent)) == (2, 2)


@pytest.mark.asyncio
async def test_no_email_when_source_is_fresh():
    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 30, 11, 0, tzinfo=timezone.utc),
        cnt_24h=150,
        sources_rows=_STALE_SOURCES,
    )
    mails = _Mails()
    await _make_worker(session, redis=_FakeRedis(), mails=mails)._check_silence()
    assert mails.sent == []


@pytest.mark.asyncio
async def test_email_failure_does_not_break_silence_check(monkeypatch):
    async def boom(subject, body):
        raise RuntimeError("smtp down")

    session = _FakeSilenceSession(
        last_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
        cnt_24h=0,
        sources_rows=_STALE_SOURCES,
    )
    health = _FakeHealth()
    worker = _make_worker(session, health=health, redis=_FakeRedis())
    worker._admin_emailer = boom
    await worker._check_silence()  # does not fall
    # The Telegram alert went out, and the anti-bounce is armed: a failure of the mail does not loop the alert
    assert len(health.alert_calls) == 1
    await worker._check_silence()
    assert len(health.alert_calls) == 1


@pytest.mark.asyncio
async def test_default_admin_email_goes_to_allowlist_and_info_inbox_without_transport(
    monkeypatch,
):
    from src.api.emailer import MockEmailSender
    from src.workers import adapters
    from src.workers.ingest import _admin_email_from_env

    monkeypatch.setenv("CABINET_ADMIN_EMAILS", "op1@example.com, Op2@example.com")
    monkeypatch.setattr(adapters, "email_sender_from_env", lambda: None)
    assert await _admin_email_from_env("s", "b") == 0  # there is no transport — a silent no-op

    mock = MockEmailSender()
    monkeypatch.setattr(adapters, "email_sender_from_env", lambda: mock)
    assert await _admin_email_from_env("subj", "body") == 3
    assert sorted(m.to for m in mock.outbox) == [
        "info@nab.rs",
        "op1@example.com",
        "op2@example.com",
    ]
    assert {m.subject for m in mock.outbox} == {"subj"}
