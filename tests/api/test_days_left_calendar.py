"""“How many days until the deadline” — the difference of CALENDAR dates in Belgrade time (newcomer_ux step 4).

Fact 30.09.2026 ~10:40 Belgrade: a card with the deadline “30.09.2026 13:00” showed
“Ističe sutra” — ``ceil(2.3 h / 24 h) = 1``. Rule: today by the calendar → 0,
tomorrow → 1, already passed → negative. “Now” is frozen — the test does not depend
on the launch time.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.api import templating

# 30.09.2026 10:40 Belgrade (CEST, +2) = 08:40 UTC.
_NOW = datetime(2026, 9, 30, 8, 40, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return _NOW if tz is not None else _NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setattr(templating, "datetime", _FrozenDatetime)


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("deadline", "expected"),
    [
        # 13:00 Belgrade today (11:00 UTC) — prod case: “danas”, not “sutra”.
        (_utc(2026, 9, 30, 11), 0),
        # 23:59 Belgrade today = 21:59 UTC — still today.
        (_utc(2026, 9, 30, 21, 59), 0),
        # 00:30 Belgrade tomorrow = 22:30 UTC today — by the calendar it is already tomorrow.
        (_utc(2026, 9, 30, 22, 30), 1),
        # 09:00 Belgrade tomorrow (< 24 h from “now”) — tomorrow, not “today”.
        (_utc(2026, 10, 1, 7), 1),
        # 3.1 days ahead by the clock, 3 by the calendar (was ceil → 4).
        (_utc(2026, 10, 3, 11), 3),
    ],
)
def test_days_left_is_calendar_days_in_belgrade(deadline, expected):
    assert templating._days_left(deadline) == expected


def test_deadline_passed_earlier_today_is_expired():
    # 09:00 Belgrade today (07:00 UTC) has already passed — “istekao”, not “danas”.
    assert templating._days_left(_utc(2026, 9, 30, 7)) < 0


def test_deadline_yesterday_is_negative():
    assert templating._days_left(_utc(2026, 9, 29, 10)) == -1


def test_naive_is_utc():
    assert templating._days_left(datetime(2026, 9, 30, 11)) == 0
