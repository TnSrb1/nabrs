"""Unit tests of WorkerHealth (the observability of the workers, REV-05).

We check: (1) no-op without configuration; (2) the correct calls of the primitives with the configuration; (3) the network errors are SWALLOWED (the alerting does not fail the worker); (4) from_env. The network primitives (_http_get / _tg_send) are monkeypatched — the real network is not needed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import src.workers.health as health_mod
from src.workers.health import SourceFreshness, WorkerHealth, build_silence_alert_text


@pytest.fixture
def calls(monkeypatch):
    """Intercepts the network primitives; returns a list of record tuples."""
    rec: list[tuple] = []

    async def fake_get(url, timeout):
        rec.append(("get", url, timeout))

    async def fake_tg(token, chat_id, text, timeout):
        rec.append(("tg", token, chat_id, text, timeout))

    monkeypatch.setattr(health_mod, "_http_get", fake_get)
    monkeypatch.setattr(health_mod, "_tg_send", fake_tg)
    return rec


# --------------------------------------------------------------------------- #
#  no-op without configuration
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unconfigured_is_noop(calls):
    h = WorkerHealth()  # all the fields None / default
    await h.heartbeat_ok()
    await h.heartbeat_fail("x")
    await h.alert("boom")
    assert calls == []


# --------------------------------------------------------------------------- #
#  heartbeat
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_heartbeat_ok_pings_base_url(calls):
    h = WorkerHealth(healthcheck_url="https://hc.example/uuid")
    await h.heartbeat_ok()
    assert calls == [("get", "https://hc.example/uuid", h.timeout)]


@pytest.mark.asyncio
async def test_heartbeat_fail_pings_fail_suffix(calls):
    # a trailing slash in the base URL must not give "//fail"
    h = WorkerHealth(healthcheck_url="https://hc.example/uuid/")
    await h.heartbeat_fail("network down")
    assert calls == [("get", "https://hc.example/uuid/fail", h.timeout)]


# --------------------------------------------------------------------------- #
#  Telegram alert
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_alert_requires_both_token_and_chat(calls):
    await WorkerHealth(tg_bot_token="t").alert("x")  # only the token → off
    await WorkerHealth(admin_chat_id=1).alert("x")  # only the chat → off
    assert calls == []


@pytest.mark.asyncio
async def test_alert_sends_when_configured(calls):
    h = WorkerHealth(tg_bot_token="TOK", admin_chat_id=123456789)
    await h.alert("ingest упал")
    assert calls == [("tg", "TOK", 123456789, "ingest упал", h.timeout)]


# --------------------------------------------------------------------------- #
#  The network errors are swallowed (the alerting does NOT fail the worker)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_network_errors_are_swallowed(monkeypatch):
    async def boom_get(url, timeout):
        raise ConnectionError("down")

    async def boom_tg(token, chat_id, text, timeout):
        raise TimeoutError("slow")

    monkeypatch.setattr(health_mod, "_http_get", boom_get)
    monkeypatch.setattr(health_mod, "_tg_send", boom_tg)

    h = WorkerHealth(
        healthcheck_url="https://hc.example/uuid",
        tg_bot_token="TOK",
        admin_chat_id=1,
    )
    # no call must throw an exception outward
    await h.heartbeat_ok()
    await h.heartbeat_fail("x")
    await h.alert("x")


# --------------------------------------------------------------------------- #
#  from_env
# --------------------------------------------------------------------------- #
def test_from_env_reads_vars(monkeypatch):
    monkeypatch.setenv("INGEST_HEALTHCHECK_URL", "https://hc.example/u")
    monkeypatch.setenv("ADMIN_BOT_TOKEN", "TOK")
    monkeypatch.setenv("TG_ALERTS_CHAT_ID", "123456789")
    monkeypatch.delenv("WORKER_ALERT_TIMEOUT_SEC", raising=False)
    h = WorkerHealth.from_env()
    assert h.healthcheck_url == "https://hc.example/u"
    assert h.tg_bot_token == "TOK"
    assert h.admin_chat_id == 123456789
    assert h.tg_enabled is True


def test_from_env_empty_is_disabled(monkeypatch):
    monkeypatch.delenv("INGEST_HEALTHCHECK_URL", raising=False)
    monkeypatch.delenv("ADMIN_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_ALERTS_CHAT_ID", raising=False)
    h = WorkerHealth.from_env()
    assert h.healthcheck_url is None
    assert h.tg_enabled is False


def test_from_env_bad_chat_id_is_none(monkeypatch):
    monkeypatch.setenv("TG_ALERTS_CHAT_ID", "not-an-int")
    assert WorkerHealth.from_env().admin_chat_id is None


def test_from_env_only_token_without_chat_is_disabled(monkeypatch):
    """Only ADMIN_BOT_TOKEN without TG_ALERTS_CHAT_ID → tg_enabled=False (03.1)."""
    monkeypatch.setenv("ADMIN_BOT_TOKEN", "TOK")
    monkeypatch.delenv("TG_ALERTS_CHAT_ID", raising=False)
    h = WorkerHealth.from_env()
    assert h.tg_bot_token == "TOK"
    assert h.admin_chat_id is None
    assert h.tg_enabled is False


def test_from_env_does_not_fall_back_to_user_bot(monkeypatch):
    """The owner’s directive: TG_BOT_TOKEN/ADMIN_CHAT_ID (@nabrs_bot) are NOT read for the alerts."""
    monkeypatch.delenv("ADMIN_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_ALERTS_CHAT_ID", raising=False)
    monkeypatch.setenv("TG_BOT_TOKEN", "USER_BOT_TOK")
    monkeypatch.setenv("ADMIN_CHAT_ID", "1")
    h = WorkerHealth.from_env()
    assert h.tg_bot_token is None
    assert h.admin_chat_id is None
    assert h.tg_enabled is False


def test_from_env_custom_timeout(monkeypatch):
    monkeypatch.setenv("WORKER_ALERT_TIMEOUT_SEC", "3.5")
    assert WorkerHealth.from_env().timeout == 3.5


def test_from_env_bad_timeout_falls_back(monkeypatch):
    monkeypatch.setenv("WORKER_ALERT_TIMEOUT_SEC", "nope")
    assert WorkerHealth.from_env().timeout == 10.0


# --------------------------------------------------------------------------- #
#  silence_threshold_hours (REV-06)
# --------------------------------------------------------------------------- #
def test_silence_threshold_default_is_30(monkeypatch):
    monkeypatch.delenv("INGEST_SILENCE_ALERT_HOURS", raising=False)
    assert health_mod.silence_threshold_hours() == 30.0


def test_silence_threshold_env_override(monkeypatch):
    monkeypatch.setenv("INGEST_SILENCE_ALERT_HOURS", "12")
    assert health_mod.silence_threshold_hours() == 12.0


def test_silence_threshold_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("INGEST_SILENCE_ALERT_HOURS", "not-a-number")
    assert health_mod.silence_threshold_hours() == 30.0


def test_silence_threshold_non_positive_falls_back(monkeypatch):
    monkeypatch.setenv("INGEST_SILENCE_ALERT_HOURS", "0")
    assert health_mod.silence_threshold_hours() == 30.0


# --------------------------------------------------------------------------- #
#  build_silence_alert_text (REV-06) — literals, not a tautology
# --------------------------------------------------------------------------- #
def test_build_silence_alert_text_contains_numbers_and_sources(monkeypatch):
    monkeypatch.delenv("INGEST_SILENCE_ALERT_HOURS", raising=False)
    text = build_silence_alert_text(
        hours_silent=40.0,
        last_tender_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
        count_24h=3,
        sources=[
            SourceFreshness(
                source_key="jnportal",
                last_run_at=datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc),
                error_count=2,
            ),
            SourceFreshness(source_key="enabavke", last_run_at=None, error_count=0),
        ],
    )
    assert "приток тендеров молчит 40.0ч (порог 30ч)" in text
    assert "Последний тендер: 2026-05-28 20:00 UTC. За 24ч новых: 3." in text
    assert "jnportal: last_run_at=2026-05-30 10:00 UTC error_count=2" in text
    assert "enabavke: last_run_at=никогда error_count=0" in text


def test_build_silence_alert_text_never_seen_tender():
    text = build_silence_alert_text(
        hours_silent=float("inf"), last_tender_at=None, count_24h=0, sources=[]
    )
    assert "Последний тендер: никогда. За 24ч новых: 0." in text
