"""The contracting-authority analytics of the tender in the web cabinet (web-ai-3/4, steps 3–4).

“Value up in the web”: we show in the cabinet the customer analytics, until now
available only in the Telegram bot. Three sections on the tender details page, all
tied to ``tender_id``:

- **contracting authority** (web-ai-3, VG-06) — who and for what sums wins from the customer,
  supplier concentration (``get_customer_analytics``);
- **competitor** (web-ai-4, VG-06 E2) — the profile of the strongest winner of this
  contracting authority: where else he wins (``get_competitor_analytics``);
- **re-procurements** (web-ai-4, VG-07) — the customer’s contracts expiring in the
  nearest ``horizon_months`` months (``get_reprocurement_forecast_view``).

The difference from the AI artifacts (web-ai-1/2): this is PURE SQL on top of ``tender_awards``
(read-only aggregations, near-zero cost) — WITHOUT calling a model, WITHOUT usage/limits,
WITHOUT ``chat_id``. The only gate — the Pro+ plan and the freeze (as for the bid verdict).

The bot’s boundaries are untouched: the gate predicate (``_is_pro_plus``) and the analytics layer
(``customer_award_stats``/``competitor_stats``/``get_reprocurement_forecast``)
are reused as they are — one gating axis, one source of aggregations. ``tid``
is taken from the web session (NOT chat_id → ``resolve_tenant_id`` is not needed).
``set_tenant(tid)`` — the FIRST action of the transaction (the single RLS invariant, although
the award tables are public and the query does no cross-tenant bypass).

Optimization web-ai-4: the tender page now has up to 3 contracting-authority analytics.
``get_narucilac_analytics`` collects all three in ONE pass
(``set_tenant``+gate+resolve of the authority+``customer_award_stats`` — once each;
the competitor profile reuses the top winner from the already computed
``CustomerStats``). The separate ``get_*`` functions are kept for point calls and
tests; the common path goes through the private ``_gate``/``_resolve_customer``.

Result statuses: ``success`` | ``gate_pro`` | ``frozen`` | ``no_tender`` |
``no_customer`` | ``no_data`` (``no_account`` from the bot is not applicable — the session
guarantees the tenant).

Tested on a mock session (ADR-013), like ``tender_ai_service``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import text

from src.ai.usage import _TIER_LIMITS
from src.analytics.awards_analytics import (
    CompetitorStats,
    CustomerStats,
    RecentAwardRow,
    YearRow,
    competitor_stats,
    customer_award_stats,
    customer_recent_awards,
    customer_yearly_trend,
)
from src.analytics.reprocurement import ReprocurementRow, get_reprocurement_forecast
from src.api import repo

# The Pro+ gate predicate is reused from the bot service (the same gating axis as the
# bid verdict); we do NOT duplicate the list of plans/thresholds. See CLAUDE.md “plan axes”.
from src.bots.awards_analytics_service import _is_pro_plus

# Pure CSV builders (stdlib csv/io, without network/DB/AI) — reused as
# they are from the bot layer's export: one point of truth for the CSV format (UTF-8 BOM, ';',
# CRLF, raw sums). The bot files are not edited — only import/call (CLAUDE.md).
from src.bots.analytics_export import (
    EXPORT_TOP_N,
    build_competitor_csv,
    build_customer_csv,
    competitor_csv_filename,
    customer_csv_filename,
)
from src.domain.access import is_frozen
from src.domain.tiers import effective_tier

# --------------------------------------------------------------------------- #
# SQL (asyncpg in text(): only CAST(:p AS type))
# --------------------------------------------------------------------------- #

_TENANT_TIER_SQL = text("""
    SELECT tier, access_until, status
    FROM tenants
    WHERE id = CAST(:tid AS uuid)
    LIMIT 1
""")

_TENDER_CUSTOMER_SQL = text("""
    SELECT customer_name, customer_tax_id, title
    FROM tenders_normalized
    WHERE id = CAST(:tender_id AS uuid)
    LIMIT 1
