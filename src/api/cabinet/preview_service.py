"""Preview of “how many tenders would fit in a month” (EPIK 3).

Reproduces the two-stage matching from :mod:`src.workers.matching`:
- Stage A: PREVIEW_WHERE_A — the fragment is imported from matching.py (one place of truth per predicate; the difference from STAGE_A_SQL is only in the SELECT, the row source and the first_seen_at window; the predicates themselves are one text).
- Stage B: :func:`keywords_match` from the same module.

Gate: only an authorized user; filter country_code='RS'.
Window: ``first_seen_at > now() - interval '30 days'``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.actuality import sql_no_deadline_fresh_tn
from src.domain.text_norm import fold
from src.workers.keyword_expand import (
    expand_term,
    expand_term_en,
    expand_terms,
    term_forms,
)
from src.workers.matching import (
    PREVIEW_CUSTOMER_FOLLOWED,
    PREVIEW_WHERE_A,
    include_term_span,
    word_start_span,
    is_english_tender,
    keywords_match,
    match_haystack,
)

#: How many examples the live counter of the template form shows (owner: 3–5).
FORM_EXAMPLES_LIMIT = 5

# One query for the counter AND the examples (template_wizard, step 2): the window rows come
# freshest first, the counter counts all that passed stage B, the examples are the first
# of them. The display columns (title / customer / deadline) travel in the same SELECT — there is no second
# “similar” query for the examples. ORDER BY is mandatory: without it "the first
# five" are non-deterministic and differ at every recount.
_PREVIEW_SQL = text(f"""
    SELECT
        tn.id,
        tn.title,
        tn.title_normalized,
        tn.description_normalized,
        tn.customer_name,
        tn.deadline_at,
        tn.source_key,
        tn.language,
        {PREVIEW_CUSTOMER_FOLLOWED} AS customer_followed
    FROM tenders_normalized tn
    WHERE tn.country_code = 'RS'
      AND tn.first_seen_at > now() - interval '30 days'
      AND {PREVIEW_WHERE_A}
    ORDER BY tn.first_seen_at DESC, tn.id
""")

# Regional coverage: the same stage A predicates, but WITHOUT the region filter
# (:regions is passed empty — the cardinality=0 branch in PREVIEW_WHERE_A
# switches it off; the delivery predicate is not rewritten, it is mirrored). In addition
# we return has_region — “the tender has a region set”. This is how we honestly count the share of
# tenders with non-empty regions among those that would otherwise match (except by region).
_COVERAGE_SQL = text(f"""
    SELECT
        tn.id,
        tn.title_normalized,
        tn.description_normalized,
        (cardinality(COALESCE(tn.regions, '{{}}')) > 0) AS has_region,
        tn.source_key,
        tn.language,
        {PREVIEW_CUSTOMER_FOLLOWED} AS customer_followed
    FROM tenders_normalized tn
    WHERE tn.country_code = 'RS'
      AND tn.first_seen_at > now() - interval '30 days'
      AND {PREVIEW_WHERE_A}
