"""The “burning” boundary (≤7 d.) on fixed clocks: before/after DST and at midnight in Belgrade.

“Days to deadline” — the difference of CALENDAR dates in Belgrade time (``src/domain/tz.py``
``calendar_days_until``). The window of the section “Deadline soon” and the colored badge must
give one answer (``templating._days_left``). Expectations — LITERALS, computed
by the calendar manually: deadlines are written as UTC moments, clocks are frozen.

Why it matters: a deadline “now + 8×24 h” across the switch to winter time (25.10.2026,
01:00 UTC) is 7 calendar days if “now” in Belgrade is before 01:01. Therefore the
“8 days” boundary floated with the hour of the day (see docs/reports/TESTING_feed-boundary-clock.md).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api import templating
from src.api.cabinet.feed_service import FEED_URGENT_WINDOW_DAYS, split_feed_by_urgency


def _utc(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


# (now UTC, deadline UTC, expected calendar days in Belgrade time).
# Deadline = “now + 8 days − 1 min”, written as a literal; in the comment — local time.
CASES = [
    # 17.10 14:00 CEST → 25.10 12:59 CET: 8 calendar days, before DST.
    ("2026-10-17 12:00", "2026-10-25 11:59", 8),
    # 18.10 02:15 CEST → 26.10 01:14 CET: 8, DST inside the window, but hour ≥ 01:01.
    ("2026-10-18 00:15", "2026-10-26 00:14", 8),
    # 18.10 00:24 CEST → 25.10 23:23 CET: “+8×24 h” across DST = 7 calendar days.
    ("2026-10-17 22:24", "2026-10-25 22:23", 7),
    # 17.10 23:59 CEST → 25.10 22:58 CET: today is still 17.10, the deadline is 8 days.
    ("2026-10-17 21:59", "2026-10-25 21:58", 8),
    # 18.10 00:00 CEST (midnight) → 25.10 22:59 CET: 7.
    ("2026-10-17 22:00", "2026-10-25 21:59", 7),
    # 18.10 00:59 CEST → 25.10 23:58 CET: 7.
    ("2026-10-17 22:59", "2026-10-25 22:58", 7),
    # 18.10 01:00 CEST → 25.10 23:59 CET: 7 (the deadline is shifted to 23:59 — calendar 25.10).
    ("2026-10-17 23:00", "2026-10-25 22:59", 7),
    # 25.10 01:30 CEST (before the switch) → 02.11 00:29 CET: 8, DST inside the window, but hour ≥ 01:01.
    ("2026-10-24 23:30", "2026-11-01 23:29", 8),
    # 26.10 00:30 CET (after the switch) → 03.11 00:29 CET: 8, DST is already behind.
    ("2026-10-25 23:30", "2026-11-02 23:29", 8),
    # 18.10 02:14 CEST → 25.10 02:13 CEST (before the switch): 7 — burning.
    ("2026-10-18 00:14", "2026-10-25 00:13", 7),
]


@pytest.mark.parametrize(("now", "deadline", "expected"), CASES)
def test_days_left_is_literal_calendar_days(freeze_feed_clock, now, deadline, expected):
    freeze_feed_clock(_utc(now))
    assert templating._days_left(_utc(deadline)) == expected


@pytest.mark.parametrize(("now", "deadline", "expected"), CASES)
def test_section_and_badge_agree_on_the_window(
    freeze_feed_clock, now, deadline, expected
):
    """The “burning” section and the badge — one predicate: the section contains exactly the tenders for which the badge is “burning”."""
    freeze_feed_clock(_utc(now))
    item = SimpleNamespace(tender_id="x", deadline_at=_utc(deadline))
    in_urgent = bool(split_feed_by_urgency([item]).urgent)
    assert in_urgent == (0 <= expected <= FEED_URGENT_WINDOW_DAYS)
    assert in_urgent == (0 <= templating._days_left(_utc(deadline)) <= 7)


def test_badge_filter_is_the_same_predicate_as_section():
    """The badge in the templates takes the ``days_left`` filter — it must be the same ``_days_left``."""
    assert templating.templates.env.filters["days_left"] is templating._days_left


def test_window_constant_matches_badge_threshold():
    """The section threshold (``FEED_URGENT_WINDOW_DAYS``) and the badge color (``<= 7`` in the templates) — 7."""
    assert FEED_URGENT_WINDOW_DAYS == 7
