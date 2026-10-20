"""Tests of the VG-06 engine ``src.analytics.awards_analytics``.

Without a real DB: a mock session with sequential results (summary → rows).
We cover: the unpacking of the CustomerStats/CompetitorStats fields, concentration_pct,
the branch months=None vs months=N (the choice of the SQL), an empty result (has_data=False).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.analytics.awards_analytics import (
    MONOPOLY_TOP1_PCT,
    CompetitorStats,
    CustomerStats,
    WinnerRow,
    YearRow,
    competitor_stats,
    customer_award_stats,
    customer_yearly_trend,
)


def _result(*, first=None, rows=None) -> MagicMock:
    r = MagicMock()
    r.first.return_value = first
    r.fetchall.return_value = rows or []
    return r


def _session_seq(results: list[MagicMock]) -> MagicMock:
    """Mock session: execute returns the prepared results in turn."""
    idx = {"n": 0}
    session = MagicMock()

    async def _exec(sql, params=None):
        i = idx["n"]
        idx["n"] += 1
        return results[i] if i < len(results) else _result()

    session.execute = _exec
    return session


# --------------------------------------------------------------------------- #
#  customer_award_stats
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_customer_stats_basic_all_time():
    summary = SimpleNamespace(
        total_awards=10,
        distinct_winners=3,
        with_amount=6,
        total_rsd=Decimal("120000000"),
        total_eur=Decimal("1000000"),
    )
    winners = [
        SimpleNamespace(
            winner_name="Лидер д.о.о.",
            winner_pib="111",
            wins=6,
            sum_rsd=Decimal("90000000"),
        ),
        SimpleNamespace(
            winner_name="Второй", winner_pib="222", wins=3, sum_rsd=Decimal("20000000")
        ),
    ]
    session = _session_seq([_result(first=summary), _result(rows=winners)])

    stats = await customer_award_stats(session, customer_pib="999", months=None)

    assert isinstance(stats, CustomerStats)
    assert stats.total_awards == 10
    assert stats.distinct_winners == 3
    assert stats.with_amount == 6
    assert stats.total_rsd == Decimal("120000000")
    assert len(stats.top_winners) == 2
    assert stats.top_winners[0].name == "Лидер д.о.о."
    assert stats.has_data is True
    # top-1 = 6 of 10 → 60%
    assert stats.concentration_pct == 60
    assert stats.months is None


@pytest.mark.asyncio
async def test_customer_stats_window_selects_cutoff():
    """months=N → the WINDOW branch: the query runs, the fields are unpacked."""
    summary = SimpleNamespace(
        total_awards=2,
        distinct_winners=2,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
    )
    session = _session_seq([_result(first=summary), _result(rows=[])])

    stats = await customer_award_stats(session, customer_pib="777", months=12)
    assert stats.total_awards == 2
    assert stats.months == 12
    assert stats.top_winners == []
    # there are no winners in the top → concentration None
    assert stats.concentration_pct is None


@pytest.mark.asyncio
async def test_customer_stats_empty_no_data():
    summary = SimpleNamespace(
        total_awards=0,
        distinct_winners=0,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
    )
    session = _session_seq([_result(first=summary), _result(rows=[])])
    stats = await customer_award_stats(session, customer_pib="000", months=None)
    assert stats.has_data is False
    assert stats.concentration_pct is None


@pytest.mark.asyncio
async def test_customer_stats_null_summary_safe():
    """summary=None (no rows) → zeros, without a failure."""
    session = _session_seq([_result(first=None), _result(rows=[])])
    stats = await customer_award_stats(session, customer_pib="x", months=None)
    assert stats.total_awards == 0
    assert stats.total_rsd == Decimal("0")
    assert stats.has_data is False


# --------------------------------------------------------------------------- #
#  customer_yearly_trend (the dynamics by year)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_yearly_trend_reverses_to_chronological():
    """The SQL gives the years in descending order (the last N) → the function unrolls them into a chronology."""
    rows = [
        SimpleNamespace(
            yr=2025,
            awards=8,
            distinct_winners=4,
            total_rsd=Decimal("80"),
            total_eur=Decimal("1"),
        ),
        SimpleNamespace(
            yr=2024,
            awards=5,
            distinct_winners=3,
            total_rsd=Decimal("50"),
            total_eur=Decimal("0"),
        ),
        SimpleNamespace(
            yr=2023,
            awards=2,
            distinct_winners=2,
            total_rsd=Decimal("0"),
            total_eur=Decimal("0"),
        ),
    ]
    session = _session_seq([_result(rows=rows)])

    trend = await customer_yearly_trend(session, customer_pib="999", years=5)

    assert [y.year for y in trend] == [2023, 2024, 2025]  # chronologically
    assert isinstance(trend[0], YearRow)
    assert trend[-1].awards == 8
    assert trend[-1].distinct_winners == 4


@pytest.mark.asyncio
async def test_yearly_trend_by_name_when_no_pib():
    rows = [
        SimpleNamespace(
            yr=2024,
            awards=3,
            distinct_winners=2,
            total_rsd=Decimal("30"),
            total_eur=Decimal("0"),
        ),
    ]
    session = _session_seq([_result(rows=rows)])

    trend = await customer_yearly_trend(session, customer_name="Град Београд", years=5)
    assert len(trend) == 1
    assert trend[0].year == 2024
    assert trend[0].awards == 3


@pytest.mark.asyncio
async def test_yearly_trend_empty():
    session = _session_seq([_result(rows=[])])
    trend = await customer_yearly_trend(session, customer_pib="0")
    assert trend == []


# --------------------------------------------------------------------------- #
#  competitor_stats
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_competitor_stats_basic():
    summary = SimpleNamespace(
        total_wins=8,
        distinct_customers=4,
        with_amount=5,
        total_rsd=Decimal("50000000"),
        total_eur=Decimal("420000"),
    )
    customers = [
        SimpleNamespace(
            customer_name="ЈКП А",
            customer_pib="aaa",
            wins=4,
            sum_rsd=Decimal("30000000"),
        ),
    ]
    session = _session_seq([_result(first=summary), _result(rows=customers)])

    stats = await competitor_stats(session, winner_pib="555", months=None)
    assert isinstance(stats, CompetitorStats)
    assert stats.total_wins == 8
    assert stats.distinct_customers == 4
    assert len(stats.top_customers) == 1
    assert stats.top_customers[0].name == "ЈКП А"
    assert stats.has_data is True


@pytest.mark.asyncio
async def test_competitor_stats_empty():
    summary = SimpleNamespace(
        total_wins=0,
        distinct_customers=0,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
    )
    session = _session_seq([_result(first=summary), _result(rows=[])])
    stats = await competitor_stats(session, winner_pib="0", months=6)
    assert stats.has_data is False
    assert stats.months == 6


# --------------------------------------------------------------------------- #
#  the name fallback (PIB is empty → the key by customer_name)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_customer_stats_by_name_when_no_pib():
    """customer_pib is empty, customer_name is present → the key by the name, the fields are counted."""
    summary = SimpleNamespace(
        total_awards=4,
        distinct_winners=2,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
    )
    winners = [
        SimpleNamespace(winner_name="W1", winner_pib="1", wins=3, sum_rsd=Decimal("0")),
    ]
    session = _session_seq([_result(first=summary), _result(rows=winners)])
    stats = await customer_award_stats(
        session, customer_pib=None, customer_name="ЈКП Топлана", months=None
    )
    assert stats.total_awards == 4
    assert stats.customer_pib == ""
    assert stats.has_data is True
    assert stats.concentration_pct == 75  # 3/4


@pytest.mark.asyncio
async def test_customer_stats_pib_takes_priority_over_name():
    """When the PIB is non-empty the name is ignored (the key by PIB)."""
    summary = SimpleNamespace(
        total_awards=1,
        distinct_winners=1,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
    )
    session = _session_seq([_result(first=summary), _result(rows=[])])
    stats = await customer_award_stats(
        session, customer_pib="123", customer_name="Имя", months=None
    )
    assert stats.customer_pib == "123"
    assert stats.total_awards == 1


# --------------------------------------------------------------------------- #
#  CustomerStats.cr3_pct and is_monopolized (VG-06 STAGE 2)
# --------------------------------------------------------------------------- #


def _make_stats(
    total_awards: int,
    distinct_winners: int,
    top_winners: list,
) -> CustomerStats:
    return CustomerStats(
        customer_pib="1",
        total_awards=total_awards,
        distinct_winners=distinct_winners,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
        top_winners=top_winners,
        months=None,
    )


def _w(wins: int) -> WinnerRow:
    return WinnerRow(name="X", pib="x", wins=wins, sum_rsd=Decimal("0"))


def test_cr3_pct_none_when_no_awards():
    stats = _make_stats(0, 0, [])
    assert stats.cr3_pct is None


def test_cr3_pct_none_when_no_top_winners():
    stats = _make_stats(5, 2, [])
    assert stats.cr3_pct is None


def test_cr3_pct_single_winner():
    stats = _make_stats(10, 1, [_w(10)])
    assert stats.cr3_pct == 100


def test_cr3_pct_sums_up_to_three():
    stats = _make_stats(10, 5, [_w(4), _w(3), _w(2), _w(1)])
    # top-3: 4+3+2=9 of 10 = 90%
    assert stats.cr3_pct == 90


def test_cr3_pct_fewer_than_three_winners():
    stats = _make_stats(10, 2, [_w(7), _w(3)])
    # top-3 (only 2 exist): 7+3=10 of 10 = 100%
    assert stats.cr3_pct == 100


def test_is_monopolized_single_winner():
    stats = _make_stats(5, 1, [_w(5)])
    assert stats.is_monopolized is True


def test_is_monopolized_above_threshold():
    # MONOPOLY_TOP1_PCT=70: 71% → True
    stats = _make_stats(100, 2, [_w(71), _w(29)])
    assert stats.concentration_pct == 71
    assert stats.is_monopolized is True


def test_is_monopolized_at_threshold():
    stats = _make_stats(100, 2, [_w(MONOPOLY_TOP1_PCT), _w(100 - MONOPOLY_TOP1_PCT)])
    assert stats.is_monopolized is True


def test_is_monopolized_below_threshold():
    # 69% — not a monopoly with distinct_winners > 1
    stats = _make_stats(100, 2, [_w(69), _w(31)])
    assert stats.is_monopolized is False


def test_is_monopolized_no_awards():
    stats = _make_stats(0, 0, [])
    # concentration_pct=None, distinct_winners=0 → not monopolized
    assert stats.is_monopolized is False


# --------------------------------------------------------------------------- #
#  A fix of the competitor_stats window: the WINDOW SQL contains a filter by decision_date
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_competitor_stats_window_captures_params():
    """months=N → the WINDOW branch: the cutoff is passed in the SQL parameters, has_data=True."""
    summary = SimpleNamespace(
        total_wins=3,
        distinct_customers=2,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
    )
    captured: list[dict] = []

    session = MagicMock()
    call_count = {"n": 0}

    async def _exec(sql, params=None):
        captured.append(dict(params or {}))
        call_count["n"] += 1
        r = MagicMock()
        if call_count["n"] == 1:
            r.first.return_value = summary
            r.fetchall.return_value = []
        else:
            r.first.return_value = None
            r.fetchall.return_value = []
        return r

    session.execute = _exec

    stats = await competitor_stats(session, winner_pib="PIB1", months=12)

    # the cutoff must be present in the parameters of the first call (the summary WINDOW)
    assert "cutoff" in captured[0], "the WINDOW SQL must pass :cutoff"
    assert stats.total_wins == 3
    assert stats.months == 12