""")


# --------------------------------------------------------------------------- #
# View models (values are already formatted for the template)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WinnerView:
    """A row of the top winners for rendering (the sum is already a string in the ru format)."""

    rank: int
    name: str
    wins: int
    sum_rsd_fmt: str  # "" if the sum is unknown/zero


@dataclass(frozen=True)
class TrendYearView:
    """A year of the contracting authority’s dynamics for rendering (sums are already strings in the ru format).

    ``bar_pct`` — the height of the bar relative to the most active year of the series
    (0..100), for a mini chart in the template without JS.
    """

    year: int
    awards: int
    distinct_winners: int
    total_rsd_fmt: str  # "" if there is no sum
    total_eur_fmt: str
    bar_pct: int


@dataclass(frozen=True)
class CustomerAnalyticsView:
    """The contracting-authority analytics result for the tender page section.

    ``status`` controls the rendering; for ``success`` the other fields are filled.
    """

    status: str  # success|gate_pro|frozen|no_tender|no_customer|no_data
    customer_name: str = ""
    customer_pib: str = ""
    total_awards: int = 0
    distinct_winners: int = 0
    with_amount: int = 0
    total_rsd_fmt: str = ""
    total_eur_fmt: str = ""
    concentration_pct: int | None = None
    cr3_pct: int | None = None
    is_monopolized: bool = False
    top_winners: list[WinnerView] = field(default_factory=list)
    trend: list[TrendYearView] = field(default_factory=list)
    # Prices and the sample period (A3). ““ = unknown → the template draws ”—”/hides the block.
    avg_rsd_fmt: str = ""
    median_rsd_fmt: str = ""
    period_fmt: str = ""
    without_date: int = 0


@dataclass(frozen=True)
class RivalView:
    """A row of the competitor’s customer top (the sum is already a string in the ru format)."""

    rank: int
    name: str
    wins: int
    sum_rsd_fmt: str  # "" if the sum is unknown/zero


@dataclass(frozen=True)
class CompetitorAnalyticsView:
    """The profile of the strongest winner of the contracting authority for the tender page section.

    ``status`` controls the rendering; for ``success`` the other fields are filled.
    """

    status: str  # success|gate_pro|frozen|no_tender|no_customer|no_data
    winner_name: str = ""
    winner_pib: str = ""
    total_wins: int = 0
    distinct_customers: int = 0
    with_amount: int = 0
    total_rsd_fmt: str = ""
    total_eur_fmt: str = ""
    top_customers: list[RivalView] = field(default_factory=list)


@dataclass(frozen=True)
class ReprocurementItemView:
    """One expected re-procurement (date/sum — already formatted)."""

    rank: int
    lot_title: str
    winner_name: str
    expiry_fmt: str  # dd.mm.YYYY
    months_until_expiry: int
    sum_rsd_fmt: str  # "" if the sum is unknown/zero


@dataclass(frozen=True)
class ReprocurementForecastView:
    """The re-procurement forecast of the contracting authority for the tender page section."""

    status: str  # success|gate_pro|frozen|no_tender|no_customer|no_data
    customer_name: str = ""
    customer_pib: str = ""
    horizon_months: int = 12
    items: list[ReprocurementItemView] = field(default_factory=list)


@dataclass(frozen=True)
class IncumbentItemView:
    """One past decision of the contracting authority for the “who won before” feed (A4).

    The values are already formatted for the template. ``sum_rsd_fmt=""`` — the sum is not yet
    published (the template shows “sum not published”, and NOT “0 RSD”).
    ``pib`` is empty for a consortium: a comma-joined list of PIBs is not an identifier.
    """

    winner_name: str
    pib: str  # "" — the PIB is unknown OR the decision is in favour of a consortium
    date_fmt: str  # dd.mm.YYYY; "" if the decision date is unknown
    sum_rsd_fmt: str  # "" if the sum is not published
    lots: int  # >1 — the decision covers several lots (the sum — for the whole decision)
    is_joint: bool  # a decision in favour of several winners (consortium)


@dataclass(frozen=True)
class IncumbentView:
    """The feed of the latest decisions of the contracting authority for the tender card (A4).

    ``status``: success | no_data (the gate/resolve come from the common pass —
    ``gate_pro`` | ``frozen`` | ``no_tender`` | ``no_customer``).
    """

    status: str
    customer_name: str = ""
    items: list[IncumbentItemView] = field(default_factory=list)


@dataclass(frozen=True)
class NarucilacAnalyticsBundle:
    """The contracting-authority analytics of one tender page, collected in one pass."""

    customer: CustomerAnalyticsView
    competitor: CompetitorAnalyticsView
    reprocurement: ReprocurementForecastView
    incumbent: IncumbentView = field(default_factory=lambda: IncumbentView("no_data"))


# --------------------------------------------------------------------------- #
# Money/date formatting (moved from the bot rendering: a space as the thousands separator)
# --------------------------------------------------------------------------- #


def _fmt_int(value: Decimal) -> str:
    """An integer with a thousands separator space: 12345678 → '12 345 678'.

    The same format as ``_fmt_money_rsd``/``_fmt_eur`` in the bot rendering (VG-06)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "0"
    return f"{n:,}".replace(",", " ")


