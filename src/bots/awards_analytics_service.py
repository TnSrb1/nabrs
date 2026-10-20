"""On-demand analytics of the contracting authority (VG-06) — the service for the Telegram button.

Called from ``src.bots.telegram.on_customer_analytics`` when the button
“🏛 Who wins?” is pressed (callback ``an:cust:{tender_id}``).

Differences from the AI services (bid/deep):
- WITHOUT a model call — plain SELECTs over ``tender_awards`` (near-zero cost).
- No monthly limit of §14.5: the gate is only by tier (Pro+) and freeze.
- The customer is taken from ``tenders_normalized`` of the current tender → the analytics by
  ``customer_pib`` in ``tender_awards`` (see VG-06 in awards_analytics).

Steps:
    a. resolve_tenant_id(chat_id) → no binding → no_account.
    b. Freeze (access_until expired) → frozen (FIX-SG-04, BEFORE the gate).
       Tier < Pro → gate_pro (the same Pro+ predicate as for the bid verdict).
    c. Loading of the tender's customer (customer_tax_id/customer_name/title).
       No tender → no_tender; no customer PIB → no_customer.
    d. customer_award_stats(customer_pib) → CustomerStats.
       No decisions for this customer → no_data.
    e. Render → an HTML message.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from src.analytics.awards_analytics import (
    CompetitorStats,
    CustomerStats,
    competitor_stats,
    customer_award_stats,
)
from src.ai.usage import _TIER_LIMITS
from src.bots.i18n import bot_t
from src.bots.status_service import resolve_tenant_id
from src.domain.access import is_frozen
from src.domain.tiers import effective_tier

logger = logging.getLogger(__name__)

_LOAD_TENANT_TIER_SQL = text("""
    SELECT tier, access_until, status FROM tenants WHERE id = :tenant_id
    """)

_LOAD_TENDER_CUSTOMER_SQL = text("""
    SELECT customer_name, customer_tax_id, title
    FROM tenders_normalized
    WHERE id = :tender_id
    LIMIT 1
    """)


@dataclass(frozen=True)
class CustomerAnalyticsResult:
    """The result of the on-demand analytics of the contracting authority for rendering in Telegram."""

    status: str  # 'success' | 'no_account' | 'gate_pro' | 'frozen'
    # | 'no_tender' | 'no_customer' | 'no_data'
    html: str = ""


@dataclass(frozen=True)
class CompetitorAnalyticsResult:
    """The result of the on-demand analytics of the competitor for rendering in Telegram."""

    status: str  # 'success' | 'no_account' | 'gate_pro' | 'frozen'
    # | 'no_tender' | 'no_customer' | 'no_data'
    html: str = ""


@dataclass(frozen=True)
class ExportResult:
    """The result of the analytics export to a CSV file."""

    status: str  # 'success'|'no_account'|'gate_pro'|'frozen'|'no_tender'|'no_customer'|'no_data'
    filename: str = ""
    content: bytes = b""


def _is_pro_plus(tier: str | None, limits: dict[str, dict[str, int | None]]) -> bool:
    """Pro+ := the tier on which the bid verdict is available (pro/business/enterprise).

    Reuses the existing SSOT of the tier limits instead of a new list
    of tiers — one source of truth (see src.ai.usage._TIER_LIMITS)."""
    return limits.get(tier or "", {}).get("bid", 0) != 0


async def run_customer_analytics(
    session: Any,
    *,
    tender_id: str,
    chat_id: int,
    months: int | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> CustomerAnalyticsResult:
    """Analytics of the contracting authority of the current tender (VG-06)."""
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    # a. Resolve tenant_id.
    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return CustomerAnalyticsResult(status="no_account")

    # b. Freeze + the Pro+ gate.
    row = (
        await session.execute(_LOAD_TENANT_TIER_SQL, {"tenant_id": tenant_id})
    ).first()
    access_until = getattr(row, "access_until", None) if row is not None else None
    if is_frozen(access_until, datetime.now(timezone.utc)):
        return CustomerAnalyticsResult(status="frozen")
    # The effective tier: trial → the rights of Pro (ARCHITECTURE §4), AFTER the frozen gate.
    status_val = str(row.status) if row and getattr(row, "status", None) else None
    raw_tier = str(row.tier) if row and row.tier else None
    tier = effective_tier(status_val, raw_tier)
    if not _is_pro_plus(tier, limits):
        return CustomerAnalyticsResult(status="gate_pro")

    # c. The customer of the current tender.
    trow = (
        await session.execute(_LOAD_TENDER_CUSTOMER_SQL, {"tender_id": tender_id})
    ).first()
    if trow is None:
        return CustomerAnalyticsResult(status="no_tender")
    customer_pib = str(trow.customer_tax_id) if trow.customer_tax_id else ""
    customer_name = str(trow.customer_name) if trow.customer_name else ""
    # We key by PIB; if the PIB is empty (for ~93% of tenders) — a fallback by the exact name.
    if not customer_pib and not customer_name:
        return CustomerAnalyticsResult(status="no_customer")

    # d. Aggregates by the customer (PIB or name).
    stats = await customer_award_stats(
        session,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        months=months,
    )
    if not stats.has_data:
        return CustomerAnalyticsResult(status="no_data")

    logger.info(
        "analytics: contracting authority pib=%s awards=%d chat=%s",
        customer_pib,
        stats.total_awards,
        chat_id,
    )
    return CustomerAnalyticsResult(
        status="success",
        html=render_customer_analytics(customer_name, customer_pib, stats, locale),
    )


# --------------------------------------------------------------------------- #
#  Render
# --------------------------------------------------------------------------- #


def _fmt_money_rsd(value: Decimal) -> str:
    """An integer RSD with a space as the thousands separator: 12345678 → '12 345 678'."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "0"
    return f"{n:,}".replace(",", " ")


