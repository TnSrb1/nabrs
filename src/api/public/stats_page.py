"""The data of the public “Statistics” showcase (``GET /statistika``, R3 ANALYTICS_PRODUCT).

Aggregates only: no tax IDs (PIB), no names of winners (a winner may be a sole proprietor — personal data, R7). The top customers are permissible — they are public bodies.

The statistics are based ONLY on the FULL data (owner's decision 14.10.2026). The population of tenders:

- the liveness predicate (:func:`src.domain.live_tender.live_tender_predicate`, shared with the cabinet dashboard) cuts off the archive import (a 2019 record seen by us in 2026);
- the start of the period is the constant :data:`src.domain.live_tender.REGULAR_SINCE` (the day from which the collection runs regularly), the same as the ``since`` on the cabinet dashboard. This is a deliberate literal: ``min(published_at)`` gave 30.05 — the tail of the initial load, and May / June looked incomplete (the reasoning and the measurement are in ``live_tender.py``); the slice is ``max(first_seen_at)``;
- the monthly series — by ``published_at`` in Europe/Belgrade.

The award decisions (``tender_awards``) — the count by the DECISION exactly as in the analytics (:func:`src.analytics.awards_analytics.decisions_cte`): the key ``(tender_id, winner_pib [+ winner_name when the PIB is NULL])``, the sum — ``_DECISION_EUR`` (for a winner of several lots — ``raw.winner_contracts_eur``). Counting by lots together with this sum would double the money. Into the count go ONLY the decisions on the tenders of the population (302 decisions on tenders before 02.06 are cut off) with a ``decision_date`` between the start of the period and today in Belgrade time (future dates are a source error); the reversed ones (``raw->'contract_row'->>'IsReversed'`` = true) are cut off before the folding. The sum — only over decisions with a known amount: NULL does not turn into 0, and next to it “the amount is known for N of M” is shown.

A per-process cache, TTL 1 h (as :mod:`geo_cache`): the page is public, the data are not personal and not tenant-scoped. The queries run under ``statement_timeout``; a failure is cached briefly (:data:`FAIL_TTL_SECONDS`), so that a flow of anonymous users does not run the heavy SQL on each request while the DB is unhealthy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from src.analytics.awards_analytics import decisions_cte
from src.domain.live_tender import (
    REGULAR_SINCE,
    regular_since_utc,
    regular_tender_predicate,
)
from src.domain.sources import HOME_COUNTRY
from src.domain.tz import to_local

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3600
FAIL_TTL_SECONDS = 60
STATEMENT_TIMEOUT_MS = 5000
TOP_N = 10

_cache: tuple[float, "PublicStats | None"] | None = None
_lock = asyncio.Lock()

#: The population: the live stream (not the archive) since the start of regular collection. One literal for all queries.
LIVE_PREDICATE = regular_tender_predicate("t")

_TZ = "Europe/Belgrade"

_BOUNDS_SQL = text(f"""
    SELECT count(*) AS n, max(t.first_seen_at) AS last_at
    FROM tenders_normalized t
    WHERE {LIVE_PREDICATE}
      AND t.country_code = CAST(:country AS text)
    """)

# The common population: the live tenders of the country, published since the start of regular collection.
_POP = f"""
    FROM tenders_normalized t
    WHERE {LIVE_PREDICATE}
      AND t.country_code = CAST(:country AS text)