def _fmt_sum_rsd(value: Decimal | None) -> str:
    """The sum in RSD or ““ for zero/None (the template shows ”—”)."""
    return f"{_fmt_int(value)} RSD" if value and value > 0 else ""


def _fmt_date(value: date | None) -> str:
    """dd.mm.YYYY or "" for None (the same format as the bot rendering of re-procurements)."""
    return value.strftime("%d.%m.%Y") if value is not None else ""


def _fmt_period(first: date | None, last: date | None) -> str:
    """The sample period “dd.mm.YYYY — dd.mm.YYYY” (A3).

    It is shown next to the aggregate always when the dates are known: “5 decisions” without
    a period reads as “for some recent time”, although it may be over 7 years. One
    day (first == last) — without a dash. If there is not a single date → "" (the template hides the block).
    """
    if first is None and last is None:
        return ""
    if first is None or last is None:
        return _fmt_date(first or last)
    if first == last:
        return _fmt_date(first)
    return f"{_fmt_date(first)} — {_fmt_date(last)}"


# --------------------------------------------------------------------------- #
# View assembly from the stat layer
# --------------------------------------------------------------------------- #


def _trend_from_rows(rows: list[YearRow]) -> list[TrendYearView]:
    """Yearly rows → a view with the relative height of the bar (``bar_pct``).

    ``bar_pct`` is normalized to the maximum of ``awards`` in the series (the most active year =
    100). An empty series → []; a year with 0 decisions (theoretically) → bar_pct 0.
    """
    if not rows:
        return []
    max_awards = max((r.awards for r in rows), default=0)
    return [
        TrendYearView(
            year=r.year,
            awards=r.awards,
            distinct_winners=r.distinct_winners,
            total_rsd_fmt=_fmt_sum_rsd(r.total_rsd),
            total_eur_fmt=_fmt_int(r.total_eur),
            bar_pct=round(r.awards * 100 / max_awards) if max_awards > 0 else 0,
        )
        for r in rows
    ]


def _view_from_stats(
    customer_name: str,
    customer_pib: str,
    stats: CustomerStats,
    trend_rows: list[YearRow] | None = None,
) -> CustomerAnalyticsView:
    """Assembles a view from :class:`CustomerStats` (formats the sums for the template).

    ``trend_rows`` (optional) — the yearly dynamics from :func:`customer_yearly_trend`;
    None/[] → the dynamics section is not rendered in the template.
    """
    top = [
        WinnerView(
            rank=i,
            name=(w.name or "—"),
            wins=w.wins,
            sum_rsd_fmt=_fmt_sum_rsd(w.sum_rsd),
        )
        for i, w in enumerate(stats.top_winners, 1)
    ]
    return CustomerAnalyticsView(
        status="success",
        customer_name=customer_name or "—",
        customer_pib=customer_pib,
        total_awards=stats.total_awards,
        distinct_winners=stats.distinct_winners,
        with_amount=stats.with_amount,
        total_rsd_fmt=_fmt_int(stats.total_rsd),
        total_eur_fmt=_fmt_int(stats.total_eur),
        concentration_pct=stats.concentration_pct,
        cr3_pct=stats.cr3_pct,
        is_monopolized=stats.is_monopolized,
        top_winners=top,
        trend=_trend_from_rows(trend_rows or []),
        avg_rsd_fmt=_fmt_sum_rsd(stats.avg_rsd),
        median_rsd_fmt=_fmt_sum_rsd(stats.median_rsd),
        period_fmt=_fmt_period(stats.first_date, stats.last_date),
        without_date=stats.without_date,
    )


