"""Unit tests of IngestWorker (T1, the closure of the entry of the pipeline C-01)."""

from __future__ import annotations

import logging
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

import httpx
import pytest

from src.parsers.base import NormalizedTender, RawTender, SourceAdapter
from src.workers.ingest import (
    IngestWorker,
    _degrade_interval,
    _fingerprint_fields,
    _normalized_to_fields,
    _quantize_money,
    compute_raw_hash,
)

FIXED_NOW = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
#  Fakes
# --------------------------------------------------------------------------- #
class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    """A session that returns the result by the type of SQL (and not by the position)."""

    def __init__(
        self,
        *,
        raw_lookup=None,
        norm_lookup=None,
        raw_insert_id="raw-1",
        norm_insert_id="norm-1",
        enriched_uids=None,
        fail_streak=None,
    ):
        self.raw_lookup = raw_lookup
        self.norm_lookup = norm_lookup
        # The answer to the reconciliation of pending_enrich with the DB (_RAW_ENRICHED_UIDS_SQL): which
        # uid already lie in tenders_raw WITH the key `_enrich`.
        self.enriched_uids = set(enriched_uids or ())
        # The answer to `UPDATE sources ... RETURNING error_count, polling_interval_sec` — the length of the series
        # of failures IN A ROW. None → the row did not come back (the worker must count it
        # as a reason to alert, rather than to stay silent).
        self.fail_streak = fail_streak
        self.raw_insert_id = raw_insert_id
        self.norm_insert_id = norm_insert_id
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        # The reconciliation of the set of pending_enrich — BEFORE the common branch of tenders_raw:
        # it has a different form of the row (source_uid, not id/raw_hash).
        if "jsonb_exists(raw_payload" in sql:
            asked = set(map(str, (params or {}).get("uids") or ()))
            return _FakeResult(
                [
                    SimpleNamespace(source_uid=uid)
                    for uid in sorted(self.enriched_uids & asked)
                ]
            )
        if "FROM tenders_raw" in sql and "SELECT" in sql:
            return _FakeResult([self.raw_lookup] if self.raw_lookup else [])
        if "UPDATE sources" in sql and "RETURNING error_count" in sql:
            return _FakeResult(
                []
                if self.fail_streak is None
                else [
                    SimpleNamespace(
                        error_count=self.fail_streak, polling_interval_sec=1800
                    )
                ]
            )
        if "INSERT INTO tenders_raw" in sql:
            return _FakeResult([SimpleNamespace(id=self.raw_insert_id)])
        if "FROM tenders_normalized" in sql and "SELECT" in sql:
            return _FakeResult([self.norm_lookup] if self.norm_lookup else [])
        if "INSERT INTO tenders_normalized" in sql:
            return _FakeResult([SimpleNamespace(id=self.norm_insert_id)])
        return _FakeResult([])

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    def ran(self, needle: str) -> bool:
        return any(needle in sql for sql, _ in self.calls)

    def params_for(self, needle: str) -> dict:
        for sql, params in self.calls:
            if needle in sql:
                return params
        raise AssertionError(f"SQL with {needle!r} was not executed")


def _stub_sources_row(session, row):
    """Substitutes the answer to `FROM sources` (``row=None`` → there is no row)."""
    orig_execute = session.execute

    async def execute(statement, params=None):
        sql = str(statement)
        if "FROM sources" in sql:
            session.calls.append((sql, params or {}))
            return _FakeResult([row] if row is not None else [])
        return await orig_execute(statement, params)

    session.execute = execute  # type: ignore[assignment]


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


def _norm(uid="42", *, title="Nabavka opreme", amount=117000.0, status="active"):
    return NormalizedTender(
        source_key="fake",
        source_uid=uid,
        country_code="RS",
        source_url="https://example/tender/42",
        title=title,
        title_normalized="nabavka opreme",
        description="opis",
        description_normalized="opis",
        customer_name="Grad",
        amount_original=amount,
        currency_original="RSD",
        amount_eur=round(amount / 117.0, 2),
        is_amount_estimated=False,
        classifications=[{"system_code": "eu_cpv", "code": "30000000"}],
        regions=["RS-00"],
        status=status,
    )


class _FakeAdapter(SourceAdapter):
    """An adapter without the network: gives the preset RawTender, normalize by the map."""

    source_key = "fake"
    source_name = "Fake"
    base_url = "https://example"
    country_codes = ["RS"]
    request_rate = 1.0
    default_polling_interval = 900
    supported_languages = ["sr-Latn"]

    def __init__(self, raws, *, normalized_by_uid=None, broken_uids=()):
        self._raws = raws
        self._norm = normalized_by_uid or {}
        self._broken = set(broken_uids)

    async def fetch_batch(self, since):
        for raw in self._raws:
            yield raw

    def build_source_uid(self, raw):
        return str(raw.payload.get("id", ""))

    def normalize(self, raw):
        uid = self.build_source_uid(raw)
        if uid in self._broken:
            raise ValueError(f"broken tender {uid}")
        return self._norm.get(uid) or _norm(uid)


def _raw(uid="42", **extra):
    payload = {"id": uid, "naziv": "Nabavka opreme", **extra}
    return RawTender(source_key="fake", payload=payload, country_code="RS")


def _make_worker(session, adapter, **kw):
    return IngestWorker(
        session_factory=_session_factory(session),
        source_key="fake",
        adapter_factory=lambda key: adapter,
        clock=lambda: FIXED_NOW,
        **kw,
    )


