"""Tests of the indicators of the observability of the flow/enrichment (Tests C/D, tmp/TASK_flow_observability.md).

The expectations are LITERALS from the fact/brief, not computed from the same constant/function that the code uses (``.claude/rules/tests-known-defects.md``: ``assert msg == build_msg(...)`` is always green and proves nothing).

The mutation check — in the docstring of each function below where it is applied; the summary of the mutations — TESTING_flow_observability.md §Mutations.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from src.workers.flow_observability import (
    MIN_SAMPLE_FOR_COMPLETENESS_ALERT,
    DOCUMENTS_MAX_FILES,
    TENDER_FIXED_REQUEST_BUDGET,
    TENDER_REQUEST_CEILING,
    CompletenessCounts,
    FlowMetricsStore,
    PassRequestStats,
    RequestCounterHandler,
    amount_missing_threshold_pct,
    build_completeness_alerts,
    build_docs_lost_alert,
    build_rate_pass_log,
    check_tender_budget,
    tender_request_budget,
    classify_endpoint,
    deadline_missing_threshold_pct,
    description_missing_threshold_pct,
    docs_lost_count,
)


def _httpx_record(method: str, url: str, status: int) -> logging.LogRecord:
    """The same trick as tests/test_log_secrets.py — the real format of httpx."""
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=(
            method,
            httpx.URL(url),
            "HTTP/1.1",
            status,
            "OK" if status < 400 else "ERR",
        ),
        exc_info=None,
    )


# --------------------------------------------------------------------------- #
#  The breakdown by endpoint (Indicator 1 — the literals of the facts of 12.09/20.09.2026)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://jnportal.src-ujn.example.invalid/GetDocument.ashx?id=1&accessToken=X",
            "GetDocument.ashx",
        ),
        (
            "https://jnportal.src-ujn.example.invalid/api/TenderPlanItemsAssociated/42",
            "TenderPlanItemsAssociated",
        ),
        (
            "https://jnportal.src-ujn.example.invalid/api/LotsTenderSpaceEO/42",
            "LotsTenderSpaceEO",
        ),
        (
            "https://jnportal.src-ujn.example.invalid/api/TenderNotices?filter=1",
            "TenderNotices",
        ),
        (
            "https://jnportal.src-ujn.example.invalid/api/documentControl/42",
            "documentControl",
        ),
        ("https://jnportal.src-ujn.example.invalid/tender-eo/12345", "/tender-eo/<id>"),
        ("https://jnportal.src-ujn.example.invalid/api/oglasi", "other"),
    ],
)
def test_classify_endpoint_matches_known_patterns(url: str, expected: str) -> None:
    assert classify_endpoint(url) == expected


# --------------------------------------------------------------------------- #
#  RequestCounterHandler — the count/classification/peak req/s
# --------------------------------------------------------------------------- #
def test_request_counter_handler_counts_total_and_429_by_endpoint() -> None:
    handler = RequestCounterHandler()
    handler.emit(_httpx_record("GET", "https://x/api/TenderNotices", 200))
    handler.emit(_httpx_record("GET", "https://x/GetDocument.ashx?id=1", 429))
    handler.emit(_httpx_record("GET", "https://x/GetDocument.ashx?id=2", 429))
    handler.emit(_httpx_record("GET", "https://x/api/LotsTenderSpaceEO/1", 200))

    assert handler.total == 4
    assert handler.status_429 == 2
    assert handler.by_endpoint == {
        "TenderNotices": 1,
        "GetDocument.ashx": 2,
        "LotsTenderSpaceEO": 1,
    }
    assert handler.status_429_by_endpoint == {"GetDocument.ashx": 2}


def test_request_counter_handler_ignores_non_httpx_lines() -> None:
    """‼️ MUTATION (in the docstring of _record): a line without a regex match is not counted — otherwise the noise INFO lines of httpx (not about a request) would inflate the counter. Proved by a direct emit of a “foreign” message."""
    handler = RequestCounterHandler()
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="connect_tcp.started host='x'",
        args=(),
        exc_info=None,
    )
    handler.emit(record)
    assert handler.total == 0


def test_peak_requests_per_second_sliding_window() -> None:
    handler = RequestCounterHandler()
    handler.emit(_httpx_record("GET", "https://x/a", 200))
    # 5 requests within one 1-second window — the peak of the fact was 13 req/s.
    base = handler._timestamps[0]
    handler._timestamps = [base, base + 0.1, base + 0.2, base + 0.9, base + 3.0]
    assert handler.peak_requests_per_second() == 4  # the first 4 fit into 1 second


def test_peak_requests_per_second_empty_is_zero() -> None:
    assert RequestCounterHandler().peak_requests_per_second() == 0


def test_pass_request_stats_pct_429_literal() -> None:
    stats = PassRequestStats(total=1246, status_429=397)
    assert stats.pct_429 == 31.9  # 397/1246*100, the brief’s fact ~32%


def test_build_rate_pass_log_contains_numbers_and_endpoint() -> None:
    stats = PassRequestStats(
        total=10,
        status_429=3,
        status_429_by_endpoint={"GetDocument.ashx": 2, "TenderNotices": 1},
    )
    text = build_rate_pass_log("jnportal", stats)
    assert "jnportal" in text
    assert "запросов=10" in text
    assert "429=3" in text
    assert "GetDocument.ashx=2" in text
    assert "TenderNotices=1" in text


# --------------------------------------------------------------------------- #
#  The request budget per card (Test C)
# --------------------------------------------------------------------------- #
def test_request_budget_literals_are_four_plus_twelve() -> None:
    """‼️ The literals — NOT from another constant: if someone raises the budget, this test must notice and force a justification by figures, as it is justified now (a measurement of 168 cards on 22.09.2026, see the docstring of the constants).

    4 — the fixed part of the card (the lots + the HTML + the plan + the list of files), 12 — the ceiling of the downloadable files; 16 — the absolute ceiling of the price of the card, exactly the maximum that was observed in production.
    """
    assert TENDER_FIXED_REQUEST_BUDGET == 4
    assert DOCUMENTS_MAX_FILES == 12
    assert TENDER_REQUEST_CEILING == 16


def test_documents_max_files_matches_parser() -> None:
    """‼️ The guard of the duplicate: the worker does not import the constant from the parser, so there is nothing left to catch the divergence with. If they diverge, the budget silently stops matching how many files the adapter really downloads."""
    from src.parsers.jnportal.documents_fetch import _MAX_FILES

    assert DOCUMENTS_MAX_FILES == _MAX_FILES


def test_tender_request_budget_is_fixed_part_plus_one_per_file() -> None:
    """The expectations — literals from the measurement of production, not through the same formula."""
    assert tender_request_budget(0) == 4  # a card without documents
    assert tender_request_budget(4) == 8  # the most frequent case (63 cards)
    assert tender_request_budget(12) == 16  # the ceiling of the files
    assert tender_request_budget(99) == 16  # above the ceiling the budget does not grow


def test_check_tender_budget_silent_on_every_observed_shape() -> None:
    """‼️ The MAIN expectation of step 2: on EVERY form of the price observed in production (168 cards in 1.5 days, the distribution of the files from the docstring of the constants), the budget must stay silent. It is because of 151 false warnings that the price of the card stopped being visible."""
    observed_files = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12]
    for files in observed_files:
        total = (
            4 + files
        )  # the fixed part by the fact of the log — exactly 4 for 168 of 168
        assert check_tender_budget("950123", total, files) is None, files


def test_check_tender_budget_catches_fifth_fixed_request() -> None:
    """What the budget was set up for: the next PR added a FIFTH request per card (the brief’s fact “4 PRs in a row brought the card from 1 to 16 unnoticed”).

    ‼️ MUTATION: replace `<=` with `<` in check_tender_budget → a card exactly on the budget would be falsely counted as an excess; the boundary is checked by the test above.
    """
    text = check_tender_budget("950123", 9, 4)  # 4 files → the budget 8, spent 9
    assert text is not None
    assert "950123" in text
    assert "9" in text
    assert "8" in text  # the budget


def test_check_tender_budget_catches_retry_storm_over_file_cap() -> None:
    """The second real form of the excess: the download inflated by retries beyond the ceiling of the files (13 GetDocument requests at the ceiling of 12)."""
    text = check_tender_budget("950123", 17, 13)
    assert text is not None
    assert "17" in text
    assert "16" in text  # the budget reached the ceiling 4 + 12


# --------------------------------------------------------------------------- #
#  The completeness of the enrichment (Indicator 2 — the MAIN one)
# --------------------------------------------------------------------------- #
def test_completeness_thresholds_are_from_fact_defaults() -> None:
    """‼️ The literals from the fact (the docstring of the module, the run of 20.09.2026 over 30 days): the maximum of the norm without deadline/description 8.1%/9.2% (19.09, n=173) → the threshold 20% with a margin; without a sum 53.7% (22.08, n=41) → the threshold 65%."""
    assert deadline_missing_threshold_pct() == 20.0
    assert description_missing_threshold_pct() == 20.0
    assert amount_missing_threshold_pct() == 65.0


def test_completeness_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_COMPLETENESS_DEADLINE_MISSING_PCT", "12.5")
    assert deadline_missing_threshold_pct() == 12.5


def test_completeness_counts_add_and_percentages() -> None:
    counts = CompletenessCounts()
    counts.add(
        amount_original=100.0,
        amount_eur=90.0,
        deadline_at="2026-01-01",
        description="x",
    )
    counts.add(amount_original=None, amount_eur=None, deadline_at=None, description="")
    counts.add(
        amount_original=None,
        amount_eur=None,
        deadline_at="2026-01-01",
        description=None,
    )

    assert counts.total == 3
    assert counts.no_amount == 2
    assert counts.no_deadline == 1
    assert counts.no_description == 2
    assert counts.pct_amount == 66.7
    assert counts.pct_deadline == 33.3
    assert counts.pct_description == 66.7


def test_build_completeness_alerts_fires_above_threshold() -> None:
    """78% without a deadline (the fact of the incident of 08.09.2026, the docstring of test_jnportal_fetch_batch_429.py) — certainly above the threshold of 20%."""
    counts = CompletenessCounts(
        total=51, no_amount=24, no_deadline=40, no_description=40
    )
    alerts = build_completeness_alerts("jnportal", counts)
    # without a sum 24/51=47.1% < the threshold 65% — silent; deadline/description 78.4% > 20% — both shout.
    assert len(alerts) == 2
    assert any("без дедлайна 40 из 51" in a for a in alerts)
    assert any("78.4%" in a for a in alerts)  # 40/51*100


def test_build_completeness_alerts_silent_within_norm() -> None:
    """8.1% without a deadline (the fact of a normal day 19.09.2026, n=173) — quieter than the threshold."""
    counts = CompletenessCounts(
        total=173, no_amount=69, no_deadline=14, no_description=16
    )
    alerts = build_completeness_alerts("jnportal", counts)
    assert alerts == []  # 8.1%/9.2% < 20%, 39.9% < 65%


def test_build_completeness_alerts_skips_tiny_sample() -> None:
    """‼️ MUTATION: remove the MIN_SAMPLE check → 1 of 1 (100% without a deadline) would generate a noise alert on the very first tender of the pass."""
    assert MIN_SAMPLE_FOR_COMPLETENESS_ALERT == 5
    counts = CompletenessCounts(total=1, no_amount=1, no_deadline=1, no_description=1)
    assert build_completeness_alerts("jnportal", counts) == []


# --------------------------------------------------------------------------- #
#  Silent loss of documents (Indicator 3)
# --------------------------------------------------------------------------- #
def test_docs_lost_count_reads_get_document_429() -> None:
    stats = PassRequestStats(
        status_429_by_endpoint={"GetDocument.ashx": 14, "TenderNotices": 2}
    )
    assert docs_lost_count(stats) == 14  # the brief’s fact: 14 per day


def test_docs_lost_alert_fires_on_any_positive_count() -> None:
    text = build_docs_lost_alert("jnportal", 1)
    assert text is not None
    assert "1 документ" in text
    assert "GetDocument.ashx" in text


def test_docs_lost_alert_silent_on_zero() -> None:
    assert build_docs_lost_alert("jnportal", 0) is None


# --------------------------------------------------------------------------- #
#  FlowMetricsStore — the daily Redis accumulator
# --------------------------------------------------------------------------- #
class _FakeHashRedis:
    """In-memory Redis: hincrby/hget/hset/hgetall/expire (enough for FlowMetricsStore, the same trick as FakeRedis in test_source_guards.py)."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        h = self.hashes.setdefault(key, {})
        h[field] = str(int(h.get(field, "0")) + amount)
        return int(h[field])

    async def hget(self, key: str, field: str):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key: str, field: str, value: str) -> None:
        self.hashes.setdefault(key, {})[field] = value

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def expire(self, key: str, ttl: int) -> None:
        pass