def _competitor_view_from_stats(
    winner_name: str, winner_pib: str, stats: CompetitorStats
) -> CompetitorAnalyticsView:
    """Assembles a view from :class:`CompetitorStats` (formats the sums for the template)."""
    top = [
        RivalView(
            rank=i,
            name=(c.name or "—"),
            wins=c.wins,
            sum_rsd_fmt=_fmt_sum_rsd(c.sum_rsd),
        )
        for i, c in enumerate(stats.top_customers, 1)
    ]
    return CompetitorAnalyticsView(
        status="success",
        winner_name=winner_name or "—",
        winner_pib=winner_pib,
        total_wins=stats.total_wins,
        distinct_customers=stats.distinct_customers,
        with_amount=stats.with_amount,
        total_rsd_fmt=_fmt_int(stats.total_rsd),
        total_eur_fmt=_fmt_int(stats.total_eur),
        top_customers=top,
    )


def _reprocurement_view_from_rows(
    customer_name: str,
    customer_pib: str,
    rows: list[ReprocurementRow],
    horizon_months: int,
) -> ReprocurementForecastView:
    """Assembles a view from the list of :class:`ReprocurementRow` (formats dates/sums)."""
    items = [
        ReprocurementItemView(
            rank=i,
            lot_title=(r.lot_title or "—"),
            winner_name=(r.winner_name or "—"),
            expiry_fmt=_fmt_date(r.expiry_date),
            months_until_expiry=r.months_until_expiry,
            sum_rsd_fmt=_fmt_sum_rsd(r.award_amount_rsd),
        )
        for i, r in enumerate(rows, 1)
    ]
    return ReprocurementForecastView(
        status="success",
        customer_name=customer_name or "—",
        customer_pib=customer_pib,
        horizon_months=horizon_months,
        items=items,
    )


# --------------------------------------------------------------------------- #
# Common steps of the gate/resolve (a single pass; AFTER set_tenant)
# --------------------------------------------------------------------------- #


def narucilac_gate(
    status: str | None,
    tier: str | None,
    access_until: datetime | None,
    now: datetime,
    limits: dict[str, dict[str, int | None]] | None = None,
) -> str | None:
    """A pure gate of the customers page → ``None`` | ``frozen`` | ``gate_pro``.

    One predicate for the page, the bot-like paths (``_gate``) and the plan marking in the
    cabinet menu (``nav_access``): the freeze FIRST (FIX-SG-04), then Pro+ from the
    EFFECTIVE plan (trial → Pro rights, ARCHITECTURE §4).
    """
    if is_frozen(access_until, now):
        return "frozen"
    if not _is_pro_plus(
        effective_tier(status, tier), limits if limits is not None else _TIER_LIMITS
    ):
        return "gate_pro"
    return None


async def _gate(
    db, *, tid: str, limits: dict[str, dict[str, int | None]]
) -> str | None:
    """Freeze + Pro+ gate (the same order and predicate as in the bot: frozen BEFORE the
    plan — FIX-SG-04). Returns a status error (frozen|gate_pro) or None.

    Assumes that ``set_tenant`` has already been called by the caller (RLS invariant).
    """
    trow = (await db.execute(_TENANT_TIER_SQL, {"tid": str(tid)})).first()
    access_until = getattr(trow, "access_until", None) if trow is not None else None
    status_val = str(trow.status) if trow and getattr(trow, "status", None) else None
    raw_tier = str(trow.tier) if trow and trow.tier else None
    return narucilac_gate(
        status_val, raw_tier, access_until, datetime.now(timezone.utc), limits
    )


async def _resolve_customer(db, *, tender_id: str) -> tuple[str, str, str | None]:
    """The contracting authority of the current tender from ``tenders_normalized``.

    Returns ``(customer_pib, customer_name, error_status)``: we key by PIB, and
    with an empty PIB — a fallback by name (normalized, see ``_CUSTOMER_NAME_KEY``).
    No tender → ``no_tender``; no PIB and no name → ``no_customer``.

    ‼️ In practice the name fallback is what works: for tenders with award data
    ``customer_tax_id`` is empty in 100% of cases (0 of 1454 on prod), i.e. the PIB branch
    for these analytics is dead. Keying the analytics by PIB would show NOTHING.
    """
    crow = (await db.execute(_TENDER_CUSTOMER_SQL, {"tender_id": tender_id})).first()
    if crow is None:
        return "", "", "no_tender"
    customer_pib = str(crow.customer_tax_id) if crow.customer_tax_id else ""
    customer_name = str(crow.customer_name) if crow.customer_name else ""
    if not customer_pib and not customer_name:
        return "", "", "no_customer"
    return customer_pib, customer_name, None


