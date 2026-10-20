"""Re-polling of open tenders: periodicity and the per-pass ceiling.

Why: the list request takes only ``PublishDate >= since``, so a tender
published before ``since`` is not re-polled after the first visit.
The submission deadline and status change later than publication (an extension, completion), and
without re-polling a stale deadline stays in the feed (a measurement of 07.10.2026:
of 1 798 open tenders of the window 1 447 were not re-polled for two days).

The solution of the repoll-open task (07.10.2026):

* the window ``[today−1 … today+30]`` — polled once per :data:`REPOLL_WINDOW_PERIOD`;
* open ones beyond the window (a deadline further than +30 days) and fresh ones without a deadline — once per
  :data:`REPOLL_FAR_PERIOD`;
* the rest (a deadline passed more than a day ago, not-fresh without a deadline) — not polled.

The ceiling :data:`REPOLL_MAX_PER_PASS` limits the number of cards per pass
(a pass runs every 1800 s, 48 per day) so that re-polling does not raise the pace
towards the portal above the current one (see ``docs/reports/TESTING_repoll-open.md``, §1).

Pure functions: no DB and no network, time is passed as an argument.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.domain.actuality import no_deadline_is_fresh

#: A deadline in the window [today−1 … today+30] — polled once a day.
REPOLL_WINDOW_PERIOD = timedelta(hours=24)
#: Open ones outside the window and fresh ones without a deadline — once every three days.
REPOLL_FAR_PERIOD = timedelta(hours=72)
#: A deadline that passed earlier than this long ago — the tender is not open, we do not poll it.
REPOLL_PAST_GRACE = timedelta(days=1)
#: The “expires soon” window: a deadline up to this boundary inclusive is in the window.
REPOLL_WINDOW_AHEAD = timedelta(days=30)
#: The ceiling of cards per pass (≈ 1 860/day at 48 passes).
REPOLL_MAX_PER_PASS = 40


def _aware(value: datetime) -> datetime:
    """naive → UTC (the contract of ``src/domain/tz.py``)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def repoll_period(
    *,
    deadline_at: datetime | None,
    published_at: datetime | None,
    first_seen_at: datetime | None,
    now: datetime,
) -> timedelta | None:
    """The re-poll period of a tender, or ``None`` if it is not open.

    ``None`` — we do not poll the tender at all: the deadline passed more than a day ago, or
    there is no deadline and the tender is not fresh (the same predicate as for the feed and matching).
    """
    now = _aware(now)
    if deadline_at is not None:
        deadline = _aware(deadline_at)
        if deadline < now - REPOLL_PAST_GRACE:
            return None
        if deadline <= now + REPOLL_WINDOW_AHEAD:
            return REPOLL_WINDOW_PERIOD
        return REPOLL_FAR_PERIOD
    if no_deadline_is_fresh(published_at, first_seen_at, now):
        return REPOLL_FAR_PERIOD
    return None


def select_due(
    rows: list[dict],
    *,
    now: datetime,
    limit: int = REPOLL_MAX_PER_PASS,
) -> list[dict]:
    """Selecting tenders to poll in this pass, the longest-unchecked first.

    ``rows`` — dicts with the keys ``source_uid``, ``deadline_at``, ``published_at``,
    ``first_seen_at``, ``checked_at`` (``tenders_raw.fetched_at`` = the stamp of the
    last check; ``None`` — never checked, goes first).
    A tender without a period (:func:`repoll_period` → ``None``) is not included.
    """
    now = _aware(now)
    due: list[tuple[datetime, dict]] = []
    for row in rows:
        period = repoll_period(
            deadline_at=row.get("deadline_at"),
            published_at=row.get("published_at"),
            first_seen_at=row.get("first_seen_at"),
            now=now,
        )
        if period is None:
            continue
        checked = row.get("checked_at")
        if checked is None:
            due.append((datetime.min.replace(tzinfo=timezone.utc), row))
            continue
        checked = _aware(checked)
        if now - checked >= period:
            due.append((checked, row))
    due.sort(key=lambda pair: pair[0])
    return [row for _, row in due[: max(limit, 0)]]
