"""The report builder “Analitika” (R4, owner decision 10.10.2026) — the SQL layer.

The user chooses from WHITE LISTS: a dataset (tenders / award decisions), 1–2 groupings, a sort
metric and filters. The server assembles a parameterized query: column names and expressions are taken ONLY from the dictionaries
of this module, all user values are bind parameters. There is no free SQL.

The structure of the query — two levels:

* the inner ``src`` — the projection of the dataset into the SAME columns (``d``, ``amount``,
  ``cpv``, ``customer_key``/``customer_label``, ``winner_key``/``winner_label``,
  ``region``, ``procedure``, ``source``, ``country``). Here also the predicates
  that belong to the dataset itself: “live tenders” without the archive and the exclusion
  of canceled decisions;
* the outer one — filters, grouping and aggregates over the columns of ``src``.

The dataset predicates (verified on production 10.10.2026, see ANALYTICS_PRODUCT.md §1):

* ``tenders`` — ``published_at >= first_seen_at - interval '365 days'``: the archive
  of 2019/2020 was imported in 06.2026 (``first_seen_at`` is fresh, ``published_at``
  is old or NULL) and the report on the “market” must not mix it with the live flow
  (16,047 rows since 30.05.2026);
* ``awards`` — without canceled contracts
  (``raw->'contract_row'->>'IsReversed' = 'true'``, 14 rows in production).

Consortium: one decision is stored in N rows (``explode_award_winners`` —
one row per member, the amount only on the first, NULL on the others; in production 12,840
rows = 11,660 decisions). Therefore the “count” is ``count(DISTINCT unit_key)``, where
for decisions ``unit_key = source_uid|lot_number``, for tenders — ``id``; the amounts —
only over the rows with an amount (there is no double count). In the grouping by winner
each member of a consortium gets the decision into its own count (it won it), and
the amount of the decision — only on the first (that is how it lies in the data).

A NULL amount is NOT zero: ``sum/avg/median`` ignore it, and ``with_amount``
(“with an amount N of M”) is always computed next to it, so that the share of gaps is visible.

CPV: for tenders — the first CPV-system classification record (both ingest
contracts — ``scheme``/``system_code``, the canon :mod:`src.domain.classifications`),
for decisions — the leading digits of ``raw->'contract_row'->>'Cpv'`` (the portal format
``“72267100 - Odrzavanje ...”``; there is no separate column, owner decision — without a migration).

The customer key by name — the same formula as in the customer analytics
(:data:`src.analytics.awards_analytics._CUSTOMER_NAME_KEY`); for decisions —
``customer_pib``, and when empty — the name (the PIB is present for 93% of decisions, for tenders 0%).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy import text

from src.analytics.awards_analytics import _CUSTOMER_NAME_KEY
from src.analytics.reprocurement import expiry_expr, expiry_window_sql
from src.domain.classifications import (
    CPV_SYSTEM,
    cpv_prefix,
    entry_code,
    entry_system,
    sql_tender_system,
)
from src.domain.text_norm import fold, fold_sql
from src.workers.matching import is_english_tender, keywords_match, match_haystack

# --------------------------------------------------------------------------- #
#  White lists
# --------------------------------------------------------------------------- #

SUBJECTS: tuple[str, ...] = ("tenders", "awards")

#: The “live tender” predicate (without the archive). ONE row — the test mutates it.
LIVE_TENDER_PREDICATE = "t.published_at >= t.first_seen_at - interval '365 days'"

#: A canceled award decision (the contract was canceled) — we do not count it.
NOT_REVERSED_PREDICATE = (
    "COALESCE(a.raw->'contract_row'->>'IsReversed', 'false') <> 'true'"
)

#: The leading digits of the CPV code from the portal string “72267100 - Name”.
_AWARD_CPV_EXPR = r"substring(a.raw->'contract_row'->>'Cpv' from '^\s*([0-9]{2,8})')"


def _qualified_customer_key(alias: str) -> str:
    """``_CUSTOMER_NAME_KEY`` with a qualified column (in awards there is a JOIN
    on ``tenders_normalized``, where there is also ``customer_name``)."""
    assert _CUSTOMER_NAME_KEY.count("customer_name") == 1
    return _CUSTOMER_NAME_KEY.replace("customer_name", f"{alias}.customer_name")


# The first CPV record of the tender — deterministically by the order in the array.
_TENDER_CPV_LATERAL = f"""
    LEFT JOIN LATERAL (
        SELECT substring(e.value->>'code' from '^\\s*([0-9]{{2,8}})') AS code
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(t.classifications) = 'array'
                 THEN t.classifications ELSE '[]'::jsonb END
        ) WITH ORDINALITY AS e(value, ord)
        WHERE {sql_tender_system("e.value")} = '{CPV_SYSTEM}'
          AND e.value->>'code' IS NOT NULL
        ORDER BY e.ord
        LIMIT 1
    ) cpv ON true"""

# ALL CPV codes of the tender — for the FILTER (a tender with several CPVs gets into the report
# if any code fits, as in the preview of the matching ``PREVIEW_WHERE_A``). For the
# GROUPING only the first code is taken (``cpv``), otherwise a tender with N codes
# would be counted N times.
_TENDER_CPV_ALL = f"""ARRAY(
        SELECT substring(e->>'code' from '^\\s*([0-9]{{2,8}})')
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(t.classifications) = 'array'
                 THEN t.classifications ELSE '[]'::jsonb END
        ) e
        WHERE {sql_tender_system("e")} = '{CPV_SYSTEM}'
          AND e->>'code' IS NOT NULL
    )"""

_SRC_SQL: dict[str, str] = {
    "tenders": f"""
    SELECT (t.published_at AT TIME ZONE 'Europe/Belgrade')::date AS d,
           t.amount_eur AS amount,
           cpv.code AS cpv,
           {_TENDER_CPV_ALL} AS cpv_all,
           t.regions AS regions_arr,
           {_qualified_customer_key("t")} AS customer_key,
           t.customer_name AS customer_label,
           NULL::text AS customer_pib,
           NULL::text AS winner_key,
           NULL::text AS winner_label,
           NULL::text AS winner_pib,
           t.id::text AS unit_key,
           COALESCE(t.title_normalized, '') AS haystack_title,
           COALESCE(t.description_normalized, '') AS haystack_desc,
           NULL::date AS expiry_date,
           t.customer_region_code AS region,
           t.procedure_type AS procedure,
           t.source_key AS source,
           t.language AS language,
           t.country_code AS country
    FROM tenders_normalized t
    {_TENDER_CPV_LATERAL}
    WHERE {LIVE_TENDER_PREDICATE}""",
    "awards": f"""
    SELECT a.decision_date AS d,
           a.award_amount_eur AS amount,
           {_AWARD_CPV_EXPR} AS cpv,
           ARRAY[{_AWARD_CPV_EXPR}] AS cpv_all,
           t.regions AS regions_arr,
           COALESCE(NULLIF(btrim(a.customer_pib), ''),
                    {_qualified_customer_key("a")}) AS customer_key,
           a.customer_name AS customer_label,
           NULLIF(btrim(a.customer_pib), '') AS customer_pib,
           COALESCE(NULLIF(btrim(a.winner_pib), ''),
                    lower(btrim(regexp_replace(a.winner_name, '\\s+', ' ', 'g'))))
               AS winner_key,
           a.winner_name AS winner_label,
           NULLIF(btrim(a.winner_pib), '') AS winner_pib,
           a.source_uid || '|' || COALESCE(a.lot_number, '') AS unit_key,
           COALESCE(a.lot_title, '') || ' ' || COALESCE(t.title_normalized, '')
               AS haystack_title,
           COALESCE(t.description_normalized, '') AS haystack_desc,
           CAST({expiry_expr("a")} AS date) AS expiry_date,
           t.customer_region_code AS region,
           t.procedure_type AS procedure,
           t.source_key AS source,
           t.language AS language,
           t.country_code AS country
    FROM tender_awards a
    LEFT JOIN tenders_normalized t ON t.id = a.tender_id
    WHERE {NOT_REVERSED_PREDICATE}""",
}


@dataclass(frozen=True)
class Dim:
    """The grouping dimension: the SQL key, the SQL label, the available datasets, the chronology."""

    key_sql: str
    label_sql: str
    subjects: tuple[str, ...] = SUBJECTS
    chrono: bool = False


DIMENSIONS: dict[str, Dim] = {
    "month": Dim(
        "to_char(date_trunc('month', d), 'YYYY-MM')",
        "to_char(date_trunc('month', d), 'YYYY-MM')",
        chrono=True,
    ),
    "quarter": Dim(
        "to_char(date_trunc('quarter', d), 'YYYY-\"Q\"Q')",
        "to_char(date_trunc('quarter', d), 'YYYY-\"Q\"Q')",
        chrono=True,
    ),
    "customer": Dim(
        "customer_key",
        "min(customer_label) || COALESCE(' · PIB ' || min(customer_pib), '')",
    ),
    "winner": Dim(
        "winner_key",
        "min(winner_label) || COALESCE(' · PIB ' || min(winner_pib), '')",
        subjects=("awards",),
    ),
    "cpv2": Dim("left(cpv, 2)", "left(cpv, 2)"),
    "cpv3": Dim("left(cpv, 3)", "left(cpv, 3)"),
    "region": Dim("region", "region"),
    "procedure": Dim("procedure", "procedure"),
    "source": Dim("source", "source"),
    "country": Dim("country", "country"),
}

#: The metrics (all are always computed; the selected one — the sort order).
MEASURES: dict[str, str] = {
    "count": "n",
    "sum": "sum_eur",
    "avg": "avg_eur",
    "median": "median_eur",
    "customers": "distinct_customers",
    "winners": "distinct_winners",
}
MEASURE_SUBJECTS: dict[str, tuple[str, ...]] = {"winners": ("awards",)}

#: The table columns that can be sorted by (the key ``sort`` → the SELECT column).
#: The white list: identifiers are not bound in SQL, therefore only a value of this dictionary
#: gets into the ORDER BY, and not the input. ``g1``/``g2`` — the groups (by label).
SORT_COLUMNS: dict[str, str] = {
    "g1": "g1_label",
    "g2": "g2_label",
    "count": "n",
    "with_amount": "with_amount",
    "sum": "sum_eur",
    "avg": "avg_eur",
    "median": "median_eur",
    "customers": "distinct_customers",
    "winners": "distinct_winners",
}
SORT_SUBJECTS: dict[str, tuple[str, ...]] = {"winners": ("awards",)}


def default_sort_dir(key: str) -> str:
    """The direction of the first click: groups — alphabetically, numbers — from the larger."""
    return "asc" if key in ("g1", "g2") else "desc"


def sort_key_ok(key: str, subject: str, has_group2: bool) -> bool:
    if key not in SORT_COLUMNS:
        return False
    if key == "g2":
        return has_group2
    return subject in SORT_SUBJECTS.get(key, SUBJECTS)


_AGG_COLS = """
    count(DISTINCT unit_key) AS n,
    count(amount) AS with_amount,
    sum(amount) AS sum_eur,
    avg(amount) AS avg_eur,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) AS median_eur,
    count(DISTINCT customer_key) AS distinct_customers,
    count(DISTINCT winner_key) AS distinct_winners"""

#: The windows of the preset “ugovori koji ističu” (months); the input — only from this list.
EXPIRY_HORIZONS: tuple[int, ...] = (3, 6, 12)

#: Ceilings: rows on the screen and in Excel.
SCREEN_LIMIT = 50
EXPORT_LIMIT = 50_000

_MAX_TEXT = 120

# Folded labels for the “contains” search: whitespace runs collapsed (as in
# the customer key), then fold — the input goes through the same steps in :func:`_fold_q`.
_CUSTOMER_FOLDED = fold_sql(
    r"btrim(regexp_replace(COALESCE(customer_label, ''), '\s+', ' ', 'g'))"
)
_WINNER_FOLDED = fold_sql(
    r"btrim(regexp_replace(COALESCE(winner_label, ''), '\s+', ' ', 'g'))"
)


def _fold_q(value: str) -> str:
    """Search input → a folded substring (a mirror of ``_CUSTOMER_FOLDED``)."""
    return fold(re.sub(r"\s+", " ", value or "").strip(), ijekavica=False)


_DIGITS = re.compile(r"\D")

#: The selection ceiling in the CPV widget (codes) and of the raw length of the input of the ``cpv`` parameter.
MAX_CPV_PREFIXES = 20
_MAX_CPV_RAW = 600


def parse_cpv_prefixes(raw: str) -> tuple[str, ...]:
    """The input of the CPV widget (``eu_cpv:45210000``, ``33*``, ``45``) → digit prefixes.

    The separators are any spaces/commas/``;``. A full 8-digit code is reduced to its
    significant part (``45210000`` → ``4521``, as :func:`cpv_prefix`, at least two
    digits): in the report, choosing a section means “and everything under it”. Garbage
    is discarded, duplicates too, the order is kept.
    """
    out: list[str] = []
    for token in re.split(r"[\s,;]+", raw[:_MAX_CPV_RAW]):
        digits = _DIGITS.sub("", token.split(":")[-1])[:8]
        if len(digits) == 8:
            digits = digits.rstrip("0")[:8] or digits
            if len(digits) < 2:
                digits = digits.ljust(2, "0")  # 30000000 → “30”, not “3”
        if digits and digits not in out:
            out.append(digits)
        if len(out) >= MAX_CPV_PREFIXES:
            break
    return tuple(out)


# --------------------------------------------------------------------------- #
#  The report specification (parsing of the input)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReportSpec:
    subject: str = "tenders"
    group1: str = "customer"
    group2: str = ""
    measure: str = "count"
    #: Sorting by a click on the header (``""`` = the default order).
    sort: str = ""
    sort_dir: str = "desc"
    date_from: date | None = None
    date_to: date | None = None
    cpv: tuple[str, ...] = ()  # code prefixes (digits); any of them — a match
    region: str = ""
    customer_q: str = ""
    winner_q: str = ""
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    profile_id: str = ""
    #: The preset “contracts expiring in the nearest N months” (0 = off; decisions only).
    expiring_months: int = 0
    #: The keywords of the template: ``{"include": [...], "exclude": [...], "dict": bool}``
    #: (filled in by the service from the profile; ``None`` = no words). Applied in two passes
    #: via :func:`resolve_keyword_units` — the result goes into ``keyword_units``.
    profile_keywords: Any = None
    #: ``unit_key`` of the units that passed ``keywords_match`` (``None`` = no filter;
    #: an empty tuple = not a single unit passed).
    keyword_units: tuple[str, ...] | None = None
    # The filters “as in my template” — filled in by the service from the tenant’s profile.
    profile_cpv: tuple[str, ...] = ()
    profile_regions: tuple[str, ...] = ()


def _parse_date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat((raw or "").strip()[:10]) if raw else None
    except ValueError:
        return None


def _parse_amount(raw: str | None) -> Decimal | None:
    s = (raw or "").strip().replace(" ", "").replace(",", ".")
    if not s or len(s) > 20:
        return None
    try:
        val = Decimal(s)
    except InvalidOperation:
        return None
    # The order ceiling: 1e999999 otherwise gives DataError/OverflowError in the DB (500).
    if not val.is_finite() or val < 0 or val.adjusted() > 15:
        return None
    return val


def parse_spec(params: Mapping[str, Any]) -> ReportSpec:
    """Raw query parameters → :class:`ReportSpec` (everything unknown → the default).

    Does not raise: the user input is cleaned to a white list, garbage is discarded.
    """

    def g(name: str) -> str:
        v = params.get(name)
        return str(v).strip()[:_MAX_TEXT] if v is not None else ""

    subject = g("subject") if g("subject") in SUBJECTS else "tenders"

    def dim_ok(name: str) -> bool:
        return name in DIMENSIONS and subject in DIMENSIONS[name].subjects

    group1 = g("g1") if dim_ok(g("g1")) else "customer"
    group2 = g("g2") if dim_ok(g("g2")) and g("g2") != group1 else ""
    measure = g("m")
    if measure not in MEASURES or subject not in MEASURE_SUBJECTS.get(
        measure, SUBJECTS
    ):
        measure = "count"
    region = g("region")
    region = region if re.fullmatch(r"[A-Z]{2}-\d{2}", region) else ""
    pid = g("profile")
    pid = pid if re.fullmatch(r"[0-9a-fA-F-]{36}", pid) else ""
    # A white list, not int(): isdigit() passes '²' → ValueError → 500 (#632).
    exp = int(g("exp")) if g("exp") in ("3", "6", "12") else 0
    sort = g("sort") if sort_key_ok(g("sort"), subject, bool(group2)) else ""
    sort_dir = g("dir").lower()
    if sort_dir not in ("asc", "desc"):
        sort_dir = default_sort_dir(sort) if sort else "desc"
    return ReportSpec(
        subject=subject,
        group1=group1,
        group2=group2,
        measure=measure,
        sort=sort,
        sort_dir=sort_dir,
        date_from=_parse_date(g("from")),
        date_to=_parse_date(g("to")),
        cpv=parse_cpv_prefixes(str(params.get("cpv") or "")),
        region=region,
        customer_q=g("customer"),
        winner_q=g("winner") if subject == "awards" else "",
        amount_min=_parse_amount(g("amin")),
        amount_max=_parse_amount(g("amax")),
        profile_id=pid,
        expiring_months=exp if subject == "awards" and exp in EXPIRY_HORIZONS else 0,
    )


def profile_filters(
    classifications: Any, regions: Any
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The tenant’s profile → (CPV prefixes, regions) for the “as in the template” filter.

    The profile codes are of the same canon as the matching (``cpv_prefix``: the section
    ``30000000`` → ``30``), so that the report of “my topic” matches the selection.
    The profile keywords are applied separately (:func:`resolve_keyword_units`).
    """
    prefixes: list[str] = []
    # The production canon — the object {"include": [...], "exclude": [...]} (12/12 live
    # profiles); a list — backward compatibility. Iteration over the keys of the object
    # silently lost the CPV of the template. exclude is not applied here (see REPORT).
    if isinstance(classifications, dict):
        classifications = classifications.get("include")
    for entry in classifications or []:
        if entry_system(entry) != CPV_SYSTEM:
            continue
        code = entry_code(entry)
        if not code:
            continue
        p = _DIGITS.sub("", str(cpv_prefix(code)))
        if len(p) >= 2 and p not in prefixes:
            prefixes.append(p)
    regs = tuple(
        r for r in (regions or []) if isinstance(r, str) and r.strip()
    )  # empty = all regions (the same meaning as in the matching)
    return tuple(prefixes), regs