def _incumbent_view_from_rows(
    customer_name: str, rows: list[RecentAwardRow]
) -> IncumbentView:
    """Rows of the latest decisions → the view of the “who won before” feed (A4).

    An empty list → ``no_data`` (the template does not draw an empty frame). A winner without
    a name is labelled “Winner not specified” — an empty line in the feed
    would look like a layout defect.
    """
    if not rows:
        return IncumbentView(status="no_data", customer_name=customer_name)

    return IncumbentView(
        status="success",
        customer_name=customer_name,
        items=[
            IncumbentItemView(
                winner_name=(r.winner_name or "").strip() or "Победитель не указан",
                pib=r.single_pib or "",
                date_fmt=_fmt_date(r.decision_date),
                sum_rsd_fmt=_fmt_sum_rsd(r.amount_rsd),
                lots=r.lots,
                is_joint=r.is_joint,
            )
            for r in rows
        ],
    )


def _competitor_from_customer_stats(
    cust_stats: CustomerStats,
) -> tuple[str, str] | None:
    """The top winner of the contracting authority as a “competitor” (needs a non-empty ``winner_pib``).

    Returns ``(winner_name, winner_pib)`` or None, if there is no top winner
    or his PIB is empty (``competitor_stats`` keys strictly by PIB).
    """
    if not cust_stats.top_winners or not cust_stats.top_winners[0].pib:
        return None
    top = cust_stats.top_winners[0]
    return (top.name or "", top.pib or "")


# --------------------------------------------------------------------------- #
# Point services (each: set_tenant → gate → resolve → stat → view)
# --------------------------------------------------------------------------- #


async def get_customer_analytics(
    db,
    *,
    tid: str,
    tender_id: str,
    months: int | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
) -> CustomerAnalyticsView:
    """The contracting-authority analytics of tender ``tender_id`` for tenant ``tid``.

    Order (matches ``run_customer_analytics`` in the bot, minus ``chat_id``):
    ``set_tenant(tid)`` FIRST → freeze → Pro+ gate → resolve of the authority →
    ``customer_award_stats`` → view. No AI/usage/limits.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    # set_tenant FIRST in the transaction (the single RLS invariant).
    await repo.set_tenant(db, tid)

    gate_err = await _gate(db, tid=tid, limits=limits)
    if gate_err:
        return CustomerAnalyticsView(status=gate_err)

    customer_pib, customer_name, cust_err = await _resolve_customer(
        db, tender_id=tender_id
    )
    if cust_err:
        return CustomerAnalyticsView(status=cust_err)

    stats = await customer_award_stats(
        db,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        months=months,
    )
    if not stats.has_data:
        return CustomerAnalyticsView(status="no_data")

    # Yearly dynamics (for all time, regardless of the profile’s months window).
    trend = await customer_yearly_trend(
        db,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
    )
    return _view_from_stats(customer_name, customer_pib, stats, trend)


async def get_competitor_analytics(
    db,
    *,
    tid: str,
    tender_id: str,
    months: int | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
) -> CompetitorAnalyticsView:
    """The profile of the strongest winner of the tender’s contracting authority (web-ai-4, VG-06 E2).

    ``set_tenant`` FIRST → freeze → Pro+ gate → resolve of the authority →
    the top winner (``customer_award_stats``) → his profile (``competitor_stats``)
    → view. Requires a non-empty ``winner_pib`` of the top winner; otherwise ``no_data``
    (competitor_stats keys strictly by PIB, there is no name fallback).
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    await repo.set_tenant(db, tid)

    gate_err = await _gate(db, tid=tid, limits=limits)
    if gate_err:
        return CompetitorAnalyticsView(status=gate_err)

    customer_pib, customer_name, cust_err = await _resolve_customer(
        db, tender_id=tender_id
    )
    if cust_err:
        return CompetitorAnalyticsView(status=cust_err)

    cust_stats = await customer_award_stats(
        db,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        months=months,
    )
    winner = _competitor_from_customer_stats(cust_stats)
    if winner is None:
        return CompetitorAnalyticsView(status="no_data")
    winner_name, winner_pib = winner

    comp = await competitor_stats(db, winner_pib=winner_pib, months=months)
    if not comp.has_data:
        return CompetitorAnalyticsView(status="no_data")

    return _competitor_view_from_stats(winner_name, winner_pib, comp)