"""

_TENDER_MONTHS_SQL = text(f"""
    SELECT date_trunc('month', t.published_at AT TIME ZONE '{_TZ}')::date AS month,
           count(*) AS n
    {_POP}
    GROUP BY 1
    ORDER BY 1
    """)

_CPV_TOP_SQL = text(f"""
    SELECT division, count(*) AS n FROM (
      SELECT (SELECT left(e->>'code', 2)
              FROM jsonb_array_elements(t.classifications) WITH ORDINALITY AS x(e, i)
              WHERE e->>'scheme' = 'CPV' AND e->>'code' IS NOT NULL
              ORDER BY i LIMIT 1) AS division
      {_POP}
        AND jsonb_typeof(t.classifications) = 'array'
    ) d
    WHERE division IS NOT NULL
    GROUP BY division
    ORDER BY n DESC, division
    LIMIT {TOP_N}
    """)

_PROCEDURE_SQL = text(f"""
    SELECT COALESCE(NULLIF(btrim(t.procedure_type), ''), '') AS code, count(*) AS n
    {_POP}
    GROUP BY 1
    ORDER BY n DESC, code
    """)

# Decisions: the unit, the key and the sum — shared with the analytics (``decisions_cte``), without a copy of the
# SQL. The country and the “completeness” — by the decision’s tender (tender_awards has no column of its own):
# only the tenders of the population. The folding goes AFTER cutting off the reversals and the dates outside the period
# (the future is a source error: production has decision_date 2027-06).
DECISIONS_WHERE = f"""tender_id IN (
          SELECT t.id FROM tenders_normalized t
          WHERE {LIVE_PREDICATE} AND t.country_code = CAST(:country AS text))
        AND decision_date >= CAST(:start_date AS date)
        AND decision_date <= CAST(:today AS date)
        AND lower(COALESCE(raw->'contract_row'->>'IsReversed', 'false')) <> 'true'"""

DECISIONS_CTE = decisions_cte(
    DECISIONS_WHERE,
    extra_cols="max(NULLIF(btrim(customer_pib), '')) AS customer_pib,\n"
    "            max(customer_name) AS customer_name",
)

_AWARD_MONTHS_SQL = text(f"""
    {DECISIONS_CTE}
    SELECT date_trunc('month', d.decision_date)::date AS month,
           count(*) AS n,
           count(d.amount_eur) AS known,
           sum(d.amount_eur) AS sum_eur
    FROM decisions d
    GROUP BY 1
    ORDER BY 1
    """)

# The customer — by tax ID (PIB), without a PIB — by the normalized name. The PIB is not given out.
_TOP_CUSTOMERS_SQL = text(f"""
    {DECISIONS_CTE}
    SELECT max(d.customer_name) AS name,
           count(*) AS n,
           sum(d.amount_eur) AS sum_eur
    FROM decisions d
    WHERE COALESCE(d.customer_pib, NULLIF(lower(btrim(d.customer_name)), ''))
          IS NOT NULL
    GROUP BY COALESCE(d.customer_pib, 'name:' || lower(btrim(d.customer_name)))
    HAVING sum(d.amount_eur) IS NOT NULL
    ORDER BY sum(d.amount_eur) DESC, count(*) DESC, max(d.customer_name)
    LIMIT {TOP_N}
    """)


def decisions_params(country: str, today: date) -> dict[str, Any]:
    """The parameters of :data:`DECISIONS_CTE`: the country, the start of the period, “today” in Belgrade time.

    Shared with “Tržište” (:mod:`src.api.cabinet.market_service`): the figures of the dashboard and of the showcase are counted by one SQL and with the same boundaries.
    """
    return {"country": country, "start_date": REGULAR_SINCE, "today": today}


@dataclass(frozen=True)
class CountRow:
    key: str  # month “MM.YYYY”, CPV section or procedure code
    count: int
    pct: int  # bar width 0..100 from the maximum of the series


@dataclass(frozen=True)
class AwardMonth:
    label: str
    count: int
    known: int
    sum_eur: Decimal | None
    pct: int


@dataclass(frozen=True)
class CustomerRow:
    name: str
    count: int
    sum_eur: Decimal


@dataclass(frozen=True)
class PublicStats:
    period_start: datetime
    as_of: datetime
    tenders_total: int
    tender_months: tuple[CountRow, ...]
    cpv_top: tuple[CountRow, ...]
    procedures: tuple[CountRow, ...]
    award_months: tuple[AwardMonth, ...]
    awards_total: int
    awards_known: int
    awards_sum_eur: Decimal | None
    top_customers: tuple[CustomerRow, ...]
    current_month: str = (
        ""  # “MM.YYYY” of the current month (Belgrade): the series is not complete yet
    )


def clear_cache() -> None:
    """Resets the cache (tests)."""
    global _cache
    _cache = None


def _pct(value: int | Decimal, top: int | Decimal) -> int:
    if not top:
        return 0
    return max(1, round(float(value) / float(top) * 100)) if value else 0


def _month_label(d: date) -> str:
    return f"{d.month:02d}.{d.year}"


def _months_between(first: date, last: date) -> list[date]:
    out = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        out.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _count_rows(pairs: list[tuple[str, int]]) -> tuple[CountRow, ...]:
    top = max((n for _, n in pairs), default=0)
    return tuple(CountRow(key=k, count=n, pct=_pct(n, top)) for k, n in pairs)


async def compute_stats(
    session: Any, *, country: str = HOME_COUNTRY, today: date | None = None
) -> PublicStats | None:
    """The showcase aggregates without a cache; ``None`` — there is no live stream (an empty DB).

    ``today`` — “today” in Belgrade time (the upper boundary of ``decision_date``; the tests fix it, so that the expectation does not depend on the launch time).

    ``country`` — a parameter for the tests on a shared one-off DB (own country — own figures); the route always takes :data:`HOME_COUNTRY`.
    """
    await session.execute(
        text("SELECT set_config('statement_timeout', :ms, true)"),
        {"ms": str(STATEMENT_TIMEOUT_MS)},
    )
    bounds = (await session.execute(_BOUNDS_SQL, {"country": country})).one()
    if not bounds.n:
        return None
    start_at: datetime = regular_since_utc()
    as_of: datetime = bounds.last_at
    pop = {"country": country}
    today_local = today or to_local(datetime.now(timezone.utc)).date()

    month_rows = (await session.execute(_TENDER_MONTHS_SQL, pop)).all()
    by_month = {r.month: int(r.n) for r in month_rows}
    first_month = date(REGULAR_SINCE.year, REGULAR_SINCE.month, 1)
    months = _months_between(
        min([first_month, *by_month]), max([*by_month, first_month])
    )
    tender_months = _count_rows([(_month_label(m), by_month.get(m, 0)) for m in months])

    cpv_rows = (await session.execute(_CPV_TOP_SQL, pop)).all()
    cpv_top = _count_rows([(str(r.division), int(r.n)) for r in cpv_rows])

    proc_rows = (await session.execute(_PROCEDURE_SQL, pop)).all()
    procedures = _count_rows([(str(r.code), int(r.n)) for r in proc_rows])

    aw_params = decisions_params(country, today_local)
    aw_rows = (await session.execute(_AWARD_MONTHS_SQL, aw_params)).all()
    aw_top = max((int(r.n) for r in aw_rows), default=0)
    award_months = tuple(
        AwardMonth(
            label=_month_label(r.month),
            count=int(r.n),
            known=int(r.known),
            sum_eur=Decimal(r.sum_eur) if r.sum_eur is not None else None,
            pct=_pct(int(r.n), aw_top),
        )
        for r in aw_rows
    )
    sums = [m.sum_eur for m in award_months if m.sum_eur is not None]

    cust_rows = (await session.execute(_TOP_CUSTOMERS_SQL, aw_params)).all()
    top_customers = tuple(
        CustomerRow(name=str(r.name or ""), count=int(r.n), sum_eur=Decimal(r.sum_eur))
        for r in cust_rows
    )

    return PublicStats(
        period_start=start_at,
        as_of=as_of,
        tenders_total=sum(r.count for r in tender_months),
        tender_months=tender_months,
        cpv_top=cpv_top,
        procedures=procedures,
        award_months=award_months,
        awards_total=sum(m.count for m in award_months),
        awards_known=sum(m.known for m in award_months),
        awards_sum_eur=sum(sums, Decimal(0)) if sums else None,
        top_customers=top_customers,
        current_month=_month_label(today_local),
    )


async def get_public_stats(session_factory: Any) -> PublicStats | None:
    """Aggregates from the cache (TTL 1 h); a DB error / timeout → ``None`` for :data:`FAIL_TTL_SECONDS`."""
    global _cache
    now = time.monotonic()
    if _cache is not None and _cache[0] > now:
        return _cache[1]
    async with _lock:
        now = time.monotonic()
        if _cache is not None and _cache[0] > now:
            return _cache[1]
        try:
            async with session_factory() as db:
                stats = await compute_stats(db)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — the showcase lives without the figures
            logger.warning("statistika unavailable: %s", type(exc).__name__)
            _cache = (now + FAIL_TTL_SECONDS, None)
            return None
        _cache = (now + CACHE_TTL_SECONDS, stats)
        return stats


def fmt_eur(value: Decimal | int | float | None) -> str:
    """“€1 234 567” — without cents. The digit groups are separated by a non-breaking space U+00A0: the number in a table cell does not break across lines (at 320px “€276 507 123” would otherwise fall apart into three lines)."""
    if value is None:
        return "—"
    return "€" + f"{float(value):,.0f}".replace(",", "\u00a0")