# --------------------------------------------------------------------------- #
#  SQL assembly
# --------------------------------------------------------------------------- #


def _where(spec: ReportSpec) -> tuple[str, dict[str, Any]]:
    conds: list[str] = []
    params: dict[str, Any] = {}
    if spec.date_from:
        conds.append("d >= CAST(:date_from AS date)")
        params["date_from"] = spec.date_from
    if spec.date_to:
        conds.append("d <= CAST(:date_to AS date)")
        params["date_to"] = spec.date_to
    if spec.cpv:
        conds.append(
            "EXISTS (SELECT 1 FROM unnest(cpv_all) c "
            "WHERE c LIKE ANY(CAST(:cpv_like AS text[])))"
        )
        params["cpv_like"] = [p + "%" for p in spec.cpv]
    if spec.profile_cpv:
        conds.append(
            "EXISTS (SELECT 1 FROM unnest(cpv_all) c "
            "WHERE c LIKE ANY(CAST(:profile_cpv AS text[])))"
        )
        params["profile_cpv"] = [p + "%" for p in spec.profile_cpv]
    if spec.expiring_months:
        # The same end date and the same window as in the reprocurement forecast.
        conds.append(expiry_window_sql("expiry_date", "expiry_months"))
        params["expiry_months"] = spec.expiring_months
    if spec.keyword_units is not None:
        conds.append("unit_key = ANY(CAST(:keyword_units AS text[]))")
        params["keyword_units"] = list(spec.keyword_units)
    if spec.region:
        conds.append("region = CAST(:region AS text)")
        params["region"] = spec.region
    if spec.profile_regions:
        # A mirror of the matching (STAGE_A_SQL / PREVIEW_WHERE_A): the intersection with
        # ``tenders_normalized.regions``; the profile’s regions act only on
        # tenders of ITS OWN country (RS-xx does not cut a BA tender). A tender of its own country
        # without a resolved district is cut off — the same as in the delivery.
        conds.append(
            "(COALESCE(regions_arr, '{}') && CAST(:profile_regions AS text[]) "
            "OR NOT EXISTS (SELECT 1 FROM unnest(CAST(:profile_regions AS text[])) r "
            "WHERE left(r, 2) = CAST(country AS text)))"
        )
        params["profile_regions"] = list(spec.profile_regions)
    cq = _fold_q(spec.customer_q)
    if cq:
        conds.append(f"strpos({_CUSTOMER_FOLDED}, CAST(:customer_q AS text)) > 0")
        params["customer_q"] = cq
    wq = _fold_q(spec.winner_q)
    if wq and spec.subject == "awards":
        conds.append(
            "(strpos(COALESCE(winner_pib, ''), CAST(:winner_raw AS text)) > 0 OR "
            f"strpos({_WINNER_FOLDED}, "
            "CAST(:winner_q AS text)) > 0)"
        )
        params["winner_q"] = wq
        params["winner_raw"] = spec.winner_q.strip()
    if spec.amount_min is not None:
        conds.append("amount >= CAST(:amount_min AS numeric)")
        params["amount_min"] = spec.amount_min
    if spec.amount_max is not None:
        conds.append("amount <= CAST(:amount_max AS numeric)")
        params["amount_max"] = spec.amount_max
    return (" WHERE " + " AND ".join(conds)) if conds else "", params