def _fmt_eur(value: Decimal) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "0"
    return f"{n:,}".replace(",", " ")


def _period_label(months: int | None, locale: str | None) -> str:
    if months is None:
        return bot_t(locale, "aa_period_all")
    return bot_t(locale, "aa_period_months").format(n=months)


def render_customer_analytics(
    customer_name: str,
    customer_pib: str,
    stats: CustomerStats,
    locale: str | None = None,
) -> str:
    """Formats CustomerStats → an HTML string for Telegram (parse_mode=HTML).

    A pure function — tested directly.
    """
    name = html.escape(customer_name or "—")
    period = _period_label(stats.months, locale)

    parts: list[str] = [
        f"🏛 <b>{bot_t(locale, 'aa_cust_title')}</b>",
        f"<b>{name}</b>\nPIB: {html.escape(customer_pib)}",
    ]

    summary = (
        f"📊 {bot_t(locale, 'aa_cust_awards').format(period=period)} "
        f"<b>{stats.total_awards}</b>\n"
        f"👥 {bot_t(locale, 'aa_cust_winners')} <b>{stats.distinct_winners}</b>"
    )
    if stats.with_amount > 0:
        summary += (
            f"\n💰 {bot_t(locale, 'aa_sum').format(n=stats.with_amount)} "
            f"<b>{_fmt_money_rsd(stats.total_rsd)} RSD</b>"
            f" (~{_fmt_eur(stats.total_eur)} EUR)"
        )
    conc = stats.concentration_pct
    cr3 = stats.cr3_pct
    if conc is not None and stats.distinct_winners > 0:
        conc_line = f"🎯 {bot_t(locale, 'aa_conc')} <b>{conc}%</b>"
        if cr3 is not None and stats.distinct_winners > 1:
            conc_line += f" · {bot_t(locale, 'aa_conc_top3')} <b>{cr3}%</b>"
        summary += f"\n{conc_line}"
    parts.append(summary)

    if stats.is_monopolized:
        parts.append(f"⚠️ {bot_t(locale, 'aa_monopoly')}")

    if stats.top_winners:
        lines = []
        for i, w in enumerate(stats.top_winners, 1):
            wname = html.escape(w.name or "—")
            money = (
                f" — {_fmt_money_rsd(w.sum_rsd)} RSD"
                if w.sum_rsd and w.sum_rsd > 0
                else ""
            )
            lines.append(f"  {i}. {wname}: {w.wins} {bot_t(locale, 'aa_wins')}{money}")
        parts.append(
            f"🏆 <b>{bot_t(locale, 'aa_top_winners')}</b>\n" + "\n".join(lines)
        )

    parts.append(f"<i>{bot_t(locale, 'aa_source')}</i>")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
