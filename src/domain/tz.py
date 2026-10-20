"""The project’s single time contract: in the DB and in logic — UTC ONLY.

Invariant: any moment of time is stored and compared in UTC (``timestamptz``,
the DB server in ``Etc/UTC``, ``datetime.now(timezone.utc)`` in code). Conversion to
local time (``Europe/Belgrade``) is done EXACTLY at the presentation boundary —
before output to a human (a bot/e-mail card, a cabinet template, a CSV export) — and NOWHERE
else. The reverse conversion (:func:`local_to_utc`) is applied only at ingest,
where the source gives Serbian wall-clock time without an offset (the eNabavke HTML card:
“06.08.2026 11:00:00” is 11:00 in Belgrade, not UTC).

Why a separate module: before, these two ends were out of sync — ingest
wrote wall-clock time as UTC (a shift of 1–2 h into the past), while the display printed the raw
UTC value without conversion; the two errors compensated each other in the UI but broke
every comparison with the real ``now()`` (feed, routing, reminders). One module
= one function per meaning, so that the ends no longer diverge.

DST is handled by :class:`zoneinfo.ZoneInfo` itself (no hardcoded “+2 hours”: in winter in
Serbia CET = +1, in summer CEST = +2). The ``tz`` parameter is built into the signatures for
a future per-tenant conversion (``tenants.tz``); for now the default everywhere is Belgrade.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import overload
from zoneinfo import ZoneInfo

# The single presentation timezone in the project (the per-tenant ``tenants.tz`` is
# a separate task, the field exists, but the UI is not yet converted by it).
SERBIA_TZ = ZoneInfo("Europe/Belgrade")


def local_to_utc(naive: datetime, tz: ZoneInfo = SERBIA_TZ) -> datetime:
    """Wall-clock local time (naive) → aware UTC.

    Treats ``naive`` as wall-clock hours in the zone ``tz`` and converts to UTC. The source is
    the eNabavke HTML card, which gives “DD.MM.YYYY HH:MM:SS” without an offset
    (Serbian local time).

    DST transitions: an ambiguous (the autumn “double” hour) and a non-existent (the spring
    “gap”) wall-clock time do NOT raise — we take ``fold=0`` (the default behavior of
    :meth:`datetime.astimezone`): for the double — the first (summer)
    occurrence of the hour, for the gap — a shift forward by the size of the transition. Procurement
    deadlines are not set for 02:00–03:00 of the clock-change night, so the choice between
    the transition branches does not affect real data; what matters is only not to crash.

    An already aware input (insurance: in real ingest only a naive one from
    ``strptime`` arrives here) is brought to UTC as is, without reinterpreting wall-clock hours.
    """
    if naive.tzinfo is not None:
        return naive.astimezone(timezone.utc)
    return naive.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)


@overload
def to_local(dt: datetime, tz: ZoneInfo = ...) -> datetime: ...
@overload
def to_local(dt: None, tz: ZoneInfo = ...) -> None: ...
def to_local(dt: datetime | None, tz: ZoneInfo = SERBIA_TZ) -> datetime | None:
    """A moment from the DB → aware time in the zone ``tz`` for display to a human.

    The only way to get local time for output. ``None`` → ``None``
    (convenient for optional fields). A naive ``dt`` is treated as UTC (only UTC is
    stored in the DB; naive is not-yet-marked UTC, the same contract as in
    ``templating._days_left``). An aware ``dt`` is converted to ``tz`` from its own zone.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def calendar_days_until(
    moment: datetime, now: datetime, tz: ZoneInfo = SERBIA_TZ
) -> int:
    """The difference of CALENDAR dates of ``moment`` and ``now`` in the zone ``tz`` (Belgrade by default).

    A single rule “how many days to the deadline” for all places of display: a deadline today by the
    local calendar → ``0`` (“ističe danas”), tomorrow → ``1``, yesterday → ``-1``.
    Not ``ceil(hours / 24)``: on 30.09.2026 at 10:40 in Belgrade a deadline of "13:00 the same
    day" thus gave ``1`` and the card wrote “ističe sutra”. Naive datetimes are UTC.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (moment.astimezone(tz).date() - now.astimezone(tz).date()).days


def _as_utc(dt: datetime) -> datetime:
    """naive → aware (UTC); aware stays as is. A tz-safe date comparison."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def is_midnight(dt: datetime) -> bool:
    """Is ``dt`` a midnight stub (exactly 00:00:00.000)?

    For some tenders eNabavke gives PublishDate as midnight of the export day —
    the real publication time is lost (a stub). Such a ``published_at``
    is UNreliable for comparison with the deadline (see `deadline_before_published`).
    A check by wall-clock components is tz-agnostic.
    """
    return dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0


def deadline_before_published(
    deadline_at: datetime | None, published_at: datetime | None
) -> bool:
    """Is the deadline EARLIER than published? (both set). tz-safe (like _passes_since).

    An eNabavke artifact (Q-29): for some tenders PublishDate is a midnight
    stub of the export day, while the deadline from the HTML card is real and in the PAST. If
    at least one is None — there is nothing to compare → False (we do not reject).

    Mitigation (the stale-tender fix): if ``published_at`` is a midnight stub
    (00:00:00), the real publication time is lost → the comparison is UNreliable, we do NOT
    zero the deadline (return False). Otherwise finished tenders with a stub publication
    lost their real deadline date. With a NON-zero publication time the behavior
    is as before (deadline earlier → True → zeroing further up the stack).
    """
    if deadline_at is None or published_at is None:
        return False
    if is_midnight(published_at):
        return False
    return _as_utc(deadline_at) < _as_utc(published_at)


def belgrade_days_within_sql(column: str, days: str, now: str = "now()") -> str:
    """SQL predicate "deadline within ≤ ``days`` calendar days in Belgrade".

    SQL twin of :func:`calendar_days_until`: the same difference of CALENDAR DATES in
    ``Europe/Belgrade``, not ``now() + interval 'N days'`` (that is 7×24 h, and in the
    morning a deadline of "day+7, 23:00" falls out of the window although by the calendar it is 7 days).
    ``days`` and ``now`` are SQL expressions (``:window_days``, ``7``, ``now()``);
    ``now`` is parametrised so that the window boundary can be checked at a fixed
    moment. Past deadlines are not cut off here — the caller does that (``> now()``).
    """
    return (
        f"(({column} AT TIME ZONE 'Europe/Belgrade')::date"
        f" - ({now} AT TIME ZONE 'Europe/Belgrade')::date) <= CAST({days} AS int)"
    )
