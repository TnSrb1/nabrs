"""Tests of the A3 add-ons over ``src.analytics.awards_analytics``: the directory of
the customer (``search_customers``), the ILIKE escaping, the prices (avg/median) and
the sample period in ``CustomerStats``.

Without a real DB: a mock session with sequential results (ADR-013).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.analytics.awards_analytics import (
    _CUSTOMER_DIRECTORY_SEARCH,
    _CUSTOMER_DIRECTORY_TOP,
    _CUSTOMER_SUMMARY_BY_NAME_ALL,
    _like_term,
    customer_award_stats,
    search_customers,
)


def _result(*, first=None, rows=None) -> MagicMock:
    r = MagicMock()
    r.first.return_value = first
    r.fetchall.return_value = rows or []
    return r


def _session_seq(results: list[MagicMock]) -> MagicMock:
    """Mock session: writes (sql, params) of each execute into ``session.calls``."""
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


def _dir_row(
    name="Општина Нови Сад", pib="100236236", awards=12, last=date(2026, 5, 4)
):
    return SimpleNamespace(
        customer_name=name, customer_pib=pib, awards=awards, last_date=last
    )


# --------------------------------------------------------------------------- #
#  ILIKE escaping
# --------------------------------------------------------------------------- #


def test_like_term_wraps_in_wildcards():
    assert _like_term("Нови Сад") == "%Нови Сад%"


def test_like_term_escapes_ilike_masks():
    """'%' and '_' in the input are literals, not masks (otherwise «100%» would find everything)."""
    assert _like_term("100%") == "%100\\%%"
    assert _like_term("а_б") == "%а\\_б%"


def test_like_term_escapes_backslash_first():
    r"""The escape character itself is escaped, and previously masks: '\%' must not become
    an escape of '%', otherwise the input '\%' would bypass the protection and '%' would become a mask again."""
    assert _like_term("\\%") == "%\\\\\\%%"


# --------------------------------------------------------------------------- #
#  search_customers
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_search_customers_without_term_uses_top_sql():
    session = _session_seq([_result(rows=[_dir_row()])])
    rows = await search_customers(session, limit=5)

    sql, params = session.calls[0]
    assert sql == str(_CUSTOMER_DIRECTORY_TOP)
    assert params == {"limit": 5, "offset": 0}
    assert [(r.name, r.pib, r.awards, r.last_date) for r in rows] == [
        ("Општина Нови Сад", "100236236", 12, date(2026, 5, 4))
    ]


@pytest.mark.asyncio
async def test_search_customers_with_term_uses_search_sql_and_masked_param():
    """The input is folded into Latin without diacritics (``fold``), as the column is in the
    SQL: a Cyrillic input becomes a Latin mask. The PIB prefix — as is."""
    session = _session_seq([_result(rows=[_dir_row()])])
    await search_customers(session, term="Нови", limit=5)

    sql, params = session.calls[0]
    assert sql == str(_CUSTOMER_DIRECTORY_SEARCH)
    assert params == {
        "term": "%novi%",
        "pib_term": "Нови%",
        "limit": 5,
        "offset": 0,
    }


@pytest.mark.asyncio
async def test_search_customers_blank_term_falls_back_to_top():
    """Spaces are not a search: otherwise ILIKE '% %' would give everything as a “result”."""
    session = _session_seq([_result(rows=[])])
    await search_customers(session, term="   ")

    sql, _ = session.calls[0]
    assert sql == str(_CUSTOMER_DIRECTORY_TOP)


@pytest.mark.asyncio
async def test_search_customers_tolerates_null_pib_and_date():
    """22.9% of rows without a PIB + decisions without a date — a directory row, not a failure."""
    session = _session_seq([_result(rows=[_dir_row(pib=None, last=None)])])
    rows = await search_customers(session)

    assert rows[0].pib is None
    assert rows[0].last_date is None
    assert rows[0].name == "Општина Нови Сад"


@pytest.mark.asyncio
async def test_search_customers_empty_result_is_empty_list():
    session = _session_seq([_result(rows=[])])
    assert await search_customers(session) == []


@pytest.mark.parametrize(
    "sql", [str(_CUSTOMER_DIRECTORY_TOP), str(_CUSTOMER_DIRECTORY_SEARCH)]
)
def test_directory_pib_is_most_frequent_not_lexicographic_max(sql):
    """The PIB of the group is taken by mode(), NOT max(): max() over text is lexicographic,
    i.e. an arbitrary PIB ('99' > '100236236' as a string) would end up in the customer's label.

    Checked on a live PG: for a name with the PIB 100236236 in most rows and '99' in
    one, max() gives '99', mode() gives '100236236'. The mock does not reproduce the semantics of mode(),
    therefore we pin the choice of the aggregate itself.
    """
    assert "mode() WITHIN GROUP (ORDER BY customer_pib)" in sql
    assert "max(customer_pib)" not in sql


# --------------------------------------------------------------------------- #
#  Prices and the sample period in CustomerStats
# --------------------------------------------------------------------------- #


def _summary(**over):
    base = dict(
        total_awards=10,
        distinct_winners=3,
        with_amount=6,
        total_rsd=Decimal("120000000"),
        total_eur=Decimal("1000000"),
        avg_rsd=Decimal("20000000"),
        median_rsd=Decimal("15000000"),
        first_date=date(2019, 3, 12),
        last_date=date(2026, 5, 4),
        without_date=2,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_customer_stats_exposes_prices_and_period():
    session = _session_seq([_result(first=_summary()), _result(rows=[])])
    stats = await customer_award_stats(session, customer_name="Општина Нови Сад")

    assert stats.avg_rsd == Decimal("20000000")
    assert stats.median_rsd == Decimal("15000000")
    assert stats.first_date == date(2019, 3, 12)
    assert stats.last_date == date(2026, 5, 4)
    assert stats.without_date == 2


@pytest.mark.asyncio
async def test_customer_stats_null_prices_stay_none_not_zero():
    """No published amount at all → avg/median = None.

    Substitution by zero would draw “average cheque 0 RSD” instead of “amount unknown”.
    """
    session = _session_seq(
        [
            _result(first=_summary(with_amount=0, avg_rsd=None, median_rsd=None)),
            _result(rows=[]),
        ]
    )
    stats = await customer_award_stats(session, customer_name="Општина Нови Сад")

    assert stats.avg_rsd is None
    assert stats.median_rsd is None


@pytest.mark.asyncio
async def test_customer_stats_null_period_tolerated():
    """All decisions without decision_date → the period None, not a failure."""
    session = _session_seq(
        [
            _result(first=_summary(first_date=None, last_date=None, without_date=10)),
            _result(rows=[]),
        ]
    )
    stats = await customer_award_stats(session, customer_name="Општина Нови Сад")

    assert stats.first_date is None
    assert stats.last_date is None
    assert stats.without_date == 10


@pytest.mark.asyncio
async def test_customer_stats_floats_from_percentile_become_decimal():
    """percentile_cont returns double precision → normalize to Decimal,
    otherwise the template/CSV would get 1.5e7."""
    session = _session_seq([_result(first=_summary(median_rsd=15000000.0)), _result()])
    stats = await customer_award_stats(session, customer_name="X")

    assert isinstance(stats.median_rsd, Decimal)
    assert int(stats.median_rsd) == 15000000


@pytest.mark.asyncio
async def test_by_name_summary_sql_carries_price_and_period_columns():
    """The shared fragment of the columns really got into the by-name variant (protection against
    adding a new metric to one of the four SQLs)."""
    sql = str(_CUSTOMER_SUMMARY_BY_NAME_ALL)
    for col in ("avg_rsd", "median_rsd", "first_date", "last_date", "without_date"):
        assert col in sql


# --------------------------------------------------------------------------- #
#  The sanity of the aggregates (a check from the A3 spec)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_concentration_shares_are_sane():
    """The share of top-1 ≤ the share of top-3 ≤ 100%, the number of wins ≤ the number of decisions."""
    winners = [
        SimpleNamespace(winner_name="A", winner_pib="1", wins=5, sum_rsd=Decimal("10")),
        SimpleNamespace(winner_name="B", winner_pib="2", wins=3, sum_rsd=Decimal("5")),
        SimpleNamespace(winner_name="C", winner_pib="3", wins=1, sum_rsd=Decimal("1")),
    ]
    session = _session_seq(
        [
            _result(first=_summary(total_awards=10, distinct_winners=3)),
            _result(rows=winners),
        ]
    )
    stats = await customer_award_stats(session, customer_name="X")

    assert sum(w.wins for w in stats.top_winners) <= stats.total_awards
    assert stats.concentration_pct == 50
    assert stats.cr3_pct == 90
    assert stats.concentration_pct <= stats.cr3_pct <= 100
