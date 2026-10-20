"""The registry of the sources from the table ``sources`` and the cursor without a backfill (SRC-0a).

The owner’s decisions of 30.09.2026: R1 — the list of sources from the table (the flag ``enabled``), not from the registry of adapters; R4 — a new source loads only the current, ``since=None`` (“pull the whole archive”) is never passed by the worker to the adapter.

The expectations — literals from the fact (``.claude/rules/tests-known-defects.md``), and not through the same code that is checked.
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.parsers.base import SourceAdapter
from src.workers import ingest as ingest_mod
from src.workers.ingest import IngestWorker

FIXED_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
ENABLED_AT = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _Session:
    """Answers the requests to ``sources``; the rest — empty."""

    def __init__(self, *, runtime_row=None, enabled_keys=()):
        self.runtime_row = runtime_row
        self.enabled_keys = list(enabled_keys)
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        if "FROM sources" in sql and "enabled = true" in sql:
            return _Result([SimpleNamespace(source_key=k) for k in self.enabled_keys])
        if "FROM sources" in sql:
            return _Result([self.runtime_row] if self.runtime_row else [])
        return _Result([])

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def _factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class _Adapter(SourceAdapter):
    source_key = "fake"
    source_name = "Fake"
    base_url = "https://example"
    country_codes = ["RS"]
    request_rate = 1.0
    default_polling_interval = 900
    supported_languages = ["sr-Latn"]

    async def fetch_batch(self, since):  # pragma: no cover — is not called
        if False:
            yield

    def build_source_uid(self, raw):  # pragma: no cover
        return ""

    def normalize(self, raw):  # pragma: no cover
        raise NotImplementedError


async def _keys(worker: IngestWorker) -> list[str]:
    res = worker.source_keys()
    if inspect.isawaitable(res):
        res = await res
    return list(res)


# --------------------------------------------------------------------------- #
#  (iv) R1: the sources — from the table sources ∩ the registry
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_source_keys_come_from_sources_table_enabled_only(monkeypatch):
    """The table {A: enabled, B: disabled}, the registry {A, B, C} → only {A}.

    Before SRC-0a ``source_keys()`` returned the whole registry {A, B, C}: the inclusion was decided by the registry of imports, not by the table. B is switched off, C has no row at all.
    """
    monkeypatch.setattr(
        ingest_mod,
        "registered_sources",
        lambda: {"A": _Adapter, "B": _Adapter, "C": _Adapter},
    )
    session = _Session(enabled_keys=["A"])  # SQL returns only enabled = true
    worker = IngestWorker(
        session_factory=_factory(session),
        adapter_factory=lambda key: _Adapter(),
        clock=lambda: FIXED_NOW,
    )

    assert await _keys(worker) == ["A"]


@pytest.mark.asyncio
async def test_enabled_row_without_adapter_is_skipped_not_crashing(monkeypatch):
    """An enabled row without an adapter (D) — a skip with a WARNING, not a fall."""
    monkeypatch.setattr(ingest_mod, "registered_sources", lambda: {"A": _Adapter})
    session = _Session(enabled_keys=["A", "D"])
    worker = IngestWorker(
        session_factory=_factory(session),
        adapter_factory=lambda key: _Adapter(),
        clock=lambda: FIXED_NOW,
    )

    assert await _keys(worker) == ["A"]


# --------------------------------------------------------------------------- #
#  (i) R4: the first pass — not since=None
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_first_pass_since_is_enabled_at_not_none():
    """``enabled=true, last_run_at=NULL`` → since = enabled_at, NOT None.

    Before SRC-0a ``since = sources.last_run_at`` and NULL went to the adapter as “the full initial load” — the whole archive of the new source.
    """
    session = _Session(
        runtime_row=SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=None,
            enabled=True,
            error_count=0,
            enabled_at=ENABLED_AT,
            baseline_at=None,
        )
    )
    worker = IngestWorker(
        session_factory=_factory(session),
        source_key="fake",
        adapter_factory=lambda key: _Adapter(),
        clock=lambda: FIXED_NOW,
    )
    seen: list = []

    async def _spy(adapter, source_key, since):
        seen.append(since)
        return ingest_mod.SourceResult(source_key)

    worker._ingest = _spy  # type: ignore[method-assign]

    await worker.ingest_source("fake")

    assert seen == [datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)]


@pytest.mark.asyncio
async def test_first_pass_since_respects_initial_lookback():
    """``initial_lookback`` of the adapter moves the cursor of the first pass back."""

    class _Lookback(_Adapter):
        initial_lookback = timedelta(days=2)

    session = _Session(
        runtime_row=SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=None,
            enabled=True,
            error_count=0,
            enabled_at=ENABLED_AT,
            baseline_at=None,
        )
    )
    worker = IngestWorker(
        session_factory=_factory(session),
        source_key="fake",
        adapter_factory=lambda key: _Lookback(),
        clock=lambda: FIXED_NOW,
    )
    seen: list = []

    async def _spy(adapter, source_key, since):
        seen.append(since)
        return ingest_mod.SourceResult(source_key)

    worker._ingest = _spy  # type: ignore[method-assign]

    await worker.ingest_source("fake")

    assert seen == [datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)]


@pytest.mark.asyncio
async def test_enabled_row_without_enabled_at_is_not_ingested():
    """Enabled, but ``enabled_at IS NULL`` — do not load (fail-closed), not since=None."""
    session = _Session(
        runtime_row=SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=None,
            enabled=True,
            error_count=0,
            enabled_at=None,
            baseline_at=None,
        )
    )
    worker = IngestWorker(
        session_factory=_factory(session),
        source_key="fake",
        adapter_factory=lambda key: _Adapter(),
        clock=lambda: FIXED_NOW,
    )
    seen: list = []

    async def _spy(adapter, source_key, since):  # pragma: no cover — must not call
        seen.append(since)
        return ingest_mod.SourceResult(source_key)

    worker._ingest = _spy  # type: ignore[method-assign]

    await worker.ingest_source("fake")

    assert seen == []
    assert not any("last_status = 'ok'" in sql for sql, _ in session.calls)


@pytest.mark.asyncio
async def test_later_pass_since_is_last_run_at():
    """The routine pass: since = last_run_at (the behavior of jnportal does not change)."""
    last_run = datetime(2026, 9, 30, 11, 30, tzinfo=timezone.utc)
    session = _Session(
        runtime_row=SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=last_run,
            enabled=True,
            error_count=0,
            enabled_at=ENABLED_AT,
            baseline_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        )
    )
    worker = IngestWorker(
        session_factory=_factory(session),
        source_key="fake",
        adapter_factory=lambda key: _Adapter(),
        clock=lambda: FIXED_NOW,
    )
    seen: list = []

    async def _spy(adapter, source_key, since):
        seen.append(since)
        return ingest_mod.SourceResult(source_key)

    worker._ingest = _spy  # type: ignore[method-assign]

    await worker.ingest_source("fake")

    assert seen == [datetime(2026, 9, 30, 11, 30, tzinfo=timezone.utc)]


@pytest.mark.asyncio
async def test_first_successful_pass_sets_baseline_in_mark_ok():
    """A silent first pass: baseline_at is set in the same UPDATE as the mark ok."""
    session = _Session(
        runtime_row=SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=None,
            enabled=True,
            error_count=0,
            enabled_at=ENABLED_AT,
            baseline_at=None,
        )
    )
    worker = IngestWorker(
        session_factory=_factory(session),
        source_key="fake",
        adapter_factory=lambda key: _Adapter(),
        clock=lambda: FIXED_NOW,
    )

    async def _ok(adapter, source_key, since):
        return ingest_mod.SourceResult(source_key)

    worker._ingest = _ok  # type: ignore[method-assign]

    await worker.ingest_source("fake")

    mark_ok = [(sql, p) for sql, p in session.calls if "last_status = 'ok'" in sql]
    assert len(mark_ok) == 1
    sql, params = mark_ok[0]
    assert "baseline_at = COALESCE(baseline_at," in sql
    assert params["now"] == FIXED_NOW


# --------------------------------------------------------------------------- #
#  R4: “only the current” on the INSERT — a skip by policy, not by a failure
# --------------------------------------------------------------------------- #
from src.parsers.base import NormalizedTender, RawTender  # noqa: E402


class _PolicySession(_Session):
    """There is neither a raw nor a normalized — the tender is new for us; we write only the SQL count."""

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        if "INSERT INTO tenders_raw" in sql or "INSERT INTO tenders_normalized" in sql:
            return _Result([SimpleNamespace(id="new-1")])
        return _Result([])


class _OneTenderAdapter(_Adapter):
    def __init__(self, normalized: NormalizedTender, *, only_actual: bool = True):
        self._n = normalized
        self.insert_only_actual = only_actual

    async def fetch_batch(self, since):
        yield RawTender(source_key="fake", payload={"id": "1"}, country_code="BA")

    def build_source_uid(self, raw):
        return "1"

    def normalize(self, raw):
        return self._n


def _n(**kw) -> NormalizedTender:
    base = dict(source_key="fake", source_uid="1", country_code="BA", status="active")
    base.update(kw)
    return NormalizedTender(**base)


async def _run_one(adapter) -> tuple[ingest_mod.SourceResult, _PolicySession]:
    session = _PolicySession()
    worker = IngestWorker(
        session_factory=_factory(session),
        source_key="fake",
        adapter_factory=lambda key: adapter,
        clock=lambda: FIXED_NOW,
    )
    result = await worker._ingest(adapter, "fake", since=ENABLED_AT)
    return result, session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "normalized",
    [
        _n(status="closed"),
        _n(status="awarded"),
        _n(status="cancelled"),
        _n(deadline_at=datetime(2026, 9, 30, 11, 0, tzinfo=timezone.utc)),
        _n(deadline_at=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)),
    ],
)
async def test_new_not_actual_tender_is_not_inserted(normalized):
    result, session = await _run_one(_OneTenderAdapter(normalized))

    assert (result.inserted, result.skipped_not_actual, result.failed) == (0, 1, False)
    assert not any("INSERT INTO tenders_raw" in sql for sql, _ in session.calls)
    assert not any("INSERT INTO tenders_normalized" in sql for sql, _ in session.calls)


@pytest.mark.asyncio
async def test_naive_deadline_is_compared_as_utc_not_crashing():
    """A naive deadline does not drop the tender into “broken” (the cursor would move — a loss)."""
    past = _n(deadline_at=datetime(2026, 9, 30, 11, 0))  # naive, an hour ago in UTC
    future = _n(deadline_at=datetime(2026, 10, 10, 11, 0))

    r_past, _ = await _run_one(_OneTenderAdapter(past))
    r_future, _ = await _run_one(_OneTenderAdapter(future))

    assert (r_past.errors, r_past.skipped_not_actual) == (0, 1)
    assert (r_future.errors, r_future.inserted) == (0, 1)


@pytest.mark.asyncio
async def test_new_actual_tender_is_inserted():
    normalized = _n(deadline_at=datetime(2026, 10, 10, tzinfo=timezone.utc))
    result, session = await _run_one(_OneTenderAdapter(normalized))

    assert (result.inserted, result.skipped_not_actual) == (1, 0)
    assert any("INSERT INTO tenders_normalized" in sql for sql, _ in session.calls)


@pytest.mark.asyncio
async def test_policy_off_inserts_closed_tender():
    """insert_only_actual=False — the previous behavior (the insertion of anything)."""
    result, _ = await _run_one(
        _OneTenderAdapter(_n(status="closed"), only_actual=False)
    )

    assert (result.inserted, result.skipped_not_actual) == (1, 0)


def test_jnportal_keeps_insert_only_actual_by_measurement():
    """The measurement of 30.09: 0 of 3667 insertions of jnportal over 30 days were non-current — the default policy (True) cuts nothing alive."""
    from src.parsers.jnportal.adapter import JnportalAdapter

    assert JnportalAdapter.insert_only_actual is True
    assert JnportalAdapter.initial_lookback == timedelta(0)