#  Analytics of the competitor (VG-06 STAGE 2)
# --------------------------------------------------------------------------- #


async def run_competitor_analytics(
    session: Any,
    *,
    tender_id: str,
    chat_id: int,
    months: int | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> CompetitorAnalyticsResult:
    """The profile of the strongest winner of the contracting authority of the current tender (VG-06).

    Limitation: requires winner_pib of the top winner; if the PIB is empty — no_data
    (competitor_stats keys strictly by winner_pib, there is no name fallback).
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    # a. Resolve tenant_id.
    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return CompetitorAnalyticsResult(status="no_account")

    # b. Freeze + the Pro+ gate (the same predicate as in run_customer_analytics).
    row = (
        await session.execute(_LOAD_TENANT_TIER_SQL, {"tenant_id": tenant_id})
    ).first()
    access_until = getattr(row, "access_until", None) if row is not None else None
    if is_frozen(access_until, datetime.now(timezone.utc)):
        return CompetitorAnalyticsResult(status="frozen")
    # The effective tier: trial → the rights of Pro (ARCHITECTURE §4), AFTER the frozen gate.
    status_val = str(row.status) if row and getattr(row, "status", None) else None
    raw_tier = str(row.tier) if row and row.tier else None
    tier = effective_tier(status_val, raw_tier)
    if not _is_pro_plus(tier, limits):
        return CompetitorAnalyticsResult(status="gate_pro")

    # c. The customer of the current tender.
    trow = (
        await session.execute(_LOAD_TENDER_CUSTOMER_SQL, {"tender_id": tender_id})
    ).first()
    if trow is None:
        return CompetitorAnalyticsResult(status="no_tender")
    customer_pib = str(trow.customer_tax_id) if trow.customer_tax_id else ""
    customer_name = str(trow.customer_name) if trow.customer_name else ""
    if not customer_pib and not customer_name:
        return CompetitorAnalyticsResult(status="no_customer")

    # d. Find the top winner of the contracting authority.
    cust_stats = await customer_award_stats(
        session,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        months=months,
    )
    if not cust_stats.top_winners or not cust_stats.top_winners[0].pib:
        return CompetitorAnalyticsResult(status="no_data")

    top = cust_stats.top_winners[0]

    # e. The competitor profile.
    comp = await competitor_stats(session, winner_pib=top.pib, months=months)
    if not comp.has_data:
        return CompetitorAnalyticsResult(status="no_data")

    logger.info(
        "analytics: competitor pib=%s wins=%d chat=%s",
        top.pib,
        comp.total_wins,
        chat_id,
    )
    return CompetitorAnalyticsResult(
        status="success",
        html=render_competitor_analytics(top.name or "", top.pib, comp, locale),
    )


def render_competitor_analytics(
    winner_name: str,
    winner_pib: str,
    stats: CompetitorStats,
    locale: str | None = None,
) -> str:
    """Formats CompetitorStats → an HTML string for Telegram (parse_mode=HTML).

    A pure function — tested directly.
    """
    name = html.escape(winner_name or "—")
    pib = html.escape(winner_pib)
    period = _period_label(stats.months, locale)

    parts: list[str] = [
        f"🥊 <b>{bot_t(locale, 'aa_comp_title')}</b>",
        f"<b>{name}</b>\nPIB: {pib}",
    ]

    summary = (
        f"📊 {bot_t(locale, 'aa_comp_wins').format(period=period)} "
        f"<b>{stats.total_wins}</b>\n"
        f"🏛 {bot_t(locale, 'aa_comp_customers')} <b>{stats.distinct_customers}</b>"
    )
    if stats.with_amount > 0:
        summary += (
            f"\n💰 {bot_t(locale, 'aa_sum').format(n=stats.with_amount)} "
            f"<b>{_fmt_money_rsd(stats.total_rsd)} RSD</b>"
            f" (~{_fmt_eur(stats.total_eur)} EUR)"
        )
    parts.append(summary)

    if stats.top_customers:
        lines = []
        for i, c in enumerate(stats.top_customers, 1):
            cname = html.escape(c.name or "—")
            money = (
                f" — {_fmt_money_rsd(c.sum_rsd)} RSD"
                if c.sum_rsd and c.sum_rsd > 0
                else ""
            )
            lines.append(f"  {i}. {cname}: {c.wins} {bot_t(locale, 'aa_wins')}{money}")
        parts.append(
            f"🏆 <b>{bot_t(locale, 'aa_top_customers')}</b>\n" + "\n".join(lines)
        )

    parts.append(f"<i>{bot_t(locale, 'aa_source')}</i>")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
#  Export to CSV (VG-06 STAGE 3a)
# --------------------------------------------------------------------------- #


async def _resolve_gate(
    session: Any,
    *,
    chat_id: int,
    limits: dict[str, dict[str, int | None]],
) -> tuple[str | None, str | None]:
    """Resolve tenant_id + check of the gate (freeze/tier).

    Returns (tenant_id, error_status). If error_status is not None — exit.
    """
    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return None, "no_account"
    row = (
        await session.execute(_LOAD_TENANT_TIER_SQL, {"tenant_id": tenant_id})
    ).first()
    access_until = getattr(row, "access_until", None) if row is not None else None
    if is_frozen(access_until, datetime.now(timezone.utc)):
        return tenant_id, "frozen"
    # The effective tier: trial → the rights of Pro (ARCHITECTURE §4), AFTER the frozen gate.
    status_val = str(row.status) if row and getattr(row, "status", None) else None
    raw_tier = str(row.tier) if row and row.tier else None
    tier = effective_tier(status_val, raw_tier)
    if not _is_pro_plus(tier, limits):
        return tenant_id, "gate_pro"
    return tenant_id, None


async def _resolve_customer(
    session: Any,
    *,
    tender_id: str,
) -> tuple[str, str, str | None]:
    """Loading the customer from tenders_normalized.

    Returns (customer_pib, customer_name, error_status).
    """
    trow = (
        await session.execute(_LOAD_TENDER_CUSTOMER_SQL, {"tender_id": tender_id})
    ).first()
    if trow is None:
        return "", "", "no_tender"
    customer_pib = str(trow.customer_tax_id) if trow.customer_tax_id else ""
    customer_name = str(trow.customer_name) if trow.customer_name else ""
    if not customer_pib and not customer_name:
        return "", "", "no_customer"
    return customer_pib, customer_name, None


async def run_customer_export(
    session: Any,
    *,
    tender_id: str,
    chat_id: int,
    months: int | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> ExportResult:
    """CSV export of the analytics of the contracting authority (VG-06 E3a). top_n=EXPORT_TOP_N."""
    from src.bots.analytics_export import (
        EXPORT_TOP_N,
        build_customer_csv,
        customer_csv_filename,
    )

    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    _, err = await _resolve_gate(session, chat_id=chat_id, limits=limits)
    if err:
        return ExportResult(status=err)

    customer_pib, customer_name, err = await _resolve_customer(
        session, tender_id=tender_id
    )
    if err:
        return ExportResult(status=err)

    stats = await customer_award_stats(
        session,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        months=months,
        top_n=EXPORT_TOP_N,
    )
    if not stats.has_data:
        return ExportResult(status="no_data")

    content = build_customer_csv(stats, customer_name=customer_name, locale=locale)
    filename = customer_csv_filename(stats, customer_name=customer_name)
    logger.info(
        "export: contracting authority pib=%s rows=%d chat=%s",
        customer_pib,
        len(stats.top_winners),
        chat_id,
    )
    return ExportResult(status="success", filename=filename, content=content)


async def run_competitor_export(
    session: Any,
    *,
    tender_id: str,
    chat_id: int,
    months: int | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> ExportResult:
    """CSV export of the competitor profile (VG-06 E3a). top_n=EXPORT_TOP_N."""
    from src.bots.analytics_export import (
        EXPORT_TOP_N,
        build_competitor_csv,
        competitor_csv_filename,
    )

    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    _, err = await _resolve_gate(session, chat_id=chat_id, limits=limits)
    if err:
        return ExportResult(status=err)

    customer_pib, customer_name, err = await _resolve_customer(
        session, tender_id=tender_id
    )
    if err:
        return ExportResult(status=err)

    # We find the top winner of the contracting authority.
    cust_stats = await customer_award_stats(
        session,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        months=months,
    )
    if not cust_stats.top_winners or not cust_stats.top_winners[0].pib:
        return ExportResult(status="no_data")

    top = cust_stats.top_winners[0]

    comp = await competitor_stats(
        session, winner_pib=top.pib, months=months, top_n=EXPORT_TOP_N
    )
    if not comp.has_data:
        return ExportResult(status="no_data")

    content = build_competitor_csv(comp, winner_name=top.name or "", locale=locale)
    filename = competitor_csv_filename(comp, winner_name=top.name or "")
    logger.info(
        "export: competitor pib=%s rows=%d chat=%s",
        top.pib,
        len(comp.top_customers),
        chat_id,
    )
    return ExportResult(status="success", filename=filename, content=content)


# --------------------------------------------------------------------------- #
#  Reprocurement forecast (VG-07 phase-1)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReprocurementResult:
    """The result of the reprocurement forecast for rendering in Telegram."""

    status: str  # 'success'|'no_account'|'gate_pro'|'frozen'|'no_tender'|'no_customer'|'no_data'
    html: str = ""


async def run_reprocurement_forecast(
    session: Any,
    *,
    tender_id: str,
    chat_id: int,
    horizon_months: int = 12,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> ReprocurementResult:
    """The expected reprocurements by the contracting authority of the current tender (VG-07 phase-1).

    The same gate rules as VG-06 (Pro+, freeze).
    Graceful-empty: if the customer has no rows with filled contract_date +
    duration_months — returns no_data (does not fail).
    """
    from src.analytics.reprocurement import get_reprocurement_forecast

    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return ReprocurementResult(status="no_account")

    row = (
        await session.execute(_LOAD_TENANT_TIER_SQL, {"tenant_id": tenant_id})
    ).first()
    access_until = getattr(row, "access_until", None) if row is not None else None
    if is_frozen(access_until, datetime.now(timezone.utc)):
        return ReprocurementResult(status="frozen")
    # The effective tier: trial → the rights of Pro (ARCHITECTURE §4), AFTER the frozen gate.
    status_val = str(row.status) if row and getattr(row, "status", None) else None
    raw_tier = str(row.tier) if row and row.tier else None
    tier = effective_tier(status_val, raw_tier)
    if not _is_pro_plus(tier, limits):
        return ReprocurementResult(status="gate_pro")

    customer_pib, customer_name, err = await _resolve_customer(
        session, tender_id=tender_id
    )
    if err:
        return ReprocurementResult(status=err)

    rows = await get_reprocurement_forecast(
        session,
        customer_pib=customer_pib or None,
        customer_name=customer_name or None,
        horizon_months=horizon_months,
    )
    if not rows:
        return ReprocurementResult(status="no_data")

    logger.info(
        "reprocurement: contracting authority pib=%s rows=%d chat=%s",
        customer_pib,
        len(rows),
        chat_id,
    )
    return ReprocurementResult(
        status="success",
        html=render_reprocurement_forecast(
            customer_name, customer_pib, rows, horizon_months, locale
        ),
    )


def render_reprocurement_forecast(
    customer_name: str,
    customer_pib: str,
    rows: list,
    horizon_months: int = 12,
    locale: str | None = None,
) -> str:
    """Formats the list of ReprocurementRow → HTML for Telegram (parse_mode=HTML).

    A pure function — tested directly.
    """
    name = html.escape(customer_name or "—")
    parts: list[str] = [
        f"🔮 <b>{bot_t(locale, 'aa_fc_title')}</b>",
        f"<b>{name}</b>\n" + bot_t(locale, "aa_fc_intro").format(n=horizon_months),
    ]
    for i, r in enumerate(rows, 1):
        title = html.escape(r.lot_title or "—")
        winner = html.escape(r.winner_name or "—")
        expiry = r.expiry_date.strftime("%d.%m.%Y")
        mo = r.months_until_expiry
        line = (
            f"{i}. <b>{title}</b>\n"
            f"   {bot_t(locale, 'aa_fc_winner')} {winner}\n"
            f"   {bot_t(locale, 'aa_fc_expires')} {expiry} "
            + bot_t(locale, "aa_fc_months").format(n=mo)
        )
        if r.award_amount_rsd and r.award_amount_rsd > 0:
            line += (
                f"\n   {bot_t(locale, 'aa_fc_sum')} "
                f"{_fmt_money_rsd(r.award_amount_rsd)} RSD"
            )
        parts.append(line)
    parts.append(f"<i>{bot_t(locale, 'aa_fc_note')}</i>")
    return "\n\n".join(parts)