def effective_sort(spec: ReportSpec) -> tuple[str, str]:
    """The actual order of rows ``(key, direction)`` — for the ▲/▼ indicator."""
    if spec.sort:
        return spec.sort, spec.sort_dir
    if DIMENSIONS[spec.group1].chrono:
        return "g1", "asc"
    return spec.measure, "desc"


def build_report_sql(spec: ReportSpec, *, limit: int) -> tuple[str, dict[str, Any]]:
    """The grouped query (the report rows) → ``(sql, params)``."""
    d1 = DIMENSIONS[spec.group1]
    d2 = DIMENSIONS[spec.group2] if spec.group2 else None
    where, params = _where(spec)
    sel = [f"{d1.key_sql} AS g1_key", f"{d1.label_sql} AS g1_label"]
    grp = [d1.key_sql]
    if d2:
        sel += [f"{d2.key_sql} AS g2_key", f"{d2.label_sql} AS g2_label"]
        grp.append(d2.key_sql)
    else:
        sel += ["NULL::text AS g2_key", "NULL::text AS g2_label"]
    measure_col = MEASURES[spec.measure]
    if spec.sort:
        # An explicit sort: the column and the direction — only from the white lists;
        # NULL (no amounts) is always at the end; the completion by group keys — determinism.
        direction = "ASC" if spec.sort_dir == "asc" else "DESC"
        order = (
            f"{SORT_COLUMNS[spec.sort]} {direction} NULLS LAST, "
            "g1_key ASC NULLS LAST" + (", g2_key ASC NULLS LAST" if d2 else "")
        )
    elif d1.chrono:
        order = "g1_key ASC NULLS LAST" + (
            f", {measure_col} DESC NULLS LAST, g2_key ASC" if d2 else ""
        )
    else:
        order = f"{measure_col} DESC NULLS LAST, g1_key ASC NULLS LAST" + (
            ", g2_key ASC NULLS LAST" if d2 else ""
        )
    sql = f"""
    SELECT {", ".join(sel)},{_AGG_COLS},
           count(*) OVER () AS total_groups
    FROM ({_SRC_SQL[spec.subject]}) src{where}
    GROUP BY {", ".join(grp)}
    ORDER BY {order}
    LIMIT CAST(:lim AS integer)"""
    params["lim"] = int(limit)
    return sql, params