""")


# Batch preview of the template showcase: the same A+B predicates, but the window is scanned ONCE
# for the whole page. The naive loop over the cards cost 22 full runs per
# each /library view (production measurement: ~0.8 s per card, 17–19 s per
# page) — the window lives in a MATERIALIZED CTE, and the card cls filters arrive as
# a row set (jsonb_to_recordset) and are joined with it in memory.
#
# ‼️ The predicate is NOT rewritten: it is the same PREVIEW_WHERE_A, in which two
# cls binds are replaced by the columns of the card set. The replacement goes by the exact text
# of the bind; the absence of the substring = an error at import (the guard is
# tests/api/test_library_counts.py), so a “second implementation” here
# is impossible: an edit of the fragment in matching.py goes into both queries at once.
#
# ‼️ The `win` window MUST select EVERY `tn.*` column that PREVIEW_WHERE_A reads:
# the predicate refers to `tn`, and here `tn` is a CTE. SRC-0a
# added `tn.country_code` to the predicate, the window did not select it — the batch failed with a
# ProgrammingError and the showcase silently lived without figures (guard —
# tests/api/test_library_counts.py::test_batch_window_selects_every_predicate_column).
_BATCH_CLS_BINDS = {
    "CAST(:cls_include AS jsonb)": "c.cls_include",
    "CAST(:cls_exclude AS jsonb)": "c.cls_exclude",
}


def _batch_where_a() -> str:
    where = PREVIEW_WHERE_A
    for bind, column in _BATCH_CLS_BINDS.items():
        if bind not in where:
            raise RuntimeError(f"PREVIEW_WHERE_A does not contain the {bind} bind")
        where = where.replace(bind, column)
    if ":cls_" in where:
        raise RuntimeError("a cls bind remained in the batch predicate")
    return where


_BATCH_SQL = text(f"""
    WITH win AS MATERIALIZED (
        SELECT tn.id,
               tn.country_code,
               tn.title_normalized,
               tn.description_normalized,
               tn.classifications,
               tn.regions,
               tn.source_key,
               tn.language,
               tn.deadline_at,
               tn.published_at,
               tn.first_seen_at,
               tn.status,
               tn.amount_eur,
               tn.contract_type,
               tn.customer_name
        FROM tenders_normalized tn
        WHERE tn.country_code = 'RS'
          AND tn.first_seen_at > now() - interval '30 days'
    )
    SELECT c.idx AS idx,
           tn.title_normalized,
           tn.description_normalized,
           tn.source_key,
           tn.language,
           {PREVIEW_CUSTOMER_FOLLOWED} AS customer_followed
    FROM jsonb_to_recordset(CAST(:cards AS jsonb))
         AS c(idx int, cls_include jsonb, cls_exclude jsonb),
         win tn
    WHERE {_batch_where_a()}
