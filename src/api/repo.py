"""A thin DB-access layer for the cabinet (Sprint-19).

It contains what does not fit into a simple ORM call inside the services:
- :func:`set_tenant` — sets ``app.current_tenant_id`` for RLS on each request to tenant data (ADR-002, DB_SCHEMA §4). It uses ``set_config(..., is_local=true)`` — a transactional analogue of ``SET LOCAL`` that accepts a bind parameter.
- reads for the public tender card (``tenders_normalized`` — NOT tenant-scoped, no RLS; the AI analysis — tenant-scoped, through the profile).

The isolation in the application is ensured by TWO mechanisms: ``set_tenant`` (MANDATORY — since VG-01 (0021) ``api`` / ``bot`` run under the role ``tendersrb_app``, NOSUPERUSER, FORCE RLS on the tenant tables; without it the query fails with ``UndefinedObjectError``) AND an explicit ``WHERE tenant_id`` in the queries — as an extra, defense-in-depth layer (the delivery worker, ARCHITECTURE §8, goes through a separate owner connection and there RLS is indeed not forced).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Sequence

from sqlalchemy import func, select, text
from sqlalchemy.engine import Row

from src.db.models import (
    Profile,
    TenderAiAnalysis,
    TenderAward,
    TenderNormalized,
)
from src.db.session import set_current_tenant
from src.dedup.also_on import also_on_count_sql
from src.domain.indexing import SITEMAP_SQL
from src.domain.live_tender import regular_tender_predicate
from src.domain.sources import HOME_COUNTRY


async def set_tenant(session, tenant_id: str) -> None:
    """Sets ``app.current_tenant_id`` on the current transaction (RLS).

    Backward compatibility: it delegates to :func:`src.db.session.set_current_tenant`.
    """
    await set_current_tenant(session, tenant_id)


async def get_tender(session, tender_id: str) -> TenderNormalized | None:
    """The tender card by id (public data, without a tenant filter)."""
    result = await session.execute(
        select(TenderNormalized).where(TenderNormalized.id == tender_id)
    )
    return result.scalar_one_or_none()


#: How many award decisions we show on the card (by lot; usually 1–2).
TENDER_AWARDS_LIMIT = 5


async def get_tender_awards(db, tender_id, today: date) -> list[TenderAward]:
    """The award decisions of the tender with a known winner (public data).

    A future decision date (``decision_date > today``) is not shown: it is a typo or a draft of the source, not a fact (see ``awards_analytics``). A row without a winner (“Winner not specified”) is not a decision, but a gap in the data.
    """
    result = await db.execute(
        select(TenderAward)
        .where(
            TenderAward.tender_id == tender_id,
            TenderAward.winner_name.is_not(None),
            TenderAward.decision_date.is_not(None),
            TenderAward.decision_date <= today,
        )
        .order_by(TenderAward.decision_date.desc(), TenderAward.id)
        .limit(TENDER_AWARDS_LIMIT)
    )
    return list(result.scalars().all())


_ALSO_ON_COUNT_SQL = text(
    f"SELECT {also_on_count_sql('tn')} FROM tenders_normalized tn"
    " WHERE tn.id = CAST(:tender_id AS uuid)"
)


async def count_canonical_alts(session, canonical_id, tender_id) -> int:
    """How many more sources are in the same canonical cluster (besides the current one).

    “also on N sources” (ARCHITECTURE §5.2 / §13.5). 0 if there is no canonical. The same predicate as the feed / card of the cabinet and the delivery (:func:`src.dedup.also_on.also_on_count_sql`): the other SourceS are counted, not the rows of the cluster.
    """
    if canonical_id is None:
        return 0
    result = await session.execute(_ALSO_ON_COUNT_SQL, {"tender_id": str(tender_id)})
    return int(result.scalar_one() or 0)


async def count_tenders(session) -> int:
    """The number of tenders in the sitemap (:data:`src.domain.indexing.SITEMAP_SQL`) —
    how many chunks the sitemap needs."""
    result = await session.execute(
        select(func.count()).select_from(TenderNormalized).where(text(SITEMAP_SQL))
    )
    return int(result.scalar_one())


async def list_tenders_for_sitemap(
    session, offset: int, limit: int
) -> list[tuple[str, datetime | None]]:
    """``(id, last_changed_at)`` in chunks for the sitemap — without the ORM hydration of the whole row.

    Only the tenders of the sitemap (open + closed ≤30 days, :mod:`src.domain.indexing`), the same filter as in :func:`count_tenders`.

    ``ORDER BY id`` is mandatory (a deterministic pagination of the chunks, the project rules: ``LIMIT`` without ``ORDER BY`` is a non-deterministic choice).
    """
    result = await session.execute(
        select(TenderNormalized.id, TenderNormalized.last_changed_at)
        .where(text(SITEMAP_SQL))
        .order_by(TenderNormalized.id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.all())


async def get_tender_ai_analysis(
    session, tender_id: str, tenant_id: str
) -> TenderAiAnalysis | None:
    """The latest AI analysis of the tender for the profiles of the current tenant (RLS isolation).

    Requires a prior :func:`set_tenant`. The join to ``profiles.tenant_id`` — the profile analyzes (``profile_id IS NOT NULL``) are visible only to their own tenant (DB_SCHEMA §4.1).
    """
    result = await session.execute(
        select(TenderAiAnalysis)
        .join(Profile, Profile.id == TenderAiAnalysis.profile_id)
        .where(
            TenderAiAnalysis.tender_id == tender_id,
            Profile.tenant_id == tenant_id,
        )
        .order_by(TenderAiAnalysis.created_at.desc())
    )
    return result.scalars().first()


# --------------------------------------------------------------------------- #
# GEO-2: /tenderi — landing pages “by CPV section” and “by region”.
#
# The aggregates (:func:`cpv_division_stats` / :func:`region_stats`) scan the whole
# ``tenders_normalized`` (~100 ms on 55K rows, measurement 26.09.2026 on production) —
# the calling code MUST cache the result (``src.api.public.geo_cache``,
# TTL ≥ 1 h); this is NOT a query on every render of the page.
#
# The listings (``list_open_tenders_by_*`` / ``list_closed_tenders_by_*``), on the contrary,
# — a query ON EVERY render, therefore they must go through the index
# ``tenders_normalized_deadline_at_idx``: open now there are only 2021 of 55128
# (measurement 26.09), so ``deadline_at > now()`` as the first condition keeps the selection for
# ORDER BY / LIMIT cheap regardless of the rarity of the section / region. The worst
# measured case (a section at the threshold of 5 tenders, “latest closed”) —
# 87 ms EXPLAIN ANALYZE on production; for a region the same case is 2 ms (regions is a
# GIN-indexable ``@>``, not a JSON scan).
# --------------------------------------------------------------------------- #

# The /tenderi pages are “Srbija”: foreign tenders (BA / MK / … from other
# sources) do not get into the Serbian SEO pages (SRC-0d, owner 30.09 R7). They
# are available at their own ``/tender/{id}`` with the source and country label. The value is
# a domain constant (``HOME_COUNTRY``), bound by the parameter ``:home_country``.
# ``CAST(... AS text)`` — asyncpg does not infer the parameter type by itself, and the column is CHAR(2).
_HOME_ONLY = "country_code = CAST(:home_country AS text)"
_HOME_PARAMS = {"home_country": HOME_COUNTRY}

_CPV_STATS_SQL = text(f"""
    SELECT division,
           count(*) AS total,
           count(*) FILTER (WHERE deadline_at > now()) AS open_n,
           sum(amount_eur) FILTER (
             WHERE deadline_at > now() AND amount_eur IS NOT NULL
           ) AS open_sum
    FROM (
      SELECT t.id, t.deadline_at, t.amount_eur,
             (SELECT left(e->>'code', 2) FROM jsonb_array_elements(t.classifications) e
              WHERE e->>'scheme' = 'CPV' AND e->>'code' IS NOT NULL LIMIT 1) AS division
      FROM tenders_normalized t
      WHERE t.classifications @> '[{{"scheme": "CPV"}}]'::jsonb
        AND t.{_HOME_ONLY}
    ) x
    WHERE division IS NOT NULL
    GROUP BY division
    """)

# The name of the section — from the label of the tenders themselves (the canonical classifications are empty,
# see TASK geo_landings): we prefer the label of the root code of the section
# (``XX000000``), otherwise the most frequent label among the codes of the section.
_CPV_LABEL_SQL = text(f"""
    SELECT division, label FROM (
      SELECT division, code, label, n,
             ROW_NUMBER() OVER (
               PARTITION BY division
               ORDER BY (code = division || '000000') DESC, n DESC
             ) AS rn
      FROM (
        SELECT left(e->>'code', 2) AS division, e->>'code' AS code,
               e->>'label' AS label, count(*) AS n
        FROM tenders_normalized t, jsonb_array_elements(t.classifications) e
        WHERE e->>'scheme' = 'CPV' AND e->>'code' IS NOT NULL AND e->>'label' IS NOT NULL
          AND t.{_HOME_ONLY}
        GROUP BY 1, 2, 3
      ) g
    ) ranked
    WHERE rn = 1
    """)


async def cpv_division_stats(session) -> dict[str, dict]:
    """The breakdown by CPV sections (the first 2 digits of the code): total / open / sum EUR + label.

    A full scan of the table — see the docstring of the section above. A section without real records (the threshold ``>= 5`` is checked by the calling route) does not appear in the dictionary.
    """
    stats_rows = (await session.execute(_CPV_STATS_SQL, _HOME_PARAMS)).all()
    label_rows = (await session.execute(_CPV_LABEL_SQL, _HOME_PARAMS)).all()
    labels = {row.division: row.label for row in label_rows}
    return {
        row.division: {
            "total": int(row.total),
            "open": int(row.open_n),
            "open_sum_eur": (float(row.open_sum) if row.open_sum is not None else None),
            "label": labels.get(row.division, row.division),
        }
        for row in stats_rows
    }


# The groups (3 digits) and the full CPV codes: “our tenders” = the regular collection
# (``regular_tender_predicate``: from REGULAR_SINCE, live), a tender is counted once
# per group / code. A full scan, as in ``cpv_division_stats`` — only through
# ``geo_cache`` (TTL ≥ 1 h). ``open_n`` / ``open_sum`` — over ALL open tenders (the same as
# in the listing ``list_open_tenders_by_cpv``), ``total`` — over the regular ones (the index threshold).
_CPV_GROUP_STATS_SQL = text(f"""
    SELECT grp,
           count(*) FILTER (WHERE regular) AS total,
           count(*) FILTER (WHERE deadline_at > now()) AS open_n,
           sum(amount_eur) FILTER (
             WHERE deadline_at > now() AND amount_eur IS NOT NULL
           ) AS open_sum
    FROM (
      SELECT DISTINCT t.id, left(e->>'code', 3) AS grp, t.deadline_at, t.amount_eur,
             COALESCE({regular_tender_predicate("t")}, false) AS regular
      FROM tenders_normalized t, jsonb_array_elements(t.classifications) e
      WHERE t.classifications @> '[{{"scheme": "CPV"}}]'::jsonb
        AND e->>'scheme' = 'CPV' AND e->>'code' ~ '^[0-9]{{3}}'
        AND t.{_HOME_ONLY}
    ) x
    GROUP BY grp
    """)

_CPV_CODE_COUNTS_SQL = text(f"""
    SELECT code, count(*) AS n
    FROM (
      SELECT DISTINCT t.id, left(e->>'code', 8) AS code
      FROM tenders_normalized t, jsonb_array_elements(t.classifications) e
      WHERE t.classifications @> '[{{"scheme": "CPV"}}]'::jsonb
        AND e->>'scheme' = 'CPV' AND e->>'code' ~ '^[0-9]{{8}}'
        AND {regular_tender_predicate("t")}
        AND t.{_HOME_ONLY}
    ) x
    GROUP BY code
    """)


async def cpv_group_stats(session) -> dict[str, dict]:
    """The breakdown by CPV groups (3 digits): ``total`` (regular) / ``open`` / ``open_sum_eur``."""
    rows = (await session.execute(_CPV_GROUP_STATS_SQL, _HOME_PARAMS)).all()
    return {
        row.grp: {
            "total": int(row.total),
            "open": int(row.open_n),
            "open_sum_eur": (float(row.open_sum) if row.open_sum is not None else None),
        }
        for row in rows
    }


async def cpv_code_counts(session) -> dict[str, int]:
    """The number of regular tenders by the full 8-digit CPV code."""
    rows = (await session.execute(_CPV_CODE_COUNTS_SQL, _HOME_PARAMS)).all()
    return {row.code: int(row.n) for row in rows}


_REGION_STATS_SQL = text(f"""
    SELECT r AS code,
           count(*) AS total,
           count(*) FILTER (WHERE deadline_at > now()) AS open_n,
           sum(amount_eur) FILTER (
             WHERE deadline_at > now() AND amount_eur IS NOT NULL
           ) AS open_sum
    FROM tenders_normalized t, unnest(t.regions) r
    WHERE t.{_HOME_ONLY}
    GROUP BY r
    """)


async def region_stats(session) -> dict[str, dict]:
    """The breakdown by regions (``regions`` text[]): total / open / sum EUR.

    A tender with several regions is counted in EACH of them — the card is relevant to every region where it applies (not a double count, but a fact of the data).
    """
    rows = (await session.execute(_REGION_STATS_SQL, _HOME_PARAMS)).all()
    return {
        row.code: {
            "total": int(row.total),
            "open": int(row.open_n),
            "open_sum_eur": (float(row.open_sum) if row.open_sum is not None else None),
        }
        for row in rows
    }


_GEO_ROW_FIELDS = "id, title, customer_name, deadline_at, source_key, amount_eur, amount_original, currency_original"


async def list_open_tenders_by_cpv(
    session, division: str, offset: int, limit: int
) -> Sequence[Row]:
    """The open tenders of the section, by submission deadline (nearest deadline first)."""
    result = await session.execute(
        text(f"""
            SELECT {_GEO_ROW_FIELDS}
            FROM tenders_normalized
            WHERE deadline_at > now()
              AND {_HOME_ONLY}
              AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(classifications) e
                WHERE e->>'scheme' = 'CPV' AND left(e->>'code', {len(division)}) = :division
              )
            ORDER BY deadline_at ASC
            LIMIT :limit OFFSET :offset
            """),
        {"division": division, "limit": limit, "offset": offset, **_HOME_PARAMS},
    )
    return result.all()


async def list_closed_tenders_by_cpv(
    session, division: str, limit: int
) -> Sequence[Row]:
    """The latest closed tenders of the section (freshest deadline first)."""
    result = await session.execute(
        text(f"""
            SELECT {_GEO_ROW_FIELDS}
            FROM tenders_normalized
            WHERE deadline_at <= now()
              AND {_HOME_ONLY}
              AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(classifications) e
                WHERE e->>'scheme' = 'CPV' AND left(e->>'code', {len(division)}) = :division
              )
            ORDER BY deadline_at DESC
            LIMIT :limit
            """),
        {"division": division, "limit": limit, **_HOME_PARAMS},
    )
    return result.all()


async def list_open_tenders_by_region(
    session, code: str, offset: int, limit: int
) -> Sequence[Row]:
    """The open tenders of the region, by submission deadline (nearest deadline first)."""
    result = await session.execute(
        text(f"""
            SELECT {_GEO_ROW_FIELDS}
            FROM tenders_normalized
            WHERE deadline_at > now() AND {_HOME_ONLY} AND regions @> ARRAY[:code]
            ORDER BY deadline_at ASC
            LIMIT :limit OFFSET :offset
            """),
        {"code": code, "limit": limit, "offset": offset, **_HOME_PARAMS},
    )
    return result.all()


async def list_closed_tenders_by_region(
    session, code: str, limit: int
) -> Sequence[Row]:
    """The latest closed tenders of the region (freshest deadline first)."""
    result = await session.execute(
        text(f"""
            SELECT {_GEO_ROW_FIELDS}
            FROM tenders_normalized
            WHERE deadline_at <= now() AND {_HOME_ONLY} AND regions @> ARRAY[:code]
            ORDER BY deadline_at DESC
            LIMIT :limit
            """),
        {"code": code, "limit": limit, **_HOME_PARAMS},
    )
    return result.all()


# --------------------------------------------------------------------------- #
# The /tenderi portal: search, filters, sorting (TASK portal_filters).
# The SQL is parameterised entirely: user input goes ONLY as bind parameters
# (``CAST(:p AS type)`` — asyncpg); the query text holds only fixed
# fragments and token indexes. Sorting — a mapping of the key from the allow-list to a
# fixed ORDER BY + the tie-breaker ``id`` (stable pagination).
# --------------------------------------------------------------------------- #


def _customer_fold_sql(col: str = "customer_name") -> str:
    """The SQL mirror of :func:`src.domain.text_norm.normalize_text` for ``customer_name``.

    ``customer_name`` has no ``*_normalized`` column (unlike the title and the description), so the folding is on the SQL side: cyrl→latn (translate over the single characters + replace for ``U+0452 / U+0459 / U+045A / U+045F`` (the Serbian Cyrillic letters)) → lower → ``đ``→``dj`` → ``unaccent``. The table is taken from ``CYR2LAT`` — one source of truth.
    ‼️ WITHOUT the iekavian form: the input is compared as ``normalize_text(x, ijekavica=False)``.
    """
    from src.domain.text_norm import CYR2LAT

    single = {k: v for k, v in CYR2LAT.items() if len(v) == 1}
    multi = {k: v for k, v in CYR2LAT.items() if len(v) > 1}
    expr = (
        f"translate(coalesce({col}, ''), "
        f"'{''.join(single)}', '{''.join(single.values())}')"
    )
    for src_ch, dst in multi.items():
        expr = f"replace({expr}, '{src_ch}', '{dst}')"
    return f"unaccent(replace(lower({expr}), 'đ', 'dj'))"


_CUSTOMER_FOLD_SQL = _customer_fold_sql()

#: The sort key → a fixed ORDER BY. For “deadline” the direction depends on
#: ``actual`` (open ones — nearest deadline first; all — freshest first).
_PORTAL_ORDER_BY = {
    "deadline": "deadline_at ASC NULLS LAST, id",
    "deadline_all": "deadline_at DESC NULLS LAST, id",
    "published_desc": "published_at DESC NULLS LAST, id",
    "amount_desc": "amount_eur DESC NULLS LAST, id",
    "amount_asc": "amount_eur ASC NULLS LAST, id",
}


def filter_clauses(f, *, alias: str = "") -> tuple[list[str], dict]:
    """The common search conditions: ``q``, the customer, the source, the amount, the publication date.

    One code for the portal and the cabinet feed: ``f`` — ``PortalFilters``, ``alias`` — the prefix of the tender table (``"tn."`` in the feed: there the query has other tables with columns ``source_key`` / ``regions`` too, and without the prefix they are ambiguous).
    The values are only bind parameters (``CAST(:p AS type)``); the column names are literals.
    """
    a = alias
    fold = _customer_fold_sql(f"{a}customer_name") if a else _CUSTOMER_FOLD_SQL
    out: list[str] = []
    out_params: dict = {}
    for i, token in enumerate(f.q_tokens):
        # The customer is folded in SQL without the iekavian form → for it a token without the step 5.
        plain = f.q_tokens_plain[i] if i < len(f.q_tokens_plain) else token
        out.append(
            f"(strpos(coalesce({a}title_normalized, ''), CAST(:q{i} AS text)) > 0 "
            f"OR strpos(coalesce({a}description_normalized, ''), CAST(:q{i} AS text)) > 0 "
            f"OR strpos({fold}, CAST(:qp{i} AS text)) > 0)"
        )
        out_params[f"q{i}"] = token
        out_params[f"qp{i}"] = plain
    if f.customer_folded:
        out.append(
            f"(strpos({fold}, CAST(:customer AS text)) > 0 "
            f"OR strpos(coalesce({a}customer_tax_id, ''), CAST(:customer_raw AS text)) > 0)"
        )
        out_params["customer"] = f.customer_folded
        out_params["customer_raw"] = f.customer
    if f.source:
        out.append(f"{a}source_key = CAST(:source AS text)")
        out_params["source"] = f.source
    amount_parts = []
    if f.min_amount is not None:
        amount_parts.append(f"{a}amount_eur >= CAST(:min_amount AS numeric)")
        out_params["min_amount"] = f.min_amount
    if f.max_amount is not None:
        amount_parts.append(f"{a}amount_eur <= CAST(:max_amount AS numeric)")
        out_params["max_amount"] = f.max_amount
    if amount_parts:
        cond = " AND ".join(amount_parts)
        if f.no_amount:
            cond = f"({cond}) OR {a}amount_eur IS NULL"
        out.append(f"({cond})")
    if f.published_from is not None:
        out.append(f"{a}published_at >= CAST(:published_from AS date)")
        out_params["published_from"] = f.published_from
    if f.published_to is not None:
        out.append(f"{a}published_at < CAST(:published_to AS date) + 1")
        out_params["published_to"] = f.published_to
    return out, out_params


def _portal_where(
    f,
    *,
    cpv_division: str | None,
    region_code: str | None,
) -> tuple[str, dict]:
    """WHERE + bind parameters for the portal filters (``f`` — ``PortalFilters``)."""
    clauses = [_HOME_ONLY]
    params: dict = dict(_HOME_PARAMS)
    if f.actual:
        clauses.append("deadline_at > now()")
    if cpv_division is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM jsonb_array_elements(classifications) e "
            "WHERE e->>'scheme' = 'CPV' "
            f"AND left(e->>'code', {len(cpv_division)}) = CAST(:division AS text))"
        )
        params["division"] = cpv_division
    region = region_code or f.region
    if region:
        clauses.append("regions @> ARRAY[CAST(:region AS text)]")
        params["region"] = region
    common, common_params = filter_clauses(f)
    clauses.extend(common)
    params.update(common_params)
    return " AND ".join(clauses), params


async def search_portal_tenders(
    session,
    f,
    *,
    offset: int,
    limit: int,
    cpv_division: str | None = None,
    region_code: str | None = None,
) -> Sequence[Row]:
    """The portal’s tender page by the filters ``f`` (``PortalFilters``)."""
    where, params = _portal_where(f, cpv_division=cpv_division, region_code=region_code)
    key = "deadline_all" if f.sort == "deadline" and not f.actual else f.sort
    order_by = _PORTAL_ORDER_BY.get(key, _PORTAL_ORDER_BY["deadline"])
    result = await session.execute(
        text(f"""
            SELECT {_GEO_ROW_FIELDS}
            FROM tenders_normalized
            WHERE {where}
            ORDER BY {order_by}
            LIMIT :limit OFFSET :offset
            """),
        {**params, "limit": limit, "offset": offset},
    )
    return result.all()


async def count_portal_tenders(
    session,
    f,
    *,
    cpv_division: str | None = None,
    region_code: str | None = None,
) -> int:
    """How many tenders are found by the filters ``f`` (“N found”)."""
    where, params = _portal_where(f, cpv_division=cpv_division, region_code=region_code)
    result = await session.execute(
        text(f"SELECT count(*) FROM tenders_normalized WHERE {where}"), params
    )
    return int(result.scalar_one())


# --------------------------------------------------------------------------- #
# The home page: facts for quoting and a card example (newcomer_ux, step 6).
# Both queries are cached by the caller (``src.api.public.geo_cache``, TTL 1 h).
# --------------------------------------------------------------------------- #


async def latest_tender_change_at(session) -> datetime | None:
    """The moment of the last change of the data (``max(last_changed_at)``) — “updated”."""
    result = await session.execute(select(func.max(TenderNormalized.last_changed_at)))
    return result.scalar_one_or_none()


async def latest_open_example(session) -> Row | None:
    """The freshest OPEN tender with a non-empty description or price — the example on the home page.

    ``deadline_at > now()`` as the first condition — by the deadline index (open ones ~2K).
    ``ORDER BY published_at DESC NULLS LAST, id`` — a deterministic choice (the project rules: ``LIMIT`` without a full ``ORDER BY`` is non-deterministic).
    """
    result = await session.execute(
        text(f"""
            SELECT {_GEO_ROW_FIELDS}
            FROM tenders_normalized
            WHERE deadline_at > now()
              AND {_HOME_ONLY}
              AND (NULLIF(btrim(description), '') IS NOT NULL OR amount_eur IS NOT NULL)
            ORDER BY published_at DESC NULLS LAST, id
            LIMIT 1
            """),
        _HOME_PARAMS,
    )
    return result.first()
