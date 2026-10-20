"""Window of "upcoming deadlines" — calendar days in Europe/Belgrade (issue #810).

A deadline "day+7, 23:00" must fall into the 7-day window both in the morning (07:00) and
in the afternoon (14:00): by the calendar this is 7 days. The old 7×24 h dropped it in the morning —
the tile and the agenda diverged from the calendar grid by hundreds of tenders.

Dates are fixed (18.10.2026, Belgrade = CEST, UTC+2), expectations are literals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.api.cabinet import feed_service

# 18.10.2026 07:00 CEST = 05:00 UTC; 14:00 CEST = 12:00 UTC.
NOW_MORNING = datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc)
NOW_AFTERNOON = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
# 25.10.2026 23:00 CET = 22:00 UTC (DST changed on 25.10 at 03:00 — day+7).
DEADLINE_DAY7_2300 = datetime(2026, 10, 25, 22, 0, tzinfo=timezone.utc)
# 26.10.2026 00:30 CET = 25.10 23:30 UTC (day+8 by the calendar).
DEADLINE_DAY8_0030 = datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)


def _deliv(tender_id: str, deadline: datetime):
    return SimpleNamespace(
        tender_id=tender_id,
        profile_id=None,
        title=f"t-{tender_id}",
        customer_name="Заказчик",
        deadline_at=deadline,
        source_key=None,
        tender_url=None,
    )


def _ids(events) -> set[str]:
    return {e.tender_id for e in events}


def test_day7_2300_in_7day_window_at_morning():
    events = feed_service.build_calendar_events(
        deadlines=[_deliv("d7", DEADLINE_DAY7_2300)],
        now=NOW_MORNING,
        horizon_days=7,
    )
    assert _ids(events) == {"d7"}
    assert events[0].days_left == 7


def test_day7_2300_in_7day_window_at_afternoon():
    events = feed_service.build_calendar_events(
        deadlines=[_deliv("d7", DEADLINE_DAY7_2300)],
        now=NOW_AFTERNOON,
        horizon_days=7,
    )
    assert _ids(events) == {"d7"}
    assert events[0].days_left == 7


def test_day8_0030_outside_7day_window_at_morning():
    events = feed_service.build_calendar_events(
        deadlines=[_deliv("d8", DEADLINE_DAY8_0030)],
        now=NOW_MORNING,
        horizon_days=7,
    )
    assert _ids(events) == set()


def test_urgent_events_keeps_day7_at_morning():
    events = feed_service.build_calendar_events(
        deadlines=[_deliv("d7", DEADLINE_DAY7_2300), _deliv("d8", DEADLINE_DAY8_0030)],
        now=NOW_MORNING,
    )
    urgent = feed_service.urgent_events(events)
    assert _ids(urgent) == {"d7"}