""")


def _drop_terms_found_in_data(terms: set[str], rows: Sequence[Any]) -> set[str]:
    """A word that is not in the dictionary but IS in the tenders — is not “untranslated”.

    A Serbian writes “škola” in Serbian: the ru/en→sr dictionary does not know it (one form), but the note “Nisu prevedene… upišite na srpskom” is false for them. A word counts as
    Serbian if in at least one row of the window (the same rows the counter counts) its fold is a substring of the folded text. The fold is the single ``text_norm.fold``.
    """
    if not terms:
        return terms
    hays = [
        fold(f"{r.title_normalized or ''} {r.description_normalized or ''}")
        for r in rows
    ]
    return {t for t in terms if not any(fold(t) in h for h in hays)}


def matches_anything(payload: dict[str, Any]) -> bool:
    """The profile matches anything at all: there is a topic OR the “All tenders” mode is declared.

    The Python mirror of the branch ``match_all OR keywords OR classifications`` from
    ``STAGE_A_SQL`` (0043). Topic = keywords OR classifications; regions, amounts and procedures only narrow the selection and the portal mirror does not make a topic out of them — exactly why a profile with seven districts and zero words does not pass here.

    It lives in one place: the form preview and the showcase counters must answer
    the same way as the routing, otherwise the form promises tenders to a profile that will not get
    a single email.
    """
    # The source is an input, not a filter: stage A (`:source_key = ANY(...)`)
    # has no “not set” leniency, a profile without platforms gets nothing.
    # Before SRC-0b the preview counted an empty list as “all sources” and promised
    # tenders to a dead profile; now it answers the same way as the routing.
    if not (payload.get("enabled_source_keys") or []):
        return False
    if payload.get("match_all"):
        return True
    # “Follow a customer” (0046) is also a topic: STAGE_A_SQL lets through a tender
    # of such a customer without words and classifications.
    if payload.get("customers_include"):
        return True
    if any((g or {}).get("all_of") for g in (payload.get("keywords_include") or [])):
        return True
    cls = payload.get("classifications") or {}
    return bool(cls.get("include"))


def profile_field(profile: Any, name: str, default: Any = None) -> Any:
    """A profile field from the ORM row OR a dict.

    The dashboard gets profiles from the service, but in tests dicts and ``SimpleNamespace`` are used in its place; demanding the full ``payload_from_profile`` contract for one check is pointless, and failing on a stub is even worse.
    """
    if isinstance(profile, dict):
        return profile.get(name, default)
    return getattr(profile, name, default)


def profile_matches_anything(profile: Any) -> bool:
    """Whether a SAVED profile (ORM row, dict or stub) will find anything at all.

    ‼️ In addition to the topic it checks the SOURCE. The source is not a filter but an input:
    `CAST(:source_key AS text) = ANY(COALESCE(p.enabled_source_keys, '{}'))` in
    ``STAGE_A_SQL`` has no “not set” leniency, so a profile without sources is dead in any mode. A real case (production, 15.08): the profile
    “Video surveillance, SKS” of a live client had an empty source — it got nothing, and the interface stayed silent about it.

    The source check is done by :func:`matches_anything` itself (since SRC-0b — shared
    by the form preview, the showcase and this banner; ``PREVIEW_WHERE_A`` still
    treats an empty list as “do not filter”, but an empty selection no longer reaches the SQL).
    """
    return matches_anything(
        {
            "match_all": profile_field(profile, "match_all", False),
            "keywords_include": profile_field(profile, "keywords_include"),
            "classifications": profile_field(profile, "classifications"),
            "enabled_source_keys": profile_field(profile, "enabled_source_keys"),
            "customers_include": profile_field(profile, "customers_include"),
        }
    )


@dataclass(frozen=True)
class PreviewResult:
    """The result of the profile preview.

    ``count`` — tenders that passed stages A+B. ``unfound`` — words not found
    in the ru/en→sr dictionary. ``region_coverage_pct`` — the share of tenders with a region set
    among those that would otherwise match (None if no region is chosen). ``hidden_by_region``
    — how many tenders would match without the region filter, on top of ``count`` (None without a region).

    ``keyword_dict_enabled`` — whether the form extension is enabled in the profile.
    ``include_forms`` / ``exclude_forms`` — for EACH entered word its Serbian forms (``{"term": word, "forms": [forms]}``), exactly those the matching searches by when ``keyword_dict`` is on. They are needed so that the user SEES which Serbian forms the search will really go by (tenders in Serbian).
    """

    count: int
    unfound: list[str]
    region_coverage_pct: int | None = None
    hidden_by_region: int | None = None
    keyword_dict_enabled: bool = False
    include_forms: list[dict] = field(default_factory=list)
    exclude_forms: list[dict] = field(default_factory=list)
    #: No source is selected — the counter is 0 for THIS reason (SRC-0b):
    #: the form shows «Izaberite bar jedan izvor», not the generic “nothing found”.
    no_sources: bool = False
    #: The first (freshest) of the counted tenders — :class:`PreviewExample`.
    #: Empty if not requested (``examples_limit=0``).
    examples: list["PreviewExample"] = field(default_factory=list)


@dataclass(frozen=True)
class PreviewExample:
    """A found tender to show under the counter: a title with highlighting.

    ``title_parts`` — the title split into pieces ``(text, matched)``:
    the template wraps the matched ones in ``<mark>``, Jinja escapes each piece
    (there is no ready-made HTML here).
    """

    id: str
    title_parts: list[tuple[str, bool]]
    customer_name: str | None
    deadline_at: Any
    archived: bool = False


def _fold_with_map(value: str) -> tuple[str, list[int]]:
    """Fold per character + a map “position in the folded → position in the source”.

    The fold changes the length (đ→dj, U+0459→lj), so match positions in the folded
    text cannot be carried over to the source title as they are.
    """
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(value):
        folded = fold(ch)
        out.append(folded)
        index.extend([i] * len(folded))
    return "".join(out), index


def highlight_parts(
    title: str, terms: Sequence[str], english_terms: Sequence[str] = ()
) -> list[tuple[str, bool]]:
    """Splits the source title into pieces, marking where include-words match.

    Where a word was found is decided by :func:`src.workers.matching.include_term_span`
    (the same rule as matching: verbatim or by stem). The first
    occurrence of each word is highlighted; a match only in the description does not colour the title.
    ``english_terms`` — en-forms: searched from the start of a word (as in the English matching
    mode), so that ``road`` is not coloured inside ``broad``.
    """
    if not title:
        return []
    folded, index = _fold_with_map(title)
    spans: list[tuple[int, int]] = []
    for term in terms:
        found = include_term_span(term, folded)
        if found is not None and found[1] > found[0]:
            spans.append((index[found[0]], index[found[1] - 1] + 1))
    for term in english_terms:
        found = word_start_span(fold(term), folded)
        if found is not None and found[1] > found[0]:
            spans.append((index[found[0]], index[found[1] - 1] + 1))
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts: list[tuple[str, bool]] = []
    pos = 0
    for start, end in merged:
        if start > pos:
            parts.append((title[pos:start], False))
        parts.append((title[start:end], True))
        pos = end
    if pos < len(title):
        parts.append((title[pos:], False))
    return parts


def _highlight_terms(payload: dict[str, Any]) -> list[str]:
    """The include words of the profile for highlighting (with word forms when ``keyword_dict`` is on)."""
    terms: list[str] = []
    for group in payload.get("keywords_include") or []:
        terms.extend((group or {}).get("all_of") or [])
    if payload.get("keyword_dict"):
        return [form for term in terms for form in expand_term(term)]
    return terms


def _highlight_terms_en(payload: dict[str, Any]) -> list[str]:
    """En-forms of include-words (with ``keyword_dict``) — highlighting with a word boundary."""
    if not payload.get("keyword_dict"):
        return []
    return [
        form
        for group in payload.get("keywords_include") or []
        for term in (group or {}).get("all_of") or []
        for form in expand_term_en(term)
    ]


def _highlight_terms_english_rows(payload: dict[str, Any]) -> list[str]:
    """Highlight words for an ENGLISH tender: raw include-words + en-forms.

    A mirror of the English-text matching (``_english_forms``): the word itself and its
    en-forms, with a word boundary. Serbian forms are not included here — otherwise the highlighting
    would colour in an English title what matching does not find there.
    """
    raw = [
        term
        for group in payload.get("keywords_include") or []
        for term in (group or {}).get("all_of") or []
    ]
    return [*raw, *_highlight_terms_en(payload)]


def _example(
    row: Any,
    terms: Sequence[str],
    english_terms: Sequence[str] = (),
    english_row_terms: Sequence[str] = (),
) -> PreviewExample:
    title = getattr(row, "title", None) or row.title_normalized or ""
    # English tender: sr-terms are not highlighted as substrings (they break the word boundary).
    if is_english_tender(
        getattr(row, "language", None), getattr(row, "source_key", None)
    ):
        title_parts = highlight_parts(title, (), english_row_terms)
    else:
        title_parts = highlight_parts(title, terms, english_terms)
    return PreviewExample(
        id=str(row.id),
        title_parts=title_parts,
        customer_name=getattr(row, "customer_name", None),
        deadline_at=getattr(row, "deadline_at", None),
        archived=bool(getattr(row, "archived", False)),
    )


def payload_from_profile(profile: Any) -> dict[str, Any]:
    """Builds the payload for :func:`preview_count` from a SAVED profile.

    Mirrors the keys read by :func:`_build_params` and ``_passes`` inside
    :func:`preview_count` (the same contract as :func:`forms.parse_profile_form` gives for an unsaved form). It is needed so that the honest banner of the feed / archive counts
    “hidden by the region filter” by REUSING the same A+B logic, and not a new predicate.
    """
    return {
        "enabled_source_keys": list(profile.enabled_source_keys or []),
        "regions": list(profile.regions or []),
        "classifications": profile.classifications or {},
        "min_amount_eur": profile.min_amount_eur,
        "max_amount_eur": profile.max_amount_eur,
        "min_days_until_deadline": profile.min_days_until_deadline,
        "include_no_amount": profile.include_no_amount,
        "keywords_include": profile.keywords_include or [],
        "keywords_exclude": list(profile.keywords_exclude or []),
        "keyword_dict": bool(profile.keyword_dict),
        # The “All tenders” mode (0043) is part of the same contract: without it the feed banner
        # would count 0 for a profile that does receive the whole stream.
        # getattr — profile stubs in tests predate the column.
        "match_all": bool(getattr(profile, "match_all", False)),
        # Item type and customers (0046) — the same contract; getattr — for stubs.
        "contract_types": list(getattr(profile, "contract_types", None) or []),
        "customers_include": list(getattr(profile, "customers_include", None) or []),
        "customers_exclude": list(getattr(profile, "customers_exclude", None) or []),
    }


def _keyword_filter(payload: dict[str, Any]):
    """Stage B as a predicate on a result row (shared by the preview and the batch)."""
    keywords_include = payload.get("keywords_include") or []
    keywords_exclude = payload.get("keywords_exclude") or []
    kw_dict_enabled = bool(payload.get("keyword_dict"))
    # The profile topic — ONLY “follow” (0046): the topic gate of STAGE_A_SQL lets through
    # only the tenders of these customers, while PREVIEW_WHERE_A without words / CPV let through
    # would get the whole stream — we cut here by the customer_followed column.
    follow_only = (
        bool(payload.get("customers_include"))
        and not payload.get("match_all")
        and not any((g or {}).get("all_of") for g in keywords_include)
        and not ((payload.get("classifications") or {}).get("include"))
    )

    def _passes(row) -> bool:
        if follow_only and not getattr(row, "customer_followed", False):
            return False
        if not keywords_include and not keywords_exclude:
            return True
        english = is_english_tender(
            getattr(row, "language", None), getattr(row, "source_key", None)
        )
        haystack = match_haystack(
            row.title_normalized, row.description_normalized, english
        )
        # “Follow” (0046) — mirror of stage_b_filter: without include, stop words yes.
        followed = bool(getattr(row, "customer_followed", False))
        return keywords_match(
            None if followed else keywords_include,
            keywords_exclude,
            haystack,
            keyword_dict_enabled=kw_dict_enabled,
            english=english,
        )

    return _passes


def _build_params(payload: dict[str, Any]) -> dict[str, Any]:
    source_keys: list[str] = payload.get("enabled_source_keys") or []
    regions: list[str] = payload.get("regions") or []

    classifications: dict = payload.get("classifications") or {}
    inc_list: list[dict] = classifications.get("include") or []
    exc_list: list[dict] = classifications.get("exclude") or []

    min_amount = payload.get("min_amount_eur")
    max_amount = payload.get("max_amount_eur")
    min_days = payload.get("min_days_until_deadline")
    include_no_amount = payload.get("include_no_amount")

    return {
        "sources": source_keys,
        "regions": regions,
        "cls_include": json.dumps(inc_list),
        "cls_exclude": json.dumps(exc_list),
        "min_days": int(min_days) if min_days is not None else None,
        "include_no_amount": (
            bool(include_no_amount) if include_no_amount is not None else False
        ),
        "min_amount": float(min_amount) if min_amount is not None else None,
        "max_amount": float(max_amount) if max_amount is not None else None,
        "contract_types": list(payload.get("contract_types") or []),
        "customers_include": list(payload.get("customers_include") or []),
        "customers_exclude": list(payload.get("customers_exclude") or []),
    }


async def preview_count(
    db: AsyncSession,
    payload: dict[str, Any],
    window_days: int = 30,
    examples_limit: int = 0,
) -> PreviewResult:
    """Counts the tenders that passed stages A+B of the profile’s matching over window_days.

    ``payload`` — dict from :func:`src.api.forms.parse_profile_form`.
    Returns :class:`PreviewResult`. When at least one region is chosen in the profile, a second
    query (the same window, without the region filter) counts the honest regional coverage —
    otherwise the filter silently hides tenders with an empty ``regions``.

    ‼️ Mirror of the “All tenders” mode (0043). ``PREVIEW_WHERE_A`` is a tender-centric
    copy of ``STAGE_A_SQL``, and both selections must answer the same way (the invariant from the project rules
    “a predicate that duplicates another module’s selection — mirror it”). A profile without a topic and without
    ``match_all`` is now not matched by the routing at all, so the preview must show 0 too: otherwise the form would promise "≈2000 tenders in 30
    days" to exactly the profile that will not get a single email.

    ``examples_limit`` > 0 — return in ``examples`` the first (freshest)
    counted tenders. The same pass over the same rows as the counter: an example
    cannot be a tender that is not in the figure.
    """
    if not (payload.get("enabled_source_keys") or []):
        return PreviewResult(count=0, unfound=[], no_sources=True)
    if not matches_anything(payload):
        return PreviewResult(count=0, unfound=[])

    params = _build_params(payload)

    keywords_include = payload.get("keywords_include") or []
    keywords_exclude = payload.get("keywords_exclude") or []
    kw_dict_enabled: bool = bool(payload.get("keyword_dict"))
    _passes = _keyword_filter(payload)

    result = await db.execute(_PREVIEW_SQL, params)
    rows = result.fetchall()
    passed = [row for row in rows if _passes(row)]
    count = len(passed)
    examples: list[PreviewExample] = []
    if examples_limit > 0:
        terms = _highlight_terms(payload)
        terms_en = _highlight_terms_en(payload)
        terms_en_rows = _highlight_terms_english_rows(payload)
        examples = [
            _example(row, terms, terms_en, terms_en_rows)
            for row in passed[:examples_limit]
        ]

    # We collect all include terms (flat: the forms are shown per word, not per group)
    all_include_terms: list[str] = []
    for g in keywords_include:
        all_include_terms.extend((g or {}).get("all_of") or [])

    # Not found in the dictionary — as before (only when the extension is on).
    unfound: set[str] = set()
    if kw_dict_enabled:
        _, inc_unfound = expand_terms(all_include_terms, enabled=True)
        _, exc_unfound = expand_terms(list(keywords_exclude), enabled=True)
        unfound = _drop_terms_found_in_data(inc_unfound | exc_unfound, rows)

    # For each entered word — its Serbian forms (what the matching searches by).
    # We always count: even with the extension off we show the words, but the UI
    # will mark that the search goes only by the entered form.
    include_forms = term_forms(all_include_terms)
    exclude_forms = term_forms(list(keywords_exclude))

    region_coverage_pct: int | None = None
    hidden_by_region: int | None = None
    if params["regions"]:
        # The same window / criteria, but the region is off (regions=[] → cardinality 0).
        cov_params = {**params, "regions": []}
        cov_result = await db.execute(_COVERAGE_SQL, cov_params)
        cov_rows = cov_result.fetchall()
        denom = 0
        numer = 0
        for row in cov_rows:
            if _passes(row):
                denom += 1
                if row.has_region:
                    numer += 1
        if denom:
            region_coverage_pct = round(100 * numer / denom)
            hidden_by_region = max(denom - count, 0)

    return PreviewResult(
        count=count,
        unfound=sorted(unfound),
        region_coverage_pct=region_coverage_pct,
        hidden_by_region=hidden_by_region,
        keyword_dict_enabled=kw_dict_enabled,
        include_forms=include_forms,
        exclude_forms=exclude_forms,
        examples=examples,
    )


#: How many open tenders the “Already open” block shows after the template.
OPEN_NOW_LIMIT = 15
#: SQL page size for the block (stage B cuts rows in Python, pages are fetched further).
OPEN_NOW_PAGE = 300
#: Cap on scanned rows: a template with a rare word does not page through the whole stream of open tenders.
OPEN_NOW_SCAN_CAP = 1500

# “Open now” — the same deadline predicate as the feed (`_CURRENT_ONLY_PREDICATE`
# in feed_service: the deadline in the future OR no deadline and the tender is younger than 30 days), and
# stage A of THAT SAME matching (`PREVIEW_WHERE_A`). Unlike `_PREVIEW_SQL`, there is NO window
# on `first_seen_at`: an open tender may be older than 30 days and still be open.
# Order — by the submission deadline (nearest first), no deadline — at the end; LIMIT — in SQL.
# `archived` — the tender appeared BEFORE the profile’s delivery cutoff (`source_opt_in` or
# `created_at`, the same COALESCE as in the matching): no notification will come for it.
_OPEN_NOW_SQL = text(f"""
    SELECT
        tn.id,
        tn.title,
        tn.title_normalized,
        tn.description_normalized,
        tn.customer_name,
        tn.deadline_at,
        tn.source_key,
        tn.language,
        {PREVIEW_CUSTOMER_FOLLOWED} AS customer_followed,
        (tn.first_seen_at < COALESCE(
            CAST(p.source_opt_in ->> tn.source_key AS timestamptz), p.created_at
        )) AS archived
    FROM tenders_normalized tn
    JOIN profiles p ON p.id = CAST(:profile_id AS uuid)
    WHERE tn.country_code = 'RS'
      AND ({sql_no_deadline_fresh_tn('tn')} OR tn.deadline_at >= now())
      AND {PREVIEW_WHERE_A}
    ORDER BY tn.deadline_at ASC NULLS LAST, tn.id
    LIMIT :lim OFFSET :off
