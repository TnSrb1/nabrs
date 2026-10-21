"""Observability of the tender flow and the card enrichment (REV-07, owner 20.09.2026).

WHY. Owner decision: the anti-flapping of the TG alert “the source broke” is REMOVED (it only
hid the problem) — in its place we need the means of CONTROLLING and DETECTING problems with the tender
flow and the card enrichment. The production facts of 20.09.2026:

  * the rate/failures of the source: 397 out of 1246 requests a day (32%) — HTTP 429,
    peaks up to 13 req/s at a declared 1 req/s, a breakdown by endpoint (NOT a general
    counter — ``grep -c 429`` also counts the lines of the traceback);
  * the completeness of the enrichment: the share of the pass’s cards without a sum/deadline/description —
    the MAIN indicator of “the client sees an empty card” (78% without a deadline in the hour of the
    collapse 08.09.2026 against 0-7% in normal times);
  * silent loss of documents: ``GetDocument.ashx`` on 429 is neither retried nor does it log the loss (``a source adapter`` (not published));
  * the request budget per card: 4 PRs in a row took the card from 1 to 16 requests unnoticed — the visibility of the price is needed;
  * broken passes (added 22.09.2026): ``passes_failed`` and the longest run in a row ``fail_streak_max`` per day. ‼️ This is NOT statistics for the sake of statistics: a single failure, cured by the next pass, no longer rings Telegram (the streak threshold —
    ``src.workers.ingest._FAIL_STREAK_ALERT_THRESHOLD``), so it MUST be visible here and in the daily summary, otherwise “distinguishing” would degenerate into “hiding”.

HOW IT IS COUNTED (without editing the adapter or the documents fetch — they belong to the parallel branch ``claude/jnportal-throttle``; the same rate-limit must not be touched twice). Instead of instrumenting the adapter we count ON OUR SIDE: :class:`RequestCounterHandler` — a ``logging.Handler`` attached to the ``httpx`` logger for the duration of one source pass. At INFO level httpx logs EVERY request with a line ``HTTP Request: <method> <url> "<version> <status> <reason>"`` — this is the same data source from which on 20.09.2026 the facts were counted by hand through ``grep``. The handler sees the ALREADY FORMATTED message AFTER :class:`src.log_secrets.SecretMaskingFilter` (logger filters are applied before handlers) — the module does not need a second network client/token.

The card budget (Test C) is counted by the same handler, but with taking the difference of ``total`` BEFORE and AFTER each ``__anext__()`` of the ``fetch_batch`` generator — precisely between two ``yield``s the adapter makes all the network requests for ONE tender (see ``IngestWorker._ingest``, a manual iteration instead of ``async for``).

THE COMPLETENESS THRESHOLD — from a FACT (the same technique as the 30h for the inflow silence, #522). A run of 20.09.2026 (``docker exec deploy-postgres-1 psql``, read-only) over ``tenders_normalized`` for 30 days, a breakdown by day: the incident day 07.09.2026 (the 429 collapse, 64.3% without a deadline — this is not “the norm”, but exactly what the alert must catch) and the days with n<20 (small-sample noise: 6-15 tenders a day) are excluded. On the remaining “normal” days the maximum is:

  * without a deadline — 8.1% (19.09.2026, n=173);
  * without a description — 9.2% (19.09.2026, n=173; the same day — both fields break
    together, one cause in the card parsing);
  * without a sum — 53.7% (22.08.2026, n=41) — the sum is OFTEN legitimately absent from the source (the estimated value is not always published, the ``customer_
    tax_id`` precedent from ``.claude/rules/matching-delivery.md``), therefore the base noise level of this field is an order of magnitude above the other two.

The thresholds are defaults with a margin above the observed maximum of the norm (the same principle as 30h above 24.1h in #522), an env override without an edit of docker-compose.

RELIABILITY CONTRACT (as ``health.py``/``ai_spend_report.py``): the observation NEVER breaks a pass. Redis is unavailable → :class:`FlowMetricsStore` is a no-op — the daily summary gets fewer figures, but the pass and the threshold alerts within ONE pass (they do not depend on Redis) keep working.

DECISION of the owner: NO suppression of repeats of the threshold alert — the problem persists, the alert comes on EVERY pass where the indicator is beyond the threshold.

CHANNEL. Exactly as ``health.py``: only the admin bot ``@tendersrb_bot`` (``ADMIN_BOT_TOKEN``/``TG_ALERTS_CHAT_ID`` via the injectable ``WorkerHealth``/``alert``-callable). This module does NOT read ``TG_BOT_TOKEN``/``ADMIN_CHAT_ID`` (the user bot ``@nabrs_bot``) — the guard ``tests/workers/test_flow_observability_channel_guard.py`` checks this statically (AST) across ALL the diagnostic modules.
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  Counter of outgoing requests (parsing the httpx log, WITHOUT editing the adapter)
# --------------------------------------------------------------------------- #
# httpx (site-packages/httpx/_client.py): logger.info(
#   'HTTP Request: %s %s "%s %d %s"', method, url, http_version, status, reason)
_HTTPX_LOG_RE = re.compile(r'^HTTP Request: (\S+) (\S+) "[^"]*? (\d{3}) ')

# The known enrichment endpoints of jnportal (fact 12.09/20.09.2026, a breakdown by
# endpoint — NOT a general counter, see .claude/rules/matching-delivery.md). The order
# matters: more specific patterns — before the general one.
_ENDPOINT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GetDocument.ashx", re.compile(r"GetDocument\.ashx")),
    ("TenderPlanItemsAssociated", re.compile(r"TenderPlanItemsAssociated")),
    ("LotsTenderSpaceEO", re.compile(r"LotsTenderSpaceEO")),
    ("TenderNotices", re.compile(r"TenderNotices")),
    ("documentControl", re.compile(r"documentControl")),
    ("/tender-eo/<id>", re.compile(r"/tender-eo/\d+")),
    # Not about the card: the pass token and the NBS rate. We classify EXACTLY these, and
    # not “everything unknown” — otherwise a new (not yet described) enrichment endpoint
    # would fall into the same bucket and stop being counted as the cost of the card.
    ("oglasi-svi", re.compile(r"/oglasi-svi")),
    ("fx-rate", re.compile(r"kurs\.resenje\.org")),
)

# Requests belonging to NO card: they are made PER PASS (the token,
# the rate) or per LIST PAGE. By time they fall into the measurement window
# of the next card (the measurement is the delta BEFORE/AFTER one ``__anext__()``) and
# inflate its cost. Fact 22.09.2026: the log has 151 warnings “cost …” against
# 149 cards whose OWN price was above budget —
# exactly these two extra ones.
PASS_SCOPED_ENDPOINTS: frozenset[str] = frozenset(
    {"TenderNotices", "oglasi-svi", "fx-rate"}
)
DOCUMENTS_ENDPOINT = "GetDocument.ashx"


def classify_endpoint(url: str) -> str:
    """The request URL → a short endpoint name (for the breakdown, not a general counter)."""
    for name, pattern in _ENDPOINT_PATTERNS:
        if pattern.search(url):
            return name
    return "other"


class RequestCounterHandler(logging.Handler):
    """Counts the outgoing HTTP requests by parsing the already ready httpx log lines.

    Attached to the ``httpx`` logger (``level=INFO``, the same level that
    ``logging.basicConfig`` already enables in all workers) for the duration of one
    source pass. It does NOT touch the network and does NOT touch the adapter — it reads what
    httpx prints anyway.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.total = 0
        self.status_429 = 0
        self.by_endpoint: Counter[str] = Counter()
        self.status_429_by_endpoint: Counter[str] = Counter()
        self._timestamps: list[float] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover — trivial
        try:
            self._record(record)
        except Exception:  # noqa: BLE001 — the counter must not break logging
            pass

    def _record(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        match = _HTTPX_LOG_RE.match(message)
        if not match:
            return
        _method, url, status_raw = match.groups()
        status = int(status_raw)
        endpoint = classify_endpoint(url)
        self.total += 1
        self.by_endpoint[endpoint] += 1
        self._timestamps.append(record.created)
        if status == 429:
            self.status_429 += 1
            self.status_429_by_endpoint[endpoint] += 1

    @property
    def pass_scoped(self) -> int:
        """The requests of the pass/page (not the card’s price) — see ``PASS_SCOPED_ENDPOINTS``."""
        return sum(self.by_endpoint[name] for name in PASS_SCOPED_ENDPOINTS)

    @property
    def documents(self) -> int:
        """The downloaded documentation files: one request per file."""
        return self.by_endpoint[DOCUMENTS_ENDPOINT]

    def peak_requests_per_second(self) -> int:
        """Maximum requests in ANY sliding 1-s window (fact: there were 13 req/s)."""
        if not self._timestamps:
            return 0
        ts = sorted(self._timestamps)
        best = 1
        left = 0
        for right in range(len(ts)):
            while ts[right] - ts[left] > 1.0:
                left += 1
            best = max(best, right - left + 1)
        return best


class _AttachedCounter:
    """A context manager: attaches :class:`RequestCounterHandler` to the ``httpx`` logger and removes it on exit (even on an exception).

    ‼️ The counter works ONLY if the effective level of the ``httpx`` logger is ``<= INFO`` — in prod this is already so (``logging.basicConfig(level=INFO)`` in every ``main()``), but relying on an external setting is dangerous: a forgotten ``basicConfig`` in a new entry point would silently zero the counter, breaking nothing (httpx would keep working, just without lines in the log). Therefore the logger level is EXPLICITLY lowered to INFO for the duration of the counting and restored on exit — the counter does not depend on someone else's configuration.
    """

    def __init__(self, handler: RequestCounterHandler) -> None:
        self._handler = handler
        self._logger = logging.getLogger("httpx")
        self._prev_level: int | None = None

    def __enter__(self) -> RequestCounterHandler:
        self._prev_level = self._logger.level
        if not self._logger.isEnabledFor(logging.INFO):
            self._logger.setLevel(logging.INFO)
        self._logger.addHandler(self._handler)
        return self._handler

    def __exit__(self, *exc: object) -> None:
        self._logger.removeHandler(self._handler)
        if self._prev_level is not None:
            self._logger.setLevel(self._prev_level)


def count_requests() -> _AttachedCounter:
    """``with count_requests() as counter: ...`` — counts the httpx requests inside the block."""
    return _AttachedCounter(RequestCounterHandler())


@dataclass(frozen=True)
class PassRequestStats:
    """The total of the request counter for one pass of the source (for the log/summary/alert)."""

    total: int = 0
    status_429: int = 0
    by_endpoint: dict[str, int] = field(default_factory=dict)
    status_429_by_endpoint: dict[str, int] = field(default_factory=dict)
    peak_rps: int = 0

    @property
    def pct_429(self) -> float:
        return round(100.0 * self.status_429 / self.total, 1) if self.total else 0.0

    @classmethod
    def from_handler(cls, handler: RequestCounterHandler) -> "PassRequestStats":
        return cls(
            total=handler.total,
            status_429=handler.status_429,
            by_endpoint=dict(handler.by_endpoint),
            status_429_by_endpoint=dict(handler.status_429_by_endpoint),
            peak_rps=handler.peak_requests_per_second(),
        )


def build_rate_pass_log(source_key: str, stats: PassRequestStats) -> str:
    """An INFO log line with the rate/failures of the pass (Indicator 1) — the figures and the address."""
    endpoint_bits = ", ".join(
        f"{name}={count}"
        for name, count in sorted(stats.status_429_by_endpoint.items())
    )
    endpoint_note = f"; 429 по эндпоинту: {endpoint_bits}" if endpoint_bits else ""
    return (
        f"ingest[{source_key}]: запросов={stats.total} 429={stats.status_429} "
        f"({stats.pct_429}%) пик={stats.peak_rps} req/s{endpoint_note}"
    )


# --------------------------------------------------------------------------- #
#  The request budget for one card (Test C)
# --------------------------------------------------------------------------- #
# Fact of the brief: 4 PRs in a row took the card from 1 to 16 requests unnoticed.
# The budget is not a ban but the visibility of the price for the author of future enrichment.
#
# ‼️ WHY THE BUDGET IS A FORMULA, NOT A NUMBER (measurement 22.09.2026). A flat
# constant of 5 gave 151 warnings in 1.5 days of the container’s life with zero
# defects — that is exactly what the budget was supposed to protect from: the cost
# of the card stopped being visible, because it drowned in the noise. An analysis of
# `docker logs -t deploy-ingest-1` over 168 enriched cards (the command — in
# TESTING_ingest_flow.md, step 2) showed a rigid structure of the price:
#
#   * a FIXED part — EXACTLY 4 requests, in 168 cards out of 168:
#       /api/searchgrid/LotsTenderSpaceEO/get         — lots (Σ EstimatedValue)
#       /tender-eo/<id>                               — HTML of the card (header)
#       /api/searchgrid/TenderPlanItemsAssociated/get — plan (CPV, period)
#       /api/documentControl/get                      — LIST of files
#     none of the four duplicates another by data — there is nothing to cut without
#     losing a field (analysis — REPORT_ingest_flow.md, step 2);
#   * a VARIABLE part — exactly one GetDocument.ashx PER FILE; the observed
#     distribution of the number of files: 0→5, 1→14, 2→4, 3→10, 4→63, 5→34, 6→10,
#     7→1, 8→3, 9→6, 10→2, 12→16 cards. The ceiling of 12 is
#     `documents_fetch._MAX_FILES`, and not a chance of the sample.
#
# Hence the budget: 4 per card + 1 for each downloaded file, but not more than
# the file ceiling. Such a budget is SILENT on all 168 cards of the fact and shouts on
# what it was set up for: on the FIFTH fixed request (a new enrichment, added by the next PR)
# and on a retry that inflates the download beyond the ceiling.
TENDER_FIXED_REQUEST_BUDGET = 4
# The ceiling of documentation files per card. A duplicate of `documents_fetch._MAX_FILES`
# (the worker does not pull the constant from the parser); the divergence is caught by the guard test
# test_documents_max_files_matches_parser.
DOCUMENTS_MAX_FILES = 12
# The absolute ceiling of the card’s price — for the docstrings and the summary: 4 + 12 = 16.
TENDER_REQUEST_CEILING = TENDER_FIXED_REQUEST_BUDGET + DOCUMENTS_MAX_FILES


def tender_request_budget(document_requests: int) -> int:
    """The request budget for a card that downloaded ``document_requests`` files."""
    capped = min(max(document_requests, 0), DOCUMENTS_MAX_FILES)
    return TENDER_FIXED_REQUEST_BUDGET + capped


def check_tender_budget(
    source_uid: str, request_count: int, document_requests: int = 0
) -> str | None:
    """The text of the WARNING if the card ``source_uid`` cost more than the budget.

    ``request_count`` — the price of the card ITSELF: the requests of the pass and of the list page
    (``PASS_SCOPED_ENDPOINTS``) are already subtracted from it by the caller.
    ``document_requests`` — how many of them went to downloading the files.
    """
    budget = tender_request_budget(document_requests)
    if request_count <= budget:
        return None
    return (
        f"ingest: карточка {source_uid} обошлась в {request_count} запросов "
        f"(бюджет {budget} = {TENDER_FIXED_REQUEST_BUDGET} на карточку + "
        f"{budget - TENDER_FIXED_REQUEST_BUDGET} документов)"
    )


# --------------------------------------------------------------------------- #
#  The completeness of the card enrichment (Indicator 2 — the MAIN one)
# --------------------------------------------------------------------------- #
# The defaults — from a FACT (see the module docstring): the observed maximum of the norm +
# a margin. An env override without an edit of docker-compose (the same technique as
# INGEST_SILENCE_ALERT_HOURS in health.py).
_DEFAULT_DEADLINE_MISSING_PCT = 20.0
_DEFAULT_DESCRIPTION_MISSING_PCT = 20.0
_DEFAULT_AMOUNT_MISSING_PCT = 65.0
# Fewer cards than this number in the pass — the share is too noisy (1 of 1 = 100%
# proves nothing), the threshold is not checked (the same technique as
# _DROP_MIN_PREV in src/parsers/guards.py).
MIN_SAMPLE_FOR_COMPLETENESS_ALERT = 5


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def deadline_missing_threshold_pct() -> float:
    return _env_float(
        "INGEST_COMPLETENESS_DEADLINE_MISSING_PCT", _DEFAULT_DEADLINE_MISSING_PCT
    )


def description_missing_threshold_pct() -> float:
    return _env_float(
        "INGEST_COMPLETENESS_DESCRIPTION_MISSING_PCT", _DEFAULT_DESCRIPTION_MISSING_PCT
    )


def amount_missing_threshold_pct() -> float:
    return _env_float(
        "INGEST_COMPLETENESS_AMOUNT_MISSING_PCT", _DEFAULT_AMOUNT_MISSING_PCT
    )


@dataclass
class CompletenessCounts:
    """The counter of the enrichment completeness per pass (only the inserted/updated cards —
    touched/skipped carry no new content, counting them would dilute the share)."""

    total: int = 0
    no_amount: int = 0
    no_deadline: int = 0
    no_description: int = 0

    def add(
        self,
        *,
        amount_original: Any,
        amount_eur: Any,
        deadline_at: Any,
        description: str | None,
    ) -> None:
        self.total += 1
        if amount_original is None and amount_eur is None:
            self.no_amount += 1
        if deadline_at is None:
            self.no_deadline += 1
        if not description:
            self.no_description += 1

    @property
    def pct_amount(self) -> float:
        return round(100.0 * self.no_amount / self.total, 1) if self.total else 0.0

    @property
    def pct_deadline(self) -> float:
        return round(100.0 * self.no_deadline / self.total, 1) if self.total else 0.0

    @property
    def pct_description(self) -> float:
        return round(100.0 * self.no_description / self.total, 1) if self.total else 0.0


def build_completeness_alerts(source_key: str, counts: CompletenessCounts) -> list[str]:
    """The threshold alerts of completeness (Indicator 2) — one text per metric,
    with the figures (value/threshold/sample). An empty sample, or a sample smaller than
    :data:`MIN_SAMPLE_FOR_COMPLETENESS_ALERT` — no alerts (too noisy).
    """
    if counts.total < MIN_SAMPLE_FOR_COMPLETENESS_ALERT:
        return []
    alerts: list[str] = []
    deadline_th = deadline_missing_threshold_pct()
    if counts.pct_deadline > deadline_th:
        alerts.append(
            f"⚠️ TenderSRB ingest[{source_key}]: без дедлайна {counts.no_deadline} из "
            f"{counts.total} ({counts.pct_deadline}%) > порога {deadline_th}% — карточки "
            f"уходят клиенту пустышкой."
        )
    description_th = description_missing_threshold_pct()
    if counts.pct_description > description_th:
        alerts.append(
            f"⚠️ TenderSRB ingest[{source_key}]: без описания {counts.no_description} из "
            f"{counts.total} ({counts.pct_description}%) > порога {description_th}%."
        )
    amount_th = amount_missing_threshold_pct()
    if counts.pct_amount > amount_th:
        alerts.append(
            f"⚠️ TenderSRB ingest[{source_key}]: без суммы {counts.no_amount} из "
            f"{counts.total} ({counts.pct_amount}%) > порога {amount_th}%."
        )
    return alerts


# --------------------------------------------------------------------------- #
#  Silent loss of documents (Indicator 3)
# --------------------------------------------------------------------------- #
# Any 429 on GetDocument.ashx — the file is lost SILENTLY (the adapter does not retry,
# a source adapter (not published) → return None). Unlike
# completeness (Indicator 2), here there is no “normal” non-zero level — any
# value > 0 deserves an alert at once, without a threshold.
_DOCS_ENDPOINT = "GetDocument.ashx"


def docs_lost_count(stats: PassRequestStats) -> int:
    return stats.status_429_by_endpoint.get(_DOCS_ENDPOINT, 0)


def build_docs_lost_alert(source_key: str, count: int) -> str | None:
    if count <= 0:
        return None
    return (
        f"⚠️ TenderSRB ingest[{source_key}]: {count} документ(ов) потеряно молча "
        f"(429 на {_DOCS_ENDPOINT}, без ретрая) — description ушёл без ТЗ."
    )


# --------------------------------------------------------------------------- #
#  The daily accumulator (Redis) — for src.workers.flow_report
# --------------------------------------------------------------------------- #
_FLOW_TTL_SEC = (
    3 * 24 * 3600
)  # 3 days — survives a restart, does not accumulate forever


class FlowMetricsStore:
    """The daily counters of the flow/enrichment — Redis, the same client as
    ``PendingEnrichStore``/``SilenceAlertGate`` (do not create a new one).

    A flat scheme (the project rules — “no nested structures”): one HASH per
    day with numeric fields + two HASHes for the breakdown by endpoint. ``redis=
    None``/a client failure → all methods are no-op (a WARNING on write) — the daily
    summary will undercount the figures, but the pass and the threshold alerts within it do not depend on
    Redis (see the module docstring).
    """

    def __init__(self, redis: Any | None) -> None:
        self._redis = redis

    @staticmethod
    def _totals_key(day: str) -> str:
        return f"flow:{day}:totals"

    @staticmethod
    def _endpoint_429_key(day: str) -> str:
        return f"flow:{day}:429_by_endpoint"

    async def record_pass(
        self,
        day: str,
        *,
        stats: PassRequestStats,
        completeness: CompletenessCounts,
        budget_over_count: int,
        budget_max: int,
        failed: bool = False,
        fail_streak: int = 0,
    ) -> None:
        if self._redis is None:
            return
        totals_key = self._totals_key(day)
        endpoint_key = self._endpoint_429_key(day)
        try:
            await self._redis.hincrby(totals_key, "requests_total", stats.total)
            await self._redis.hincrby(totals_key, "requests_429", stats.status_429)
            await self._redis.hincrby(totals_key, "cards_total", completeness.total)
            await self._redis.hincrby(
                totals_key, "cards_no_amount", completeness.no_amount
            )
            await self._redis.hincrby(
                totals_key, "cards_no_deadline", completeness.no_deadline
            )
            await self._redis.hincrby(
                totals_key, "cards_no_description", completeness.no_description
            )
            await self._redis.hincrby(
                totals_key, "budget_over_count", budget_over_count
            )
            docs_lost = docs_lost_count(stats)
            if docs_lost:
                await self._redis.hincrby(totals_key, "docs_lost", docs_lost)
            # peak_rps/budget_max — the maximum per day among ALL cards (not
            # only those that exceeded the budget), not a sum; Redis cannot do an atomic
            # max over a string field of a HASH without Lua — read-modify-write
            # is acceptable: observability, not money; a race would understate the peak.
            prev = await self._redis.hget(totals_key, "peak_rps")
            prev_rps = int(prev) if prev else 0
            if stats.peak_rps > prev_rps:
                await self._redis.hset(totals_key, "peak_rps", str(stats.peak_rps))
            prev_budget = await self._redis.hget(totals_key, "budget_max")
            prev_budget_max = int(prev_budget) if prev_budget else 0
            if budget_max > prev_budget_max:
                await self._redis.hset(totals_key, "budget_max", str(budget_max))
            # Broken passes (step 3): a single network failure in Telegram does not
            # go away — therefore it MUST be visible HERE, otherwise “distinguishing”
            # would degenerate into “hiding”. The counter per day + the longest streak.
            if failed:
                await self._redis.hincrby(totals_key, "passes_failed", 1)
                prev_streak = await self._redis.hget(totals_key, "fail_streak_max")
                prev_streak_max = int(prev_streak) if prev_streak else 0
                if fail_streak > prev_streak_max:
                    await self._redis.hset(
                        totals_key, "fail_streak_max", str(fail_streak)
                    )
            for name, count in stats.status_429_by_endpoint.items():
                if count:
                    await self._redis.hincrby(endpoint_key, name, count)
            await self._redis.expire(totals_key, _FLOW_TTL_SEC)
            await self._redis.expire(endpoint_key, _FLOW_TTL_SEC)
        except Exception:  # noqa: BLE001 — the accumulator does not break the pass
            logger.warning(
                "flow_observability: Redis unavailable for writing the daily counters"
            )

    async def read_day(self, day: str) -> dict[str, Any]:
        """The day total: numeric fields + the breakdown of 429 by endpoint. Empty/failed
        Redis → all zeros (the summary still goes out, just without these figures)."""
        empty = {
            "requests_total": 0,
            "requests_429": 0,
            "cards_total": 0,
            "cards_no_amount": 0,
            "cards_no_deadline": 0,
            "cards_no_description": 0,
            "budget_over_count": 0,
            "budget_max": 0,
            "docs_lost": 0,
            "peak_rps": 0,
            "passes_failed": 0,
            "fail_streak_max": 0,
            "by_endpoint_429": {},
        }
        if self._redis is None:
            return empty
        try:
            totals = await self._redis.hgetall(self._totals_key(day))
            endpoint = await self._redis.hgetall(self._endpoint_429_key(day))
        except Exception:  # noqa: BLE001
            logger.warning(
                "flow_observability: Redis unavailable for reading the daily counters"
            )
            return empty

        def _decode(v: Any) -> str:
            return v.decode() if isinstance(v, bytes) else v

        totals = {_decode(k): _decode(v) for k, v in (totals or {}).items()}
        endpoint = {_decode(k): int(_decode(v)) for k, v in (endpoint or {}).items()}
        for key in empty:
            if key == "by_endpoint_429":
                continue
            empty[key] = int(totals.get(key, 0) or 0)
        empty["by_endpoint_429"] = endpoint
        return empty