# --------------------------------------------------------------------------- #
#  Pure functions
# --------------------------------------------------------------------------- #
def test_raw_hash_stable_and_order_independent():
    a = compute_raw_hash({"x": 1, "y": "2"})
    b = compute_raw_hash({"y": "2", "x": 1})
    assert a == b
    assert a != compute_raw_hash({"x": 1, "y": "3"})


# --------------------------------------------------------------------------- #
#  INSERT of a new one
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_new_tender_inserts_raw_and_normalized_with_first_seen():
    raw = _raw("42")
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    worker = _make_worker(session, _FakeAdapter([raw]))

    result = await worker._ingest(_FakeAdapter([raw]), "fake", since=None)

    assert (result.inserted, result.updated, result.touched, result.skipped) == (
        1,
        0,
        0,
        0,
    )
    assert session.ran("INSERT INTO tenders_raw")
    norm_params = session.params_for("INSERT INTO tenders_normalized")
    assert norm_params["now"] == FIXED_NOW
    assert norm_params["source_uid"] == "42"
    assert norm_params["tenders_raw_id"] == "raw-1"
    assert session.commits == 1


# --------------------------------------------------------------------------- #
#  Skip without changes (raw_hash matched — notify is silent)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unchanged_payload_skips_without_touching_normalized():
    raw = _raw("42")
    same_hash = compute_raw_hash(raw.payload)
    session = _FakeSession(
        raw_lookup=SimpleNamespace(id="raw-1", raw_hash=same_hash),
    )
    worker = _make_worker(session, _FakeAdapter([raw]))

    result = await worker._ingest(_FakeAdapter([raw]), "fake", since=None)

    assert result.skipped == 1
    assert result.inserted == result.updated == result.touched == 0
    assert not session.ran("tenders_normalized")
    assert not session.ran("UPDATE tenders_raw")
    assert not session.ran("INSERT INTO tenders_raw")


# --------------------------------------------------------------------------- #
#  UPDATE of the content (last_changed_at moves)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_content_change_updates_normalized_and_moves_last_changed():
    raw = _raw("42", rev="2")
    existing = SimpleNamespace(
        id="norm-1",
        source_url="https://example/tender/42",
        title="Nabavka opreme",
        title_normalized="nabavka opreme",
        description="opis",
        description_normalized="opis",
        customer_name="Grad",
        customer_tax_id=None,
        customer_region_code=None,
        amount_original=117000.0,
        currency_original="RSD",
        amount_eur=1000.0,
        is_amount_estimated=False,
        published_at=None,
        deadline_at=None,
        opening_at=None,
        classifications=[{"system_code": "eu_cpv", "code": "30000000"}],
        procedure_type=None,
        contract_type=None,
        language=None,
        regions=["RS-00"],
        status="closed",
        documents=[],
    )
    session = _FakeSession(
        raw_lookup=SimpleNamespace(id="raw-1", raw_hash="old"),
        norm_lookup=existing,
    )
    worker = _make_worker(session, _FakeAdapter([raw]))

    result = await worker._ingest(_FakeAdapter([raw]), "fake", since=None)

    assert result.updated == 1
    assert session.ran("UPDATE tenders_raw")
    params = session.params_for("UPDATE tenders_normalized")
    assert params["now"] == FIXED_NOW
    assert params["id"] == "norm-1"
    assert any(
        "last_changed_at = :now" in sql
        for sql, _ in session.calls
        if "UPDATE tenders_normalized" in sql
    )


# --------------------------------------------------------------------------- #
#  Lightweight touch (the content is the same, only last_seen_at)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_same_content_new_payload_does_lightweight_touch():
    raw = _raw("42", served_at="now")
    norm = _norm("42")
    existing = SimpleNamespace(
        id="norm-1",
        source_url=norm.source_url,
        title=norm.title,
        title_normalized=norm.title_normalized,
        description=norm.description,
        description_normalized=norm.description_normalized,
        customer_name=norm.customer_name,
        customer_tax_id=norm.customer_tax_id,
        customer_region_code=norm.customer_region_code,
        amount_original=norm.amount_original,
        currency_original=norm.currency_original,
        amount_eur=norm.amount_eur,
        is_amount_estimated=norm.is_amount_estimated,
        published_at=norm.published_at,
        deadline_at=norm.deadline_at,
        opening_at=norm.opening_at,
        classifications=norm.classifications,
        procedure_type=norm.procedure_type,
        contract_type=norm.contract_type,
        language=norm.language,
        regions=norm.regions,
        status=norm.status,
        documents=norm.documents,
    )
    session = _FakeSession(
        raw_lookup=SimpleNamespace(id="raw-1", raw_hash="old"),
        norm_lookup=existing,
    )
    adapter = _FakeAdapter([raw], normalized_by_uid={"42": norm})
    worker = _make_worker(session, adapter)

    result = await worker._ingest(adapter, "fake", since=None)

    assert result.touched == 1
    assert result.updated == 0
    touch_sqls = [sql for sql, _ in session.calls if "UPDATE tenders_normalized" in sql]
    assert touch_sqls and all("last_changed_at" not in s for s in touch_sqls)


# --------------------------------------------------------------------------- #
#  A broken tender does not fall the pass
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_broken_tender_does_not_abort_batch():
    raws = [_raw("1"), _raw("2"), _raw("3")]
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    adapter = _FakeAdapter(raws, broken_uids={"2"})
    worker = _make_worker(session, adapter)

    result = await worker._ingest(adapter, "fake", since=None)

    assert result.inserted == 2
    assert result.errors == 1
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_empty_source_uid_is_skipped_not_error():
    raw = RawTender(source_key="fake", payload={}, country_code="RS")
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    adapter = _FakeAdapter([raw])
    worker = _make_worker(session, adapter)

    result = await worker._ingest(adapter, "fake", since=None)

    assert result.skipped == 1
    assert result.errors == 0
    assert not session.ran("INSERT INTO tenders_raw")