@pytest.mark.asyncio
async def test_flow_metrics_store_records_and_reads_back() -> None:
    redis = _FakeHashRedis()
    store = FlowMetricsStore(redis)
    stats = PassRequestStats(
        total=100,
        status_429=32,
        peak_rps=13,
        status_429_by_endpoint={"GetDocument.ashx": 14},
    )
    completeness = CompletenessCounts(
        total=51, no_amount=24, no_deadline=40, no_description=40
    )

    await store.record_pass(
        "2026-09-20",
        stats=stats,
        completeness=completeness,
        budget_over_count=2,
        budget_max=16,
    )
    day = await store.read_day("2026-09-20")

    assert day["requests_total"] == 100
    assert day["requests_429"] == 32
    assert day["peak_rps"] == 13
    assert day["docs_lost"] == 14
    assert day["cards_total"] == 51
    assert day["cards_no_deadline"] == 40
    assert day["budget_over_count"] == 2
    assert day["budget_max"] == 16
    assert day["by_endpoint_429"] == {"GetDocument.ashx": 14}


@pytest.mark.asyncio
async def test_flow_metrics_store_accumulates_across_passes() -> None:
    """Two passes in one day — are summed (INCRBY), not overwritten."""
    redis = _FakeHashRedis()
    store = FlowMetricsStore(redis)
    stats = PassRequestStats(total=10, status_429=1)
    completeness = CompletenessCounts(
        total=5, no_amount=1, no_deadline=1, no_description=1
    )

    await store.record_pass(
        "2026-09-20",
        stats=stats,
        completeness=completeness,
        budget_over_count=0,
        budget_max=3,
    )
    await store.record_pass(
        "2026-09-20",
        stats=stats,
        completeness=completeness,
        budget_over_count=0,
        budget_max=1,
    )
    day = await store.read_day("2026-09-20")

    assert day["requests_total"] == 20
    assert day["cards_total"] == 10
    assert day["budget_max"] == 3  # the maximum, not the sum (3 > 1 remains)


