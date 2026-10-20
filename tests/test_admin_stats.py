"""Tests of the statistics and the pagination of admin_service (TASK admin-stats).

Covers:
- UserRow.days_left: NULL / future / past / boundary cases
- list_users: the computation of the offset by the page number, clamp page<=0
- get_platform_stats: the mapping of the SQL row → PlatformStats, NULL → 0
- get_user_delivered_count: the normal return, NULL → 0
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.admin.admin_service import (
    PAGE_SIZE,
    PlatformStats,
    UserRow,
    get_platform_stats,
    get_user_delivered_count,
    list_users,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_UID = "00000000-0000-0000-0000-000000000001"
_TID = "00000000-0000-0000-0000-000000000002"


def _user_row(access_until) -> UserRow:
    return UserRow(
        id=_UID,
        email="test@example.com",
        created_at=None,
        tenant_id=_TID,
        tier="pro",
        access_until=access_until,
        profile_count=2,
    )


def _rows_mock(rows=None):
    m = MagicMock()
    m.all.return_value = rows or []
    return m


def _scalar_mock(value):
    m = MagicMock()
    m.scalar_one.return_value = value
    return m


def _first_mock(row):
    m = MagicMock()
    m.first.return_value = row
    return m


# --------------------------------------------------------------------------- #
# UserRow.days_left
# --------------------------------------------------------------------------- #


def test_days_left_none_access_until():
    """access_until IS NULL → days_left is None (permanent)."""
    u = _user_row(None)
    assert u.days_left is None


def test_days_left_future():
    """access_until in the future → days_left > 0."""
    future = datetime.now(timezone.utc) + timedelta(days=10)
    u = _user_row(future)
    assert u.days_left is not None
    assert u.days_left > 0


def test_days_left_past():
    """access_until in the past → days_left <= 0."""
    past = datetime.now(timezone.utc) - timedelta(days=5)
    u = _user_row(past)
    assert u.days_left is not None
    assert u.days_left <= 0


def test_days_left_less_than_one_day():
    """23 hours to the end → days_left == 1 (ceiling)."""
    future = datetime.now(timezone.utc) + timedelta(hours=23)
    u = _user_row(future)
    assert u.days_left == 1


def test_days_left_exactly_7_days():
    """About 7 days → days_left == 7."""
    # a small margin to compensate for the time of the test run
    future = datetime.now(timezone.utc) + timedelta(days=7, seconds=-1)
    u = _user_row(future)
    assert u.days_left == 7


def test_days_left_just_expired():
    """1 second ago → days_left == 0 (ceiling(−1/86400) = 0)."""
    just_past = datetime.now(timezone.utc) - timedelta(seconds=1)
    u = _user_row(just_past)
    # ceil(-1/86400) = ceil(-0.0000115...) = 0
    assert u.days_left == 0


# --------------------------------------------------------------------------- #
# list_users — pagination
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_users_page1_offset_zero():
    """page=1 → offset=0, limit=PAGE_SIZE."""
    db = AsyncMock()
    db.execute.side_effect = [_rows_mock(), _scalar_mock(0)]

    users, total = await list_users(db, page=1)

    assert users == []
    assert total == 0
    call_params = db.execute.call_args_list[0].args[1]
    assert call_params["offset"] == 0
    assert call_params["limit"] == PAGE_SIZE


@pytest.mark.asyncio
async def test_list_users_page2_offset():
    """page=2 → offset=PAGE_SIZE."""
    db = AsyncMock()
    db.execute.side_effect = [_rows_mock(), _scalar_mock(45)]

    _, total = await list_users(db, page=2)

    assert total == 45
    call_params = db.execute.call_args_list[0].args[1]
    assert call_params["offset"] == PAGE_SIZE


@pytest.mark.asyncio
async def test_list_users_page3_offset():
    """page=3 → offset=2*PAGE_SIZE."""
    db = AsyncMock()
    db.execute.side_effect = [_rows_mock(), _scalar_mock(100)]

    await list_users(db, page=3)

    call_params = db.execute.call_args_list[0].args[1]
    assert call_params["offset"] == 2 * PAGE_SIZE


@pytest.mark.asyncio
async def test_list_users_page_clamped_to_one():
    """page=0 → it is brought to 1, offset=0."""
    db = AsyncMock()
    db.execute.side_effect = [_rows_mock(), _scalar_mock(0)]

    await list_users(db, page=0)

    call_params = db.execute.call_args_list[0].args[1]
    assert call_params["offset"] == 0


@pytest.mark.asyncio
async def test_list_users_total_none_to_zero():
    """scalar_one returned None → total=0."""
    db = AsyncMock()
    db.execute.side_effect = [_rows_mock(), _scalar_mock(None)]

    _, total = await list_users(db, page=1)

    assert total == 0


# --------------------------------------------------------------------------- #
# get_platform_stats
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_platform_stats_maps_correctly():
    """The counters from the SQL are correctly displayed in PlatformStats."""
    db = AsyncMock()
    tender_row = MagicMock()
    tender_row.total_all_time = 1000
    tender_row.total_month = 200
    tender_row.total_day = 15
    db.execute.side_effect = [_first_mock(tender_row), _scalar_mock(42)]

    stats = await get_platform_stats(db)

    assert isinstance(stats, PlatformStats)
    assert stats.tenders_all_time == 1000
    assert stats.tenders_month == 200
    assert stats.tenders_day == 15
    assert stats.users_total == 42


@pytest.mark.asyncio
async def test_get_platform_stats_null_to_zero():
    """NULL in the DB (empty tables) → 0 in PlatformStats."""
    db = AsyncMock()
    tender_row = MagicMock()
    tender_row.total_all_time = None
    tender_row.total_month = None
    tender_row.total_day = None
    db.execute.side_effect = [_first_mock(tender_row), _scalar_mock(None)]

    stats = await get_platform_stats(db)

    assert stats.tenders_all_time == 0
    assert stats.tenders_month == 0
    assert stats.tenders_day == 0
    assert stats.users_total == 0


# --------------------------------------------------------------------------- #
# get_user_delivered_count
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_user_delivered_count_normal():
    """scalar_one returned 7 → count == 7."""
    db = AsyncMock()
    db.execute.return_value = _scalar_mock(7)

    count = await get_user_delivered_count(db, _TID)

    assert count == 7


@pytest.mark.asyncio
async def test_get_user_delivered_count_null_to_zero():
    """COUNT returns NULL (an empty table) → 0."""
    db = AsyncMock()
    db.execute.return_value = _scalar_mock(None)

    count = await get_user_delivered_count(db, _TID)

    assert count == 0