""")


async def open_now_examples(
    db: AsyncSession,
    payload: dict[str, Any],
    profile_id: str,
    limit: int = OPEN_NOW_LIMIT,
) -> list[PreviewExample]:
    """Tenders OPEN NOW that would match the profile (read-only).

    The “Already open tenders” block on the screen after a template is created or edited. Stage B is the same :func:`_keyword_filter` as the counter’s; it runs in Python, so the SQL returns pages by ``OPEN_NOW_PAGE`` (ORDER BY / LIMIT / OFFSET in SQL), and the next page is fetched only if fewer than ``limit`` were collected and fewer than ``OPEN_NOW_SCAN_CAP`` rows were scanned (a cap for a template with a rare word). The query writes nothing to delivery: neither ``delivery_outbox`` nor ``user_channels`` nor matching is touched (the test proves it).
    """
    if not (payload.get("enabled_source_keys") or []):
        return []
    if not matches_anything(payload):
        return []

    params = _build_params(payload)
    passes = _keyword_filter(payload)
    examples: list[PreviewExample] = []
    terms = _highlight_terms(payload)
    terms_en = _highlight_terms_en(payload)
    terms_en_rows = _highlight_terms_english_rows(payload)
    offset = 0
    while len(examples) < limit and offset < OPEN_NOW_SCAN_CAP:
        page = await db.execute(
            _OPEN_NOW_SQL,
            {**params, "profile_id": profile_id, "lim": OPEN_NOW_PAGE, "off": offset},
        )
        rows = page.fetchall()
        for row in rows:
            if passes(row):
                examples.append(_example(row, terms, terms_en, terms_en_rows))
                if len(examples) >= limit:
                    break
        if len(rows) < OPEN_NOW_PAGE:
            break
        offset += OPEN_NOW_PAGE
    return examples


def _cls_lists(payload: dict[str, Any]) -> tuple[list, list]:
    classifications: dict = payload.get("classifications") or {}
    return (classifications.get("include") or [], classifications.get("exclude") or [])


async def preview_counts(
    db: AsyncSession,
    payloads: Sequence[dict[str, Any]],
) -> list[int]:
    """Preview counters for SEVERAL profiles in one window scan (showcase).

    Returns a list of the same length and in the same order as ``payloads``. The value of each element equals ``preview_count(payload).count`` — the predicates are the same (stage A from ``PREVIEW_WHERE_A``, stage B from ``keywords_match``); only the query layout changes.

    The payloads are grouped by the NON-cls parameters of stage A (sources, regions, amounts, deadline): the showcase cards have the same ones, so in practice this is one query per page. If they diverge, there will be one query per group, but the result does not change because of it.
    """
    if not payloads:
        return []

    groups: dict[str, list[int]] = {}
    params_by_group: dict[str, dict[str, Any]] = {}
    for idx, payload in enumerate(payloads):
        # The same routing mirror as in preview_count (0043): a card without a topic
        # and without match_all gets nothing, its counter is 0, not “the whole stream”.
        if not matches_anything(payload):
            continue
        params = _build_params(payload)
        shared = {
            k: v for k, v in params.items() if k not in ("cls_include", "cls_exclude")
        }
        key = json.dumps(shared, sort_keys=True, default=str)
        groups.setdefault(key, []).append(idx)
        params_by_group[key] = shared

    counts = [0] * len(payloads)
    for key, indexes in groups.items():
        cards = []
        for idx in indexes:
            inc, exc = _cls_lists(payloads[idx])
            cards.append({"idx": idx, "cls_include": inc, "cls_exclude": exc})
        result = await db.execute(
            _BATCH_SQL, {**params_by_group[key], "cards": json.dumps(cards)}
        )
        passes = {idx: _keyword_filter(payloads[idx]) for idx in indexes}
        for row in result.fetchall():
            if passes[row.idx](row):
                counts[row.idx] += 1
    return counts
