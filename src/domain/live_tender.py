"""The “live tender” predicate and the start of regular collection — ONE for the cabinet dashboard and the ``/statistika`` storefront.

“Live” = published no earlier than a year before we saw it. The archive
(the 2019–2020 import in production) either has no ``published_at`` (NULL → the predicate is NULL → the row
falls outside FILTER), or was published a year+ before ``first_seen_at``.

The data start date is :data:`REGULAR_SINCE`, a constant, NOT ``min(published_at)``.
Earlier “no date is written as a literal”: the start was taken from the data. This gave
30.05.2026 — the tail of the initial load (May = 210 tenders against a norm of 150–250 PER
DAY), and the first months of the storefront were incomplete. Now the date is recorded deliberately: it is
owner’s decision of 14.10.2026 "statistics only on complete data, counting from the day
from which collection runs regularly", and it cannot be derived from ``min()`` (the tail of the load
has the same ``published_at``). Both the dashboard counters (``since``) and the storefront period take
it from here (invariant #645: one date on both surfaces).

The module returns an SQL fragment, not an ORM expression: both places build a raw ``text()``.
"""

from __future__ import annotations

from datetime import date, datetime, time

from src.domain.tz import local_to_utc

#: The first day of REGULAR collection (Europe/Belgrade). An owner measurement of 14.10.2026 06:20 UTC,
#: SQL on production, jnportal, ``published_at::date``: 30.05 = 204 (the tail of the initial
#: load), 31.05 = 6, 01.06 = 10 (a working-day norm of 150–250), from 02.06 = 148, 196,
#: 174… — an even flow. By ``first_seen_at`` the initial load was 03–04.06.
REGULAR_SINCE = date(2026, 6, 2)


def live_tender_predicate(alias: str = "") -> str:
    """The SQL liveness condition; ``alias`` is the table prefix (``"t"`` → ``t.published_at``)."""
    p = f"{alias}." if alias else ""
    return f"{p}published_at >= {p}first_seen_at - interval '365 days'"


def regular_since_utc() -> datetime:
    """Midnight of :data:`REGULAR_SINCE` in Belgrade, aware UTC."""
    return local_to_utc(datetime.combine(REGULAR_SINCE, time.min))


def regular_tender_predicate(alias: str = "") -> str:
    """The statistics population: a live tender published since :data:`REGULAR_SINCE`.

    The boundary is midnight in Belgrade (``AT TIME ZONE`` in SQL, DST-correct); the literal
    is built from the module’s ``date``, not from input.
    """
    p = f"{alias}." if alias else ""
    since = f"CAST('{REGULAR_SINCE.isoformat()}' AS timestamp) AT TIME ZONE 'Europe/Belgrade'"
    return f"{live_tender_predicate(alias)} AND {p}published_at >= ({since})"
