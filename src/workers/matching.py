"""Two-stage matching of a tender against the subscribers' profiles.

Implements the routing logic from ARCHITECTURE §13.2 and the template queries of
DB_SCHEMA §6:

- **Stage A** (:func:`stage_a_candidates`) — the SQL pre-filter §6.1: by arrays
  (sources, classifications, regions, procedures, languages), numeric
  limits (budget, days-until-deadline) and the moment of subscription (SRC-0a:
  the quiet first pass of the source + ``profiles.source_opt_in``). The country of the tender
  does NOT cut the profile — the source gate is the choice of ``enabled_source_keys``.
  Returns 5–50 candidate profiles in 5–30 ms.
  **The NULL semantics of the tender** (Chat 28): a tender field ``NULL``/empty →
  the corresponding limit of the profile is *not applicable*, the profile is NOT cut
  (jnportal v1 does not return deadline/procedure/amount). When the tender’s value is set, the filter works as before.
- **Stage B** (:func:`keywords_match`) — the text comparison §6.2: on the
  Python side (ARCHITECTURE §13.2 “precompiled regex / position”).
  The semantics of the two-level groups: ``OR`` between the groups of ``keywords_include``,
  ``AND`` inside ``all_of``; any word from ``keywords_exclude`` excludes.
- **Stage C** (:func:`stage_c_soft_match`) — soft-match via embedding §6.3.
  In Sprint-16 — a **stub** (by the task); it returns an empty list.

The write of the result — :func:`write_outbox` (§6.4): one INSERT per channel with
``ON CONFLICT (profile_id, tender_id, channel) DO NOTHING`` (idempotency).
The channel is checked before the write against the live addressee (:func:`live_channels`): no
active ``user_channels`` — no record in the queue.

The module opens no connections itself: the SQL functions accept a ready
``AsyncSession``. The pure functions (:func:`keywords_match`,
:func:`primary_classification`) do not touch the DB and are tested directly.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from functools import lru_cache
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.actuality import sql_no_deadline_fresh, sql_no_deadline_fresh_tn
from src.domain.classifications import sql_profile_system, sql_tender_system
from src.domain.classifications import primary_classification as _primary_canonical
from src.domain.text_norm import fold, stem_sr

logger = logging.getLogger(__name__)


def _fill(sql: str, **exprs: str) -> str:
    """Substitutes SQL expressions into the template by the tokens ``@name@``.

    Needed because the query cannot be built as an f-string: it contains the literals ``'{}'``
    (an empty array). A missing token is an error at import, not a silent divergence of the predicates: the comparison of the classification systems must go through
    :mod:`src.domain.classifications` in BOTH queries.
    """
    for name, expr in exprs.items():
        token = f"@{name}@"
        if token not in sql:
            raise RuntimeError(f"The SQL template does not contain the token {token}")
        sql = sql.replace(token, expr)
    if "@" in sql:
        raise RuntimeError("unsubstituted tokens @…@ remain in the SQL")
    return sql


def sql_customer_key(expr: str) -> str:
    """The SQL key of the customer: case and spaces do not distinguish one customer.

    The ONLY place of the rule (0046): by it the ``customers_include`` / ``customers_exclude`` of the profile are compared with the ``customer_name`` of the tender in stage A and in the preview; the customer hint of the form is also grouped by it. Both sides are collapsed in SQL — there is no second implementation in Python. Not the PIB:
    ``customer_tax_id`` of new tenders is empty (0 of 10 300 over 90 days, measurement
    01.10.2026). The name is written by one source in one alphabet, so lower + the collapse of spaces suffices (1913 → 1910 distinct over 90 days).
    """
    return f"lower(btrim(regexp_replace({expr}, '\\s+', ' ', 'g')))"


def sql_customer_in(array_expr: str, name_expr: str) -> str:
    """``EXISTS``: the customer ``name_expr`` is in the array of names ``array_expr``.

    A NULL tender name gives FALSE (a comparison with NULL is not TRUE): “watch” does not let it through, “exclude” does not cut it.
    """
    return (
        f"EXISTS (SELECT 1 FROM unnest(COALESCE({array_expr}, '{{}}')) cust "
        f"WHERE {sql_customer_key('cust')} = {sql_customer_key(name_expr)})"
    )


# --------------------------------------------------------------------------- #
#  The tender representation for the matching
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TenderView:
    """The fields of ``tenders_normalized`` needed by routing (DB_SCHEMA §2.10).

    A separate value object, so that the matching does not depend on the ORM and is easy to build
    in unit tests. ``haystack`` is assembled from the normalized title/description
    once — stage B is run against it.
    """

    id: str
    country_code: str | None
    source_key: str | None
    regions: list[str] = field(default_factory=list)
    classifications: list[dict[str, Any]] = field(default_factory=list)
    procedure_type: str | None = None
    language: str | None = None
    amount_eur: float | None = None
    deadline_at: datetime | None = None
    # The publication date — the reference point of “without a deadline = actual for 30 days”
    # (src/domain/actuality.py); if it is absent — the count from first_seen_at.
    published_at: datetime | None = None
    # The subject type (goods|services|works) and the customer — the profile filters of 0046.
    contract_type: str | None = None
    customer_name: str | None = None
    title_normalized: str | None = None
    description_normalized: str | None = None
    status: str | None = None
    # The moment of the first appearance for us — the cutoff by subscription (SRC-0a, R4/R5).
    # None (only in unit tests) → stage A will not let the tender through to anybody.
    first_seen_at: datetime | None = None
    # The cross-source link (0047, src/dedup/linker.py): the cluster and the verdict
    # 'dup'/'probable'. Needed by routing for suppressing a duplicate behind the flag.
    canonical_tender_id: str | None = None
    canonical_confidence: str | None = None

    @property
    def haystack(self) -> str:
        """The normalized text for the text matching (stage B)."""
        return match_haystack(
            self.title_normalized,
            self.description_normalized,
            is_english_tender(self.language, self.source_key),
        )


@dataclass(frozen=True)
class Candidate:
    """The candidate profile after stage A (with the fields for stage B and the outbox)."""

    profile_id: str
    tenant_id: str
    keywords_include: dict | list | None
    keywords_exclude: list[str] | None
    ai_analysis_level: str | None
    delivery_channels: list[str] | None
    keyword_dict: bool = False
    # The customer of the tender in the profile’s “watch” list (0046): stage B lets through
    # a tender without a match of the include words; the stop words act.
    customer_followed: bool = False


# --------------------------------------------------------------------------- #
#  Stage A — the SQL pre-filter (§6.1)
# --------------------------------------------------------------------------- #

# The WHERE block — from DB_SCHEMA §6.1 (the country_code filter ADR-033, classifications
# through JSONB, prefix match `*` -> LIKE `%`). The SELECT list is extended with the fields
# needed by stage B and the write into the outbox (keywords_*, ai_analysis_level,
# delivery_channels), so as not to make a second round-trip.
#
# CAST on ALL bind parameters (asyncpg). The root of the class AmbiguousParameterError:
# for a NULL value of a parameter the driver cannot infer its type from the context and
# throws “could not determine data type of parameter $N”. This hits any
# bare parameter in `IS NULL`, `= ANY(...)`, a comparison, `LIKE`, `BETWEEN`.
# Therefore EACH :param in STAGE_A_SQL gets an explicit CAST to its type — not one
# stays bare:
#   :tender_country_code -> text   (the country of the profile’s regions, see below)
#   :source_key          -> text   (`= ANY(p.enabled_source_keys)`, JOIN sources,
#                                   key `p.source_opt_in ->> ...`)
#   :first_seen          -> timestamptz (the cutoff by subscription, SRC-0a)
#   :tender_regions      -> text[] (`p.regions && ...`)
#   :tender_system_code / :tender_classification_code -> text
#                                  (comparison with `inc->>'...'` and `LIKE replace(...)`)
#   :proc_type           -> text   (in `IS NULL` AND in `= ANY(...)` — both places)
#   :lang                -> text   (in `IS NULL` AND in `= ANY(...)` — both places)
#   :contract_type       -> text   (in `IS NULL` AND in `= ANY(...)` — both places)
#   :customer_name       -> text   (the customer key, sql_customer_key)
#   :amount_eur          -> numeric
#   :deadline            -> timestamptz
#   :tender_status       -> text   (the tender activity filter, see below)
#
# The root of the current failure (Chat 28, after #99): the NULL guard of #99 introduced
# `:proc_type IS NULL OR ... = ANY(...)` and `:lang IS NULL OR ...`. A bare
# parameter in `IS NULL` is not typed by the array context of `= ANY(...)` —
# asyncpg fell on `$6` (:proc_type=NULL for a jnportal tender without a procedure). That
# same latent risk was with `:lang` at NULL. Earlier, casts on these parameters were not
# placed (type “was inferred from `= ANY`”); with the arrival of the `IS NULL` branch this
# stopped being valid — we close the whole class by casting all parameters.
#
# The NULL semantics of the tender (Chat 28, the fix of “routing matches 0 profiles”, #99).
# jnportal v1 (OpenData XLSX) does not carry deadline/procedure/amount — for the tender they are
# NULL. In SQL’s three-valued logic `NULL >= 0`, `NULL = ANY(...)`, `NULL BETWEEN
# ...` give NULL (not TRUE) → the profile is falsely cut. Therefore each such
# limit is wrapped as `CAST(:<param> AS <type>) IS NULL OR (...)`: there is no value
# in the tender → the limit is not applicable, the profile passes. When the value is set —
# the condition is the previous one (the meaning of the filter does not change). The array filters
# (regions/classifications) are already protected by the branches `cardinality(...) = 0` /
# `jsonb_array_length(...) = 0`.
#
# the NULL-resistance of the profile’s array columns (the onboarding fix). The bot could create
# a profile with NULL in `enabled_source_keys`/`regions`/`procedure_types`/`languages`
# (the fields were not set at instantiation). On a NULL array `cardinality(NULL)`
# gives NULL (not 0) → the branch “the filter is not set” is not TRUE, and `:p = ANY(NULL)` is also
# NULL → the profile was falsely cut. We wrap each such column in
# `COALESCE(<col>, '{}')`: NULL is treated as an empty array (the filter is not set),
# for non-empty values the semantics are the previous ones. For `enabled_source_keys` the leniency
# “no filter” does NOT exist (the source is mandatory) — COALESCE only makes NULL a
# deterministic “no sources” (the profile does not match), without the fall of the 3-valued
# logic.
#
# The activity filter of the tender (the fix of “closed tenders are sent as new”,
# Q-29). The status is a property of the TENDER (not of the profile), therefore we filter by
# the bound parameter of the tender `:tender_status`, not by a column of the profile.
# Semantics: (1) if the status is known and finished (closed/awarded/cancelled) → do not
# send; (2) if the status is NULL → a second fuse by the deadline: send only
# if the deadline is not in the past (a NULL deadline is skipped). CAST on the parameters
# (asyncpg AmbiguousParameterError).
#
# ‼️ The comparison of the classification systems — through :mod:`src.domain.classifications`
# (see the module): the tender’s side writes `scheme`, the profile’s side `system_code`,
# and until 04.08 the predicate compared `system_code` from both sides → a profile with a
# CPV filter matched none of the tenders. Here the PROFILE’s side is canonicalised
# (SQL), the tender’s side comes already canonicalised as a bind parameter
# from :func:`primary_classification`.
#
# ‼️ THE CUTOFF BY THE MOMENT OF SUBSCRIPTION (SRC-0a, owner decisions 30.09.2026 R4/R5). One
# place closes all three paths into the outbox — NOTIFY INSERT, NOTIFY UPDATE and replay
# by checkpoint: all of them go through this stage.
#   * `s.baseline_at IS NOT NULL AND :first_seen > s.baseline_at` — the tenders
#     of the QUIET first pass of a new source (including a repeated enabling)
#     are not sent to anybody: otherwise the first pass would go out as “everything at once” to all
#     profiles with its key. (The branch `IS NOT NULL` itself is redundant for
#     the three-valued logic — the comparison with NULL is no longer TRUE — but it makes the intent
#     explicit.) The live jnportal has baseline_at = 1970-01-01 (0045) — it does not cut.
#   * `:first_seen >= COALESCE(opt_in[source], p.created_at)` — the profile
#     gets only what came AFTER it enabled the source (or was
#     created). Without this an UPDATE of an old open tender went to the profile
#     a profile created later (measurement 30.09: 2 of 3165 outbox rows over 30 days).
# ‼️ PREVIEW_WHERE_A (the form’s preview) deliberately does NOT get the time cutoff:
# the preview shows the HISTORY for the window (“≈ N over 30 days”), the delivery — only
# the future. The divergence is deliberate, do not mirror it.
#
# The country (SRC-0a): the cutoff `p.country_code = :tender_country_code` is no longer
# there — `profiles.country_code` is NOT NULL DEFAULT 'RS', and a foreign tender
# would not match anybody. The source gate is the choice of the key in enabled_source_keys.
# The profile’s regions apply only to the tenders of the SAME country: a profile with
# `RS-xx` does not cut a BA tender (which has regions empty or its own codes); the country
# of the region is the first two letters of the ISO 3166-2 code. The executor’s default, the owner did not decide.
# The mirror — PREVIEW_WHERE_A (there the tender’s country is the column tn.country_code).
#
# The subject type and the customers (0046). `contract_types` — like `procedure_types`
# (NULL at the tender → not applicable). `customers_exclude` — a tender of a customer from the
# list does not pass. `customers_include` — the “watch” mode: a tender of such a
# customer passes WITHOUT the topic — it replaces the topic gate, the include classifications and
# the include words of stage B (the flag `customer_followed` goes into Candidate). The explicit
# NEGATIVES and the narrowings (stop words, cls-exclude, “exclude”, regions, type,
# sums, deadlines) act on it too: “watch” removes only the requirement
# to match by topic. A customer in both lists → “exclude” is stronger.
STAGE_A_SQL = text(
    _fill(
        """
    SELECT p.id,
           p.tenant_id,
           p.keywords_include,
           p.keywords_exclude,
           p.ai_analysis_level,
           p.delivery_channels,
           p.keyword_dict,
           f.customer_followed
    FROM profiles p
    JOIN tenants t ON t.id = p.tenant_id
    JOIN sources s ON s.source_key = CAST(:source_key AS text)
    CROSS JOIN LATERAL (
      SELECT @followed@ AS customer_followed
    ) f
    WHERE p.is_active = true
      AND t.status IN ('trial', 'active')
      AND (t.access_until IS NULL OR t.access_until > now())
      AND CAST(:source_key AS text) = ANY(COALESCE(p.enabled_source_keys, '{}'))
      AND s.baseline_at IS NOT NULL
      AND CAST(:first_seen AS timestamptz) > s.baseline_at
      AND CAST(:first_seen AS timestamptz) >= COALESCE(
            CAST(p.source_opt_in ->> CAST(:source_key AS text) AS timestamptz),
            p.created_at)
      -- Профиль обязан ЛИБО объявить режим «весь поток», ЛИБО задать тему.
      -- До 0043 «показывать всё» получалось само собой из пустоты (здесь все
      -- ветки «фильтр не задан» истинны, а stage_b без include-групп
      -- возвращает True) — и клиент, бросивший форму на полпути, получал
      -- зеркало портала: 470 сообщений за 7 дней живому подписчику с профилем
      -- «CLIENT_PROFILE», в котором не было ни слова, ни кода, ни региона.
      -- Регионы/суммы/процедуры сюда НЕ входят: они сужают выборку, но темы
      -- не задают, и профиль остаётся тем же зеркалом.
      AND (
        p.match_all = true
        OR jsonb_array_length(COALESCE(p.keywords_include, '[]'::jsonb)) > 0
        OR jsonb_array_length(
             COALESCE(p.classifications->'include', '[]'::jsonb)) > 0
        OR f.customer_followed
      )
      AND (cardinality(COALESCE(p.regions, '{}')) = 0
           OR COALESCE(p.regions, '{}') && CAST(:tender_regions AS text[])
           OR NOT EXISTS (
             SELECT 1 FROM unnest(COALESCE(p.regions, '{}')) r
             WHERE left(r, 2) = CAST(:tender_country_code AS text)))
      AND (
        jsonb_array_length(COALESCE(p.classifications->'include', '[]'::jsonb)) = 0
        OR f.customer_followed
        OR EXISTS (
          SELECT 1 FROM jsonb_array_elements(p.classifications->'include') inc
          WHERE CAST(:tender_system_code AS text) = @profile_system_inc@
            AND CAST(:tender_classification_code AS text)
                LIKE replace(inc->>'code', '*', '%')
        )
      )
      AND NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(
                       COALESCE(p.classifications->'exclude', '[]'::jsonb)) exc
        WHERE CAST(:tender_system_code AS text) = @profile_system_exc@
          AND CAST(:tender_classification_code AS text)
              LIKE replace(exc->>'code', '*', '%')
      )
      AND (CAST(:proc_type AS text) IS NULL
           OR cardinality(COALESCE(p.procedure_types, '{}')) = 0
           OR CAST(:proc_type AS text) = ANY(COALESCE(p.procedure_types, '{}')))
      AND (CAST(:contract_type AS text) IS NULL
           OR cardinality(COALESCE(p.contract_types, '{}')) = 0
           OR CAST(:contract_type AS text) = ANY(COALESCE(p.contract_types, '{}')))
      AND NOT @excluded@
      AND (CAST(:lang AS text) IS NULL
           OR cardinality(COALESCE(p.languages, '{}')) = 0
           OR CAST(:lang AS text) = ANY(COALESCE(p.languages, '{}')))
      AND (
          (CAST(:amount_eur AS numeric) IS NULL
           AND (COALESCE(p.include_no_amount, false) = true
                OR (p.min_amount_eur IS NULL AND p.max_amount_eur IS NULL)))
          OR CAST(:amount_eur AS numeric) BETWEEN COALESCE(p.min_amount_eur, 0)
                              AND COALESCE(p.max_amount_eur, 1e18)
      )
      AND (
          CAST(:deadline AS timestamptz) IS NULL
          OR EXTRACT(EPOCH FROM (CAST(:deadline AS timestamptz) - NOW())) / 86400.0
             >= COALESCE(p.min_days_until_deadline, 0)
      )
      AND (CAST(:tender_status AS text) IS NULL
           OR CAST(:tender_status AS text) NOT IN ('closed', 'awarded', 'cancelled'))
      -- Актуальность по сроку. Срока нет → актуален 30 дней от публикации
      -- (owner 06.10.2026, src/domain/actuality.py); срок есть — как раньше.
      AND (
          @no_deadline_fresh@
          OR (CAST(:deadline AS timestamptz) IS NOT NULL
              AND (CAST(:tender_status AS text) IS NOT NULL
                   OR CAST(:deadline AS timestamptz) > NOW()))
      )
    """,
        no_deadline_fresh=sql_no_deadline_fresh(
            deadline="CAST(:deadline AS timestamptz)",
            published="CAST(:published AS timestamptz)",
            first_seen="CAST(:first_seen AS timestamptz)",
        ),
        profile_system_inc=sql_profile_system("inc"),
        profile_system_exc=sql_profile_system("exc"),
        followed=sql_customer_in("p.customers_include", "CAST(:customer_name AS text)"),
        excluded=sql_customer_in("p.customers_exclude", "CAST(:customer_name AS text)"),
    )
)


# --------------------------------------------------------------------------- #
#  The predicates of stage A — the tender-centric perspective (for the preview)
# --------------------------------------------------------------------------- #
# The WHERE fragment for PREVIEW_SQL: the “reverse” side of STAGE_A_SQL.
# STAGE_A_SQL finds the profiles that match a tender; this fragment filters
# the tenders by the profile’s configuration (passed as bind parameters).
#
# Parameters: :sources (text[]), :regions (text[]),
#            :cls_include (jsonb), :cls_exclude (jsonb),
#            :min_days (int), :include_no_amount (boolean),
#            :min_amount (numeric), :max_amount (numeric),
#            :contract_types / :customers_include / :customers_exclude (text[]).
#
# Imported by preview_service — the single place of truth per predicate.
# Both queries (routing + preview) live in one module; a divergence is visible at once.
#
# The comparison of the classification systems — by canonicalising BOTH sides
# (:mod:`src.domain.classifications`): here, unlike STAGE_A_SQL, the tender’s side
# also comes as a column, not as a bind parameter.
#
# ‼️ The cutoffs by the moment of subscription (baseline_at / source_opt_in, SRC-0a) here
# are deliberately ABSENT: the preview shows the history over the window, STAGE_A_SQL — the delivery
# only the future. The region — a mirror of STAGE_A_SQL (the profile’s regions act
# only on the tenders of their own country).
#
# The subject type and the customers (0046) — a mirror of STAGE_A_SQL: “watch” removes
# the requirement of the include classifications here, and the include words — in the stage B of the preview
# (the column PREVIEW_CUSTOMER_FOLLOWED in the SELECT, see preview_service._passes).
PREVIEW_WHERE_A = _fill(
    """(
    cardinality(COALESCE(CAST(:sources AS text[]), '{}')) = 0
    OR tn.source_key = ANY(CAST(:sources AS text[]))
)
AND (
    cardinality(COALESCE(CAST(:regions AS text[]), '{}')) = 0
    OR COALESCE(tn.regions, '{}') && CAST(:regions AS text[])
    OR NOT EXISTS (
        SELECT 1 FROM unnest(CAST(:regions AS text[])) r
        WHERE left(r, 2) = CAST(tn.country_code AS text)
    )
)
AND (
    jsonb_array_length(COALESCE(CAST(:cls_include AS jsonb), '[]'::jsonb)) = 0
    OR @followed_tn@
    OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements(COALESCE(tn.classifications, '[]'::jsonb)) tc,
             jsonb_array_elements(COALESCE(CAST(:cls_include AS jsonb), '[]'::jsonb)) inc
        WHERE @tender_system_tc@ = @profile_system_inc@
          AND tc->>'code' LIKE replace(inc->>'code', '*', '%')
    )
)
AND NOT EXISTS (
    SELECT 1
    FROM jsonb_array_elements(COALESCE(tn.classifications, '[]'::jsonb)) tc,
         jsonb_array_elements(COALESCE(CAST(:cls_exclude AS jsonb), '[]'::jsonb)) exc
    WHERE @tender_system_tc@ = @profile_system_exc@
      AND tc->>'code' LIKE replace(exc->>'code', '*', '%')
)
AND (
    tn.contract_type IS NULL
    OR cardinality(COALESCE(CAST(:contract_types AS text[]), '{}')) = 0
    OR tn.contract_type = ANY(CAST(:contract_types AS text[]))
)
AND NOT @excluded_tn@
AND (
    tn.deadline_at IS NULL
    OR EXTRACT(EPOCH FROM (tn.deadline_at - NOW())) / 86400.0
       >= COALESCE(CAST(:min_days AS int), 0)
)
AND (tn.status IS NULL OR tn.status NOT IN ('closed', 'awarded', 'cancelled'))
AND (
    @no_deadline_fresh_tn@
    OR (tn.deadline_at IS NOT NULL
        AND (tn.status IS NOT NULL OR tn.deadline_at > NOW()))
)
AND (
    (tn.amount_eur IS NULL AND (
        CAST(:include_no_amount AS boolean) = true
        OR (CAST(:min_amount AS numeric) IS NULL AND CAST(:max_amount AS numeric) IS NULL)
    ))
    OR tn.amount_eur BETWEEN COALESCE(CAST(:min_amount AS numeric), 0)
                      AND COALESCE(CAST(:max_amount AS numeric), 1e18)
)""",
    tender_system_tc=sql_tender_system("tc"),
    profile_system_inc=sql_profile_system("inc"),
    profile_system_exc=sql_profile_system("exc"),
    no_deadline_fresh_tn=sql_no_deadline_fresh_tn("tn"),
    followed_tn=sql_customer_in(
        "CAST(:customers_include AS text[])", "tn.customer_name"
    ),
    excluded_tn=sql_customer_in(
        "CAST(:customers_exclude AS text[])", "tn.customer_name"
    ),
)

#: The column for the preview’s SELECT: the customer of the tender ``tn`` is in the “watch” list.
#: The stage B of the preview removes the include-words requirement by it — like
#: :func:`stage_b_filter` by ``Candidate.customer_followed``.
PREVIEW_CUSTOMER_FOLLOWED = sql_customer_in(
    "CAST(:customers_include AS text[])", "tn.customer_name"
)


def primary_classification(
    classifications: Sequence[dict[str, Any]] | None,
) -> tuple[str | None, str | None]:
    """The primary classification of the tender → ``(system_code, code)`` in the CANON.

    The query §6.1 is parameterised by one pair ``system_code``/``code``, so
    we take the first record with a code. The canon and the reading of both ingest contracts
    (``system_code`` ↔ ``scheme``) — in :mod:`src.domain.classifications`: without
    it a profile with a CPV filter matched none of the tenders. The matching by
    several classifications of the tender (a union over each) is a documented
    extension (PR body).
    """
    return _primary_canonical(classifications)


async def stage_a_candidates(
    session: AsyncSession, tender: TenderView, *, sql: Any = None
) -> list[Candidate]:
    """Stage A: the SQL pre-filter of the candidate profiles (§6.1).

    Returns the profiles that passed all the array/numeric filters and the cutoff by the
    moment of subscription. The text match (keywords) — separately, stage B.
    ``sql`` — a substitution of the query (only the evaluation tools, scripts/source_dryrun.py);
    by default ``STAGE_A_SQL``.
    """
    system_code, classification_code = primary_classification(tender.classifications)
    params = {
        "tender_country_code": tender.country_code,
        "source_key": tender.source_key,
        "tender_regions": list(tender.regions or []),
        "tender_system_code": system_code,
        "tender_classification_code": classification_code,
        "proc_type": tender.procedure_type,
        "contract_type": tender.contract_type,
        "customer_name": tender.customer_name,
        "lang": tender.language,
        "amount_eur": tender.amount_eur,
        "deadline": tender.deadline_at,
        "published": tender.published_at,
        "tender_status": tender.status,
        "first_seen": tender.first_seen_at,
    }
    result = await session.execute(STAGE_A_SQL if sql is None else sql, params)
    return [
        Candidate(
            profile_id=str(row.id),
            tenant_id=str(row.tenant_id),
            keywords_include=row.keywords_include,
            keywords_exclude=row.keywords_exclude,
            ai_analysis_level=row.ai_analysis_level,
            delivery_channels=row.delivery_channels,
            keyword_dict=bool(row.keyword_dict),
            customer_followed=bool(getattr(row, "customer_followed", False)),
        )
        for row in result
    ]


# --------------------------------------------------------------------------- #
#  Stage B — the text comparison (§6.2), pure Python
# --------------------------------------------------------------------------- #


def _include_groups(keywords_include: Any) -> list[dict[str, Any]]:
    """Normalises ``keywords_include`` to a list of groups ``{"all_of": [...]}``.

    In the DB it is stored as a JSONB array of groups (DB_SCHEMA §2.6). NULL/empty → [].
    """
    if not keywords_include:
        return []
    if isinstance(keywords_include, list):
        return keywords_include
    return []


def _iter_classifications(classifications: Any) -> list[dict[str, Any]]:
    """Normalises the input to a list of dict records: None→[]; dict→[dict]; list→dicts."""
    if classifications is None:
        return []
    if isinstance(classifications, dict):
        return [classifications]
    if isinstance(classifications, list):
        return [c for c in classifications if isinstance(c, dict)]
    return []


@lru_cache(maxsize=4096)
def _fold_term(term: str | None) -> str:
    """:func:`fold` for a TERM (a word of the template, its word form) with a cache.

    The terms are the same on each row of the haystack, and the folding was recomputed each time
    (measurement 02.10: 6.5 of 7.9 s — fold). The cache is only for the terms:
    the haystack is unique on each row and is folded without a cache. The folding is one —
    :func:`src.domain.text_norm.fold`; here only the memoisation of its result.
    """
    return fold(term)


def _term_in(term: str | None, folded_hay: str) -> bool:
    """A folded term — a non-empty substring of the folded haystack.

    Both sides via :func:`src.domain.text_norm.fold`: the haystack is folded
    once by the caller, the term — here (via the cache :func:`_fold_term`). A term that is empty
    after the folding (e.g. one space) matches nothing.
    """
    folded = _fold_term(term)
    return bool(folded) and folded in folded_hay


@lru_cache(maxsize=4096)
def _exclude_forms_pattern(forms: tuple[str, ...]) -> re.Pattern[str] | None:
    """The regexp of the stop word forms: a literal occurrence of any (as :func:`_term_in`).

    The same technique as for include (:func:`_include_forms_pattern`): ``search`` over the
    union = OR over the forms, without a Python loop on each row.
    """
    parts = [re.escape(folded) for folded in (_fold_term(f) for f in forms) if folded]
    return re.compile("|".join(parts)) if parts else None


def _any_form_in(forms: Sequence[str], folded_hay: str) -> bool:
    """At least one word form (after the folding) — a substring of the folded haystack."""
    pattern = _exclude_forms_pattern(tuple(forms))
    return pattern is not None and pattern.search(folded_hay) is not None


#: The stems that in Serbian stand INSIDE a word (a prefix/compound), not only
#: at the beginning: for them the stem is searched WITHOUT a left boundary. Paired with
#: :data:`src.domain.text_norm.STEM_AMBIGUOUS` (there — “the stem = the prefix of other
#: words”, here — “the stem = the middle of ITS OWN words”). A measurement (prod, 90 days,
#: 10 300 RS tenders, 01.10.2026), all the carriers cut off by the word boundary
#: were reviewed: hran — 48 occurrences, all “ishrana/ishranu/ishrane” (namirnice
#: za ishranu; hrana 158 without a boundary → 124 with a boundary); snabdevanj — 10, all
#: “vodosnabdevanje/-a” (water supply). NOT a general rule for prefixes:
#: “po+vrat”, “pred+skol”, “da+lekovod” — the same prefixes give false
#: matches. Extend only by a measurement with the carriers reviewed by eye.
#: The owner’s decision of 01.10: “include all” — ``medicinsk`` (“telemedicinskog”),
#: ``montaz`` («elektromontaznih»), ``sanacij`` («resanacija»), ``zgrad``
#: (“izgradnja”). The price is known and accepted: ``montaz`` catches also “demontaza”,
#: ``zgrad`` — any “izgradnja” (not only “zgrada”).
STEM_INNER: frozenset[str] = frozenset(
    {"hran", "snabdevanj", "medicinsk", "montaz", "sanacij", "zgrad"}
)


@lru_cache(maxsize=4096)
def _stem_pattern(term: str) -> re.Pattern[str] | None:
    """The regexp of an include term by the STEMS of words (:func:`src.domain.text_norm.stem_sr`).

    One word → its stem as a substring. A phrase → the stems in the same order through
    spaces; after a truncated word any ending is allowed (``\\w*``),
    after a non-truncated one — not: “kancelarijski materijal” catches “kancelarijskog
    materijala”, but not “materijal kancelarijski”. ``None`` — an empty term or
    no word is truncated (then the literal search is enough).
    """
    parts: list[str] = []
    stemmed = False
    inner = False
    for word in fold(term).split():
        stem = stem_sr(word)
        inner = inner or (not parts and stem in STEM_INNER)
        stemmed = stemmed or stem != word
        parts.append(re.escape(stem) + (r"\w*" if stem != word else ""))
    if not parts or not stemmed:
        # No word is truncated — the regexp would repeat the literal search
        # which :func:`include_term_span` has already done.
        return None
    # ``(?<!\w)`` — the stem from the START of a word: without a boundary “vrat” caught “povrat”.
    # The exception — the first word with a stem from :data:`STEM_INNER` (“ishrana”).
    return re.compile(("" if inner else r"(?<!\w)") + r"\s+".join(parts))


def include_term_span(term: str | None, folded_hay: str) -> tuple[int, int] | None:
    """Where the include term was found in the folded haystack: literally OR by the stems.

    A Serb writes the word in full (“gradjevina”), and without the stems lost most
    of the tenders (prod, 90 days: 0 against 172 by “gradjevin”). The literal occurrence
    is checked first — the result is never narrower than before. ‼️ Only include:
    the stop words (exclude) remain literal — the client does not see a false exclusion.

    Returns ``(start, end)`` in ``folded_hay`` or ``None``. The position is needed by
    the highlighting of examples in the template form — it calls THIS function, not its own copy of
    the rule, so exactly what the tender was found by is highlighted.
    """
    folded = _fold_term(term)
    if not folded:
        return None
    pos = folded_hay.find(folded)
    if pos >= 0:
        return pos, pos + len(folded)
    pattern = _stem_pattern(term or "")
    found = pattern.search(folded_hay) if pattern is not None else None
    return found.span() if found is not None else None


def _include_term_in(term: str | None, folded_hay: str) -> bool:
    """The include term is found in the folded haystack (see :func:`include_term_span`)."""
    return include_term_span(term, folded_hay) is not None


@lru_cache(maxsize=4096)
def _include_forms_pattern(forms: tuple[str, ...]) -> re.Pattern[str] | None:
    """One regexp for ALL the word forms of the term: “literally OR by the stems” of each.

    The existence of a match of the union equals OR over the forms, so
    ``search`` over it is equivalent to ``any(include_term_span(f) …)`` — but without a
    Python loop over 10–50 forms on each row (measurement 02.10: b0d4 — 1.4M
    calls of ``include_term_span``). The forms that are empty after the folding do not match.
    """
    parts: list[str] = []
    for form in forms:
        folded = _fold_term(form)
        if not folded:
            continue
        parts.append(re.escape(folded))
        stem = _stem_pattern(form or "")
        if stem is not None:
            parts.append(stem.pattern)
    return re.compile("|".join(parts)) if parts else None


def _any_include_form_in(forms: Sequence[str], folded_hay: str) -> bool:
    """At least one include word form is found (literally or by the stems)."""
    pattern = _include_forms_pattern(tuple(forms))
    return pattern is not None and pattern.search(folded_hay) is not None


#: Sources whose texts are ALWAYS English (``tenders_normalized.language`` for them
#: is also ``en``; the key is a safeguard for an empty language).
ENGLISH_SOURCES: frozenset[str] = frozenset({"worldbank", "ebrd"})


def is_english_tender(language: str | None, source_key: str | None) -> bool:
    """English-language tender: language ``en*`` OR a source from :data:`ENGLISH_SOURCES`."""
    return (language or "").lower().startswith("en") or source_key in ENGLISH_SOURCES


def match_haystack(
    title_normalized: str | None, description_normalized: str | None, english: bool
) -> str:
    """Stage B text: A SINGLE predicate for routing, preview, delivery and reports.

    An English tender — ONLY the title (owner decision 18.10, option A): the description
    of WB/TED is a multi-kilobyte template ("project", "development" in every REOI/SPN);
    by it the profile words gave 148 pairs, after dropping the description — 59 (measurement
    docs/reports/PAIRS_kw-sr-en.md: by title 43 correct, 10 false, 6 disputed).
    A Serbian/Russian tender — title + description, as before.
    """
    title = title_normalized or ""
    if english:
        return title
    return f"{title} {description_normalized or ''}"


def _english_forms(term: str, keyword_dict_enabled: bool) -> tuple[str, ...]:
    """Term forms for English text: the term itself (+ en-forms under the flag).

    Serbian dictionary word forms are NOT included here: in English text they give only
    noise (WB measurement 17.10: the Cyrillic stem U+043F U+0440 U+043E U+0433 U+0440 U+0430 U+043C → "program", the "sanacij" series → "rehabilitation"
    is already covered by en-forms).
    """
    if not keyword_dict_enabled:
        return (term,)
    from src.workers.keyword_expand import expand_term_en

    return (term, *expand_term_en(term))


#: An en-form shorter than this — a word boundary on BOTH sides: ``car`` must not find
#: ``cardiology``, ``bus`` — ``business`` (kw-sr-en measurement: left-boundary for short
#: forms catches the start of another word). Longer — left side only: ``construct`` → ``construction``.
_SHORT_EN_FORM = 4


def _en_form_regex(form: str) -> str:
    """One folded en-form → part of the regex. Short — with a plural and a right boundary."""
    if len(form) <= _SHORT_EN_FORM:
        return re.escape(form) + r"(?:es|s)?(?!\w)"
    return re.escape(form)


@lru_cache(maxsize=4096)
def _word_start_pattern(forms: tuple[str, ...]) -> re.Pattern[str] | None:
    """Regex "any form from the START of a word" (``(?<!\\w)``) over folded forms.

    English text: ``put`` must not be found inside ``input``/``output``,
    ``program`` — inside ``subprogram``; any ending after the stem is allowed
    (``construct`` → ``construction``). Short forms (≤4) — also with a right boundary,
    plural (``cars``, ``buses``) is allowed. NOT used for Serbian texts.
    """
    parts = [_en_form_regex(f) for f in (_fold_term(x) for x in forms) if f]
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + ")") if parts else None


def _word_start_hit(forms: Sequence[str], folded_hay: str) -> bool:
    pattern = _word_start_pattern(tuple(forms))
    return pattern is not None and pattern.search(folded_hay) is not None


def word_start_span(form: str, folded_hay: str) -> tuple[int, int] | None:
    """First occurrence of a form from the START of a word in the folded text (en-form highlighting)."""
    pattern = _word_start_pattern((form,))
    found = pattern.search(folded_hay) if pattern else None
    return (found.start(), found.end()) if found else None


def _english_match(
    keywords_include: Any,
    keywords_exclude: Sequence[str] | None,
    hay: str,
    keyword_dict_enabled: bool,
) -> bool:
    """Stage B for English text (word boundary + en-forms)."""
    for kw in keywords_exclude or []:
        if kw and _word_start_hit(_english_forms(kw, keyword_dict_enabled), hay):
            return False
    groups = _include_groups(keywords_include)
    if not groups:
        return True
    for group in groups:
        terms = (group or {}).get("all_of") or []
        if terms and all(
            _word_start_hit(_english_forms(t, keyword_dict_enabled), hay) for t in terms
        ):
            return True
    return False


def keywords_match(
    keywords_include: Any,
    keywords_exclude: Sequence[str] | None,
    haystack: str,
    keyword_dict_enabled: bool = False,
    english: bool = False,
) -> bool:
    """Stage B: whether the tender matches the profile's keywords (§6.2).

    Semantics (ARCHITECTURE §12.2):

    - **include** — a list of groups ``{"all_of": [...]}``. The tender fits if
      *at least one* group matched *entirely* (all words of the group are substrings of
      ``haystack``). Empty include — no filter, counts as matched.
    - **exclude** — stop words: *any* match excludes the tender.

    Comparison is by substring over the SINGLE folding :func:`src.domain.text_norm.fold`
    (the same one that writes ``title_normalized`` on ingest): both ``haystack`` and each
    word/word form are folded to one form (cyrl→latn→lower→unaccent).
    ‼️ Without folding the search side, a Serbian-Cyrillic dictionary would never
    match a Latin haystack — a real defect. ``haystack`` is already normalized
    on ingest; re-folding is idempotent (needed for comparison in one
    alphabet and for callers passing raw text).

    ``keyword_dict_enabled=True`` — expands each term through the ru/en→sr dictionary
    (keyword_expand) before comparison: exclude — any form excludes;
    include all_of — each term must yield at least one form in the
    text (AND of positions, OR of forms within a position).

    ``english=True`` (English-language tender, :func:`is_english_tender`) — a different
    mode: forms (+ English ones from the dictionary under the flag) are searched from the START of a word, not
    as a substring. The Serbian path (``english=False``) is unchanged.
    """
    hay = fold(haystack)

    if english:
        return _english_match(
            keywords_include, keywords_exclude, hay, keyword_dict_enabled
        )

    if keyword_dict_enabled:
        from src.workers.keyword_expand import expand_term_forms

        for kw in keywords_exclude or []:
            if kw and _any_form_in(expand_term_forms(kw), hay):
                return False

        groups = _include_groups(keywords_include)
        if not groups:
            return True

        for group in groups:
            terms = (group or {}).get("all_of") or []
            if terms and all(
                _any_include_form_in(expand_term_forms(t), hay) for t in terms
            ):
                return True
        return False

    for kw in keywords_exclude or []:
        if _term_in(kw, hay):
            return False

    groups = _include_groups(keywords_include)
    if not groups:
        return True

    for group in groups:
        terms = (group or {}).get("all_of") or []
        if terms and all(_include_term_in(term, hay) for term in terms):
            return True
    return False


@dataclass(frozen=True)
class MatchReason:
    """The reason of the tender's match with the profile (computed on the fly when debug_match=true).

    Attributes:
        matched_keywords: the words from the first fully matched group of keywords_include.
            Empty if there is no include filter (the match is always) or the reason could not
            be found.
        matched_cpv: the codes (patterns) from profile.classifications->'include' that gave a
            prefix match to one of the tender's classifications. Empty if
            classifications is not set or there is no match.
        matched_tender_cpv: the codes of the tender ITSELF that gave the match (without duplicates) — for
            display to the client; ``matched_cpv`` — the template patterns (debugging).
    """

    matched_keywords: list[str]
    matched_cpv: list[str]
    # The codes of the tender ITSELF that fell under matched_cpv (without duplicates, in the order of the
    # tender). We show the client these (45421000), not the template (45*).
    matched_tender_cpv: list[str] = field(default_factory=list)


def match_reason(
    keywords_include: Any,
    keywords_exclude: Sequence[str] | None,
    haystack: str,
    tender_classifications: Any = None,
    profile_classifications: Any = None,
    keyword_dict_enabled: bool = False,
    english: bool = False,
) -> MatchReason:
    """Computes the reason of the match (stage B + CPV) for the debug label of the card.

    Replicates the logic of :func:`keywords_match` (does not call it again):
    returns the words from the first matched include group. For CPV: checks
    each ``include`` pattern from ``profile_classifications`` against the codes from
    ``tender_classifications`` through fnmatch (the equivalent of SQL LIKE after
    ``replace('*','%')``).

    ``keyword_dict_enabled=True`` — the same mode as in :func:`keywords_match`:
    each term is expanded via the dictionary, and the client's word gets into the reason
    if ANY of its forms matched. Without the flag the “Matched” row lost the words
    found through the dictionary (REWORK why-matched).

    Does not change the public API of :func:`keywords_match` — a separate pure function.
    On an exception from the exclude words it returns an empty reason (the tender should
    not have matched, but the call is possible for diagnostics).
    """
    # The folding of both sides — the same as in :func:`keywords_match` (one predicate
    # per meaning). matched_kw returns the ORIGINAL of the words (for display), the comparison — by the
    # folded form.
    hay = fold(haystack)

    # The same predicates as in keywords_match (the branch by the flag, then one loop over the
    # groups): a divergence here = the “Matched” row lies about the match.
    if english:
        for kw in keywords_exclude or []:
            if kw and _word_start_hit(_english_forms(kw, keyword_dict_enabled), hay):
                return MatchReason(matched_keywords=[], matched_cpv=[])

        def term_hit(term: str) -> bool:
            return _word_start_hit(_english_forms(term, keyword_dict_enabled), hay)

    elif keyword_dict_enabled:
        from src.workers.keyword_expand import expand_term_forms

        for kw in keywords_exclude or []:
            if kw and _any_form_in(expand_term_forms(kw), hay):
                return MatchReason(matched_keywords=[], matched_cpv=[])

        def term_hit(term: str) -> bool:
            return _any_include_form_in(expand_term_forms(term), hay)

    else:
        for kw in keywords_exclude or []:
            if _term_in(kw, hay):
                return MatchReason(matched_keywords=[], matched_cpv=[])

        def term_hit(term: str) -> bool:
            return _include_term_in(term, hay)

    matched_kw: list[str] = []
    for group in _include_groups(keywords_include):
        terms = (group or {}).get("all_of") or []
        if terms and all(term_hit(term) for term in terms):
            matched_kw = list(terms)
            break

    matched_cpv: list[str] = []
    matched_tender_cpv: list[str] = []
    if profile_classifications and tender_classifications:
        inc_list: list[dict] = []
        if isinstance(profile_classifications, dict):
            inc_list = profile_classifications.get("include") or []

        tender_codes = [
            str(c.get("code", "")).strip()
            for c in _iter_classifications(tender_classifications)
            if c.get("code")
        ]

        for inc in inc_list or []:
            pattern = str((inc or {}).get("code", "")).strip()
            if not pattern:
                continue
            hit = False
            for tc_code in tender_codes:
                if fnmatch.fnmatch(tc_code, pattern):
                    if not hit:
                        matched_cpv.append(pattern)
                        hit = True
                    if tc_code not in matched_tender_cpv:
                        matched_tender_cpv.append(tc_code)

    return MatchReason(
        matched_keywords=matched_kw,
        matched_cpv=matched_cpv,
        matched_tender_cpv=matched_tender_cpv,
    )


def stage_b_filter(
    candidates: Sequence[Candidate], tender: TenderView
) -> list[Candidate]:
    """Applies stage B to the candidates of stage A, keeping the matched ones."""
    hay = tender.haystack
    english = is_english_tender(tender.language, tender.source_key)
    return [
        c
        for c in candidates
        if keywords_match(
            # “Watch” (0046): the include words are not required, the stop words — yes.
            None if c.customer_followed else c.keywords_include,
            c.keywords_exclude,
            hay,
            keyword_dict_enabled=c.keyword_dict,
            english=english,
        )
    ]


# --------------------------------------------------------------------------- #
#  Stage C — soft-match via embedding (§6.3) — a STUB (Sprint-16)
# --------------------------------------------------------------------------- #


async def stage_c_soft_match(
    session: AsyncSession, tender: TenderView
) -> list[Candidate]:
    """Stage C: soft-match via embedding (§6.3).

    **A stub** by the Sprint-16 task: the pgvector match of ``ai_extra_context_embedding``
    is postponed (the embeddings of tenders are needed — the AI pipeline of Sprint-18). For now always
    an empty list; the implementation — a separate sprint.
    """
    return []


# --------------------------------------------------------------------------- #
#  The live channels of the tenant — the predicate “there is someone to deliver to”
# --------------------------------------------------------------------------- #
# The fix of “delivery to a dead channel”: `profiles.delivery_channels` is the SETTING of the
# profile (“where to send”), not a fact of a live addressee’s presence. A check against
# `user_channels` on the tender→outbox path was nowhere, therefore a profile with a
# single NON-active channel stamped knowingly dead records: routing
# wrote them into the outbox, delivery honestly failed with “channel inactive or no target”.
# A channel leaves active also without the user’s participation (a manual normalization of the bindings,
# docs/chat74_finalization.md §5) — the profile at the same time stays active, therefore
# “the channel is set up” and “there is where to send” must be checked separately.
#
# The predicate mirrors `_LOAD_JOBS_SQL` from `src/workers/delivery.py`: the same selection
# of the user_channels row (`DISTINCT ON` + `ORDER BY (status='active') DESC,
# linked_at DESC NULLS LAST`), the same criterion of sending (`not job.target or
# channel_status not in (None, 'active')` → fail). `external_id` is NOT unique
# (CLAUDE.md), there may be many rows per channel — we take the SAME one that delivery
# will take, otherwise the predicate would diverge from reality: the channel is recognized as live by
# one row, while the delivery goes by another (and vice versa).
#
# The contour: routing works under the superuser `DATABASE_URL` (deploy/
# docker-compose.yml, service routing) and goes across all tenants — RLS does not
# restrict it, as in STAGE_A_SQL by `profiles`. Therefore the tenant scope is set by
# an explicit `u.tenant_id = :tenant_id` (not a GUC); `set_tenant`/SECURITY DEFINER are not
# needed. A CAST on the parameter — asyncpg (AmbiguousParameterError).
LIVE_CHANNELS_SQL = text("""
    SELECT DISTINCT ON (uc.channel)
           uc.channel     AS channel,
           uc.status      AS status,
           uc.external_id AS external_id,
           -- Язык уведомлений в этот канал: язык КАНАЛА (0038), иначе язык
           -- кабинета. NULL в uc.locale = «наследовать users.locale», а не
           -- «язык неизвестен» — поэтому COALESCE (зеркало
           -- delivery._LOAD_JOBS_SQL). На предикат «живой» НЕ влияет.
           COALESCE(uc.locale, u.locale) AS locale
    FROM user_channels uc
    JOIN users u ON u.id = uc.user_id
    WHERE u.tenant_id = CAST(:tenant_id AS uuid)
    ORDER BY uc.channel,
             (uc.status = 'active') DESC,
             uc.linked_at DESC NULLS LAST
    """)


@dataclass(frozen=True)
class ChannelState:
    """The state of the tenant’s channel — the ``user_channels`` row that delivery will take."""

    channel: str
    status: str | None
    external_id: str | None
    # The language of the notifications into this channel (COALESCE(uc.locale, u.locale)). The default None
    # — “the language is not set anywhere, the catalog fallback decides”: the field was added AFTER the
    # other consumers of the predicate (the cabinet, channel_dead_notify), and they
    # construct ChannelState without it. It does not affect :attr:`is_live`.
    locale: str | None = None

    @property
    def is_live(self) -> bool:
        """Delivery will get there: the channel is active AND the addressee is non-empty (see :data:`LIVE_CHANNELS_SQL`)."""
        return self.status == "active" and bool((self.external_id or "").strip())


async def channel_states(
    session: AsyncSession, tenant_id: str
) -> dict[str, ChannelState]:
    """The channels the tenant has EVER set up → their current state.

    The key is there ⇔ the tenant has a ``user_channels`` row for this channel; the value
    answers whether it is live now or not. One can distinguish “the channel dropped” from “the channel
    was never connected” only this way: :func:`live_channels` collapses both
    cases into “not in the set”.
    """
    result = await session.execute(LIVE_CHANNELS_SQL, {"tenant_id": tenant_id})
    return {
        row.channel: ChannelState(
            channel=row.channel,
            status=row.status,
            external_id=row.external_id,
            # getattr: the fake rows in the consumers' tests are set up without the
            # locale column — the extension of the selection does not break them.
            locale=getattr(row, "locale", None),
        )
        for row in result
    }


async def live_channels(session: AsyncSession, tenant_id: str) -> set[str]:
    """The tenant's channels where the delivery will really get (see :data:`LIVE_CHANNELS_SQL`).

    Live = the freshest ``active`` row of ``user_channels`` of this channel with a
    non-empty ``external_id``. Exactly it is taken by the delivery, and exactly these two
    conditions it checks before sending — the predicate is exactly the inverse of its failure
    “channel inactive or no target”. A channel the tenant does not have at all
    is not considered live.
    """
    states = await channel_states(session, tenant_id)
    return {name for name, state in states.items() if state.is_live}


class LiveChannelsCache:
    """The cache of :func:`live_channels` per tenant for ONE routing pass.

    The candidates of one tenant go in a batch (tender × profiles) — without the cache this is
    +1 SELECT per pair. The RESULT is cached, the predicate remains one
    (:data:`LIVE_CHANNELS_SQL`, the mirror of the delivery selection).

    ‼️ It lives exactly one pass and is created anew in :meth:`route_tender`: a channel
    leaves active without the user’s participation, and the unbinding must be picked up
    by the next pass. There must be no shared/long-lived instance.
    """

    def __init__(self) -> None:
        self._by_tenant: dict[str, set[str]] = {}

    async def get(self, session: AsyncSession, tenant_id: str) -> set[str]:
        cached = self._by_tenant.get(tenant_id)
        if cached is None:
            cached = await live_channels(session, tenant_id)
            self._by_tenant[tenant_id] = cached
        return cached


# --------------------------------------------------------------------------- #
#  Write to the outbox (§6.4)
# --------------------------------------------------------------------------- #

WRITE_WEB_OUTBOX_SQL = text("""
    INSERT INTO delivery_outbox
        (profile_id, tender_id, channel, status, requires_ai, match_kind, match_score)
    VALUES
        (:profile_id, :tender_id, 'web', 'web_only', false, :match_kind, :match_score)
    ON CONFLICT (profile_id, tender_id, channel) DO NOTHING
    """)

WRITE_OUTBOX_SQL = text("""
    INSERT INTO delivery_outbox
        (profile_id, tender_id, channel, status, requires_ai, match_kind, match_score)
    SELECT
        :profile_id,
        :tender_id,
        channel,
        CASE WHEN :ai_level <> 'none' THEN 'ai_pending' ELSE 'pending' END,
        :ai_level <> 'none',
        :match_kind,
        :match_score
    FROM unnest(CAST(:channels AS text[])) AS channel
    ON CONFLICT (profile_id, tender_id, channel) DO NOTHING
    """)


# Cabinet as the third channel (owner 18.10.2026, #781): a tenant without a live Telegram/e-mail
# gets an outbox row for channel ``web`` with status ``web_only`` — the match
# is visible in the cabinet feed. Delivery does NOT take it (claim: status ∈ pending/ai_done and
# channel from enabled_channels), the AI worker does not either (not ai_pending), the health counters
# of the queue do not count it. Connecting a channel later does not send old items: rows
# for telegram/email for already shown tenders do not exist; claim does not create them.
WEB_ONLY_STATUS = "web_only"

# The outbox statuses “the client has received or will receive”: ``sent`` + the whole path to the sending
# (``pending``/``delivering`` — delivery; ``ai_pending``/``analyzing``/``ai_done``
# — the AI worker, whose rollback degrades to ``pending`` rather than losing the row)
# + ``web_only`` (shown in the cabinet: a second copy of the cluster is not needed).
# The terminal “did not arrive” are NOT included: ``failed`` (403/a persistent channel error,
# delivery), ``suppressed`` (the daily limit of the profile, delivery), ``ai_rejected``
# (a failure of the model’s response, ai.py). A whitelist, not a blacklist: an unknown future
# status must not silently suppress a second copy.
CLUSTER_DELIVERED_STATUSES: tuple[str, ...] = (
    "sent",
    "delivering",
    "pending",
    "ai_pending",
    "analyzing",
    "ai_done",
    WEB_ONLY_STATUS,
)

# The profiles to which ANOTHER row of the same cluster has already been delivered (or is on its way).
# The neighbouring row must itself be linked as ``dup``: “probably the same” (2–3 points)
# does not suppress in either direction — otherwise the jnportal copy would vanish
# because of the delivery of TED, linked only by a place name.
CLUSTER_DELIVERED_PROFILES_SQL = text("""
    SELECT DISTINCT o.profile_id
    FROM delivery_outbox o
    JOIN tenders_normalized t ON t.id = o.tender_id
    WHERE t.canonical_tender_id = :canonical_id
      AND t.id <> :tender_id
      AND t.canonical_confidence = 'dup'
      AND o.status = ANY(:statuses)
    """)


async def suppress_cluster_duplicates(
    session: AsyncSession, tender: TenderView, matched: list[Candidate]
) -> list[Candidate]:
    """Removes the profiles that have already received the same procurement from another source.

    ONLY for a DUPLICATE (``canonical_confidence = 'dup'``, ≥4 scoring points of
    :mod:`src.dedup.cross_source`); “probable” is never suppressed. It is called by
    routing ONLY in the mode ``CROSS_SOURCE_DEDUP_MODE=suppress`` (the owner's decision
    of 01.10.2026: the first month — only the mark, the mode is off).
    """
    if tender.canonical_confidence != "dup" or not tender.canonical_tender_id:
        return matched
    result = await session.execute(
        CLUSTER_DELIVERED_PROFILES_SQL,
        {
            "canonical_id": tender.canonical_tender_id,
            "tender_id": tender.id,
            "statuses": list(CLUSTER_DELIVERED_STATUSES),
        },
    )
    delivered = {str(row.profile_id) for row in result}
    if not delivered:
        return matched
    kept = [c for c in matched if str(c.profile_id) not in delivered]
    logger.info(
        "routing: tender %s — a duplicate of cluster %s, profiles suppressed: %d",
        tender.id,
        tender.canonical_tender_id,
        len(matched) - len(kept),
    )
    return kept


async def _write_web_row(
    session: AsyncSession,
    candidate: Candidate,
    tender: TenderView,
    match_kind: str,
    match_score: float,
) -> None:
    """A ``web_only`` row — a match for the cabinet feed, without sending and without AI."""
    await session.execute(
        WRITE_WEB_OUTBOX_SQL,
        {
            "profile_id": candidate.profile_id,
            "tender_id": tender.id,
            "match_kind": match_kind,
            "match_score": match_score,
        },
    )


async def write_outbox(
    session: AsyncSession,
    *,
    candidate: Candidate,
    tender: TenderView,
    match_kind: str = "new",
    match_score: float = 1.0,
    live_cache: LiveChannelsCache | None = None,
) -> None:
    """Writes outbox rows for one profile — one per delivery channel (§6.4).

    Idempotent: ``ON CONFLICT (profile_id, tender_id, channel) DO NOTHING``.
    A repeated routing of the same tender to the same profile is a no-op (no duplicates).

    ``status``/``requires_ai`` depend on the profile's ``ai_analysis_level``:
    ``'none'`` (or absent) → ``pending`` without AI; otherwise → ``ai_pending``.
    A profile without delivery channels (or without a live addressee) does not get into the send queue,
    but gets a ``web_only`` row for the cabinet feed.

    ``delivery_channels`` is the profile SETTING ("where to send"), not the fact of a live
    addressee, so the channel is additionally checked against
    :func:`live_channels` (see its docstring). No live addressee — no row.

    ``live_cache`` — the :class:`LiveChannelsCache` of the current run: candidates
    of one tenant share one SELECT. Without it the SELECT is done on every call.
    """
    channels = candidate.delivery_channels or []
    if not channels:
        # Cabinet — the third channel: the tender is visible in the feed even without configured delivery.
        await _write_web_row(session, candidate, tender, match_kind, match_score)
        # A fact, not a skip (#820): the profile does not get into the send queue; in
        # outbox there is only a web_only row for the feed. The log — so that the number
        # "matches, but 0 deliveries" could be explained without guesswork. No PII
        # (`.claude/rules/frontend.md`): only the id, not the address/phone.
        logger.info(
            "outbox: profile %s (tenant %s) without delivery channels — a web_only row written",
            candidate.profile_id,
            candidate.tenant_id,
        )
        return

    cache = live_cache if live_cache is not None else LiveChannelsCache()
    live = await cache.get(session, candidate.tenant_id)
    channels = [c for c in channels if c in live]
    if not channels:
        # Not an error: the user may have unlinked/blocked the bot — nowhere to send, but in
        # the cabinet the match remains (third channel).
        await _write_web_row(session, candidate, tender, match_kind, match_score)
        logger.debug(
            "outbox: profile %s skipped — no live channel (configured: %s)",
            candidate.profile_id,
            candidate.delivery_channels,
        )
        return
    # NULL <> 'none' in SQL gives NULL (not TRUE) — we bring it to 'none' in advance,
    # so that requires_ai/status are computed deterministically.
    ai_level = candidate.ai_analysis_level or "none"
    await session.execute(
        WRITE_OUTBOX_SQL,
        {
            "profile_id": candidate.profile_id,
            "tender_id": tender.id,
            "channels": list(channels),
            "ai_level": ai_level,
            "match_kind": match_kind,
            "match_score": match_score,
        },
    )