def build_totals_sql(spec: ReportSpec) -> tuple[str, dict[str, Any]]:
    """The total row by the same filters (without grouping) + the sample period."""
    where, params = _where(spec)
    sql = f"""
    SELECT {_AGG_COLS},
           min(d) AS first_date, max(d) AS last_date
    FROM ({_SRC_SQL[spec.subject]}) src{where}"""
    return sql, params


def build_coverage_sql(subject: str) -> str:
    """The coverage of the whole dataset (without the user’s filters) — for “Napomena”."""
    return f"""
    SELECT count(DISTINCT unit_key) AS n, count(amount) AS with_amount,
           min(d) AS first_date, max(d) AS last_date,
           array_remove(array_agg(DISTINCT source), NULL) AS sources
    FROM ({_SRC_SQL[subject]}) src"""


# --------------------------------------------------------------------------- #
#  The keywords of the template
# --------------------------------------------------------------------------- #

#: The ceiling of the candidates for the text predicate: ``keywords_match`` — Python
#: (folding + word stems); in SQL it cannot be expressed without a copy, and a copy of the predicate =
#: two sources of truth (see ``text_norm``). Above the ceiling the report asks to narrow
#: the filters, rather than silently trimming the sample.
MAX_KEYWORD_CANDIDATES = 40_000