# --------------------------------------------------------------------------- #
#  fetch_batch falls entirely → the pass is marked failed
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_batch_network_error_marks_source_failed():
    class _BoomAdapter(_FakeAdapter):
        async def fetch_batch(self, since):
            raise ConnectionError("network down")
            yield  # pragma: no cover — it makes the function an async generator

    session = _FakeSession()
    adapter = _BoomAdapter([])
    worker = _make_worker(session, adapter)

    result = await worker._ingest(adapter, "fake", since=None)

    assert result.failed is True
    assert result.inserted == 0


# --------------------------------------------------------------------------- #
#  fail_reason: the reason of the broken pass for the alert (08.09.2026, jnportal 429)
# --------------------------------------------------------------------------- #
class _FakeHealth:
    """Records the calls of heartbeat_fail/alert (without the real network)."""

    def __init__(self):
        self.heartbeat_fail_calls: list[str | None] = []
        self.alert_calls: list[str] = []

    async def heartbeat_ok(self) -> None:
        pass

    async def heartbeat_fail(self, reason: str | None = None) -> None:
        self.heartbeat_fail_calls.append(reason)

    async def alert(self, text: str) -> None:
        self.alert_calls.append(text)


@pytest.mark.asyncio
async def test_ingest_http_status_error_429_sets_fail_reason():
    class _RateLimitedAdapter(_FakeAdapter):
        async def fetch_batch(self, since):
            resp = SimpleNamespace(status_code=429)
            raise httpx.HTTPStatusError("rate limited", request=None, response=resp)
            yield  # pragma: no cover — it makes the function an async generator

    session = _FakeSession()
    adapter = _RateLimitedAdapter([])
    worker = _make_worker(session, adapter)

    result = await worker._ingest(adapter, "fake", since=None)

    assert result.failed is True
    assert result.fail_reason == "HTTP 429"


@pytest.mark.asyncio
async def test_ingest_network_error_sets_fail_reason_to_exception_name():
    session = _FakeSession()

    class _BoomAdapter(_FakeAdapter):
        async def fetch_batch(self, since):
            raise ConnectionError("network down")
            yield  # pragma: no cover

    adapter = _BoomAdapter([])
    worker = _make_worker(session, adapter)

    result = await worker._ingest(adapter, "fake", since=None)

    assert result.fail_reason == "ConnectionError"


@pytest.mark.asyncio
async def test_ingest_source_alert_text_names_http_429_reason():
    """The regression #jnportal_429: the alert no longer stays silent about the reason of the failure."""

    class _RateLimitedAdapter(_FakeAdapter):
        async def fetch_batch(self, since):
            resp = SimpleNamespace(status_code=429)
            raise httpx.HTTPStatusError("rate limited", request=None, response=resp)
            yield  # pragma: no cover

    session = _FakeSession(fail_streak=2)  # a second failure in a row → the alert
    _stub_sources_row(
        session,
        SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=None,
            enabled=True,
            enabled_at=FIXED_NOW,
        ),
    )
    health = _FakeHealth()
    adapter = _RateLimitedAdapter([])
    worker = _make_worker(session, adapter, health=health)

    await worker.ingest_source("fake")

    assert len(health.alert_calls) == 1
    assert "HTTP 429" in health.alert_calls[0]
    assert "сорвался 2-й раз подряд (fetch_batch): HTTP 429" in health.alert_calls[0]
    assert len(health.heartbeat_fail_calls) == 1
    assert health.heartbeat_fail_calls[0] == "fake: fetch_batch failed: HTTP 429"
    # PII/secrets are not carried into the alert: neither the query parameters of the URL, nor the value of the
    # the AccessToken header.
    assert "?" not in health.alert_calls[0]
    # The reason is known (HTTP 429) → the tail “check the portal/AccessToken/the network”
    # is NOT printed: it is about other reasons and leads away from the real one (08.09.2026,
    # an hour of the diagnostics wasted with a 200-portal and a valid token).
    assert (
        health.alert_calls[0]
        == "⚠️ TenderSRB ingest: источник «fake» сорвался 2-й раз подряд "
        "(fetch_batch): HTTP 429. Подробности — в логе контейнера ingest."
    )
    assert "проверь портал/AccessToken/сеть" not in health.alert_calls[0]


@pytest.mark.asyncio
async def test_ingest_source_alert_keeps_generic_tail_when_reason_unknown():
    """The reason is unknown (fail_reason=None) — the old general advice remains."""

    session = _FakeSession(fail_streak=2)  # a second failure in a row → the alert
    _stub_sources_row(
        session,
        SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=None,
            enabled=True,
            enabled_at=FIXED_NOW,
        ),
    )
    health = _FakeHealth()
    adapter = _FakeAdapter([])
    worker = _make_worker(session, adapter, health=health)

    async def _fake_ingest(adapter, source_key, since):
        from src.workers.ingest import SourceResult

        return SourceResult(source_key, failed=True, fail_reason=None)

    worker._ingest = _fake_ingest  # type: ignore[method-assign]

    await worker.ingest_source("fake")

    assert len(health.alert_calls) == 1
    assert health.alert_calls[0] == (
        "⚠️ TenderSRB ingest: источник «fake» сорвался 2-й раз подряд "
        "(fetch_batch). Поток новых тендеров мог встать — проверь портал/"
        "AccessToken/сеть."
    )


