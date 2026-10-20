"""Tests of VG-07 phase-1 — src.analytics.reprocurement.

Without a real DB: a mock session with prepared rows.
We cover:
- months_until: boundary cases (today=0, +1 month, a past date → the SQL cuts it off)
- get_reprocurement_forecast: suitable rows get in; the SQL filters itself
- graceful-empty: an empty fetchall → []
- name fallback: customer_pib is empty, customer_name is set → the by_name path
- an amount None and zero → not shown
- expiry as datetime (asyncpg may return a datetime) → is normalized
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.analytics.reprocurement import (
    ReprocurementRow,
    get_reprocurement_forecast,
    months_until,
)

# --------------------------------------------------------------------------- #
#  months_until — a pure function, tested directly
# --------------------------------------------------------------------------- #


def test_months_until_today_is_zero():
    today = date(2026, 7, 2)
    assert months_until(today, today) == 0


def test_months_until_one_month():
    today = date(2026, 7, 2)
    expiry = date(2026, 8, 2)
    # 31 days / 30.44 ≈ 1.02 → round → 1
    assert months_until(expiry, today) == 1


def test_months_until_twelve_months():
    today = date(2026, 1, 1)
    expiry = date(2027, 1, 1)
    # 365 / 30.44 ≈ 11.99 → round → 12
    assert months_until(expiry, today) == 12


def test_months_until_clamps_to_zero_for_past():
    today = date(2026, 7, 2)
    past = date(2026, 6, 1)
    # a past date gives a negative delta → clamps to 0
    assert months_until(past, today) == 0


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #


def _session_with_rows(rows: list) -> MagicMock:
    """Mock session: execute returns a result with .fetchall()=rows."""
    session = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = rows

    async def _exec(sql, params=None):
        return result

    session.execute = _exec
    return session


def _row(
    *,
    customer_pib: str | None = "100200300",
    customer_name: str | None = "ЈКП Водовод",
    winner_name: str | None = "Добавр д.о.о.",
    lot_title: str | None = "Набавка цеви",
    award_amount_rsd=Decimal("5000000"),
    decision_date: date | None = date(2025, 1, 15),
    expiry_date: date = date(2027, 1, 15),
) -> SimpleNamespace:
    return SimpleNamespace(
        customer_pib=customer_pib,
        customer_name=customer_name,
        winner_name=winner_name,
        lot_title=lot_title,
        award_amount_rsd=award_amount_rsd,
        decision_date=decision_date,
        expiry_date=expiry_date,
    )


# --------------------------------------------------------------------------- #
#  get_reprocurement_forecast
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_basic_forecast_returns_rows():
    future_expiry = date.today() + timedelta(days=180)
    session = _session_with_rows([_row(expiry_date=future_expiry)])

    rows = await get_reprocurement_forecast(session, customer_pib="100200300")

    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, ReprocurementRow)
    assert r.expiry_date == future_expiry
    assert r.months_until_expiry >= 0
    assert r.customer_pib == "100200300"
    assert r.lot_title == "Набавка цеви"
    assert r.winner_name == "Добавр д.о.о."


@pytest.mark.asyncio
async def test_graceful_empty_returns_empty_list():
    session = _session_with_rows([])
    rows = await get_reprocurement_forecast(session, customer_pib="999")
    assert rows == []


@pytest.mark.asyncio
async def test_multiple_rows_sorted_ascending():
    today = date.today()
    near = today + timedelta(days=30)
    far = today + timedelta(days=270)
    session = _session_with_rows([_row(expiry_date=near), _row(expiry_date=far)])
    rows = await get_reprocurement_forecast(session, customer_pib="111")
    assert len(rows) == 2
    # The SQL already sorts ASC, the mock returns in the same order
    assert rows[0].expiry_date == near
    assert rows[1].expiry_date == far


@pytest.mark.asyncio
async def test_expiry_as_datetime_normalised_to_date():
    """asyncpg may return a datetime — it must be normalized to a date."""
    future_dt = datetime(2027, 6, 1, 12, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(
        customer_pib="X",
        customer_name="Y",
        winner_name="Z",
        lot_title="T",
        award_amount_rsd=None,
        decision_date=None,
        expiry_date=future_dt,
    )
    session = _session_with_rows([row])
    rows = await get_reprocurement_forecast(session, customer_pib="X")
    assert len(rows) == 1
    assert rows[0].expiry_date == date(2027, 6, 1)


@pytest.mark.asyncio
async def test_null_amount_stays_none():
    future_expiry = date.today() + timedelta(days=60)
    session = _session_with_rows(
        [_row(expiry_date=future_expiry, award_amount_rsd=None)]
    )
    rows = await get_reprocurement_forecast(session, customer_pib="111")
    assert rows[0].award_amount_rsd is None


@pytest.mark.asyncio
async def test_by_name_fallback_when_pib_empty():
    """customer_pib is empty — the name path is used (we pass customer_name)."""
    future_expiry = date.today() + timedelta(days=90)
    captured_params: list[dict] = []

    session = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = [_row(expiry_date=future_expiry)]

    async def _exec(sql, params=None):
        captured_params.append(dict(params or {}))
        return result

    session.execute = _exec

    rows = await get_reprocurement_forecast(
        session, customer_pib=None, customer_name="ЈКП Водовод"
    )
    assert len(rows) == 1
    # the name path passes "name", not "pib"
    assert "name" in captured_params[0]
    assert "pib" not in captured_params[0]


@pytest.mark.asyncio
async def test_horizon_passed_to_query():
    """horizon_months is passed through into the SQL parameters."""
    captured_params: list[dict] = []

    session = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = []

    async def _exec(sql, params=None):
        captured_params.append(dict(params or {}))
        return result

    session.execute = _exec

    await get_reprocurement_forecast(session, customer_pib="123", horizon_months=6)
    assert captured_params[0]["horizon"] == 6


@pytest.mark.asyncio
async def test_row_with_none_expiry_skipped():
    """A row with expiry_date=None (must not be in the SQL output, but just in case) is skipped."""
    bad_row = SimpleNamespace(
        customer_pib="A",
        customer_name="B",
        winner_name="C",
        lot_title="D",
        award_amount_rsd=None,
        decision_date=None,
        expiry_date=None,
    )
    session = _session_with_rows([bad_row])
    rows = await get_reprocurement_forecast(session, customer_pib="A")
    assert rows == []