class KeywordScopeTooBroad(Exception):
    """The candidates for the keywords are more than ``MAX_KEYWORD_CANDIDATES``."""


def build_keyword_candidates_sql(
    spec: ReportSpec, *, limit: int
) -> tuple[str, dict[str, Any]]:
    """The units of the dataset under the other filters + their text (for ``keywords_match``)."""
    where, params = _where(replace(spec, keyword_units=None))
    sql = f"""
    SELECT DISTINCT ON (unit_key) unit_key, haystack_title, haystack_desc,
           language, source
    FROM ({_SRC_SQL[spec.subject]}) src{where}
    ORDER BY unit_key
    LIMIT CAST(:lim AS integer)"""
    params["lim"] = int(limit)
    return sql, params


def _keyword_filter(rows: list[Any], kw: Mapping[str, Any]) -> tuple[str, ...]:
    out: list[str] = []
    for r in rows:
        english = is_english_tender(r.language, r.source)
        if keywords_match(
            kw.get("include"),
            kw.get("exclude"),
            match_haystack(r.haystack_title, r.haystack_desc, english),
            keyword_dict_enabled=bool(kw.get("dict")),
            english=english,
        ):
            out.append(r.unit_key)
    return tuple(out)


def has_keywords(kw: Mapping[str, Any] | None) -> bool:
    """The profile has at least one include word or a stop word."""
    if not kw:
        return False
    groups = kw.get("include") or []
    has_include = any((g or {}).get("all_of") for g in groups if isinstance(g, dict))
    return has_include or any(kw.get("exclude") or [])