@pytest.mark.asyncio
async def test_flow_metrics_store_no_redis_is_noop() -> None:
    store = FlowMetricsStore(None)
    await store.record_pass(
        "2026-09-20",
        stats=PassRequestStats(),
        completeness=CompletenessCounts(),
        budget_over_count=0,
        budget_max=0,
    )
    day = await store.read_day("2026-09-20")
    assert day["requests_total"] == 0


@pytest.mark.asyncio
async def test_flow_metrics_store_broken_redis_does_not_raise() -> None:
    class _BrokenRedis:
        async def hincrby(self, *a, **k):
            raise ConnectionError("redis down")

        async def hget(self, *a, **k):
            raise ConnectionError("redis down")

        async def hgetall(self, *a, **k):
            raise ConnectionError("redis down")

    store = FlowMetricsStore(_BrokenRedis())
    await store.record_pass(
        "2026-09-20",
        stats=PassRequestStats(total=1),
        completeness=CompletenessCounts(total=1),
        budget_over_count=0,
        budget_max=0,
    )
    day = await store.read_day("2026-09-20")
    assert day["requests_total"] == 0  # a Redis failure → zeros, not an exception


# --------------------------------------------------------------------------- #
#  The broken passes in the daily accumulator (step 3)
# --------------------------------------------------------------------------- #
async def test_record_pass_counts_failed_passes_and_keeps_longest_streak():
    """The counter of the broken passes is summed, the length of the series is the MAXIMUM per day (not the sum): three failures in a row are one incident of length 3, not 6."""
    redis = _FakeHashRedis()
    store = FlowMetricsStore(redis)

    for streak in (1, 2, 3):
        await store.record_pass(
            "2026-09-22",
            stats=PassRequestStats(total=3),
            completeness=CompletenessCounts(),
            budget_over_count=0,
            budget_max=0,
            failed=True,
            fail_streak=streak,
        )

    # A new incident on the same day starts with a series of 1 — the maximum does not fall.
    await store.record_pass(
        "2026-09-22",
        stats=PassRequestStats(total=3),
        completeness=CompletenessCounts(),
        budget_over_count=0,
        budget_max=0,
        failed=True,
        fail_streak=1,
    )

    day = await store.read_day("2026-09-22")

    assert day["passes_failed"] == 4
    assert day["fail_streak_max"] == 3


async def test_record_pass_successful_pass_does_not_touch_failure_counters():
    redis = _FakeHashRedis()
    store = FlowMetricsStore(redis)

    await store.record_pass(
        "2026-09-22",
        stats=PassRequestStats(total=3),
        completeness=CompletenessCounts(),
        budget_over_count=0,
        budget_max=0,
    )

    day = await store.read_day("2026-09-22")

    assert day["passes_failed"] == 0
    assert day["fail_streak_max"] == 0
