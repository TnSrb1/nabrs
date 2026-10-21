"""Flow Report — the daily summary “flow and enrichment” (REV-07, owner 20.09.2026).

WHY. The owner ordered the means of controlling the tender flow and the card enrichment (in place of the removed anti-flapping of the source-failure alert — see the `src/workers/ingest.py`
docstring REV-07). The threshold alerts (immediate, without suppression of repeats) go out of `IngestWorker` on EVERY pass where an indicator is beyond the threshold. This worker is the SECOND visibility channel: a daily summary of all four indicators with figures, even when everything is normal (“this is not muting, but regular reporting — the baseline against which deterioration is visible”, the brief). The mechanism is copied from `src.workers.ai_spend_report` (the same technique of dependency injection and polling); nothing new was invented.

FOUR INDICATORS:
  1. The rate/failures of the source (requests, 429, peak req/s, a breakdown by endpoint) —
     from `FlowMetricsStore` (Redis, accumulated by `IngestWorker` over the day).
  2. The completeness of the enrichment (without sum/deadline/description) — SQL over
     `tenders_normalized` for the last 24h (a public table without RLS, the same pattern as
     `ingest.py::_INGEST_FRESHNESS_SQL`; owner connection, `set_tenant` is not needed).
  3. Silent loss of documents (429 on GetDocument.ashx) — `FlowMetricsStore`.
  4. The inflow over 24h (the same SQL as indicator 2, the field `total`) — does NOT duplicate
     the inflow silence guard #522 (`SilenceAlertGate`/`_check_silence` in
     ingest.py); it simply shows the figure next to the others.

THE SENDING TIME — 06:00 UTC (after the portal’s night publication window 22:00-01:00
UTC, by this time the day has settled). `FLOW_REPORT_HOUR_UTC` — an env override without an edit of docker-compose.

IDEMPOTENCY WITHOUT A NEW TABLE (the RED boundary of the brief — alembic/ must not be touched). `ai_spend_report` claims through `ai_spend_alert_log` (DB); here, instead of a new migration — Redis `SET NX` (`flow:report_sent:<date>`, TTL 2 days) with the same client as `FlowMetricsStore`/`PendingEnrichStore`. Redis unavailable → the claim always succeeds (the summary is better sent twice than not sent at all — the same reliability contract as the rest of REV-07/06).

RELIABILITY CONTRACT: the observation NEVER breaks the contour (as ai_spend_report/health.py) — a DB/Redis/network failure is swallowed as a WARNING, the worker lives on.

CHANNEL — only the admin bot (`WorkerHealth.from_env`, `ADMIN_BOT_TOKEN`/`TG_ALERTS_CHAT_ID`). The module does not read `TG_BOT_TOKEN`/`ADMIN_CHAT_ID` (the user `@nabrs_bot`) — the guard `tests/workers/test_flow_observability_channel_guard.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import text

from src.db.session import session_scope
from src.workers.flow_observability import (
    TENDER_REQUEST_CEILING,
    FlowMetricsStore,
    amount_missing_threshold_pct,
    deadline_missing_threshold_pct,
    description_missing_threshold_pct,
)

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = (
    3600.0  # once an hour — the summary is idempotent, the check is cheap
)
_DEFAULT_REPORT_HOUR_UTC = 6

# Indicators 2/4: the last 24h of tenders_normalized (a public table without
# RLS, owner connection — the same pattern as _INGEST_FRESHNESS_SQL).
# `no_deadline` — the metric of the COMPLETENESS of the enrichment (the deadline is not extracted), and not of
# actuality: such a tender lives in the feed/matching for 30 days from publication
# (owner 06.10.2026, src/domain/actuality.py), so a high share here = less
# time for the client to submit, not “eternal” tenders in the feed.
_DAILY_COMPLETENESS_SQL = text("""
    SELECT
        COUNT(*)                                                       AS total,
        COUNT(*) FILTER (WHERE deadline_at IS NULL)                    AS no_deadline,
        COUNT(*) FILTER (
            WHERE amount_original IS NULL AND amount_eur IS NULL
        )                                                               AS no_amount,
        COUNT(*) FILTER (
            WHERE description IS NULL OR description = ''
        )                                                               AS no_description
    FROM tenders_normalized
    WHERE first_seen_at >= NOW() - INTERVAL '24 hours'
    """)


def _env_int(name: str, default: int) -> int:
    import os

    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if 0 <= value <= 23 else default


def report_hour_utc() -> int:
    return _env_int("FLOW_REPORT_HOUR_UTC", _DEFAULT_REPORT_HOUR_UTC)


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 1) if denominator else 0.0


def build_daily_summary_text(
    *,
    day: str,
    completeness: dict[str, Any],
    flow: dict[str, Any],
) -> str:
    """The text of the daily summary — all four indicators with figures (even when normal).

    ‼️ Not “something is wrong” — concrete numbers/thresholds, the same principle as
    `build_silence_alert_text`/`build_report_text` (ai_spend_report).
    """
    total = int(completeness.get("total", 0) or 0)
    no_deadline = int(completeness.get("no_deadline", 0) or 0)
    no_amount = int(completeness.get("no_amount", 0) or 0)
    no_description = int(completeness.get("no_description", 0) or 0)

    requests_total = int(flow.get("requests_total", 0) or 0)
    requests_429 = int(flow.get("requests_429", 0) or 0)
    peak_rps = int(flow.get("peak_rps", 0) or 0)
    docs_lost = int(flow.get("docs_lost", 0) or 0)
    budget_over = int(flow.get("budget_over_count", 0) or 0)
    budget_max = int(flow.get("budget_max", 0) or 0)
    passes_failed = int(flow.get("passes_failed", 0) or 0)
    fail_streak_max = int(flow.get("fail_streak_max", 0) or 0)
    by_endpoint_429 = flow.get("by_endpoint_429") or {}

    lines = [
        f"📊 TenderSRB — поток и обогащение за сутки ({day})",
        "",
        f"1) Темп/отказы источника: запросов {requests_total}, 429 "
        f"{requests_429} ({_pct(requests_429, requests_total)}%), пик "
        f"{peak_rps} req/s.",
    ]
    if by_endpoint_429:
        endpoint_bits = ", ".join(
            f"{name}={count}" for name, count in sorted(by_endpoint_429.items())
        )
        lines.append(f"   429 по эндпоинту: {endpoint_bits}")
    lines.extend(
        [
            f"2) Полнота обогащения (n={total}): без дедлайна {no_deadline} "
            f"({_pct(no_deadline, total)}%, порог {deadline_missing_threshold_pct()}%; "
            "в ленте живут 30 дн.) · "
            f"без описания {no_description} "
            f"({_pct(no_description, total)}%, порог {description_missing_threshold_pct()}%) · "
            f"без суммы {no_amount} "
            f"({_pct(no_amount, total)}%, порог {amount_missing_threshold_pct()}%).",
            f"3) Документов потеряно молча (429 без ретрая): {docs_lost}.",
            # ‼️ A single failed pass does not ring Telegram (the streak threshold —
            # src.workers.ingest._FAIL_STREAK_ALERT_THRESHOLD), therefore it
            # MUST be VISIBLE here: otherwise “distinguishing” would degenerate into
            # “hiding”, against the owner’s direct instruction of 20.09.2026.
            f"4) Сорванных проходов: {passes_failed}, "
            f"максимум подряд {fail_streak_max}.",
            f"5) Приток за 24ч: {total} тендеров.",
            (
                f"Бюджет запросов на карточку: превышений {budget_over}, "
                f"карточек за сутки не было."
                if total == 0
                else f"Бюджет запросов на карточку: превышений {budget_over}, "
                f"самая дорогая карточка {budget_max} запр. "
                f"(потолок {TENDER_REQUEST_CEILING})."
            ),
        ]
    )
    return "\n".join(lines)


class FlowReportWorker:
    """Once a day (at :func:`report_hour_utc`) — the summary of all four
    indicators of the flow/enrichment to the service channel."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] = session_scope,
        redis: Any | None = None,
        alert_sender: Callable[[str], Awaitable[None]] | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._flow_store = FlowMetricsStore(redis)
        self._redis = redis
        self._alert = alert_sender
        self._poll_interval = poll_interval
        self._now = now_factory or (lambda: datetime.now(timezone.utc))
        self._stopped = asyncio.Event()

    @classmethod
    def from_env(cls, **over: Any) -> "FlowReportWorker":
        from src.workers.health import WorkerHealth

        params: dict[str, Any] = {
            "alert_sender": WorkerHealth.from_env(label="flow_report").alert,
        }
        params.update(over)
        return cls(**params)

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        logger.info(
            "flow_report: polling started (interval=%.0fs, send hour=%d UTC)",
            self._poll_interval,
            report_hour_utc(),
        )
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ) as exc:  # noqa: BLE001 — observation does not break the contour
                logger.error(
                    "flow_report: cycle did not complete (%s)", type(exc).__name__
                )
            await self._sleep(self._poll_interval)

    async def poll_once(self) -> str | None:
        """Once a day (after the send hour has come) — collect and send
        the summary. Returns the text of the summary (for tests), or None if it has already been sent today,
        it is not yet time, or it could not be collected."""
        now = self._now().astimezone(timezone.utc)
        if now.hour < report_hour_utc():
            return None
        day = now.date().isoformat()
        if not await self._claim_day(day):
            return None

        try:
            completeness = await self._load_completeness()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "flow_report: the completeness measurement for %s was not collected (%s)",
                day,
                type(exc).__name__,
            )
            return None
        flow = await self._flow_store.read_day(day)

        message = build_daily_summary_text(
            day=day, completeness=completeness, flow=flow
        )
        logger.info("flow_report: the summary for %s collected", day)
        if self._alert is not None:
            try:
                await self._alert(message)
            except (
                Exception
            ) as exc:  # noqa: BLE001 — the channel does not break the worker
                logger.warning(
                    "flow_report: the summary for %s not sent (%s)",
                    day,
                    type(exc).__name__,
                )
        return message

    async def _claim_day(self, day: str) -> bool:
        """``SET NX`` in Redis — at most one summary per day. Without Redis, or on a
        failure → the claim always succeeds (better twice than never)."""
        if self._redis is None:
            return True
        key = f"flow:report_sent:{day}"
        try:
            claimed = await self._redis.set(key, "1", nx=True, ex=2 * 24 * 3600)
        except Exception:  # noqa: BLE001
            logger.warning("flow_report: Redis unavailable for the day claim %s", day)
            return True
        return bool(claimed)

    async def _load_completeness(self) -> dict[str, Any]:
        async with self._session_factory() as session:
            row = (await session.execute(_DAILY_COMPLETENESS_SQL)).first()
        if row is None:
            return {"total": 0, "no_deadline": 0, "no_amount": 0, "no_description": 0}
        return {
            "total": int(row.total or 0),
            "no_deadline": int(row.no_deadline or 0),
            "no_amount": int(row.no_amount or 0),
            "no_description": int(row.no_description or 0),
        }

    async def _sleep(self, delay: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the daily flow summary worker (the container via Compose)."""
    import os

    from src.log_secrets import install_secret_log_filter

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()

    redis_client = None
    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0")
        )
    except Exception:  # noqa: BLE001 — the summary does not have to have Redis
        logger.warning(
            "flow_report: Redis unavailable at start-up — the summary will be without the rate figures"
        )

    await FlowReportWorker.from_env(redis=redis_client).run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