async def resolve_keyword_units(db, spec: ReportSpec) -> ReportSpec:
    """First pass: ``keywords_match`` over candidates → ``spec.keyword_units``.

    The predicate is the same as for matching/preview (:func:`keywords_match` over the text
    :func:`match_haystack`: title + description, for English — title only;
    ``fold`` from ``text_norm``). Decisions have ``lot_title`` added to the title.
    Without words in the profile — the spec is returned as is.
    """
    kw = spec.profile_keywords
    if not has_keywords(kw):
        return spec
    sql, params = build_keyword_candidates_sql(spec, limit=MAX_KEYWORD_CANDIDATES + 1)
    rows = (await db.execute(text(sql), params)).fetchall()
    if len(rows) > MAX_KEYWORD_CANDIDATES:
        raise KeywordScopeTooBroad
    # Pure CPU (folding, word stems) — not on the event loop.
    units = await asyncio.to_thread(_keyword_filter, rows, kw)
    return replace(spec, keyword_units=units)


# --------------------------------------------------------------------------- #
#  Execution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReportRow:
    g1_key: str | None
    g1_label: str | None
    g2_key: str | None
    g2_label: str | None
    n: int
    with_amount: int
    sum_eur: Decimal | None
    avg_eur: Decimal | None
    median_eur: float | None
    distinct_customers: int
    distinct_winners: int


