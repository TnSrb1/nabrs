"""Tests of stats_service (ADR-013: mock session without a real DB).

We check:
- the global counters of live tenders (all_time / this_month / today / since);
- days-left for NULL (open-ended), a future and a past access_until;
- the tenant’s deliveries: total + the breakdown by profiles, including several profiles;
- an empty outbox → total=0, by_profile=[].
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.api.cabinet.stats_service import (
    get_days_left,
    get_system_stats,
    get_tenant_delivery_stats,
)

_TID = "11111111-1111-1111-1111-111111111111"
# published_at of the earliest live tender: 30.05.2026 UTC → 30.05 in Belgrade.
_SINCE_DT = datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc)


class _Result:
    def __init__(self, *, rows=None, scalar=None):
        self._rows = list(rows) if rows is not None else []
        self._scalar = scalar

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    """Mock AsyncSession: set_config does not touch the results queue."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "set_config" in sql or "app.current_tenant_id" in sql:
            return _Result()
        return self._results.pop(0) if self._results else _Result()


# --------------------------------------------------------------------------- #
# get_system_stats
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_system_stats_returns_counts():
    row = SimpleNamespace(all_time=1000, this_month=50, today=5, since=_SINCE_DT)
    s = _Session([_Result(rows=[row])])
    stats = await get_system_stats(s)
    assert stats.all_time == 1000
    assert stats.this_month == 50
    assert stats.today == 5
    # Not the min(published_at) of the row, but the start of the regular collection (owner 14.10.2026).
    assert stats.since == date(2026, 6, 2)


@pytest.mark.asyncio
async def test_system_stats_empty_table():
    row = SimpleNamespace(all_time=0, this_month=0, today=0, since=None)
    s = _Session([_Result(rows=[row])])
    stats = await get_system_stats(s)
    assert stats.all_time == 0
    assert stats.today == 0
    assert stats.since is None


@pytest.mark.asyncio
async def test_system_stats_no_row():
    s = _Session([_Result(rows=[])])
    stats = await get_system_stats(s)
    assert stats.all_time == 0


# --------------------------------------------------------------------------- #
# get_days_left
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_days_left_null_is_unlimited():
    s = _Session([_Result(rows=[SimpleNamespace(access_until=None)])])
    result = await get_days_left(s, _TID)
    assert result.days is None
    assert result.label == "бессрочно"


@pytest.mark.asyncio
async def test_days_left_future_date():
    future = datetime.now(timezone.utc) + timedelta(days=10)
    s = _Session([_Result(rows=[SimpleNamespace(access_until=future)])])
    result = await get_days_left(s, _TID)
    assert result.days is not None
    assert result.days > 0
    assert "дн." in result.label


@pytest.mark.asyncio
async def test_days_left_past_date_expired():
    past = datetime.now(timezone.utc) - timedelta(days=3)
    s = _Session([_Result(rows=[SimpleNamespace(access_until=past)])])
    result = await get_days_left(s, _TID)
    assert result.days is not None
    assert result.days <= 0
    assert result.label == "доступ истёк"


@pytest.mark.asyncio
async def test_days_left_exactly_tomorrow():
    tomorrow = datetime.now(timezone.utc) + timedelta(hours=23)
    s = _Session([_Result(rows=[SimpleNamespace(access_until=tomorrow)])])
    result = await get_days_left(s, _TID)
    assert result.days == 1


@pytest.mark.asyncio
async def test_days_left_no_row():
    s = _Session([_Result(rows=[])])
    result = await get_days_left(s, _TID)
    assert result.days is None
    assert result.label == "бессрочно"


# --------------------------------------------------------------------------- #
# get_tenant_delivery_stats
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tenant_stats_multiple_profiles():
    rows = [
        SimpleNamespace(profile_id="aaaa", profile_name="Строительство", delivered=30),
        SimpleNamespace(profile_id="bbbb", profile_name="Без названия", delivered=5),
    ]
    s = _Session([_Result(rows=rows)])
    stats = await get_tenant_delivery_stats(s, _TID)
    assert stats.total == 35
    assert len(stats.by_profile) == 2
    assert stats.by_profile[0].profile_name == "Строительство"
    assert stats.by_profile[0].count == 30
    assert stats.by_profile[1].count == 5


@pytest.mark.asyncio
async def test_tenant_stats_empty_outbox():
    s = _Session([_Result(rows=[])])
    stats = await get_tenant_delivery_stats(s, _TID)
    assert stats.total == 0
    assert stats.by_profile == []


@pytest.mark.asyncio
async def test_tenant_stats_single_profile():
    rows = [SimpleNamespace(profile_id="cccc", profile_name="Медицина", delivered=12)]
    s = _Session([_Result(rows=rows)])
    stats = await get_tenant_delivery_stats(s, _TID)
    assert stats.total == 12
    assert stats.by_profile[0].profile_id == "cccc"