# --------------------------------------------------------------------------- #
#  ingest_source: since from last_run_at, the mark of last_run_at after the pass
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ingest_source_marks_last_run_ok_on_success():
    raw = _raw("42")
    last_run = datetime(2026, 5, 29, tzinfo=timezone.utc)
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    orig_execute = session.execute

    async def execute(statement, params=None):
        sql = str(statement)
        if "FROM sources" in sql:
            session.calls.append((sql, params or {}))
            return _FakeResult(
                [
                    SimpleNamespace(
                        polling_interval_sec=1800, last_run_at=last_run, enabled=True
                    )
                ]
            )
        return await orig_execute(statement, params)

    session.execute = execute  # type: ignore[assignment]
    adapter = _FakeAdapter([raw])
    worker = _make_worker(session, adapter)

    interval = await worker.ingest_source("fake")

    assert interval == 1800
    assert session.ran("last_status = 'ok'")
    ok_params = session.params_for("last_status = 'ok'")
    assert ok_params["now"] == FIXED_NOW


@pytest.mark.asyncio
async def test_disabled_source_is_skipped():
    session = _FakeSession()
    _stub_sources_row(
        session,
        SimpleNamespace(polling_interval_sec=900, last_run_at=None, enabled=False),
    )
    adapter = _FakeAdapter([_raw("1")])
    worker = _make_worker(session, adapter)

    interval = await worker.ingest_source("fake")

    assert interval == 900
    assert not session.ran("INSERT INTO tenders_raw")
    assert not session.ran("last_status = 'ok'")


# --------------------------------------------------------------------------- #
#  fail-closed: there is no row in sources → we do NOT load (the VG-13 mine, W1)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_source_without_sources_row_is_not_ingested():
    """‼️ A source in the registry, but without a row in ``sources`` → _ingest is NOT called.

    ingest_source is also called directly (the key in the constructor, bypassing source_keys() over the table, SRC-0a): there is no row → enabled=None. While the check was `enabled is False`, None did not catch it and the worker went into _ingest = a write to the production DB + routing → extra deliveries. Mutation: return `if enabled is False` → the test turns red.
    """
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    _stub_sources_row(session, None)  # sources is empty — as for a sleeping adapter
    adapter = _FakeAdapter([_raw("1")])
    worker = _make_worker(session, adapter)

    ingested: list[str] = []

    async def _spy(adapter_, source_key, since):  # pragma: no cover — must not call
        ingested.append(source_key)
        raise AssertionError("_ingest was called for a source without a row in sources")

    worker._ingest = _spy  # type: ignore[assignment]

    interval = await worker.ingest_source("fake")

    assert ingested == []
    assert interval == 900  # adapter.default_polling_interval
    assert not session.ran("INSERT INTO tenders_raw")
    assert not session.ran("INSERT INTO tenders_normalized")
    assert not session.ran("last_status = 'ok'")


@pytest.mark.asyncio
async def test_enabled_source_still_ingests_after_fail_closed():
    """The regression of the production source: enabled=true is loaded as before (jnportal)."""
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    _stub_sources_row(
        session,
        SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=None,
            enabled=True,
            enabled_at=FIXED_NOW,
        ),
    )
    adapter = _FakeAdapter([_raw("1")])
    worker = _make_worker(session, adapter)

    interval = await worker.ingest_source("fake")

    assert interval == 1800
    assert session.ran("INSERT INTO tenders_raw")
    assert session.ran("last_status = 'ok'")


# --------------------------------------------------------------------------- #
#  The degradation of the polling interval under a steady failure (VG-03 03.2)
# --------------------------------------------------------------------------- #
def test_degrade_interval_below_threshold_unchanged():
    assert _degrade_interval(1800, 0) == 1800
    assert _degrade_interval(1800, 2) == 1800


def test_degrade_interval_at_threshold_doubles():
    assert _degrade_interval(1800, 3) == 3600


def test_degrade_interval_grows_exponentially_then_caps_multiplier():
    base = 1800
    assert _degrade_interval(base, 4) == base * 4
    assert _degrade_interval(base, 5) == base * 8
    # the multiplier is limited by the ceiling (_DEGRADE_MAX_MULTIPLIER=8) — it does not grow further
    assert _degrade_interval(base, 6) == base * 8
    assert _degrade_interval(base, 50) == base * 8


def test_degrade_interval_caps_at_absolute_max():
    huge_base = 6 * 3600  # already at the ceiling before the multiplier
    assert _degrade_interval(huge_base, 10) == 6 * 3600


@pytest.mark.asyncio
async def test_ingest_source_degrades_interval_on_repeated_errors():
    """error_count >= the threshold → the interval is thinned (we do not hammer the dead portal)."""
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    orig_execute = session.execute

    async def execute(statement, params=None):
        sql = str(statement)
        if "FROM sources" in sql:
            session.calls.append((sql, params or {}))
            return _FakeResult(
                [
                    SimpleNamespace(
                        polling_interval_sec=1800,
                        last_run_at=None,
                        enabled=True,
                        error_count=5,
                    )
                ]
            )
        return await orig_execute(statement, params)

    session.execute = execute  # type: ignore[assignment]
    adapter = _FakeAdapter([_raw("1")])
    worker = _make_worker(session, adapter)

    interval = await worker.ingest_source("fake")

    assert interval == 1800 * 8  # error_count=5 → the multiplier is at the ceiling


