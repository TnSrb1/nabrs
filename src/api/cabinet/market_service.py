"""“Market” — the Vesti dashboard tab (owner 13.10, step 3).

The market from OWN data: SQL over ``tenders_normalized`` / ``tender_awards``, no AI and no external feeds. The texts are built by the template from the i18n catalog; here only the figures.

Two parts with different cost and different isolation:

- SHARED (:func:`get_shared`) — yesterday’s publications by country, the top-3 of the week by estimated value, award decisions over 7 days. Not tenant-scoped, so it is computed ONCE A DAY and stored in Redis under a date key (``vesti:market:v1:<date in Belgrade time>``): a new date — a new key, so expiry does not have to be tracked. Redis absent or down — a per-process cache under the same key; a DB failure — a short pause (:data:`FAIL_TTL_SECONDS`), so that a sick DB does not get a query on every view.
- PERSONAL (:func:`get_personal`) — “in your areas”: the CPV codes of the tenant’s active templates. Called AFTER ``repo.set_tenant`` in the dashboard transaction (RLS on ``profiles``; ``tenant_id`` in the WHERE — the second axis), under ``statement_timeout`` and in a SAVEPOINT: the timeout must not break the rest of the page.

The counting unit for decisions is a DECISION (tender + winner), not a lot and not a row: the SQL is :data:`src.api.public.stats_page.DECISIONS_CTE` (imported, not copied), with the same cutoffs as on /statistika: RS tenders since the start of regular collection, no reversals, ``decision_date`` not later than “today” (production has 2027-06-30). The amount is the winner’s contracts (``raw.winner_contracts_rsd``): counting by lots would double the money.

Winners (tax ID / name) are not exposed externally — it may be a sole proprietor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from src.api.public.stats_page import DECISIONS_CTE, decisions_params
from src.domain.classifications import (
    CPV_SYSTEM,
    cpv_prefix,
    sql_tender_system,
)
from src.domain.live_tender import live_tender_predicate
from src.domain.sources import HOME_COUNTRY
from src.domain.tz import SERBIA_TZ, belgrade_days_within_sql

logger = logging.getLogger(__name__)

#: Countries of the “Published yesterday” row — in this order; zeros are shown (owner 13.10).
COUNTRIES = ("RS", "BA", "ME", "MK")
TOP_N = 3
WEEK_DAYS = 7
CACHE_KEY = "vesti:market:v1:{day}"
REDIS_TTL_SECONDS = 26 * 3600
FAIL_TTL_SECONDS = 60
SHARED_TIMEOUT_MS = 8000
PERSONAL_TIMEOUT_MS = 1500

_LIVE = live_tender_predicate("t")
_TZ = "Europe/Belgrade"

# Yesterday in Belgrade time: the publication date in the local zone, not UTC (otherwise a tender
# published at 00:30 Belgrade time would move to “yesterday − 1”).
_YESTERDAY_SQL = text(f"""
    SELECT t.country_code AS cc, count(*) AS n
    FROM tenders_normalized t
    WHERE {_LIVE}
      AND (t.published_at AT TIME ZONE '{_TZ}')::date = CAST(:day AS date)
    GROUP BY t.country_code
    """)

_TOP_SQL = text(f"""
    SELECT t.id::text AS id, t.title, t.customer_name, t.country_code AS cc,
           t.amount_eur
    FROM tenders_normalized t
    WHERE {_LIVE}
      AND t.amount_eur IS NOT NULL
      AND (t.published_at AT TIME ZONE '{_TZ}')::date
          BETWEEN CAST(:week_start AS date) AND CAST(:day AS date)
    ORDER BY t.amount_eur DESC, t.id
    LIMIT {TOP_N}
    """)

# Decisions for 7 days — THE SAME unit, population and cutoffs as on /statistika
# (``DECISIONS_CTE``: decision = tender + winner, the sum is the winner’s contracts; only
# tenders of the country since the start of regular collection; no reversals and no future dates). Here
# beyond that — only the “last 7 days” window by decision date.
_AWARDS_SQL = text(f"""
    {DECISIONS_CTE}
    , w AS (
      SELECT d.tender_id::text AS tender_id, d.amount_rsd AS rsd,
             NULLIF(btrim(d.customer_name), '') AS customer
      FROM decisions d
      WHERE d.decision_date > CAST(:today AS date) - {WEEK_DAYS}
    )
    SELECT (SELECT count(*) FROM w) AS n,
           (SELECT count(rsd) FROM w) AS known,
           (SELECT sum(rsd) FROM w) AS total_rsd,
           (SELECT rsd FROM w WHERE rsd IS NOT NULL ORDER BY rsd DESC, tender_id LIMIT 1)
             AS top_rsd,
           (SELECT customer FROM w WHERE rsd IS NOT NULL
              ORDER BY rsd DESC, tender_id LIMIT 1) AS top_customer,
           (SELECT tender_id FROM w WHERE rsd IS NOT NULL
              ORDER BY rsd DESC, tender_id LIMIT 1) AS top_tender_id
    """)

# Personal: CPV templates of the tenant’s active profiles (a row = country + pattern,
# the pairs go as parallel arrays). Comparison with a tender — as in the preview / matching:
# the canonical eu_cpv system, ``LIKE`` on the pattern with ``*`` → ``%``. “New” means
# published within 7 days; “expiring” means the deadline in the next 7 days; one tender
# counts once, even if it matches several templates.
_PERSONAL_SQL = text(f"""
    SELECT count(DISTINCT t.id) FILTER (
             WHERE t.published_at >= now() - interval '7 days'
               AND t.published_at <= now()) AS new_n,
           count(DISTINCT t.id) FILTER (
             WHERE t.deadline_at > now()
               AND t.deadline_at < now() + interval '9 days'
               AND {belgrade_days_within_sql("t.deadline_at", "7")}) AS expiring_n
    FROM tenders_normalized t
    JOIN unnest(CAST(:ccs AS text[]), CAST(:pats AS text[])) AS m(cc, pat)
      ON t.country_code = m.cc
    WHERE {_LIVE}
      AND (
        (t.published_at >= now() - interval '7 days' AND t.published_at <= now())
        OR (t.deadline_at > now() AND t.deadline_at < now() + interval '9 days'
            AND {belgrade_days_within_sql("t.deadline_at", "7")})
      )
      AND EXISTS (
        SELECT 1 FROM jsonb_array_elements(COALESCE(t.classifications, '[]'::jsonb)) tc
        WHERE {sql_tender_system("tc")} = '{CPV_SYSTEM}'
          AND tc->>'code' LIKE m.pat
      )
    """)

_PROFILES_CPV_SQL = text("""
    SELECT p.country_code AS cc, p.classifications AS cls
    FROM profiles p
    WHERE p.tenant_id = CAST(:tid AS uuid)
      AND p.is_active IS TRUE
      AND p.deleted_at IS NULL
    """)

_lock = asyncio.Lock()
_local: dict[str, dict[str, Any]] = {}
_fail_until: float = 0.0


def clear_cache() -> None:
    """Resets the process cache (tests)."""
    global _fail_until
    _local.clear()
    _fail_until = 0.0


def local_today(now: datetime | None = None) -> date:
    """Today’s date in Belgrade time."""
    return (now or datetime.now(timezone.utc)).astimezone(SERBIA_TZ).date()


def _num(value: Any) -> float | None:
    return float(value) if value is not None else None


async def compute_shared(session: Any, today: date) -> dict[str, Any]:
    """Shared market figures for ``today`` (Belgrade date), a JSON-compatible dict."""
    await session.execute(
        text("SELECT set_config('statement_timeout', :ms, true)"),
        {"ms": str(SHARED_TIMEOUT_MS)},
    )
    yesterday = today - timedelta(days=1)
    week_start = yesterday - timedelta(days=WEEK_DAYS - 1)

    rows = (await session.execute(_YESTERDAY_SQL, {"day": yesterday})).all()
    counts = {str(r.cc): int(r.n) for r in rows if r.cc}
    published = {
        "date": yesterday.isoformat(),
        "total": sum(counts.values()),
        "by_country": {cc: counts.get(cc, 0) for cc in COUNTRIES},
    }

    top_rows = (
        await session.execute(_TOP_SQL, {"day": yesterday, "week_start": week_start})
    ).all()
    top = {
        "date": yesterday.isoformat(),
        "items": [
            {
                "id": str(r.id),
                "title": r.title or "",
                "customer": r.customer_name or "",
                "country": r.cc,
                "amount_eur": _num(r.amount_eur),
            }
            for r in top_rows
        ],
    }

    a = (
        await session.execute(_AWARDS_SQL, decisions_params(HOME_COUNTRY, today))
    ).one()
    awards = {
        "date": today.isoformat(),
        "count": int(a.n or 0),
        "known": int(a.known or 0),
        "total_rsd": _num(Decimal(a.total_rsd)) if a.total_rsd is not None else None,
        "top": (
            {
                "amount_rsd": _num(a.top_rsd),
                "customer": a.top_customer or "",
                "tender_id": a.top_tender_id,
            }
            if a.top_rsd is not None
            else None
        ),
    }
    return {
        "day": today.isoformat(),
        "published": published,
        "top": top,
        "awards": awards,
    }


async def get_shared(
    session_factory: Any, redis: Any | None, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Shared figures from the daily cache; ``None`` — could not be computed (an honest empty state)."""
    global _fail_until
    today = local_today(now)
    key = CACHE_KEY.format(day=today.isoformat())
    if key in _local:
        return _local[key]
    if redis is not None:
        try:
            raw = await redis.get(key)
            if raw:
                data = json.loads(raw)
                _local[key] = data
                return data
        except Exception as exc:  # noqa: BLE001 — the cache is not critical
            logger.warning("vesti market redis get: %s", type(exc).__name__)
    if time.monotonic() < _fail_until:
        return None
    async with _lock:
        if key in _local:
            return _local[key]
        try:
            async with session_factory() as db:
                data = await compute_shared(db, today)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — the dashboard lives without the market too
            logger.warning("vesti market unavailable: %s", type(exc).__name__)
            _fail_until = time.monotonic() + FAIL_TTL_SECONDS
            return None
        # Current day only: yesterday’s keys are not kept in process memory.
        _local.clear()
        _local[key] = data
        if redis is not None:
            try:
                await redis.set(key, json.dumps(data), ex=REDIS_TTL_SECONDS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("vesti market redis set: %s", type(exc).__name__)
        return data


def cpv_patterns(rows: list[tuple[str, Any]]) -> tuple[list[str], list[str]]:
    """Profiles ``(country, classifications)`` → parallel arrays ``(countries, patterns)``.

    Takes ``include`` with the CPV system; code → prefix (:func:`cpv_prefix`), ``*`` →
    ``%`` for ``LIKE``. A profile without a CPV filter gives no pairs.
    """
    ccs: list[str] = []
    pats: list[str] = []
    for cc, cls in rows:
        include = (cls or {}).get("include") if isinstance(cls, dict) else None
        for entry in include or []:
            if not isinstance(entry, dict):
                continue
            system = str(entry.get("system_code") or entry.get("scheme") or "")
            if system.strip().lower() not in ("eu_cpv", "cpv"):
                continue
            code = cpv_prefix(entry.get("code"))
            if not isinstance(code, str) or not code.strip():
                continue
            ccs.append(cc)
            pats.append(code.strip().replace("*", "%"))
    return ccs, pats


async def get_personal(db: Any, tenant_id: str) -> dict[str, Any] | None:
    """“In your areas”: ``{"new": N, "expiring": N}`` by the CPV codes of the active templates.

    ``None`` — nothing honest to show: the active templates have no CPV filter (counting by words is not possible), or the query did not fit into the time limit. Call AFTER ``repo.set_tenant`` in the same transaction.
    """
    try:
        async with db.begin_nested():
            await db.execute(
                text("SELECT set_config('statement_timeout', :ms, true)"),
                {"ms": str(PERSONAL_TIMEOUT_MS)},
            )
            profs = (await db.execute(_PROFILES_CPV_SQL, {"tid": tenant_id})).all()
            ccs, pats = cpv_patterns([(str(p.cc), p.cls) for p in profs])
            if not pats:
                return None
            row = (await db.execute(_PERSONAL_SQL, {"ccs": ccs, "pats": pats})).one()
    except (
        Exception
    ) as exc:  # noqa: BLE001 — a timeout or failure does not break the dashboard
        logger.warning("vesti personal unavailable: %s", type(exc).__name__)
        return None
    return {"new": int(row.new_n or 0), "expiring": int(row.expiring_n or 0)}


def _fmt_num(value: float | None) -> str:
    """An integer with a space as thousands separator (“182 998 486”), like the amounts on the tender card."""
    return f"{value:,.0f}".replace(",", " ") if value is not None else "—"


def _fmt_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def build_view(
    shared: dict[str, Any] | None,
    personal: dict[str, Any] | None,
    *,
    today: date,
) -> dict[str, Any]:
    """Tab data for the template: the figures are already formatted, the texts are in the catalog.

    Each record carries ``date`` (the date the figures refer to). No data — ``empty=True`` with the same date: the template will say “no data for {date}” rather than stay silent.
    """
    yesterday = _fmt_date((today - timedelta(days=1)).isoformat())
    today_s = _fmt_date(today.isoformat())
    out: dict[str, Any] = {}

    pub = (shared or {}).get("published")
    out["published"] = {
        "date": _fmt_date(pub["date"]) if pub else yesterday,
        "empty": not pub or pub["total"] == 0,
        "total": _fmt_num(pub["total"]) if pub else "0",
        "c": {cc: pub["by_country"].get(cc, 0) for cc in COUNTRIES} if pub else {},
    }

    top = (shared or {}).get("top")
    out["top"] = {
        "date": _fmt_date(top["date"]) if top else yesterday,
        "empty": not top or not top["items"],
        # “rows”, not “items”: in Jinja `top.items` is the dict method, not a key.
        "rows": [
            {
                "href": f"/app/tenders/{i['id']}",
                "title": i["title"],
                "customer": i["customer"],
                "amount": _fmt_num(i["amount_eur"]),
            }
            for i in (top["items"] if top else [])
        ],
    }

    aw = (shared or {}).get("awards")
    out["awards"] = {
        "date": _fmt_date(aw["date"]) if aw else today_s,
        "empty": not aw or aw["count"] == 0,
        "count": _fmt_num(aw["count"]) if aw else "0",
        "total": _fmt_num(aw["total_rsd"]) if aw and aw["total_rsd"] else None,
        "top": (
            {
                "href": f"/app/tenders/{aw['top']['tender_id']}",
                "customer": aw["top"]["customer"],
                "amount": _fmt_num(aw["top"]["amount_rsd"]),
            }
            if aw and aw["top"] and aw["top"]["tender_id"]
            else None
        ),
    }

    out["personal"] = {
        "date": today_s,
        "empty": personal is None,
        "new": _fmt_num(personal["new"]) if personal else "0",
        "expiring": _fmt_num(personal["expiring"]) if personal else "0",
    }
    return out
