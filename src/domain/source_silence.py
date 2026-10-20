"""Source silence: hours of quiet and the “time to alert” decision (SRC-0d).

One predicate per meaning: it is called by both the ingest worker (``_check_silence`` — alerts to
Telegram/email) and the admin (the banner + the source row in ``/admin/sources``).
Before, the threshold lived only in the worker — the admin would not see what the
alert sees. The module is pure: no DB, no network, no parser imports.

Owner decision 30.09.2026 (evening) items 4–5: silence is counted from ``enabled_at``
(a source without a single tender does not ring right after being enabled), the threshold is per
source (the adapter attribute ``silence_threshold_hours``, otherwise the common one).
"""

from __future__ import annotations

from datetime import datetime


def silence_hours(
    now: datetime, last_tender_at: datetime | None, enabled_at: datetime | None
) -> float | None:
    """Hours of quiet: from the last tender, and if there were no tenders — from ``enabled_at``.

    ``None`` — there is no reference point at all (no tenders and no ``enabled_at``): the worker
    treats this as “silent infinitely” (the row was enabled bypassing the contract).
    """
    reference = last_tender_at if last_tender_at is not None else enabled_at
    if reference is None:
        return None
    return max(0.0, (now - reference).total_seconds() / 3600.0)


def is_silent(hours: float | None, threshold_hours: float) -> bool:
    """``None`` (no reference point) — silent; otherwise quiet ≥ the threshold."""
    return hours is None or hours >= threshold_hours