@pytest.mark.asyncio
async def test_ingest_source_row_without_error_count_is_not_degraded():
    """A sources row without the attribute error_count (regression) → the interval as it is."""
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    orig_execute = session.execute

    async def execute(statement, params=None):
        sql = str(statement)
        if "FROM sources" in sql:
            session.calls.append((sql, params or {}))
            return _FakeResult(
                [
                    SimpleNamespace(
                        polling_interval_sec=1800,
                        last_run_at=None,
                        enabled=True,
                        enabled_at=FIXED_NOW,
                    )
                ]
            )
        return await orig_execute(statement, params)

    session.execute = execute  # type: ignore[assignment]
    adapter = _FakeAdapter([_raw("1")])
    worker = _make_worker(session, adapter)

    interval = await worker.ingest_source("fake")

    assert interval == 1800


# --------------------------------------------------------------------------- #
#  The quantisation of money in the raw-SQL path of the ingest
# --------------------------------------------------------------------------- #
def test_quantize_money_dirty_float_to_two_places():
    assert _quantize_money(4500000 / 117) == Decimal("38461.54")


def test_quantize_money_real_db_tail_case():
    assert _quantize_money(98030.33999999999650754) == Decimal("98030.34")


def test_quantize_money_none_returns_none():
    assert _quantize_money(None) is None


def test_quantize_money_round_half_up():
    assert _quantize_money(Decimal("0.535")) == Decimal("0.54")
    assert _quantize_money(Decimal("0.545")) == Decimal("0.55")


def test_quantize_money_accepts_decimal_int_str():
    assert _quantize_money(Decimal("38461.54")) == Decimal("38461.54")
    assert _quantize_money(100) == Decimal("100.00")
    assert _quantize_money("38461.5400000001") == Decimal("38461.54")


def test_quantize_money_exactly_two_decimal_places():
    assert _quantize_money(4500000 / 117).as_tuple().exponent == -2
    assert _quantize_money(100).as_tuple().exponent == -2


def test_normalized_to_fields_quantizes_both_money_columns():
    n = replace(
        _norm("42"),
        amount_original=4500000 / 117,
        amount_eur=98030.33999999999650754,
    )
    fields = _normalized_to_fields(n)
    assert fields["amount_eur"] == Decimal("98030.34")
    assert fields["amount_original"] == Decimal("38461.54")
    assert fields["amount_eur"].as_tuple().exponent == -2
    assert fields["amount_original"].as_tuple().exponent == -2


def test_normalized_to_fields_none_money_stays_none():
    n = replace(_norm("42"), amount_original=None, amount_eur=None)
    fields = _normalized_to_fields(n)
    assert fields["amount_original"] is None
    assert fields["amount_eur"] is None


def test_fingerprint_stable_across_passes_no_false_update():
    n = replace(_norm("42"), amount_original=4500000 / 117, amount_eur=98030.339999)
    fp1 = _fingerprint_fields(_normalized_to_fields(n))
    fp2 = _fingerprint_fields(_normalized_to_fields(n))
    assert fp1 == fp2


def _doc_entry(**over):
    entry = {
        "name": "Tehnicka.pdf",
        "ext": ".pdf",
        "size": 1234,
        "sha256": "ab" * 32,
        "doc_status": "Активан",
        "version": "2026-07-14T02:04",
        "extracted": "ok",
        "chars": 900,
    }
    entry.update(over)
    return entry


def test_documents_fingerprint_ignores_extraction_status():
    """Extraction status with the same bytes (sha256/size) does not change the fingerprint.

    Otherwise a failure of extraction ok↔error gave a content UPDATE (last_changed_at, NOTIFY,
    re-matching, AI cache reset) for an unchanged file. A network download failure
    (``size``/``sha256`` → None) is NOT covered by this test — see TESTING, "What is NOT verified".
    """
    ok = replace(_norm("42"), documents=[_doc_entry()])
    err = replace(_norm("42"), documents=[_doc_entry(extracted="error", chars=0)])
    assert _fingerprint_fields(_normalized_to_fields(ok)) == _fingerprint_fields(
        _normalized_to_fields(err)
    )


def test_documents_fingerprint_changes_with_sha256():
    """A different file under the same name is a content change (UPDATE)."""
    a = replace(_norm("42"), documents=[_doc_entry()])
    b = replace(_norm("42"), documents=[_doc_entry(sha256="cd" * 32)])
    assert _fingerprint_fields(_normalized_to_fields(a)) != _fingerprint_fields(
        _normalized_to_fields(b)
    )


def _fp_with_documents(documents):
    return _fingerprint_fields(
        _normalized_to_fields(replace(_norm("42"), documents=documents))
    )


def test_documents_fingerprint_bih_url_change_changes_fingerprint():
    """bih_enabavke: document ``{title,url,kind}`` — a change of ``url`` is visible to the fingerprint.

    The whitelist ``name/size/sha256/...`` dropped all three keys of this source (1987 rows in prod) →
    a change of the document address gave no content UPDATE.
    """
    a = _fp_with_documents(
        [{"title": "Specifikacija", "url": "https://x/a.pdf", "kind": "spec"}]
    )
    b = _fp_with_documents(
        [{"title": "Specifikacija", "url": "https://x/b.pdf", "kind": "spec"}]
    )
    assert a != b


def test_documents_fingerprint_ukck_url_change_changes_fingerprint():
    """ukck_nabavke: document ``{name,url,type,size}`` — a change of ``url`` is visible to the fingerprint.

    The whitelist dropped ``url`` — a change of the document address gave no UPDATE.
    """
    a = _fp_with_documents(
        [{"name": "Ponuda.pdf", "url": "https://x/a", "type": "pdf", "size": 10}]
    )
    b = _fp_with_documents(
        [{"name": "Ponuda.pdf", "url": "https://x/b", "type": "pdf", "size": 10}]
    )
    assert a != b


