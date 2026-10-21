"""A4 — the incumbent of the customer: the normalized name key and the feed of the latest decisions.

Without a real DB: a mock session with sequential results (ADR-013).

What is pinned here and why:
- ``normalize_customer_name`` — the MIRROR of the SQL formula ``_CUSTOMER_NAME_KEY``. Both
  sides of the comparison must normalize identically, otherwise the predicate finds
  nothing. The mock does not reproduce the PG semantics → we pin both the formula and its mirror.
- the unit of the feed is a DECISION, not a lot row: the sum in ``award_amount_rsd``
  is repeated on every lot of the decision (checked on live data: 131/131
  multi-lot groups with a constant sum), therefore ``sum()`` over the lots would double
  the sums, and a feed by rows would print the same award five times in a row.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.analytics.awards_analytics import (
    _CUSTOMER_NAME_KEY,
    _CUSTOMER_RECENT_AWARDS_BY_NAME,
    _CUSTOMER_SUMMARY_BY_NAME_ALL,
    _CUSTOMER_SUMMARY_BY_NAME_WINDOW,
    _CUSTOMER_TOP_WINNERS_BY_NAME_ALL,
    _CUSTOMER_TREND_BY_NAME_ALL,
    RecentAwardRow,
    customer_award_stats,
    customer_recent_awards,
    customer_yearly_trend,
    normalize_customer_name,
)


def _result(*, first=None, rows=None) -> MagicMock:
    r = MagicMock()
    r.first.return_value = first
    r.fetchall.return_value = rows or []
    return r


def _session_seq(results: list[MagicMock]) -> MagicMock:
    idx = {"n": 0}
    session = MagicMock()
    session.calls = []

    async def _exec(sql, params=None):
        session.calls.append((str(sql), params))
        i = idx["n"]
        idx["n"] += 1
        return results[i] if i < len(results) else _result()

    session.execute = _exec
    return session


def _award_row(
    winner_name="ЕНЕРГОТОК ДОО БЕОГРАД",
    winner_pib="100172936",
    amount_rsd=Decimal("7891300.00"),
    amount_eur=Decimal("67000.00"),
    decision_date=date(2026, 7, 3),
    lots=1,
    lot_title="Поправке инсталација",
):
    return SimpleNamespace(
        winner_name=winner_name,
        winner_pib=winner_pib,
        amount_rsd=amount_rsd,
        amount_eur=amount_eur,
        decision_date=decision_date,
        lots=lots,
        lot_title=lot_title,
    )


# --------------------------------------------------------------------------- #
#  Normalisation of the name: the Python mirror and the SQL formula
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Општина Ковачица", "општина ковачица"),
        ("Општина Ковачица ", "општина ковачица"),  # trailing space (live data)
        ("  Општина Ковачица", "општина ковачица"),
        ("ЗАВОД ЗА  ЈАВНО ЗДРАВЉЕ ШАБАЦ", "завод за јавно здравље шабац"),  # double
        ("завод за\tјавно\nздравље шабац", "завод за јавно здравље шабац"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_collapses_spaces_and_lowercases(raw, expected):
    """Case + runs of whitespace + edges — the same three steps as in the SQL formula.

    On the live production “ZAVOD ZA JAVNO ZDRAVLJE SABAC” exists in two spellings
    (21 rows + 9 with a double space): without normalization the profile showed 21
    decisions out of 30.
    """
    assert normalize_customer_name(raw) == expected


def test_name_key_sql_formula_is_pinned():
    """The key formula is a contract between the SQL and the Python mirror.

    It changes only together with ``normalize_customer_name``: if they diverge, the predicate will stop
    finding rows (comparing a normalized parameter with a non-normalized column), and silently, as “no data”.
    """
    assert (
        _CUSTOMER_NAME_KEY
        == r"lower(btrim(regexp_replace(customer_name, '\s+', ' ', 'g')))"
    )


@pytest.mark.parametrize(
    "sql",
    [
        str(_CUSTOMER_SUMMARY_BY_NAME_ALL),
        str(_CUSTOMER_SUMMARY_BY_NAME_WINDOW),
        str(_CUSTOMER_TOP_WINNERS_BY_NAME_ALL),
        str(_CUSTOMER_TREND_BY_NAME_ALL),
        str(_CUSTOMER_RECENT_AWARDS_BY_NAME),
    ],
)
def test_every_by_name_predicate_uses_the_shared_key(sql):
    """No by-name predicate compares the name LITERALLY.

    One predicate per meaning (the project rules): if one of the SQLs forgot to normalize, this block
    will diverge from the neighbouring one on the same page (the profile 30, the feed 21).
    """
    assert _CUSTOMER_NAME_KEY + " = :name" in sql
    assert "WHERE customer_name = :name" not in sql
    assert "AND customer_name = :name" not in sql


# --------------------------------------------------------------------------- #
#  The side of the bind parameter is normalized by the same mirror
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stats_normalizes_name_param():
    """Otherwise the normalized column would be compared with the raw parameter → 0 rows."""
    session = _session_seq([_result(first=None), _result(rows=[])])
    await customer_award_stats(session, customer_name="  Општина   КОВАЧИЦА ")

    _, params = session.calls[0]
    assert params["name"] == "општина ковачица"


@pytest.mark.asyncio
async def test_trend_normalizes_name_param():
    session = _session_seq([_result(rows=[])])
    await customer_yearly_trend(session, customer_name="Општина  Ковачица ")

    _, params = session.calls[0]
    assert params["name"] == "општина ковачица"


# --------------------------------------------------------------------------- #
#  The feed of the latest decisions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_recent_awards_normalizes_name_and_limits():
    session = _session_seq([_result(rows=[_award_row()])])
    rows = await customer_recent_awards(
        session, customer_name="ЗАВОД ЗА  ЈАВНО ЗДРАВЉЕ ШАБАЦ", limit=8
    )

    sql, params = session.calls[0]
    assert sql == str(_CUSTOMER_RECENT_AWARDS_BY_NAME)
    assert params == {"name": "завод за јавно здравље шабац", "limit": 8}
    assert len(rows) == 1
    assert rows[0].winner_name == "ЕНЕРГОТОК ДОО БЕОГРАД"
    assert rows[0].amount_rsd == Decimal("7891300.00")
    assert rows[0].decision_date == date(2026, 7, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
async def test_recent_awards_blank_name_returns_empty_without_query(name):
    """An empty name → [], not a query: the normalized «» would match the key
    of rows with an empty name and would collect a feed of FOREIGN decisions."""
    session = _session_seq([_result(rows=[_award_row()])])
    rows = await customer_recent_awards(session, customer_name=name)

    assert rows == []
    assert session.calls == [], "no query must go out for an empty name"


@pytest.mark.asyncio
async def test_recent_awards_empty_history_is_empty_list_not_error():
    """A customer without a history (the A4 spec) — an empty list, not a failure."""
    session = _session_seq([_result(rows=[])])
    assert await customer_recent_awards(session, customer_name="Општина X") == []


@pytest.mark.asyncio
async def test_recent_awards_null_amount_stays_none_not_zero():
    """“The amount is not published” ≠ «0 RSD» (some decisions have no amount)."""
    session = _session_seq(
        [_result(rows=[_award_row(amount_rsd=None, amount_eur=None)])]
    )
    rows = await customer_recent_awards(session, customer_name="Општина X")

    assert rows[0].amount_rsd is None
    assert rows[0].amount_eur is None


@pytest.mark.asyncio
async def test_recent_awards_tolerates_null_winner_and_date():
    session = _session_seq(
        [
            _result(
                rows=[_award_row(winner_name=None, winner_pib=None, decision_date=None)]
            )
        ]
    )
    rows = await customer_recent_awards(session, customer_name="Општина X")

    assert rows[0].winner_name is None
    assert rows[0].decision_date is None
    assert rows[0].single_pib is None


def test_recent_awards_sql_aggregates_per_decision_not_per_lot():
    """The unit of the feed is the decision (tender + winner + date), the amount is max(), not sum().

    The amount is repeated on every lot of the decision (live data: 131/131 multi-lot
    groups with the same amount within the group, 397 rows = 27% of the table). sum() over the
    lots would give 5×8.3 mln = 41 mln instead of 8.3 mln, and a feed by rows would print
    the same award five times in a row. The mock does not reproduce the GROUP BY semantics → we pin the SQL itself.
    """
    sql = str(_CUSTOMER_RECENT_AWARDS_BY_NAME)
    assert "GROUP BY tender_id, winner_name, winner_pib, decision_date" in sql
    assert "max(award_amount_rsd)" in sql
    assert "sum(award_amount_rsd)" not in sql
    assert "count(*)" in sql  # the number of lots of the decision
    # winner_name — in the grouping key: for some rows winner_pib is empty, and
    # grouping by the PIB alone would glue different PIB-less winners together.
    assert (
        "NULLS LAST" in sql
    )  # decisions without a date do not displace the fresh ones from the top


# --------------------------------------------------------------------------- #
#  Data debt: winner_pib with a “gluing” through a comma (7 rows out of 1454)
# --------------------------------------------------------------------------- #


def test_joint_award_pib_is_not_shown_as_identifier():
    """The gluing of the consortium's PIBs is not an identifier: we do not give it outward.

    «101470969, 103229839» is found in no register; showing it as a PIB
    would mean showing garbage (the A4 spec).
    """
    row = RecentAwardRow(
        winner_name="МАРИНКО ДОО, РУКИ ЦООП ДОО",
        winner_pib="101470969, 103229839",
        amount_rsd=Decimal("1000"),
        amount_eur=None,
        decision_date=date(2026, 1, 1),
    )

    assert row.is_joint is True
    assert row.single_pib is None


def test_single_pib_is_passed_through():
    row = RecentAwardRow(
        winner_name="ЕНЕРГОТОК ДОО БЕОГРАД",
        winner_pib="100172936",
        amount_rsd=None,
        amount_eur=None,
        decision_date=None,
    )

    assert row.is_joint is False
    assert row.single_pib == "100172936"


def test_multilot_flag():
    single = RecentAwardRow(None, None, None, None, None, lots=1)
    many = RecentAwardRow(None, None, None, None, None, lots=12)

    assert single.is_multilot is False
    assert many.is_multilot is True