async def get_reprocurement_forecast_view(
    db,
    *,
    tid: str,
    tender_id: str,
    horizon_months: int = 12,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
) -> ReprocurementForecastView:
    """Expected re-procurements of the tender’s contracting authority (web-ai-4, VG-07).

    ``set_tenant`` FIRST → freeze → Pro+ gate → resolve of the authority →
    ``get_reprocurement_forecast`` → view. Graceful-empty: no contracts with
    filled ``contract_date`` + ``duration_months`` → ``no_data``.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    await repo.set_tenant(db, tid)

    gate_err = await _gate(db, tid=tid, limits=limits)
    if gate_err:
        return ReprocurementForecastView(status=gate_err)

    customer_pib, customer_name, cust_err = await _resolve_customer(
        db, tender_id=tender_id
    )
    if cust_err:
        return ReprocurementForecastView(status=cust_err)

    rows = await get_reprocurement_forecast(
        db,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        horizon_months=horizon_months,
    )
    if not rows:
        return ReprocurementForecastView(
            status="no_data",
            customer_name=customer_name,
            customer_pib=customer_pib,
            horizon_months=horizon_months,
        )

    return _reprocurement_view_from_rows(
        customer_name, customer_pib, rows, horizon_months
    )


# --------------------------------------------------------------------------- #
# Bundle — all three analytics in one pass (optimization web-ai-4)
# --------------------------------------------------------------------------- #


async def get_narucilac_analytics(
    db,
    *,
    tid: str,
    tender_id: str,
    months: int | None = None,
    horizon_months: int = 12,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
) -> NarucilacAnalyticsBundle:
    """Contracting authority + competitor + re-procurements in one pass (for the tender page).

    ``set_tenant`` + gate + resolve of the authority are executed ONCE, not 3× as with the
    independent call of the ``get_*``. The competitor profile reuses the top winner
    from the already computed ``CustomerStats`` (without a second ``customer_award_stats``).
    On a gate/resolve error — the same status in all three sections.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    # set_tenant FIRST in the transaction (the single RLS invariant) — once.
    await repo.set_tenant(db, tid)

    gate_err = await _gate(db, tid=tid, limits=limits)
    if gate_err:
        return NarucilacAnalyticsBundle(
            customer=CustomerAnalyticsView(status=gate_err),
            competitor=CompetitorAnalyticsView(status=gate_err),
            reprocurement=ReprocurementForecastView(status=gate_err),
            incumbent=IncumbentView(status=gate_err),
        )

    customer_pib, customer_name, cust_err = await _resolve_customer(
        db, tender_id=tender_id
    )
    if cust_err:
        return NarucilacAnalyticsBundle(
            customer=CustomerAnalyticsView(status=cust_err),
            competitor=CompetitorAnalyticsView(status=cust_err),
            reprocurement=ReprocurementForecastView(status=cust_err),
            incumbent=IncumbentView(status=cust_err),
        )

    # a. Contracting authority (+ we reuse CustomerStats for the competitor profile).
    cust_stats = await customer_award_stats(
        db,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        months=months,
    )
    if cust_stats.has_data:
        trend = await customer_yearly_trend(
            db,
            customer_pib=customer_pib or None,
            customer_name=customer_name or None,
        )
        customer_view = _view_from_stats(customer_name, customer_pib, cust_stats, trend)
    else:
        customer_view = CustomerAnalyticsView(status="no_data")

    # b. Competitor — the top winner of the contracting authority (without a repeated customer_award_stats).
    winner = _competitor_from_customer_stats(cust_stats)
    if winner is None:
        competitor_view = CompetitorAnalyticsView(status="no_data")
    else:
        winner_name, winner_pib = winner
        comp = await competitor_stats(db, winner_pib=winner_pib, months=months)
        competitor_view = (
            _competitor_view_from_stats(winner_name, winner_pib, comp)
            if comp.has_data
            else CompetitorAnalyticsView(status="no_data")
        )

    # c. Re-procurements.
    rows = await get_reprocurement_forecast(
        db,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        horizon_months=horizon_months,
    )
    reprocurement_view = (
        _reprocurement_view_from_rows(customer_name, customer_pib, rows, horizon_months)
        if rows
        else ReprocurementForecastView(
            status="no_data",
            customer_name=customer_name,
            customer_pib=customer_pib,
            horizon_months=horizon_months,
        )
    )

    # d. Incumbent — the feed of the latest decisions of the contracting authority (A4). A separate SELECT:
    #    this is not an aggregate but a list of decisions, it cannot be taken from CustomerStats.
    #    Keyed by name (100% of award rows have an empty PIB for the tender) — the same
    #    with the normalized predicate, the same as the profile above.
    incumbent_view = (
        _incumbent_view_from_rows(
            customer_name, await customer_recent_awards(db, customer_name=customer_name)
        )
        if customer_name
        else IncumbentView(status="no_data")
    )

    return NarucilacAnalyticsBundle(
        customer=customer_view,
        competitor=competitor_view,
        reprocurement=reprocurement_view,
        incumbent=incumbent_view,
    )


