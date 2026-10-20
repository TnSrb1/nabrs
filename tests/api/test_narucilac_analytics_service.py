"""Tests of web-ai-4: competitor profile + re-procurement forecast + customer bundle.

ADR-013: mock session without DB. We check (following test_customer_analytics_service):
- set_tenant is called FIRST (RLS invariant), in the bundle — EXACTLY once;
- the Pro+ gate uses the SAME predicate as the bot (the bot boundaries are untouched);
- freeze → frozen (BEFORE the plan), non-Pro → gate_pro; no tender/customer/data
  → no_tender/no_customer/no_data;
- success: mapping of aggregates to the view + format of amounts/dates (space as thousands separator);
- bundle: an error of the gate/resolve — one status across all three sections; success — three independent statuses, the competitor reuses the customer’s top winner.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.api.cabinet import customer_analytics_service as svc
from src.api.cabinet.customer_analytics_service import (
    get_competitor_analytics,
    get_narucilac_analytics,
    get_reprocurement_forecast_view,
)
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


def _cust_summary_row(
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


def _cust_winners_rows(rows):
    return _Result(
        rows=[
            SimpleNamespace(winner_name=n, winner_pib=p, wins=w, sum_rsd=s)
            for (n, p, w, s) in rows
        ]
    )


def _comp_summary_row(
    total_wins=12,
    distinct_customers=4,
    with_amount=9,
    total_rsd=90000000,
    total_eur=760000,
):
    return _Result(
        rows=[
            SimpleNamespace(
                total_wins=total_wins,
                distinct_customers=distinct_customers,
                with_amount=with_amount,
                total_rsd=total_rsd,
                total_eur=total_eur,
            )
        ]
    )


def _comp_customers_rows(rows):
    return _Result(
        rows=[
            SimpleNamespace(customer_name=n, customer_pib=p, wins=w, sum_rsd=s)
            for (n, p, w, s) in rows
        ]
    )


def _repro_rows(rows):
    """rows: list of (lot_title, winner_name, amount, expiry_date)."""
    return _Result(
        rows=[
            SimpleNamespace(
                customer_pib="100236236",
                customer_name="Општина Нови Сад",
                winner_name=wn,
                lot_title=lt,
                award_amount_rsd=amt,
                decision_date=None,
                expiry_date=exp,
            )
            for (lt, wn, amt, exp) in rows
        ]
    )


def _trend_rows(rows=None):
    """Annual dynamics (VG-06 deepening). rows: [(yr, awards, distinct, rsd, eur)]."""
    rows = rows if rows is not None else [(2024, 4, 2, 0, 0), (2023, 2, 1, 0, 0)]
    return _Result(
        rows=[
            SimpleNamespace(
                yr=yr, awards=a, distinct_winners=dw, total_rsd=rsd, total_eur=eur
            )
            for (yr, a, dw, rsd, eur) in rows
        ]
    )


# The customer’s top winner with a non-empty PIB (the condition for the competitor profile).
_WINNERS_OK = [("ДОО Алфа", "111", 6, 9000000), ("ДОО Бета", "222", 3, 2000000)]


# --------------------------------------------------------------------------- #
#  Competitor profile
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_competitor_success_maps_and_formats():
    s = _Session(
        [
            _tenant_row(tier="pro"),
            _customer_row(),
            _cust_summary_row(),
            _cust_winners_rows(_WINNERS_OK),
            _comp_summary_row(),
            _comp_customers_rows(
                [
                    ("Општина Нови Сад", "100236236", 6, 50000000),
                    ("Град Београд", "999", 3, 0),
                ]
            ),
        ]
    )
    v = await get_competitor_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "success"
    # The customer’s top winner (winner[0]) → the competitor.
    assert v.winner_name == "ДОО Алфа"
    assert v.winner_pib == "111"
    assert v.total_wins == 12
    assert v.distinct_customers == 4
    assert v.with_amount == 9
    assert v.total_rsd_fmt == "90 000 000"
    assert v.total_eur_fmt == "760 000"
    assert len(v.top_customers) == 2
    assert v.top_customers[0].rank == 1
    assert v.top_customers[0].name == "Општина Нови Сад"
    assert v.top_customers[0].wins == 6
    assert v.top_customers[0].sum_rsd_fmt == "50 000 000 RSD"
    # Zero amount → empty string (the template will show “—”).
    assert v.top_customers[1].sum_rsd_fmt == ""


@pytest.mark.asyncio
async def test_competitor_set_tenant_called_first():
    s = _Session(
        [
            _tenant_row(),
            _customer_row(),
            _cust_summary_row(),
            _cust_winners_rows(_WINNERS_OK),
            _comp_summary_row(),
            _comp_customers_rows([]),
        ]
    )
    await get_competitor_analytics(s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS)
    assert "set_config" in s.executed[0] or "app.current_tenant_id" in s.executed[0]


@pytest.mark.asyncio
async def test_competitor_gate_pro_for_non_pro_tier():
    s = _Session([_tenant_row(tier="basic")])
    v = await get_competitor_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "gate_pro"


@pytest.mark.asyncio
async def test_competitor_frozen_before_tier_gate():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    s = _Session([_tenant_row(tier="pro", access_until=past)])
    v = await get_competitor_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "frozen"


@pytest.mark.asyncio
async def test_competitor_no_tender():
    s = _Session([_tenant_row(), _Result(rows=[])])
    v = await get_competitor_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_tender"


@pytest.mark.asyncio
async def test_competitor_no_customer():
    s = _Session([_tenant_row(), _customer_row(name="", tax_id="")])
    v = await get_competitor_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_customer"


@pytest.mark.asyncio
async def test_competitor_no_data_when_top_winner_pib_empty():
    # A top winner without a PIB → the competitor cannot be built by PIB.
    s = _Session(
        [
            _tenant_row(),
            _customer_row(),
            _cust_summary_row(),
            _cust_winners_rows([("ДОО Без ПИБ", None, 5, 100)]),
        ]
    )
    v = await get_competitor_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_data"


@pytest.mark.asyncio
async def test_competitor_no_data_when_competitor_stats_empty():
    # A top winner exists, but there is no competitor profile (has_data=False).
    s = _Session(
        [
            _tenant_row(),
            _customer_row(),
            _cust_summary_row(),
            _cust_winners_rows(_WINNERS_OK),
            _comp_summary_row(total_wins=0, distinct_customers=0, with_amount=0),
            _comp_customers_rows([]),
        ]
    )
    v = await get_competitor_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_data"


# --------------------------------------------------------------------------- #
#  Re-procurement forecast
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reprocurement_success_maps_and_formats():
    exp = date.today() + timedelta(days=90)
    s = _Session(
        [
            _tenant_row(tier="pro"),
            _customer_row(),
            _repro_rows(
                [
                    ("Канцеларијски материјал", "ДОО Алфа", 4500000, exp),
                    ("Услуге чишћења", "ДОО Бета", None, exp + timedelta(days=30)),
                ]
            ),
        ]
    )
    v = await get_reprocurement_forecast_view(
        s, tid=_TID, tender_id=_TENDER, horizon_months=12, tier_limits=_LIMITS
    )
    assert v.status == "success"
    assert v.customer_name == "Општина Нови Сад"
    assert v.customer_pib == "100236236"
    assert v.horizon_months == 12
    assert len(v.items) == 2
    assert v.items[0].rank == 1
    assert v.items[0].lot_title == "Канцеларијски материјал"
    assert v.items[0].winner_name == "ДОО Алфа"
    assert v.items[0].expiry_fmt == exp.strftime("%d.%m.%Y")
    assert v.items[0].sum_rsd_fmt == "4 500 000 RSD"
    # An unknown amount → an empty string (the template will show “—”).
    assert v.items[1].sum_rsd_fmt == ""


@pytest.mark.asyncio
async def test_reprocurement_set_tenant_called_first():
    s = _Session([_tenant_row(), _customer_row(), _repro_rows([])])
    await get_reprocurement_forecast_view(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert "set_config" in s.executed[0] or "app.current_tenant_id" in s.executed[0]


@pytest.mark.asyncio
async def test_reprocurement_gate_pro_for_non_pro_tier():
    s = _Session([_tenant_row(tier="basic")])
    v = await get_reprocurement_forecast_view(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "gate_pro"


@pytest.mark.asyncio
async def test_reprocurement_frozen_before_tier_gate():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    s = _Session([_tenant_row(tier="pro", access_until=past)])
    v = await get_reprocurement_forecast_view(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "frozen"


@pytest.mark.asyncio
async def test_reprocurement_no_customer():
    s = _Session([_tenant_row(), _customer_row(name="", tax_id="")])
    v = await get_reprocurement_forecast_view(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_customer"


@pytest.mark.asyncio
async def test_reprocurement_no_data_when_no_contracts():
    s = _Session([_tenant_row(), _customer_row(), _repro_rows([])])
    v = await get_reprocurement_forecast_view(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_data"
    # customer_name/pib are passed through even at no_data (for the template context).
    assert v.customer_name == "Општина Нови Сад"


# --------------------------------------------------------------------------- #
#  Bundle (all three analytics in one pass)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bundle_success_all_three_and_single_set_tenant():
    exp = date.today() + timedelta(days=60)
    s = _Session(
        [
            _tenant_row(tier="pro"),
            _customer_row(),
            _cust_summary_row(),
            _cust_winners_rows(_WINNERS_OK),
            _trend_rows(),  # annual dynamics — between the customer and the competitor
            _comp_summary_row(),
            _comp_customers_rows([("Општина Нови Сад", "100236236", 6, 50000000)]),
            _repro_rows([("Услуге", "ДОО Алфа", 1000000, exp)]),
        ]
    )
    b = await get_narucilac_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert b.customer.status == "success"
    assert b.competitor.status == "success"
    assert b.reprocurement.status == "success"
    # Annual dynamics is present in the customer section.
    assert [y.year for y in b.customer.trend] == [2023, 2024]
    # The competitor reuses the customer’s top winner.
    assert b.competitor.winner_pib == "111"
    assert b.reprocurement.items[0].lot_title == "Услуге"
    # EXACTLY one set_tenant for all three sections (web-ai-4 optimisation).
    set_tenant_calls = [
        e for e in s.executed if "set_config" in e or "app.current_tenant_id" in e
    ]
    assert len(set_tenant_calls) == 1


@pytest.mark.asyncio
async def test_bundle_gate_pro_propagates_to_all_three():
    s = _Session([_tenant_row(tier="basic")])
    b = await get_narucilac_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert b.customer.status == "gate_pro"
    assert b.competitor.status == "gate_pro"
    assert b.reprocurement.status == "gate_pro"


@pytest.mark.asyncio
async def test_bundle_frozen_propagates_to_all_three():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    s = _Session([_tenant_row(tier="pro", access_until=past)])
    b = await get_narucilac_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert b.customer.status == "frozen"
    assert b.competitor.status == "frozen"
    assert b.reprocurement.status == "frozen"


@pytest.mark.asyncio
async def test_bundle_no_customer_propagates_to_all_three():
    s = _Session([_tenant_row(), _customer_row(name="", tax_id="")])
    b = await get_narucilac_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert b.customer.status == "no_customer"
    assert b.competitor.status == "no_customer"
    assert b.reprocurement.status == "no_customer"


@pytest.mark.asyncio
async def test_bundle_mixed_statuses_independent():
    # The customer exists, but the top winner has no PIB (competitor no_data) and no
    # contracts (re-procurement no_data) — the customer is still success.
    s = _Session(
        [
            _tenant_row(),
            _customer_row(),
            _cust_summary_row(),
            _cust_winners_rows([("ДОО Без ПИБ", None, 5, 100)]),
            _trend_rows(),  # The dynamics are computed (the customer is success), even if there is no competitor
            _repro_rows([]),
        ]
    )
    b = await get_narucilac_analytics(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert b.customer.status == "success"
    assert b.customer.trend  # the dynamics are filled
    assert b.competitor.status == "no_data"
    assert b.reprocurement.status == "no_data"


# --------------------------------------------------------------------------- #
#  Parity of the gate predicate
# --------------------------------------------------------------------------- #


def test_gate_predicate_is_the_bot_predicate():
    # Bot boundaries untouched: the same predicate object, a single gating axis.
    assert svc._is_pro_plus is bot_is_pro_plus


# --------------------------------------------------------------------------- #
#  CSV export (web-ai-5) — the same gate/resolve, a pure award layer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_customer_export_success_builds_csv():
    s = _Session(
        [
            _tenant_row(tier="pro"),
            _customer_row(),
            _cust_summary_row(),
            _cust_winners_rows(_WINNERS_OK),
        ]
    )
    v = await svc.get_customer_export(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "success"
    assert v.filename.startswith("awards_customer_") and v.filename.endswith(".csv")
    assert isinstance(v.content, bytes)
    text = v.content.decode("utf-8")
    # UTF-8 BOM + raw data of the customer/winners.
    assert text.startswith("﻿")
    assert "Општина Нови Сад" in text
    assert "ДОО Алфа" in text


@pytest.mark.asyncio
async def test_customer_export_set_tenant_called_first():
    s = _Session(
        [
            _tenant_row(),
            _customer_row(),
            _cust_summary_row(),
            _cust_winners_rows(_WINNERS_OK),
        ]
    )
    await svc.get_customer_export(s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS)
    assert "set_config" in s.executed[0] or "app.current_tenant_id" in s.executed[0]


@pytest.mark.asyncio
async def test_customer_export_gate_pro_for_non_pro_tier():
    s = _Session([_tenant_row(tier="basic")])
    v = await svc.get_customer_export(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "gate_pro"
    assert v.content == b""


@pytest.mark.asyncio
async def test_customer_export_frozen_before_tier_gate():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    s = _Session([_tenant_row(tier="pro", access_until=past)])
    v = await svc.get_customer_export(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "frozen"


@pytest.mark.asyncio
async def test_customer_export_no_data():
    s = _Session([_tenant_row(), _customer_row(), _cust_summary_row(total_awards=0)])
    v = await svc.get_customer_export(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_data"


@pytest.mark.asyncio
async def test_competitor_export_success_builds_csv():
    s = _Session(
        [
            _tenant_row(tier="pro"),
            _customer_row(),
            _cust_summary_row(),
            _cust_winners_rows(_WINNERS_OK),
            _comp_summary_row(),
            _comp_customers_rows([("Општина Нови Сад", "100236236", 6, 50000000)]),
        ]
    )
    v = await svc.get_competitor_export(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "success"
    assert v.filename.startswith("awards_competitor_") and v.filename.endswith(".csv")
    text = v.content.decode("utf-8")
    assert text.startswith("﻿")
    # The competitor profile = the customer’s top winner (winner[0]).
    assert "ДОО Алфа" in text
    assert "Општина Нови Сад" in text


@pytest.mark.asyncio
async def test_competitor_export_no_data_when_top_winner_pib_empty():
    s = _Session(
        [
            _tenant_row(),
            _customer_row(),
            _cust_summary_row(),
            _cust_winners_rows([("ДОО Без ПИБ", None, 5, 100)]),
        ]
    )
    v = await svc.get_competitor_export(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "no_data"


@pytest.mark.asyncio
async def test_competitor_export_gate_pro_for_non_pro_tier():
    s = _Session([_tenant_row(tier="basic")])
    v = await svc.get_competitor_export(
        s, tid=_TID, tender_id=_TENDER, tier_limits=_LIMITS
    )
    assert v.status == "gate_pro"
