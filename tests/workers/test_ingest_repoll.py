"""The record of the result of the repeated poll and the flag (ingest, repoll-open).

Without a DB and a network: the fake session records the SQL that is executed. The main property — a tender without a change of the deadline/status does NOT touch tenders_normalized (there a trigger sends a NOTIFY → routing → matching), but only the mark of tenders_raw.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from src.workers import ingest as ingest_mod
from src.workers.ingest import (
    _REPOLL_TOUCH_RAW_SQL,
    _REPOLL_UPDATE_NORM_SQL,
    IngestWorker,
    repoll_enabled,
    repoll_max_per_pass,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
OLD_DEADLINE = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
NEW_DEADLINE = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)


class _FakeSession:
    def __init__(self, log: list):
        self._log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        self._log.append((str(stmt).strip(), dict(params or {})))

    async def commit(self):
        self._log.append(("COMMIT", {}))


def _stub_worker():
    log: list = []
    worker = SimpleNamespace(_session_factory=lambda: _FakeSession(log))
    return worker, log


def _row(*, deadline, status="active"):
    return {
        "source_uid": "375535",
        "deadline_at": deadline,
        "published_at": None,
        "first_seen_at": None,
        "status": status,
        "checked_at": None,
    }


def _apply(card, row):
    worker, log = _stub_worker()
    changed = asyncio.run(
        IngestWorker._repoll_apply(worker, "jnportal", "375535", row, card, NOW)
    )
    return changed, log


def test_changed_deadline_updates_normalized_and_touches_raw():
    changed, log = _apply(
        {"deadline_at": NEW_DEADLINE, "status": "active"},
        _row(deadline=OLD_DEADLINE),
    )
    assert changed is True
    statements = [s for s, _ in log]
    assert _REPOLL_UPDATE_NORM_SQL.text.strip() in statements
    assert _REPOLL_TOUCH_RAW_SQL.text.strip() in statements
    update_params = next(p for s, p in log if s == _REPOLL_UPDATE_NORM_SQL.text.strip())
    assert update_params["deadline_at"] == NEW_DEADLINE
    assert log[-1][0] == "COMMIT"


def test_unchanged_tender_never_touches_tenders_normalized():
    # The main invariant by the trigger: without a change — not a single UPDATE of the normalized row.
    changed, log = _apply(
        {"deadline_at": OLD_DEADLINE, "status": "active"},
        _row(deadline=OLD_DEADLINE),
    )
    assert changed is False
    statements = [s for s, _ in log]
    assert _REPOLL_UPDATE_NORM_SQL.text.strip() not in statements
    assert statements == [_REPOLL_TOUCH_RAW_SQL.text.strip(), "COMMIT"]


def test_empty_card_does_not_erase_stored_values():
    changed, log = _apply(
        {"deadline_at": None, "status": None}, _row(deadline=OLD_DEADLINE)
    )
    assert changed is False
    assert _REPOLL_UPDATE_NORM_SQL.text.strip() not in [s for s, _ in log]


def test_deadline_before_publication_is_not_a_change():
    # A broken deadline of the portal (earlier than the publication 2026-10-01 09:30 UTC) is not written:
    # the same guard as in the main pass. The expectations are literals.
    row = _row(deadline=None)
    row["published_at"] = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    changed, log = _apply(
        {
            "deadline_at": datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
            "status": "active",
        },
        row,
    )
    assert changed is False
    assert _REPOLL_UPDATE_NORM_SQL.text.strip() not in [s for s, _ in log]


def test_status_change_alone_counts_as_change():
    changed, _ = _apply(
        {"deadline_at": OLD_DEADLINE, "status": "closed"},
        _row(deadline=OLD_DEADLINE, status="active"),
    )
    assert changed is True


def test_repoll_disabled_by_default(monkeypatch):
    monkeypatch.delenv("INGEST_JNPORTAL_REPOLL", raising=False)
    assert repoll_enabled() is False


def test_repoll_flag_and_cap_read_from_env(monkeypatch):
    monkeypatch.setenv("INGEST_JNPORTAL_REPOLL", "1")
    monkeypatch.setenv("INGEST_JNPORTAL_REPOLL_MAX_PER_PASS", "7")
    assert repoll_enabled() is True
    assert repoll_max_per_pass() == 7
    monkeypatch.setenv("INGEST_JNPORTAL_REPOLL_MAX_PER_PASS", "junk")
    assert repoll_max_per_pass() == 40


def test_maybe_repoll_is_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("INGEST_JNPORTAL_REPOLL", raising=False)
    calls = []

    class _Adapter:
        def repoll_cards(self, uids):  # pragma: no cover — must not be called
            calls.append(uids)

    async def _open(*_a, **_k):  # pragma: no cover — must not be called
        calls.append("opened")
        return 0, 0

    worker = SimpleNamespace(_repoll_open=_open)
    asyncio.run(IngestWorker._maybe_repoll(worker, _Adapter(), "jnportal"))
    assert calls == []


def test_maybe_repoll_logs_only_exception_type(monkeypatch, caplog):
    monkeypatch.setenv("INGEST_JNPORTAL_REPOLL", "1")

    class _Adapter:
        def repoll_cards(self, uids):
            return None

    async def _boom(*_a, **_k):
        raise RuntimeError("GET https://x/GetDocument.ashx?accessToken=SECRET-123")

    worker = SimpleNamespace(_repoll_open=_boom)
    with caplog.at_level("WARNING", logger=ingest_mod.logger.name):
        asyncio.run(IngestWorker._maybe_repoll(worker, _Adapter(), "jnportal"))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text
    assert "SECRET-123" not in text
