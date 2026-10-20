"""The periodicity and the selection of the repeated poll of the open tenders (src/domain/repoll.py).

Pure functions: the time is passed as an argument, there is no network and no DB. The boundary of the window and the ceiling are pinned by literals from the decision of the task repoll-open (07.10.2026).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.domain.repoll import (
    REPOLL_FAR_PERIOD,
    REPOLL_MAX_PER_PASS,
    REPOLL_WINDOW_PERIOD,
    repoll_period,
    select_due,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _period(deadline=None, published=None, first_seen=None):
    return repoll_period(
        deadline_at=deadline,
        published_at=published,
        first_seen_at=first_seen,
        now=NOW,
    )


# --------------------------------------------------------------------------- #
#  repoll_period: the window and openness
# --------------------------------------------------------------------------- #
def test_deadline_in_window_is_daily():
    assert _period(deadline=NOW + timedelta(days=10)) == REPOLL_WINDOW_PERIOD


def test_deadline_just_passed_within_grace_is_still_in_window():
    assert _period(deadline=NOW - timedelta(hours=12)) == REPOLL_WINDOW_PERIOD


def test_deadline_at_window_edge_is_in_window():
    # the boundary inclusive: exactly +30 days — still the window
    assert _period(deadline=NOW + timedelta(days=30)) == REPOLL_WINDOW_PERIOD


def test_deadline_beyond_window_is_far():
    assert _period(deadline=NOW + timedelta(days=31)) == REPOLL_FAR_PERIOD


def test_deadline_passed_more_than_a_day_ago_is_not_open():
    assert _period(deadline=NOW - timedelta(days=2)) is None


def test_no_deadline_fresh_is_far():
    assert _period(published=NOW - timedelta(days=5)) == REPOLL_FAR_PERIOD


def test_no_deadline_stale_is_not_open():
    assert _period(published=NOW - timedelta(days=45)) is None


def test_no_deadline_and_no_dates_is_not_open():
    assert _period() is None


def test_naive_datetimes_are_treated_as_utc():
    naive = datetime(2026, 10, 17, 12, 0)  # = NOW + 10 days, UTC
    assert _period(deadline=naive) == REPOLL_WINDOW_PERIOD


# --------------------------------------------------------------------------- #
#  select_due: who is polled in this pass
# --------------------------------------------------------------------------- #
def _row(uid, *, deadline=None, checked=None, published=None):
    return {
        "source_uid": uid,
        "deadline_at": deadline,
        "published_at": published,
        "first_seen_at": None,
        "checked_at": checked,
    }


def test_never_checked_goes_first():
    rows = [
        _row("a", deadline=NOW + timedelta(days=5), checked=NOW - timedelta(hours=1)),
        _row("b", deadline=NOW + timedelta(days=5), checked=None),
    ]
    due = select_due(rows, now=NOW)
    assert [r["source_uid"] for r in due] == ["b"]


def test_window_tender_checked_recently_is_not_due():
    rows = [
        _row("a", deadline=NOW + timedelta(days=5), checked=NOW - timedelta(hours=10))
    ]
    assert select_due(rows, now=NOW) == []


def test_window_tender_checked_a_day_ago_is_due():
    rows = [
        _row("a", deadline=NOW + timedelta(days=5), checked=NOW - timedelta(hours=25))
    ]
    assert [r["source_uid"] for r in select_due(rows, now=NOW)] == ["a"]


def test_far_tender_checked_a_day_ago_is_not_due_yet():
    rows = [
        _row("a", deadline=NOW + timedelta(days=60), checked=NOW - timedelta(hours=30))
    ]
    assert select_due(rows, now=NOW) == []


def test_far_tender_checked_three_days_ago_is_due():
    rows = [
        _row("a", deadline=NOW + timedelta(days=60), checked=NOW - timedelta(hours=73))
    ]
    assert [r["source_uid"] for r in select_due(rows, now=NOW)] == ["a"]


def test_closed_tender_never_due_even_if_never_checked():
    rows = [_row("a", deadline=NOW - timedelta(days=10), checked=None)]
    assert select_due(rows, now=NOW) == []


def test_oldest_checked_first_and_cap_applied():
    rows = [
        _row(
            str(i),
            deadline=NOW + timedelta(days=5),
            checked=NOW - timedelta(hours=30 + i),
        )
        for i in range(5)
    ]
    due = select_due(rows, now=NOW, limit=2)
    assert [r["source_uid"] for r in due] == ["4", "3"]


def test_default_cap_is_forty_per_pass():
    assert REPOLL_MAX_PER_PASS == 40
    rows = [_row(str(i), deadline=NOW + timedelta(days=5)) for i in range(50)]
    assert len(select_due(rows, now=NOW)) == 40