def test_documents_fingerprint_ignores_only_volatile_extracted_and_chars():
    """jnportal: a difference only in ``extracted``/``chars`` — the same fingerprint.

    The same file (identical sha256/size): the extraction result is not content.
    """
    ok = _fp_with_documents([_doc_entry(extracted="ok", chars=900)])
    err = _fp_with_documents([_doc_entry(extracted="error", chars=0)])
    assert ok == err


def test_documents_fingerprint_sha256_still_changes_with_volatile_fields_equal():
    """The same jnportal document: when sha256 changes the fingerprint is different (even with the same extracted/chars)."""
    a = _fp_with_documents([_doc_entry(extracted="ok", chars=900)])
    b = _fp_with_documents([_doc_entry(sha256="cd" * 32, extracted="ok", chars=900)])
    assert a != b


# --------------------------------------------------------------------------- #
#  contract_type: passed through into fields / the fingerprint / INSERT / UPDATE (NEXT №1)
# --------------------------------------------------------------------------- #
def test_normalized_to_fields_carries_contract_type():
    n = replace(_norm("42"), contract_type="goods")
    assert _normalized_to_fields(n)["contract_type"] == "goods"


def test_contract_type_is_part_of_fingerprint():
    """A change of contract_type changes the fingerprint → it is a content UPDATE, not a touch."""
    base = _norm("42")
    fp_goods = _fingerprint_fields(
        _normalized_to_fields(replace(base, contract_type="goods"))
    )
    fp_services = _fingerprint_fields(
        _normalized_to_fields(replace(base, contract_type="services"))
    )
    fp_none = _fingerprint_fields(
        _normalized_to_fields(replace(base, contract_type=None))
    )
    assert fp_goods != fp_services
    assert fp_goods != fp_none


@pytest.mark.asyncio
async def test_insert_carries_contract_type_param():
    raw = _raw("42")
    norm = replace(_norm("42"), contract_type="works")
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    adapter = _FakeAdapter([raw], normalized_by_uid={"42": norm})
    worker = _make_worker(session, adapter)

    await worker._ingest(adapter, "fake", since=None)

    params = session.params_for("INSERT INTO tenders_normalized")
    assert params["contract_type"] == "works"


@pytest.mark.asyncio
async def test_contract_type_change_is_content_update_not_touch():
    """A difference only in contract_type → a content UPDATE (not a lightweight touch)."""
    raw = _raw("42", rev="2")
    norm = replace(_norm("42"), contract_type="services")
    existing = SimpleNamespace(
        id="norm-1",
        source_url=norm.source_url,
        title=norm.title,
        title_normalized=norm.title_normalized,
        description=norm.description,
        description_normalized=norm.description_normalized,
        customer_name=norm.customer_name,
        customer_tax_id=norm.customer_tax_id,
        customer_region_code=norm.customer_region_code,
        amount_original=norm.amount_original,
        currency_original=norm.currency_original,
        amount_eur=norm.amount_eur,
        is_amount_estimated=norm.is_amount_estimated,
        published_at=norm.published_at,
        deadline_at=norm.deadline_at,
        opening_at=norm.opening_at,
        classifications=norm.classifications,
        procedure_type=norm.procedure_type,
        contract_type="goods",  # the difference → a content UPDATE
        language=norm.language,
        regions=norm.regions,
        status=norm.status,
        documents=norm.documents,
    )
    session = _FakeSession(
        raw_lookup=SimpleNamespace(id="raw-1", raw_hash="old"),
        norm_lookup=existing,
    )
    adapter = _FakeAdapter([raw], normalized_by_uid={"42": norm})
    worker = _make_worker(session, adapter)

    result = await worker._ingest(adapter, "fake", since=None)

    assert result.updated == 1
    assert result.touched == 0
    params = session.params_for("UPDATE tenders_normalized")
    assert params["contract_type"] == "services"


# --------------------------------------------------------------------------- #
#  The quantisation: the boundaries (down/up, negatives, float against Decimal)
# --------------------------------------------------------------------------- #
def test_quantize_money_rounds_down_below_half():
    assert _quantize_money("0.534") == Decimal("0.53")
    assert _quantize_money("38461.5449") == Decimal("38461.54")


def test_quantize_money_negative_half_rounds_away_from_zero():
    """ROUND_HALF_UP on negatives = away from zero (not “up” toward zero)."""
    assert _quantize_money(Decimal("-0.545")) == Decimal("-0.55")
    assert _quantize_money(Decimal("-0.544")) == Decimal("-0.54")
    assert _quantize_money(Decimal("-0.005")) == Decimal("-0.01")


def test_quantize_money_float_goes_through_str_not_binary():
    """float → ``Decimal(str(value))``, and not ``Decimal(float)``.

    0.145 in binary is 0.1449999…, and a direct ``Decimal(0.145)`` would give 0.14. The pin to the conversion via str: we count what a person sees — 0.15.
    """
    assert _quantize_money(0.145) == Decimal("0.15")
    assert Decimal(0.145).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == Decimal(
        "0.14"
    )


def test_quantize_money_returns_decimal_not_float():
    """The type of the bind: Decimal goes into the raw SQL, otherwise the float “tail” would travel into the DB."""
    for value in (100, "100.004", 100.004, Decimal("100.004")):
        assert isinstance(_quantize_money(value), Decimal)


# --------------------------------------------------------------------------- #
#  The bind conversions: what really goes as parameters into the text()-SQL
# --------------------------------------------------------------------------- #
def test_normalized_to_fields_money_is_decimal_for_any_input_type():
    n = replace(_norm("42"), amount_original=117000, amount_eur=Decimal("1000.005"))
    fields = _normalized_to_fields(n)
    assert fields["amount_original"] == Decimal("117000.00")
    assert fields["amount_eur"] == Decimal("1000.01")
    assert isinstance(fields["amount_original"], Decimal)
    assert isinstance(fields["amount_eur"], Decimal)