@dataclass(frozen=True)
class ReportTotals:
    n: int = 0
    with_amount: int = 0
    sum_eur: Decimal | None = None
    avg_eur: Decimal | None = None
    median_eur: float | None = None
    distinct_customers: int = 0
    distinct_winners: int = 0
    first_date: date | None = None
    last_date: date | None = None


@dataclass(frozen=True)
class Coverage:
    n: int = 0
    with_amount: int = 0
    first_date: date | None = None
    last_date: date | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportResult:
    rows: list[ReportRow] = field(default_factory=list)
    total_groups: int = 0
    totals: ReportTotals = field(default_factory=ReportTotals)
    coverage: Coverage = field(default_factory=Coverage)
    truncated: bool = False
    generated_at: datetime | None = None


def _row(r: Any) -> ReportRow:
    return ReportRow(
        g1_key=r.g1_key,
        g1_label=r.g1_label,
        g2_key=r.g2_key,
        g2_label=r.g2_label,
        n=int(r.n),
        with_amount=int(r.with_amount),
        sum_eur=r.sum_eur,
        avg_eur=r.avg_eur,
        median_eur=r.median_eur,
        distinct_customers=int(r.distinct_customers),
        distinct_winners=int(r.distinct_winners),
    )


async def run_report(
    db, spec: ReportSpec, *, limit: int, with_coverage: bool = False
) -> ReportResult:
    """Runs the report: the rows (up to ``limit``), the total and (optionally) the coverage of the dataset.

    The timeout/rollback is on the caller’s side (the service sets ``statement_timeout``).
    """
    spec = await resolve_keyword_units(db, spec)
    sql, params = build_report_sql(spec, limit=limit + 1)
    rows = (await db.execute(text(sql), params)).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    tsql, tparams = build_totals_sql(spec)
    t = (await db.execute(text(tsql), tparams)).first()
    totals = (
        ReportTotals(
            n=int(t.n),
            with_amount=int(t.with_amount),
            sum_eur=t.sum_eur,
            avg_eur=t.avg_eur,
            median_eur=t.median_eur,
            distinct_customers=int(t.distinct_customers),
            distinct_winners=int(t.distinct_winners),
            first_date=t.first_date,
            last_date=t.last_date,
        )
        if t is not None
        else ReportTotals()
    )
    coverage = Coverage()
    if with_coverage:
        c = (await db.execute(text(build_coverage_sql(spec.subject)))).first()
        if c is not None:
            coverage = Coverage(
                n=int(c.n),
                with_amount=int(c.with_amount),
                first_date=c.first_date,
                last_date=c.last_date,
                sources=tuple(sorted(c.sources or ())),
            )
    return ReportResult(
        rows=[_row(r) for r in rows],
        total_groups=int(rows[0].total_groups) if rows else 0,
        totals=totals,
        coverage=coverage,
        truncated=truncated,
    )