# --------------------------------------------------------------------------- #
# CSV export of the contracting-authority/competitor analytics (web-ai-5)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnalyticsExportView:
    """Result of the CSV export of the analytics for the web cabinet (web-ai-5).

    ``status='success'`` → ``content`` (the ready CSV bytes) + ``filename``; otherwise
    the gate/empty (``gate_pro`` | ``frozen`` | ``no_tender`` | ``no_customer`` |
    ``no_data``) — the route decides what to show (a redirect to /plans or a 404).
    The same format/builder of CSV as the bot export (reused), without an
    AI call/charge — pure SQL on top of the award layer.
    """

    status: str  # success|gate_pro|frozen|no_tender|no_customer|no_data
    filename: str = ""
    content: bytes = b""


async def get_customer_export(
    db,
    *,
    tid: str,
    tender_id: str,
    months: int | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> AnalyticsExportView:
    """CSV export of the contracting-authority analytics of tender ``tender_id`` (web-ai-5).

    The order is as in ``get_customer_analytics`` (``set_tenant`` FIRST → freeze →
    Pro+ gate → resolve of the authority → ``customer_award_stats`` with ``top_n=EXPORT_TOP_N``),
    then the pure CSV builder (``build_customer_csv``). No AI/usage/limits.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    # set_tenant FIRST in the transaction (the single RLS invariant).
    await repo.set_tenant(db, tid)

    gate_err = await _gate(db, tid=tid, limits=limits)
    if gate_err:
        return AnalyticsExportView(status=gate_err)

    customer_pib, customer_name, cust_err = await _resolve_customer(
        db, tender_id=tender_id
    )
    if cust_err:
        return AnalyticsExportView(status=cust_err)

    stats = await customer_award_stats(
        db,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        months=months,
        top_n=EXPORT_TOP_N,
    )
    if not stats.has_data:
        return AnalyticsExportView(status="no_data")

    return AnalyticsExportView(
        status="success",
        filename=customer_csv_filename(stats, customer_name=customer_name),
        content=build_customer_csv(stats, customer_name=customer_name, locale=locale),
    )


async def get_competitor_export(
    db,
    *,
    tid: str,
    tender_id: str,
    months: int | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> AnalyticsExportView:
    """CSV export of the profile of the contracting authority’s strongest competitor (web-ai-5).

    The order is as in ``get_competitor_analytics`` (``set_tenant`` FIRST → freeze →
    Pro+ gate → resolve of the authority → the top winner → ``competitor_stats`` with
    ``top_n=EXPORT_TOP_N``), then ``build_competitor_csv``. Requires a non-empty
    ``winner_pib`` of the top winner, otherwise ``no_data``. No AI/usage/limits.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    await repo.set_tenant(db, tid)

    gate_err = await _gate(db, tid=tid, limits=limits)
    if gate_err:
        return AnalyticsExportView(status=gate_err)

    customer_pib, customer_name, cust_err = await _resolve_customer(
        db, tender_id=tender_id
    )
    if cust_err:
        return AnalyticsExportView(status=cust_err)

    cust_stats = await customer_award_stats(
        db,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        months=months,
    )
    winner = _competitor_from_customer_stats(cust_stats)
    if winner is None:
        return AnalyticsExportView(status="no_data")
    winner_name, winner_pib = winner

    comp = await competitor_stats(
        db, winner_pib=winner_pib, months=months, top_n=EXPORT_TOP_N
    )
    if not comp.has_data:
        return AnalyticsExportView(status="no_data")

    return AnalyticsExportView(
        status="success",
        filename=competitor_csv_filename(comp, winner_name=winner_name),
        content=build_competitor_csv(comp, winner_name=winner_name, locale=locale),
    )