def test_normalized_to_fields_passes_non_money_through_unchanged():
    """ONLY the money fields are quantised — the others go as they are."""
    n = _norm("42")
    fields = _normalized_to_fields(n)
    assert fields["currency_original"] == "RSD"
    assert fields["title"] == n.title
    assert fields["classifications"] == n.classifications
    assert fields["regions"] == n.regions
    assert fields["is_amount_estimated"] is False


@pytest.mark.asyncio
async def test_insert_binds_money_as_quantized_decimal():
    """‼️ The ORM type QuantizedNumeric does not take part in the raw SQL — the pin on the Python bind."""
    raw = _raw("42")
    norm = replace(
        _norm("42"), amount_original=4500000 / 117, amount_eur=98030.33999999999650754
    )
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    adapter = _FakeAdapter([raw], normalized_by_uid={"42": norm})
    worker = _make_worker(session, adapter)

    await worker._ingest(adapter, "fake", since=None)

    params = session.params_for("INSERT INTO tenders_normalized")
    assert params["amount_original"] == Decimal("38461.54")
    assert params["amount_eur"] == Decimal("98030.34")
    assert isinstance(params["amount_original"], Decimal)
    assert params["amount_original"].as_tuple().exponent == -2
    assert params["amount_eur"].as_tuple().exponent == -2


@pytest.mark.asyncio
async def test_insert_binds_jsonb_fields_as_strings():
    """``CAST(:classifications AS jsonb)`` requires a string, not a python list."""
    raw = _raw("42")
    norm = replace(
        _norm("42"),
        classifications=[{"system_code": "eu_cpv", "code": "30000000"}],
        documents=[{"url": "https://example/doc.pdf"}],
    )
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    adapter = _FakeAdapter([raw], normalized_by_uid={"42": norm})
    worker = _make_worker(session, adapter)

    await worker._ingest(adapter, "fake", since=None)

    params = session.params_for("INSERT INTO tenders_normalized")
    assert params["classifications"] == json.dumps(
        norm.classifications, sort_keys=True, ensure_ascii=False
    )
    assert params["documents"] == json.dumps(
        norm.documents, sort_keys=True, ensure_ascii=False
    )
    # regions — text[], it is bound as a list (there is no jsonb cast in the SQL).
    assert params["regions"] == ["RS-00"]


@pytest.mark.asyncio
async def test_update_binds_money_and_jsonb_same_as_insert():
    """The UPDATE path binds the same way as the INSERT (the same scalar/json fields)."""
    raw = _raw("42")
    norm = replace(_norm("42"), amount_eur=98030.33999999999650754)
    existing = SimpleNamespace(
        id="norm-1", **_normalized_to_fields(_norm("42", title="Staro"))
    )
    session = _FakeSession(
        raw_lookup=SimpleNamespace(id="raw-1", raw_hash="old"),
        norm_lookup=existing,
    )
    adapter = _FakeAdapter([raw], normalized_by_uid={"42": norm})
    worker = _make_worker(session, adapter)

    result = await worker._ingest(adapter, "fake", since=None)

    assert result.updated == 1
    params = session.params_for("UPDATE tenders_normalized")
    assert params["amount_eur"] == Decimal("98030.34")
    assert isinstance(params["classifications"], str)
    assert isinstance(params["documents"], str)


# --------------------------------------------------------------------------- #
#  Step 3: a single network failure vs. a series of failures in a row (the production fact of 22.09.2026)
# --------------------------------------------------------------------------- #
class _TimingOutAdapter(_FakeAdapter):
    """The pass breaks with a ConnectTimeout — exactly what happened in production at 2026-09-22T02:25:28Z (and was healed by the next pass at 02:55)."""

    async def fetch_batch(self, since):
        raise httpx.ConnectTimeout("timed out")
        yield  # pragma: no cover


def _worker_for_failed_pass(fail_streak, health):
    session = _FakeSession(fail_streak=fail_streak)
    _stub_sources_row(
        session,
        SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=None,
            enabled=True,
            enabled_at=FIXED_NOW,
        ),
    )
    return session, _make_worker(session, _TimingOutAdapter([]), health=health)


