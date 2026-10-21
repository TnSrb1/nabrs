"""Tests of narucilac_page_service (A3, ADR-013: mock session without DB).

We check the “Customer analytics” page in the cabinet:
- set_tenant is called FIRST (RLS invariant), the gate — BEFORE reading the award data;
- the Pro+ gate / freeze reuse the bot’s predicate (the bot boundaries are untouched);
- the profile is keyed BY NAME — by the same predicate as the directory (otherwise the counter in the list would diverge from the profile);
- no customer selected → the directory without a profile; selected without decisions → no_data (an empty state, not a 500 and not an empty table);
- success: prices and the sampling period are passed to the view formatted.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.analytics.awards_analytics import _CUSTOMER_NAME_KEY
from src.api.cabinet import customer_analytics_service as cas
from src.api.cabinet import narucilac_page_service as nps
from src.api.cabinet.narucilac_page_service import (
    get_narucilac_export,
    get_narucilac_page,
)
from src.bots import analytics_export as bot_export
from src.bots.awards_analytics_service import _is_pro_plus as bot_is_pro_plus

_TID = "11111111-1111-1111-1111-111111111111"

# Explicit limits (not tied to the real _TIER_LIMITS): Pro+ := bid != 0.
_LIMITS = {"pro": {"bid": 5}, "basic": {"bid": 0}}


class _Result:
    def __init__(self, *, rows=None):
        self._rows = list(rows) if rows is not None else []

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Session:
    """Mock AsyncSession: set_config (RLS) does not touch the queue, but the fact of the call is recorded in ``executed`` to check the set_tenant invariant."""

    def __init__(self, results):
        self._results = list(results)
        self.executed: list[str] = []
        self.params: list[dict | None] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append(sql)
        self.params.append(params)
        if "set_config" in sql or "app.current_tenant_id" in sql:
            return _Result()
        return self._results.pop(0) if self._results else _Result()


def _tenant_row(tier="pro", access_until=None, status="active"):
    return _Result(
        rows=[SimpleNamespace(tier=tier, access_until=access_until, status=status)]
    )


def _directory_rows(rows=None):
    if rows is None:
        rows = [
            SimpleNamespace(
                customer_name="Општина Нови Сад",
                customer_pib="100236236",
                awards=12,
                last_date=date(2026, 5, 4),
            )
        ]
    return _Result(rows=rows)


def _summary_row(**over):
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
    return _Result(rows=[SimpleNamespace(**base)])


def _winners_rows():
    return _Result(
        rows=[
            SimpleNamespace(
                winner_name="Лидер д.о.о.",
                winner_pib="111",
                wins=6,
                sum_rsd=Decimal("90000000"),
            )
        ]
    )


def _trend_rows():
    return _Result(
        rows=[
            SimpleNamespace(
                yr=2026,
                awards=4,
                distinct_winners=2,
                total_rsd=Decimal("50000000"),
                total_eur=Decimal("400000"),
            ),
            SimpleNamespace(
                yr=2025,
                awards=6,
                distinct_winners=3,
                total_rsd=Decimal("70000000"),
                total_eur=Decimal("600000"),
            ),
        ]
    )


def _ok_session():
    """The full success path: plan → directory → summary → winners → trend."""
    return _Session(
        [
            _tenant_row(),
            _directory_rows(),
            _summary_row(),
            _winners_rows(),
            _trend_rows(),
        ]
    )


# --------------------------------------------------------------------------- #
#  RLS / gate
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_set_tenant_is_first_statement():
    session = _ok_session()
    await get_narucilac_page(session, tid=_TID, tier_limits=_LIMITS)

    assert session.executed, "there was not a single request"
    first = session.executed[0]
    assert "set_config" in first or "app.current_tenant_id" in first


@pytest.mark.asyncio
async def test_non_pro_is_gated_before_reading_awards():
    """A non-Pro must not get even the directory of customers."""
    session = _Session([_tenant_row(tier="basic")])
    page = await get_narucilac_page(session, tid=_TID, tier_limits=_LIMITS)

    assert page.status == "gate_pro"
    assert page.candidates == []
    assert not any("tender_awards" in sql for sql in session.executed)


@pytest.mark.asyncio
async def test_frozen_wins_over_tier():
    """The freeze is checked BEFORE the plan (FIX-SG-04) — even for Pro."""
    past = datetime.now(timezone.utc) - timedelta(days=1)
    session = _Session([_tenant_row(tier="pro", access_until=past)])
    page = await get_narucilac_page(session, tid=_TID, tier_limits=_LIMITS)

    assert page.status == "frozen"


def test_gate_predicate_is_literally_the_bot_one():
    """The gating axis is one: not a “same” predicate, but the SAME object (the project rules “plan axes”).
    Comparison by identity — a copy of the predicate would make the test fall."""
    assert cas._is_pro_plus is bot_is_pro_plus


@pytest.mark.asyncio
async def test_gate_routes_through_shared_predicate(monkeypatch):
    """The page really asks the shared predicate, not its own list of plans.

    The previous version of the test called the bot’s predicate directly and was a tautology: the service could be rewritten with its own list — the test would stay green. Here we substitute the shared predicate and check that the page’s verdict changed.
    """
    seen: list[str | None] = []

    def _spy(tier, limits):
        seen.append(tier)
        return False

    monkeypatch.setattr(cas, "_is_pro_plus", _spy)
    session = _ok_session()
    page = await get_narucilac_page(session, tid=_TID, tier_limits=_LIMITS)

    assert seen == ["pro"], "the gate did not ask the shared predicate"
    assert page.status == "gate_pro"


# --------------------------------------------------------------------------- #
#  Directory / selection
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_customer_selected_shows_directory_without_profile():
    session = _Session([_tenant_row(), _directory_rows()])
    page = await get_narucilac_page(session, tid=_TID, tier_limits=_LIMITS)

    assert page.status == "success"
    assert page.selected is None
    assert page.selected_status == ""
    assert [(c.name, c.pib, c.awards, c.last_date_fmt) for c in page.candidates] == [
        ("Општина Нови Сад", "100236236", 12, "04.05.2026")
    ]


@pytest.mark.asyncio
async def test_selected_customer_is_keyed_by_name_not_pib():
    """‼️ The profile must count by the same predicate as the directory (by name).

    Keying by PIB would undercount the rows with an empty PIB (22.9%) — the list would promise one number of decisions, the profile would show another.
    """
    session = _ok_session()
    await get_narucilac_page(
        session, tid=_TID, customer_name="Општина Нови Сад", tier_limits=_LIMITS
    )

    summary_calls = [
        (sql, prm)
        for sql, prm in zip(session.executed, session.params)
        if "total_awards" in sql
    ]
    assert summary_calls, "the summary was not requested"
    sql, params = summary_calls[0]
    # The predicate is by NAME (not by PIB), but on the normalized one (A4): the literal
    # exact match split the spelling variants of one customer across different
    # profiles. We check the key itself, not the outdated literal `customer_name = :name`.
    assert _CUSTOMER_NAME_KEY + " = :name" in sql
    assert "customer_pib = :pib" not in sql
    assert "customer_pib" not in params
    assert params["name"] == "општина нови сад"


@pytest.mark.asyncio
async def test_selected_customer_without_awards_is_no_data_not_error():
    """Empty state: the customer exists, there are no decisions → a clear hint, not a 500."""
    session = _Session(
        [_tenant_row(), _directory_rows(), _summary_row(total_awards=0), _Result()]
    )
    page = await get_narucilac_page(
        session, tid=_TID, customer_name="Општина Без Решења", tier_limits=_LIMITS
    )

    assert page.status == "success"
    assert page.selected_status == "no_data"
    assert page.selected is None
    assert page.selected_name == "Општина Без Решења"
    assert page.candidates, "the directory stays — the user has somewhere to go next"


@pytest.mark.asyncio
async def test_blank_selected_name_is_treated_as_not_selected():
    """?name= (empty/spaces) is not a selection: otherwise we would search for a customer with the name ''."""
    session = _Session([_tenant_row(), _directory_rows()])
    page = await get_narucilac_page(
        session, tid=_TID, customer_name="   ", tier_limits=_LIMITS
    )

    assert page.selected_status == ""
    assert not any("total_awards" in sql for sql in session.executed)


@pytest.mark.asyncio
async def test_search_term_is_trimmed_and_capped():
    session = _Session([_tenant_row(), _directory_rows()])
    page = await get_narucilac_page(
        session, tid=_TID, term="  " + "я" * 500, tier_limits=_LIMITS
    )

    assert len(page.term) == 120


# --------------------------------------------------------------------------- #
#  success — mapping to the view
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_success_maps_prices_period_and_winners():
    session = _ok_session()
    page = await get_narucilac_page(
        session, tid=_TID, customer_name="Општина Нови Сад", tier_limits=_LIMITS
    )

    assert page.status == "success"
    assert page.selected_status == "success"
    a = page.selected
    assert a.customer_name == "Општина Нови Сад"
    # The PIB is pulled in for reference from the directory (without an extra request).
    assert a.customer_pib == "100236236"
    assert a.total_awards == 10
    assert a.distinct_winners == 3
    # A space as the thousands separator — the common format of the cabinet/bot.
    assert a.median_rsd_fmt == "15 000 000 RSD"
    assert a.avg_rsd_fmt == "20 000 000 RSD"
    assert a.period_fmt == "12.03.2019 — 04.05.2026"
    assert a.without_date == 2
    assert a.top_winners[0].name == "Лидер д.о.о."
    assert a.top_winners[0].wins == 6
    # The trend unfolds into chronology (the SQL gives it in descending year order).
    assert [y.year for y in a.trend] == [2025, 2026]


@pytest.mark.asyncio
async def test_success_without_amounts_renders_dashes_not_zeroes():
    """No amounts → empty format strings (the template will show “—”), not “0 RSD”."""
    session = _Session(
        [
            _tenant_row(),
            _directory_rows(),
            _summary_row(with_amount=0, avg_rsd=None, median_rsd=None),
            _winners_rows(),
            _trend_rows(),
        ]
    )
    page = await get_narucilac_page(
        session, tid=_TID, customer_name="Општина Нови Сад", tier_limits=_LIMITS
    )

    assert page.selected.avg_rsd_fmt == ""
    assert page.selected.median_rsd_fmt == ""


@pytest.mark.asyncio
async def test_success_without_any_decision_date_has_empty_period():
    session = _Session(
        [
            _tenant_row(),
            _directory_rows(),
            _summary_row(first_date=None, last_date=None, without_date=10),
            _winners_rows(),
            _trend_rows(),
        ]
    )
    page = await get_narucilac_page(
        session, tid=_TID, customer_name="Општина Нови Сад", tier_limits=_LIMITS
    )

    assert page.selected.period_fmt == ""
    assert page.selected.without_date == 10


@pytest.mark.asyncio
async def test_single_day_period_has_no_dash():
    session = _Session(
        [
            _tenant_row(),
            _directory_rows(),
            _summary_row(first_date=date(2026, 5, 4), last_date=date(2026, 5, 4)),
            _winners_rows(),
            _trend_rows(),
        ]
    )
    page = await get_narucilac_page(
        session, tid=_TID, customer_name="Општина Нови Сад", tier_limits=_LIMITS
    )

    assert page.selected.period_fmt == "04.05.2026"


# --------------------------------------------------------------------------- #
#  CSV export (C4)
# --------------------------------------------------------------------------- #


def _export_session():
    """The export path: plan → summary → winners. The directory is NOT read —
    the export does not need it (the profile is keyed by name, the PIB is reference here).
    """
    return _Session([_tenant_row(), _summary_row(), _winners_rows()])


@pytest.mark.asyncio
async def test_export_set_tenant_is_first_statement():
    session = _export_session()
    await get_narucilac_export(
        session, tid=_TID, customer_name="Општина Нови Сад", tier_limits=_LIMITS
    )

    assert session.executed, "there was not a single request"
    first = session.executed[0]
    assert "set_config" in first or "app.current_tenant_id" in first


@pytest.mark.asyncio
async def test_export_non_pro_is_gated_before_reading_awards():
    """The same gate as the page: a non-Pro neither exports nor reads the award data."""
    session = _Session([_tenant_row(tier="basic")])
    export = await get_narucilac_export(
        session, tid=_TID, customer_name="Општина Нови Сад", tier_limits=_LIMITS
    )

    assert export.status == "gate_pro"
    assert export.content == b""
    assert not any("tender_awards" in sql for sql in session.executed)


@pytest.mark.asyncio
async def test_export_frozen_wins_over_tier():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    session = _Session([_tenant_row(tier="pro", access_until=past)])
    export = await get_narucilac_export(
        session, tid=_TID, customer_name="Општина Нови Сад", tier_limits=_LIMITS
    )

    assert export.status == "frozen"


@pytest.mark.asyncio
async def test_export_gate_routes_through_shared_predicate(monkeypatch):
    """The export asks the SHARED predicate, not its own list of plans.

    Without this the export could acquire its own gating axis and diverge from the page that offers it (the project rules “plan axes”).
    """
    seen: list[str | None] = []

    def _spy(tier, limits):
        seen.append(tier)
        return False

    monkeypatch.setattr(cas, "_is_pro_plus", _spy)
    export = await get_narucilac_export(
        _export_session(),
        tid=_TID,
        customer_name="Општина Нови Сад",
        tier_limits=_LIMITS,
    )

    assert seen == ["pro"], "the gate did not ask the shared predicate"
    assert export.status == "gate_pro"


@pytest.mark.asyncio
async def test_export_without_selected_customer_is_no_customer():
    """?name= empty — there is nothing to export; we do not request the summary."""
    session = _Session([_tenant_row()])
    export = await get_narucilac_export(
        session, tid=_TID, customer_name="   ", tier_limits=_LIMITS
    )

    assert export.status == "no_customer"
    assert not any("total_awards" in sql for sql in session.executed)


@pytest.mark.asyncio
async def test_export_without_awards_is_no_data():
    session = _Session([_tenant_row(), _summary_row(total_awards=0), _Result()])
    export = await get_narucilac_export(
        session, tid=_TID, customer_name="Општина Без Решења", tier_limits=_LIMITS
    )

    assert export.status == "no_data"
    assert export.content == b""


@pytest.mark.asyncio
async def test_export_is_keyed_by_name_like_the_page():
    """The export key = the page key (the normalized name, not the PIB).

    If they diverged, the downloaded file would show something other than what the user saw on the screen.
    """
    session = _export_session()
    await get_narucilac_export(
        session, tid=_TID, customer_name="Општина Нови Сад", tier_limits=_LIMITS
    )

    summary_calls = [
        (sql, prm)
        for sql, prm in zip(session.executed, session.params)
        if "total_awards" in sql
    ]
    assert summary_calls, "the summary was not requested"
    sql, params = summary_calls[0]
    assert _CUSTOMER_NAME_KEY + " = :name" in sql
    assert "customer_pib = :pib" not in sql
    assert params["name"] == "општина нови сад"


@pytest.mark.asyncio
async def test_export_takes_full_top_not_the_page_five():
    """The file contains the top 50 (EXPORT_TOP_N), not the top five shown on the page."""
    session = _export_session()
    await get_narucilac_export(
        session, tid=_TID, customer_name="Општина Нови Сад", tier_limits=_LIMITS
    )

    limits_asked = [p["limit"] for p in session.params if p and "limit" in p]
    assert limits_asked == [bot_export.EXPORT_TOP_N]


@pytest.mark.asyncio
async def test_export_success_builds_excel_readable_cyrillic_csv():
    """UTF-8 BOM + ';' — otherwise Excel in RS/EU locales will show gibberish / one column."""
    session = _export_session()
    export = await get_narucilac_export(
        session, tid=_TID, customer_name="Општина Нови Сад", tier_limits=_LIMITS
    )

    assert export.status == "success"
    assert export.content.startswith(
        b"\xef\xbb\xbf"
    ), "no BOM — Excel will break the Cyrillic"
    text = export.content.decode("utf-8-sig")
    assert "Наручилац;Општина Нови Сад" in text
    # The profile data and the top winners — in the file.
    assert "Всего наград;10" in text
    assert "Лидер д.о.о.;111;6;90000000" in text
    assert export.filename.endswith(".csv")


def test_export_reuses_the_bot_csv_builder():
    """There must be no second CSV contour: not a “same” format, but the SAME object.

    Comparison by identity — a copy of the builder in the web layer would make the test fall.
    """
    assert nps.build_customer_csv is bot_export.build_customer_csv
    assert nps.customer_csv_filename is bot_export.customer_csv_filename


# --------------------------------------------------------------------------- #
#  Sorting / pages of the directory
# --------------------------------------------------------------------------- #


def _dir_row(name, total, awards=3):
    return SimpleNamespace(
        customer_name=name,
        customer_pib=None,
        awards=awards,
        last_date=None,
        total=total,
    )


def _dir_params(session):
    return next(
        p for sql, p in zip(session.executed, session.params) if "tender_awards" in sql
    )


@pytest.mark.asyncio
async def test_sort_and_page_reach_sql_as_binds():
    session = _Session([_tenant_row(), _Result(rows=[_dir_row("Општина А", 45)])])
    page = await get_narucilac_page(
        session, tid=_TID, sort="name_asc", page=3, tier_limits=_LIMITS
    )

    sql = next(s for s in session.executed if "tender_awards" in s)
    assert "ORDER BY customer_name ASC, awards DESC" in sql
    assert _dir_params(session) == {"limit": 20, "offset": 40}
    assert (page.sort, page.page, page.total, page.total_pages) == (
        "name_asc",
        3,
        45,
        3,
    )


@pytest.mark.asyncio
async def test_unknown_sort_and_bad_page_fall_back_to_defaults():
    session = _Session([_tenant_row(), _Result(rows=[_dir_row("Општина А", 1)])])
    page = await get_narucilac_page(
        session, tid=_TID, sort="x'; DROP TABLE y--", page=-5, tier_limits=_LIMITS
    )

    sql = next(s for s in session.executed if "tender_awards" in s)
    assert "ORDER BY awards DESC, customer_name" in sql
    assert "DROP" not in sql
    assert _dir_params(session)["offset"] == 0
    assert (page.sort, page.page) == ("decisions_desc", 1)


@pytest.mark.asyncio
async def test_page_past_the_end_goes_to_last_page():
    # An empty page 9 (offset 160): the probe of the first page gives total=25 → 2 pages,
    # then — the last (offset 20), not the first.
    session = _Session(
        [
            _tenant_row(),
            _Result(rows=[]),
            _Result(rows=[_dir_row("Општина А", 25)]),
            _Result(rows=[_dir_row("Општина Б", 25)]),
        ]
    )
    page = await get_narucilac_page(session, tid=_TID, page=9, tier_limits=_LIMITS)

    offsets = [p["offset"] for p in session.params if p and "offset" in p]
    assert offsets == [160, 0, 20]
    assert (page.page, page.total_pages, len(page.candidates)) == (2, 2, 1)
    assert page.candidates[0].name == "Општина Б"
