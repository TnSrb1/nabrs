"""The daily reconciliation of the number of notices with the source (M2, ``docs/development/SOURCE_MONITORING_2026-10-15.md``).

For each ENABLED source whose adapter can ``count_published_on`` (jnportal, ted) we take the last ``RECONCILE_DAYS`` full days of publication: the number at the source itself against ``tenders_normalized``. The result is written into ``source_reconcile`` (the history); on a divergence > ``max(2, 2%)`` — an alert through the same service channel as ``_check_silence`` (the Telegram admin bot + an e-mail). It catches the silent losses of the window, the shortfall against totalCount, an IP block by a stub, and a fall of the volume without a zero.

It is called by ``IngestWorker.run_once`` (after the night batch, from 01:30 UTC, once a day): there is no separate container and no compose edits. The write goes under the superuser of ingest (``tendersrb_app`` on ``source_reconcile`` only reads, migration 0055).

The anti-flapping — through the DB, survives a restart: the alert for (source, day) goes out only if the previous reconciliation of this day had no divergence (or there was none). Each of the 3 days is re-reconciled daily — a closed divergence silently disappears from the admin view.

RELIABILITY CONTRACT: a failure of the reconciliation of one source/day — a WARNING, and on; the observation does not break ingest. The live requests — no more often than ``_PAUSE_SEC`` (≤1/s, the boundary of the brief).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import text

from src.domain.source_reconcile import (
    RECONCILE_DAYS,
    build_reconcile_alert_text,
    is_mismatch,
)
from src.parsers.base import SourceAdapter

logger = logging.getLogger(__name__)

#: Earlier than this time (UTC) we do not reconcile: the night batch of the portal — 22:00-01:00 UTC.
RECONCILE_START_UTC = time(1, 30)
_PAUSE_SEC = 1.1
#: After a failure of the source do not repeat more often than once an hour (the ingest pass runs every few minutes).
_RETRY_AFTER_FAIL = timedelta(hours=1)

_OUR_COUNT_SQL = text("""
    SELECT COUNT(*) AS n
    FROM tenders_normalized
    WHERE source_key = CAST(:source_key AS text)
      AND (published_at AT TIME ZONE CAST(:tz AS text))::date = CAST(:day AS date)
    """)
_PREV_SQL = text("""
    SELECT source_count, our_count
    FROM source_reconcile
    WHERE source_key = CAST(:source_key AS text) AND day = CAST(:day AS date)
    """)
_LAST_CHECK_SQL = text("""
    SELECT MAX(checked_at) AS last_at
    FROM source_reconcile
    WHERE source_key = CAST(:source_key AS text)
    """)
_UPSERT_SQL = text("""
    INSERT INTO source_reconcile (source_key, day, source_count, our_count, checked_at)
    VALUES (CAST(:source_key AS text), CAST(:day AS date),
            CAST(:source_count AS integer), CAST(:our_count AS integer),
            CAST(:checked_at AS timestamptz))
    ON CONFLICT (source_key, day) DO UPDATE
        SET source_count = EXCLUDED.source_count,
            our_count = EXCLUDED.our_count,
            checked_at = EXCLUDED.checked_at
    """)


@dataclass
class ReconcileResult:
    source_key: str
    day: date
    source_count: int
    our_count: int
    mismatch: bool
    alerted: bool = False


def reconcile_days(now: datetime) -> list[date]:
    """The last ``RECONCILE_DAYS`` full days of publication (UTC), old → new.

    Today is incomplete (the night batch runs until 01:00 and later) — we do not reconcile.
    """
    today = now.astimezone(timezone.utc).date()
    return [today - timedelta(days=n) for n in range(RECONCILE_DAYS, 0, -1)]


def due_today(now: datetime) -> bool:
    """The window of the daily reconciliation is open: after 01:30 UTC."""
    return now.astimezone(timezone.utc).time() >= RECONCILE_START_UTC


class SourceReconciler:
    """The reconciliation of the sources; the dependencies are injected (as for the other workers)."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        alert: Callable[[str], Awaitable[None]],
        email: Callable[[str, str], Awaitable[Any]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._alert = alert
        self._email = email
        self._sleep = sleep
        self._failed_at: dict[str, datetime] = {}

    async def _already_done_today(self, source_key: str, now: datetime) -> bool:
        window_start = datetime.combine(
            now.astimezone(timezone.utc).date(), RECONCILE_START_UTC, timezone.utc
        )
        async with self._session_factory() as session:
            row = (
                await session.execute(_LAST_CHECK_SQL, {"source_key": source_key})
            ).first()
        last = row.last_at if row is not None else None
        return last is not None and last >= window_start

    async def reconcile_enabled(
        self,
        sources: Sequence[tuple[str, str | None, SourceAdapter]],
        *,
        now: datetime,
    ) -> list[ReconcileResult]:
        """Reconciles the passed (enabled) sources; not on time / already reconciled — a skip."""
        if not due_today(now):
            return []
        results: list[ReconcileResult] = []
        for source_key, label, adapter in sources:
            if type(adapter).count_published_on is SourceAdapter.count_published_on:
                continue  # a source without a reconciliation method — is not reconciled
            failed = self._failed_at.get(source_key)
            if failed is not None and now - failed < _RETRY_AFTER_FAIL:
                continue
            try:
                if await self._already_done_today(source_key, now):
                    continue
                results.extend(
                    await self._reconcile_one(source_key, label, adapter, now)
                )
            except Exception as exc:  # noqa: BLE001 — monitoring does not break ingest
                self._failed_at[source_key] = now
                logger.warning(
                    "reconcile[%s]: the reconciliation failed (%s)",
                    source_key,
                    type(exc).__name__,
                )
        return results

    async def _reconcile_one(
        self,
        source_key: str,
        label: str | None,
        adapter: SourceAdapter,
        now: datetime,
    ) -> list[ReconcileResult]:
        out: list[ReconcileResult] = []
        for i, day in enumerate(reconcile_days(now)):
            if i:
                await self._sleep(_PAUSE_SEC)
            out.append(await self.check_day(source_key, label, adapter, day, now))
        return out

    async def measure(
        self, source_key: str, adapter: SourceAdapter, day: date
    ) -> tuple[int, int] | None:
        """(at the source, at ours) for the day; ``None`` — the source is not reconciled. WITHOUT a write."""
        source_count = await adapter.count_published_on(day)
        if source_count is None:
            return None
        async with self._session_factory() as session:
            our = (
                await session.execute(
                    _OUR_COUNT_SQL,
                    {
                        "source_key": source_key,
                        "tz": adapter.reconcile_tz,
                        "day": day,
                    },
                )
            ).scalar_one()
        return int(source_count), int(our)

    async def check_day(
        self,
        source_key: str,
        label: str | None,
        adapter: SourceAdapter,
        day: date,
        now: datetime,
    ) -> ReconcileResult:
        measured = await self.measure(source_key, adapter, day)
        if measured is None:
            raise RuntimeError("count_published_on returned None")
        source_count, our_count = measured
        mismatch = is_mismatch(source_count, our_count)
        async with self._session_factory() as session:
            prev = (
                await session.execute(_PREV_SQL, {"source_key": source_key, "day": day})
            ).first()
            await session.execute(
                _UPSERT_SQL,
                {
                    "source_key": source_key,
                    "day": day,
                    "source_count": source_count,
                    "our_count": our_count,
                    "checked_at": now,
                },
            )
            await session.commit()
        prev_mismatch = prev is not None and is_mismatch(
            int(prev.source_count), int(prev.our_count)
        )
        result = ReconcileResult(source_key, day, source_count, our_count, mismatch)
        if mismatch and not prev_mismatch:
            text_ = build_reconcile_alert_text(
                source_key=source_key,
                source_label=label,
                day=day,
                source_count=source_count,
                our_count=our_count,
            )
            await self._alert(text_)
            if self._email is not None:
                try:
                    await self._email(
                        f"TenderSRB: сверка источника «{label or source_key}»", text_
                    )
                except Exception as exc:  # noqa: BLE001 — a mail failure does not break the reconciliation
                    logger.warning(
                        "reconcile[%s]: the e-mail alert did not go out (%s)",
                        source_key,
                        type(exc).__name__,
                    )
            result.alerted = True
        return result