@pytest.mark.asyncio
async def test_single_network_timeout_does_not_ring_telegram(caplog):
    """‼️ THE PRODUCTION FACT: over 72 passes one ConnectTimeout, the next pass is routine, the inflow did not suffer — and yet the alert went to the owner in Telegram and was read as “the errors are climbing again”.

    A single failure does not ring Telegram, but it does not disappear either: the heartbeat_fail goes out, a WARNING is written, the counter of the day grows.
    """
    health = _FakeHealth()
    _session, worker = _worker_for_failed_pass(1, health)

    with caplog.at_level(logging.WARNING, logger="src.workers.ingest"):
        await worker.ingest_source("fake")

    assert health.alert_calls == []  # Telegram stays silent
    assert len(health.heartbeat_fail_calls) == 1  # but the signal outward DOES go
    assert any("единичный срыв прохода" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_second_failure_in_a_row_rings_telegram_and_names_the_streak():
    """From the second failure IN A ROW the alert goes out and names the length of the series as a number."""
    health = _FakeHealth()
    _session, worker = _worker_for_failed_pass(2, health)

    await worker.ingest_source("fake")

    assert len(health.alert_calls) == 1
    assert health.alert_calls[0] == (
        "⚠️ TenderSRB ingest: источник «fake» сорвался 2-й раз подряд "
        "(fetch_batch): ConnectTimeout. Подробности — в логе контейнера ingest."
    )


@pytest.mark.asyncio
async def test_holding_failure_rings_every_pass_not_once():
    """‼️ The difference between “to distinguish” and “to hide” (owner 20.09.2026). The old anti-bounce stayed silent BY TIME, and a problem that persisted stopped ringing. Here, on the seventh failure in a row, the alert goes out just as on the second."""
    health = _FakeHealth()
    _session, worker = _worker_for_failed_pass(7, health)

    await worker.ingest_source("fake")

    assert len(health.alert_calls) == 1
    assert "сорвался 7-й раз подряд" in health.alert_calls[0]


@pytest.mark.asyncio
async def test_missing_sources_row_still_alerts():
    """The fail-open fallback: the sources row was not updated (RETURNING is empty) — there is nowhere to learn the length of the series, and then the alert must go out, rather than stay silent."""
    health = _FakeHealth()
    _session, worker = _worker_for_failed_pass(None, health)

    await worker.ingest_source("fake")

    assert len(health.alert_calls) == 1


@pytest.mark.asyncio
async def test_failed_pass_is_recorded_in_daily_totals():
    """A single failure does not ring — so it must be in the daily summary."""
    health = _FakeHealth()
    session = _FakeSession(fail_streak=1)
    _stub_sources_row(
        session,
        SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=None,
            enabled=True,
            enabled_at=FIXED_NOW,
        ),
    )
    recorded: dict = {}

    class _SpyMetrics:
        async def record_pass(self, day, **kwargs):
            recorded.update(kwargs)
            recorded["day"] = day

    worker = _make_worker(session, _TimingOutAdapter([]), health=health)
    worker._flow_metrics = _SpyMetrics()  # type: ignore[assignment]

    await worker.ingest_source("fake")

    assert recorded["failed"] is True
    assert recorded["fail_streak"] == 1


# --------------------------------------------------------------------------- #
#  repoll-open: a failure of the poll of the cards = a broken pass (the 429 is not swallowed)
# --------------------------------------------------------------------------- #
def _repoll_429() -> httpx.HTTPStatusError:
    resp = SimpleNamespace(status_code=429)
    return httpx.HTTPStatusError("rate limited", request=None, response=resp)


async def _repoll_429_open(*_a, **_k):
    raise _repoll_429()


def _repoll_worker(monkeypatch, session, health):
    """A worker with repoll on, where the poll falls with a 429 (the SQL of the poll is not here)."""
    monkeypatch.setenv("INGEST_JNPORTAL_REPOLL", "1")

    class _RepollAdapter(_FakeAdapter):
        async def repoll_cards(self, tender_ids):
            raise _repoll_429()
            yield  # pragma: no cover — it makes the function an async generator

    _stub_sources_row(
        session,
        SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=None,
            enabled=True,
            enabled_at=FIXED_NOW,
            error_count=2,
        ),
    )
    adapter = _RepollAdapter([])
    worker = _make_worker(session, adapter, health=health)
    worker._repoll_open = _repoll_429_open  # type: ignore[method-assign]
    return worker


@pytest.mark.asyncio
async def test_repoll_429_is_failed_pass_with_alert_and_no_ok_mark(monkeypatch):
    """A 429 on the poll → error_count grows, heartbeat_fail and the alert with the stage repoll."""
    session = _FakeSession(fail_streak=2)
    health = _FakeHealth()
    worker = _repoll_worker(monkeypatch, session, health)

    await worker.ingest_source("fake")

    assert health.heartbeat_fail_calls == ["fake: repoll failed: HTTP 429"]
    assert len(health.alert_calls) == 1
    assert "сорвался 2-й раз подряд (repoll): HTTP 429" in health.alert_calls[0]
    assert not any("last_status = 'ok'" in sql for sql, _ in session.calls)
    assert any("RETURNING error_count" in sql for sql, _ in session.calls)


@pytest.mark.asyncio
async def test_repoll_429_degrades_next_pass_interval(monkeypatch):
    """The next pass is not earlier than the degraded interval — the same _degrade_interval.

    The start of the pass: error_count=2 → 1800. The poll fell with a 429 → the counter became 3 → the interval that goes into the scheduler’s sleep is already 3600 (not 1800).
    """
    session = _FakeSession(fail_streak=3)
    health = _FakeHealth()
    worker = _repoll_worker(monkeypatch, session, health)

    interval = await worker.ingest_source("fake")

    assert interval == _degrade_interval(1800, 3) == 3600
    assert interval > _degrade_interval(1800, 2)


@pytest.mark.asyncio
async def test_repoll_success_still_marks_source_ok(monkeypatch):
    """A poll without a failure — the pass is clean: last_status='ok', error_count is reset."""
    monkeypatch.setenv("INGEST_JNPORTAL_REPOLL", "1")

    class _RepollAdapter(_FakeAdapter):
        async def repoll_cards(self, tender_ids):
            return
            yield  # pragma: no cover

    session = _FakeSession()
    health = _FakeHealth()
    _stub_sources_row(
        session,
        SimpleNamespace(
            polling_interval_sec=1800,
            last_run_at=None,
            enabled=True,
            enabled_at=FIXED_NOW,
            error_count=2,
        ),
    )
    worker = _make_worker(session, _RepollAdapter([]), health=health)

    async def _ok_open(*_a, **_k):
        return 0, 0

    worker._repoll_open = _ok_open  # type: ignore[method-assign]

    interval = await worker.ingest_source("fake")

    assert health.heartbeat_fail_calls == []
    assert health.alert_calls == []
    assert any("last_status = 'ok'" in sql for sql, _ in session.calls)
    assert interval == 1800
