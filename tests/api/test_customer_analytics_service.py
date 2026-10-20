"""Tests of customer_analytics_service (web-ai-3, ADR-013: mock session without DB).

We check the customer analytics in the web cabinet:
- set_tenant is called FIRST (RLS invariant);
- the Pro+ gate uses the SAME predicate as the bot (the bot boundaries are untouched);
- freeze → frozen (BEFORE the plan), non-Pro → gate_pro;
- no tender → no_tender; no customer → no_customer; no decisions → no_data;
- success: mapping of aggregates to the view + amount format (space as thousands separator).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.api.cabinet import customer_analytics_service as svc
from src.api.cabinet.customer_analytics_service import get_customer_analytics
from src.bots.awards_analytics_service import _is_pro_plus as bot_is_pro_plus

_TID = "11111111-1111-1111-1111-111111111111"
_TENDER = "22222222-2222-2222-2222-222222222222"

# Explicit limits (not tied to the real _TIER_LIMITS): Pro+ := bid != 0.
_LIMITS = {"pro": {"bid": 5}, "basic": {"bid": 0}}


class _Result:
    def __init__(self, *, rows=None):
        self._rows = list(rows) if rows is not None else []

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def fetchall(self):
        return list(self._rows)


class _Session:
    """Mock AsyncSession: set_config (RLS) does not touch the queue, but the fact of the call is recorded in ``executed`` to check the set_tenant invariant."""

    def __init__(self, results):
        self._results = list(results)
        self.executed: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append(sql)
        if "set_config" in sql or "app.current_tenant_id" in sql:
            return _Result()
        return self._results.pop(0) if self._results else _Result()


def _tenant_row(tier="pro", access_until=None):
    return _Result(rows=[SimpleNamespace(tier=tier, access_until=access_until)])


def _customer_row(name="Општина Нови Сад", tax_id="100236236", title="Набавка"):
    return _Result(
        rows=[SimpleNamespace(customer_name=name, customer_tax_id=tax_id, title=title)]
    )


def _summary_row(
    total_awards=10,
    distinct_winners=3,
    with_amount=8,
    total_rsd=12345678,
    total_eur=105000,
):
    return _Result(
        rows=[
            SimpleNamespace(
                total_awards=total_awards,
                distinct_winners=distinct_winners,
                with_amount=with_amount,
                total_rsd=total_rsd,
                total_eur=total_eur,
            )
        ]
    )


def _winners_rows(rows):
    return _Result(
        rows=[
            SimpleNamespace(winner_name=n, winner_pib=p, wins=w, sum_rsd=s)
            for (n, p, w, s) in rows
        ]
    )


def _trend_rows(rows):
    """rows: [(yr, awards, distinct_winners, total_rsd, total_eur), ...] in SQL order (DESC)."""
    return _Result(
        rows=[
            SimpleNamespace(
                yr=yr,
                awards=a,
                distinct_winners=dw,
                total_rsd=rsd,
                total_eur=eur,
            )
            for (yr, a, dw, rsd, eur) in rows
        ]
    )


# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_success_maps_and_formats():
    s = _Session(
        [
            _tenant_row(tier="pro"),
            _customer_row(),
            _summary_row(),
            _winners_rows(
                [
                    ("ДОО Алфа", "111", 6, 9000000),
                    ("ДОО Бета", "222", 3, 2000000),
                    ("ДОО Гама", "333", 1, 0),
                ]
            ),
        ]
    )
    v = await get_customer_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "success"
    assert v.customer_name == "Општина Нови Сад"
    assert v.customer_pib == "100236236"
    assert v.total_awards == 10
    assert v.distinct_winners == 3
    assert v.with_amount == 8
    # Amount format — a space as thousands separator (as in the bot).
    assert v.total_rsd_fmt == "12 345 678"
    assert v.total_eur_fmt == "105 000"
    # Concentration top-1 = 6/10 = 60%, top-3 = 10/10 = 100%.
    assert v.concentration_pct == 60
    assert v.cr3_pct == 100
    assert len(v.top_winners) == 3
    assert v.top_winners[0].rank == 1
    assert v.top_winners[0].name == "ДОО Алфа"
    assert v.top_winners[0].wins == 6
    assert v.top_winners[0].sum_rsd_fmt == "9 000 000 RSD"
    # Zero amount → empty string (the template will show “—”).
    assert v.top_winners[2].sum_rsd_fmt == ""


@pytest.mark.asyncio
async def test_set_tenant_called_first():
    s = _Session([_tenant_row(), _customer_row(), _summary_row(), _winners_rows([])])
    await get_customer_analytics(s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS)
    assert "set_config" in s.executed[0] or "app.current_tenant_id" in s.executed[0]


@pytest.mark.asyncio
async def test_gate_pro_for_non_pro_tier():
    s = _Session([_tenant_row(tier="basic")])
    v = await get_customer_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "gate_pro"


@pytest.mark.asyncio
async def test_frozen_before_tier_gate():
    # access_until in the past → frozen, even if the plan is Pro.
    past = datetime.now(timezone.utc) - timedelta(days=1)
    s = _Session([_tenant_row(tier="pro", access_until=past)])
    v = await get_customer_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "frozen"


@pytest.mark.asyncio
async def test_no_tender():
    s = _Session([_tenant_row(), _Result(rows=[])])
    v = await get_customer_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_tender"


@pytest.mark.asyncio
async def test_no_customer_when_pib_and_name_empty():
    s = _Session([_tenant_row(), _customer_row(name="", tax_id="")])
    v = await get_customer_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_customer"


@pytest.mark.asyncio
async def test_no_data_when_no_awards():
    s = _Session(
        [
            _tenant_row(),
            _customer_row(),
            _summary_row(total_awards=0, distinct_winners=0, with_amount=0),
            _winners_rows([]),
        ]
    )
    v = await get_customer_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_data"


@pytest.mark.asyncio
async def test_with_amount_zero_leaves_totals_but_no_money_tiles():
    # Decisions exist, but amounts are unknown → success, with_amount=0.
    s = _Session(
        [
            _tenant_row(),
            _customer_row(),
            _summary_row(
                total_awards=4,
                distinct_winners=2,
                with_amount=0,
                total_rsd=0,
                total_eur=0,
            ),
            _winners_rows([("ДОО Алфа", "111", 3, 0), ("ДОО Бета", "222", 1, 0)]),
        ]
    )
    v = await get_customer_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "success"
    assert v.with_amount == 0
    assert v.concentration_pct == 75  # 3/4


def test_gate_predicate_is_the_bot_predicate():
    # Bot boundaries untouched: the same predicate object, a single gating axis.
    assert svc._is_pro_plus is bot_is_pro_plus


# --------------------------------------------------------------------------- #
#  Annual dynamics (trend) — VG-06 deepening
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_success_includes_yearly_trend_chronological_with_bars():
    s = _Session(
        [
            _tenant_row(tier="pro"),
            _customer_row(),
            _summary_row(),
            _winners_rows([("ДОО Алфа", "111", 6, 9000000)]),
            # SQL order DESC (latest years on top); the service reverses it into chronological order.
            _trend_rows(
                [
                    (2025, 10, 5, 100, 1),
                    (2024, 5, 3, 50, 0),
                    (2023, 2, 2, 0, 0),
                ]
            ),
        ]
    )
    v = await get_customer_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "success"
    # Chronological order (ascending by year).
    assert [y.year for y in v.trend] == [2023, 2024, 2025]
    # bar_pct normalized to the max of awards (2025=10 → 100%); 2024=5 → 50%; 2023=2 → 20%.
    assert v.trend[-1].bar_pct == 100
    assert v.trend[1].bar_pct == 50
    assert v.trend[0].bar_pct == 20
    assert v.trend[-1].awards == 10
    assert v.trend[-1].distinct_winners == 5


@pytest.mark.asyncio
async def test_no_data_does_not_query_trend():
    # total_awards=0 → no_data BEFORE the trend query: an extra result in the queue is not touched.
    s = _Session(
        [
            _tenant_row(),
            _customer_row(),
            _summary_row(total_awards=0, distinct_winners=0, with_amount=0),
            _winners_rows([]),
        ]
    )
    v = await get_customer_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_data"
    assert v.trend == []


def test_trend_from_rows_bar_pct_normalization():
    from src.analytics.awards_analytics import YearRow

    rows = [
        YearRow(year=2023, awards=1, distinct_winners=1, total_rsd=0, total_eur=0),
        YearRow(year=2024, awards=4, distinct_winners=2, total_rsd=0, total_eur=0),
    ]
    trend = svc._trend_from_rows(rows)
    assert [t.bar_pct for t in trend] == [25, 100]  # 1/4=25%, 4/4=100%
    assert svc._trend_from_rows([]) == []


# --------------------------------------------------------------------------- #
# Incumbent: feed of the latest decisions of the customer (A4)
# --------------------------------------------------------------------------- #


def _recent(**over):
    from decimal import Decimal

    from src.analytics.awards_analytics import RecentAwardRow

    base = dict(
        winner_name="ЕНЕРГОТОК ДОО БЕОГРАД",
        winner_pib="100172936",
        amount_rsd=Decimal("7891300"),
        amount_eur=Decimal("67000"),
        decision_date=date(2026, 7, 3),
        lot_title="Поправке инсталација",
        lots=1,
    )
    base.update(over)
    return RecentAwardRow(**base)


def test_incumbent_view_empty_history_is_no_data_not_empty_frame():
    """Customer without history (TZ A4) → no_data; the template does not draw an empty frame."""
    v = svc._incumbent_view_from_rows("Општина X", [])

    assert v.status == "no_data"
    assert v.items == []


def test_incumbent_view_maps_rows_to_formatted_items():
    v = svc._incumbent_view_from_rows("Општина Нови Сад", [_recent()])

    assert v.status == "success"
    assert v.customer_name == "Општина Нови Сад"
    (it,) = v.items
    assert it.winner_name == "ЕНЕРГОТОК ДОО БЕОГРАД"
    assert it.pib == "100172936"
    assert it.date_fmt == "03.07.2026"
    assert it.sum_rsd_fmt == "7 891 300 RSD"
    assert it.lots == 1
    assert it.is_joint is False


def test_incumbent_view_unpublished_sum_is_blank_not_zero():
    """Amount not published → ““ (the template says so in words), and NOT ”0 RSD”."""
    (it,) = svc._incumbent_view_from_rows("X", [_recent(amount_rsd=None)]).items

    assert it.sum_rsd_fmt == ""


def test_incumbent_view_missing_date_is_blank_not_crash():
    (it,) = svc._incumbent_view_from_rows("X", [_recent(decision_date=None)]).items

    assert it.date_fmt == ""


def test_incumbent_view_joint_award_hides_glued_pib():
    """Data debt: the consortium's PIB concatenation does not go outside (it is not an identifier)."""
    (it,) = svc._incumbent_view_from_rows(
        "X",
        [
            _recent(
                winner_name="МАРИНКО ДОО, РУКИ ЦООП ДОО",
                winner_pib="101470969, 103229839",
            )
        ],
    ).items

    assert it.is_joint is True
    assert it.pib == ""


def test_incumbent_view_nameless_winner_gets_placeholder():
    """An empty winner name — a caption, not an empty string in the feed."""
    (it,) = svc._incumbent_view_from_rows("X", [_recent(winner_name="  ")]).items

    assert it.winner_name == "Победитель не указан"
