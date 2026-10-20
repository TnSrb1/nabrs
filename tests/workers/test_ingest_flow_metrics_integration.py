"""The observability integration in IngestWorker (Test C — the budget per card in a REAL pass, not only on the pure function; tmp/TASK_flow_observability.md).

It uses the same fakes as ``tests/workers/test_ingest.py`` (``_FakeSession``/``_make_worker``/``_FakeAdapter``/``_norm``/``_raw`` — the same trick as ``test_ingest_pending_state.py``, a second fake httpx client is not set up). Each fake ``fetch_batch`` emits REAL INFO lines via ``logging.getLogger("httpx")`` — exactly as httpx does in production — to prove that ``IngestWorker._ingest`` counts over the REAL log, and not over its own opinion of the number of requests.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

from src.parsers.base import SourceAdapter
from tests.workers.test_ingest import (
    _FakeSession,
    _make_worker,
    _norm,
    _raw,
)

_httpx_logger = logging.getLogger("httpx")


def _log_one(url: str, status: int = 200) -> None:
    _httpx_logger.info(
        'HTTP Request: %s %s "%s %d %s"',
        "GET",
        httpx.URL(url),
        "HTTP/1.1",
        status,
        "OK" if status < 400 else "ERR",
    )


def _log_requests(n: int, *, status: int = 200) -> None:
    for _ in range(n):
        _log_one("https://jnportal.src-ujn.example.invalid/api/TenderNotices", status)


_BASE = "https://jnportal.src-ujn.example.invalid"
# ‼️ The URLs — the same ones that the adapter really sends for the card (the fact of the production log
# 22.09.2026). Earlier the fake sent N TenderNotices and called it «the price of
# the card»: the price of the card and the request of the LIST PAGE are different things, and as of
# 22.09.2026 the worker distinguishes them (PASS_SCOPED_ENDPOINTS). A fake that sends
# a wrong URL would prove something other than what the name of the test promises.
_CARD_FIXED_URLS = (
    "/api/searchgrid/LotsTenderSpaceEO/get?skip=0&take=100",
    "/tender-eo/394171",
    "/api/searchgrid/TenderPlanItemsAssociated/get?skip=0&take=20",
    "/api/documentControl/get?tenderId=394171",
)


def _log_card(cost: int) -> None:
    """A card of the price ``cost``: 4 fixed requests + the rest as files."""
    for path in _CARD_FIXED_URLS[:cost]:
        _log_one(f"{_BASE}{path}")
    for i in range(max(cost - len(_CARD_FIXED_URLS), 0)):
        _log_one(f"{_BASE}/GetDocument.ashx?id={i}")


class _CostedAdapter(SourceAdapter):
    """Each tender is traversed by the given number of requests (it emulates the enrichment: the list + the card + N files) — before the yield, exactly as the real adapter does the network BEFORE it gives the RawTender."""

    source_key = "fake"
    source_name = "Fake"
    base_url = "https://example"
    country_codes = ["RS"]
    request_rate = 1.0
    default_polling_interval = 900
    supported_languages = ["sr-Latn"]

    def __init__(self, items: list[tuple[str, int]]) -> None:
        # items: [(uid, requests_for_this_card), ...]
        self._items = items

    async def fetch_batch(self, since):
        for uid, cost in self._items:
            _log_card(cost)
            yield _raw(uid)

    def build_source_uid(self, raw):
        return str(raw.payload.get("id", ""))

    def normalize(self, raw):
        uid = self.build_source_uid(raw)
        return _norm(
            uid,
            amount=None if uid == "over_budget" else 117000.0,
        )


@pytest.mark.asyncio
async def test_per_tender_request_delta_matches_real_httpx_log(monkeypatch):
    """The card 'cheap' costs 2 requests, 'pricey' — 16 (the brief's fact: “4 PRs brought the card from 1 to 16 requests, nobody noticed”) — the sum of the delta BEFORE/AFTER __anext__() must match the really logged lines."""
    adapter = _CostedAdapter([("cheap", 2), ("pricey", 17)])
    session = _FakeSession()
    worker = _make_worker(session, adapter)

    caplog_records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: caplog_records.append(record.getMessage())
    ingest_logger = logging.getLogger("src.workers.ingest")
    # ‼️ The logger level is GLOBAL process state: without restoration
    # it stays at WARNING until the end of the run, and ANY next test that expects an INFO line
    # from `src.workers.ingest` gets an empty caplog and silently checks nothing.
    # Taken separately the file is green — it is visible only on the
    # full run (the fact of 22.09.2026: this is how the test of the pending_enrich line fell).
    prev_level = ingest_logger.level
    ingest_logger.addHandler(handler)
    ingest_logger.setLevel(logging.WARNING)
    try:
        result = await worker._ingest(adapter, "fake", since=None)
    finally:
        ingest_logger.removeHandler(handler)
        ingest_logger.setLevel(prev_level)

    assert result.request_stats.total == 19  # 2 + 17 — the real log, not an estimate
    assert result.budget_max == 17
    # 'cheap' — 2 requests at a budget of 4, silent; 'pricey' — 17 at a budget of 16
    # (4 per card + 12 files, the ceiling), the excess is exactly one.
    assert result.budget_over_count == 1
    assert any("950123" not in m for m in caplog_records)  # sanity: the list is non-empty — ok
    assert any("pricey" in m and "17" in m for m in caplog_records)


@pytest.mark.asyncio
async def test_completeness_counted_only_for_inserted_and_updated():
    """Indicator 2 — only inserted/updated carry the content; touched/
    skipped (raw_hash did not change) must not dilute the share."""
    raw_unchanged = _raw("dup")
    from src.workers.ingest import compute_raw_hash

    same_hash = compute_raw_hash(raw_unchanged.payload)
    session = _FakeSession(raw_lookup=SimpleNamespace(id="raw-1", raw_hash=same_hash))

    class _TwoCardAdapter(_CostedAdapter):
        async def fetch_batch(self, since):
            yield raw_unchanged  # skip — raw_hash matched
            yield _raw("new1")  # insert — the full set of fields

    adapter = _TwoCardAdapter([])
    worker = _make_worker(session, adapter)

    result = await worker._ingest(adapter, "fake", since=None)

    assert result.skipped == 1
    assert result.inserted == 1
    assert result.completeness.total == 1  # NOT 2 — skip is not counted


@pytest.mark.asyncio
async def test_docs_lost_and_rate_survive_a_failed_pass():
    """A pass broken in the middle (fetch_batch threw an exception) must carry the PARTIAL statistics — otherwise the last successful piece is lost from sight exactly when the observability is needed most."""

    class _CrashingAdapter(_CostedAdapter):
        async def fetch_batch(self, since):
            _log_requests(1, status=429)  # 1 request to GetDocument.ashx, 429
            resp = SimpleNamespace(status_code=429)
            raise httpx.HTTPStatusError("boom", request=None, response=resp)
            yield  # pragma: no cover — it makes the function an async generator

    # we override classify: this test deliberately sends a TenderNotices-like
    # URL via _log_requests; for docs_lost we emulate a direct request to
    # GetDocument.ashx.
    adapter = _CrashingAdapter([])
    session = _FakeSession()
    worker = _make_worker(session, adapter)

    async def _fake_fetch_batch(since):
        _httpx_logger.info(
            'HTTP Request: %s %s "%s %d %s"',
            "GET",
            httpx.URL("https://jnportal.src-ujn.example.invalid/GetDocument.ashx?id=1"),
            "HTTP/1.1",
            429,
            "Too Many Requests",
        )
        resp = SimpleNamespace(status_code=429)
        raise httpx.HTTPStatusError("boom", request=None, response=resp)
        yield  # pragma: no cover

    adapter.fetch_batch = _fake_fetch_batch  # type: ignore[method-assign]

    result = await worker._ingest(adapter, "fake", since=None)

    assert result.failed is True
    assert result.request_stats.status_429_by_endpoint.get("GetDocument.ashx") == 1


@pytest.mark.asyncio
async def test_page_request_is_not_charged_to_the_next_card():
    """‼️ The request of the LIST PAGE falls into the same window of measurement as the next card (the delta between two yields), but it is not the price of the card.

    The form — exactly the one in production: the list page, then the card with 4 fixed requests and 4 files (the most frequent case — 63 cards of 168). The price of the card = 8, the budget = 4 + 4 = 8 → silence. Without subtracting the requests of the pass the delta would be 9 and would give a false warning — these are the two extra out of 151 in the production log of 22.09.2026.
    """

    class _PagedAdapter(_CostedAdapter):
        async def fetch_batch(self, since):
            _log_one(f"{_BASE}/oglasi-svi")  # the token of the pass
            _log_one(f"{_BASE}/api/searchgrid/TenderNotices/get?skip=0&take=100")
            _log_card(8)
            yield _raw("on_budget")

    adapter = _PagedAdapter([])
    worker = _make_worker(_FakeSession(), adapter)

    result = await worker._ingest(adapter, "fake", since=None)

    assert result.request_stats.total == 10  # 1 token + 1 page + 8 card
    assert result.budget_over_count == 0
    assert result.budget_max == 8  # the most expensive card of the day, even within the budget
