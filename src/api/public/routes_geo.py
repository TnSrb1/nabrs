"""GEO-2: landing pages ``/tenderi`` — “by CPV section” and “by region”.

An intermediate layer between the landing page and the card ``/tender/{id}`` (routes_tender): the AI assistants and search engines answer "tenderi za <subject> / u <city>" with thematic selections, and until this task such a page did not exist and the cards were not linked by references (TASK geo_landings, owner’s decision 26.09.2026).

Against thin content: a section / region gets a page only if it has ``>= MIN_TENDERS_FOR_PAGE`` tenders over all time (not only the open ones) — otherwise a 404. The CPV × region combinations are deliberately not made (see the TASK).

The aggregates (the counters, the set of sections / regions) are cached in-process (:mod:`src.api.public.geo_cache`, TTL 1 h) — they are not computed on every render. The tender listings (open / latest closed) go as a direct query on every render: the index on ``deadline_at`` keeps it cheap (see the docstring of ``src.api.repo``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.api import deps, repo
from src.api.cabinet.library_sectors import cpv_label
from src.api.config import CabinetSettings
from src.api.pager import page_param
from src.api.public import cpv_directory, geo_cache
from src.api.public.sources_view import placeholders_for
from src.api.public.portal_filters import SORT_KEYS, PortalFilters, parse_filters
from src.api.public.i18n import GEO, TENDER, public_context, seo_canonical_and_hreflang
from src.api.templating import templates
from src.domain.indexing import INDEX_CPV_GROUP_MIN_TENDERS
from src.domain.regions import OKRUG_CHOICES, OKRUG_CODES, okrug_label

router = APIRouter(tags=["public"])

logger = logging.getLogger(__name__)

#: The threshold of “not thin content” — a section / region page exists only with
#: >= this many tenders OVER ALL TIME (not only the open ones). Owner’s decision 26.09.2026.
MIN_TENDERS_FOR_PAGE = 5

#: Open tenders — page by page (the protocol of the sitemap / the pagination of the cabinet feed does not
#: dictate the number; it is taken as a reasonable listing page).
PAGE_SIZE = 50


#: The time ceiling of the portal query WITH FILTERS (ms): count + page on production for
#: ``actual=0`` + ``q`` + sorting by amount ≈1.4 s; without a limit this is a DoS by an anonymous user.
PORTAL_STATEMENT_TIMEOUT_MS = 3000

#: SQLSTATE query_canceled (statement_timeout).
_QUERY_CANCELED = "57014"

#: “Latest closed” — a fixed tail without pagination (TASK, step 2).
CLOSED_LIMIT = 20


def _localize_cpv(stats: dict[str, dict], lang: str) -> dict[str, dict]:
    """A copy of the CPV section statistics with the name in the page language.

    ``label`` in the cache comes from the tender data (Serbian Cyrillic) and does not depend on the language; for non-sr we take the name of the section root (``XX000000``) from ``cpv_codes.json``. The fallback is EXPLICIT: no name in the reference → the previous label from the data. The cache is not mutated.
    """
    if lang == "sr":
        return stats
    return {
        div: {**s, "label": cpv_label(f"{div}000000", lang) or s["label"]}
        for div, s in stats.items()
    }


def _cpv_sections(stats: dict[str, dict]) -> list[tuple[str, dict]]:
    """The sections that passed the threshold, sorted by code (for the hub and the neighbours)."""
    return sorted(
        ((div, s) for div, s in stats.items() if s["total"] >= MIN_TENDERS_FOR_PAGE),
        key=lambda kv: kv[0],
    )


def _cpv_group_valid(group: str) -> bool:
    """The group exists in the reference ``cpv_codes.json`` (the root ``NNN00000``)."""
    return (
        len(group) == 3
        and group.isascii()
        and group.isdigit()
        and cpv_label(f"{group}00000", "en") is not None
    )


def cpv_indexable_groups(groups: dict[str, dict]) -> list[str]:
    """The groups with ``>= INDEX_CPV_GROUP_MIN_TENDERS`` tenders: the page is in the index and the sitemap.

    ONE predicate for the sitemap, the ``robots`` of the page, the list of the groups of the section and ``/cpv-kodovi`` — otherwise a page in the sitemap would serve ``noindex``.
    """
    return sorted(
        g
        for g, s in groups.items()
        if s["total"] >= INDEX_CPV_GROUP_MIN_TENDERS and _cpv_group_valid(g)
    )


def _region_sections(stats: dict[str, dict]) -> list[tuple[str, dict]]:
    """The regions with open tenders, in code order (RS-00, RS-01, …).

    The threshold — ``total >= MIN_TENDERS_FOR_PAGE`` and ``open > 0``: a region without open tenders is not visible in the hub or in the sitemap (audit 07.10: “Currently open 0” on RS-26 / 27 / 29). A direct visit to such a section stays 200, but with noindex.
    """
    out: list[tuple[str, dict]] = []
    for code in sorted(OKRUG_CODES):
        s = stats.get(code, {"total": 0, "open": 0, "open_sum_eur": None})
        if s["total"] >= MIN_TENDERS_FOR_PAGE and s["open"] > 0:
            out.append((code, s))
    return out


def _neighbors(
    sections: list[tuple[str, dict]], code: str
) -> tuple[str | None, str | None]:
    """The neighbouring section / region by code (for the interlinking of the neighbouring pages)."""
    codes = [c for c, _ in sections]
    try:
        idx = codes.index(code)
    except ValueError:
        return None, None
    prev_code = codes[idx - 1] if idx > 0 else None
    next_code = codes[idx + 1] if idx < len(codes) - 1 else None
    return prev_code, next_code


def _breadcrumb_list(base: str, t: dict, extra: list[tuple[str, str]]) -> dict:
    items = [
        {
            "@type": "ListItem",
            "position": 1,
            "name": t["breadcrumb_home"],
            "item": base,
        },
        {
            "@type": "ListItem",
            "position": 2,
            "name": t["breadcrumb_hub"],
            "item": f"{base}/tenderi",
        },
    ]
    for i, (name, url) in enumerate(extra, start=3):
        items.append({"@type": "ListItem", "position": i, "name": name, "item": url})
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": items,
    }


async def _load_filters(request: Request, db, *, allow_region: bool) -> PortalFilters:
    """The portal filters from the query (allow-list) + the set of enabled sources."""
    labels = await geo_cache.get_source_labels(db)
    return parse_filters(
        request.query_params, source_keys=labels, allow_region=allow_region
    )


def _is_query_canceled(exc: DBAPIError) -> bool:
    orig = getattr(exc, "orig", None)
    return (
        getattr(orig, "sqlstate", None) == _QUERY_CANCELED
        or type(orig).__name__ == "QueryCanceledError"
    )


async def _portal_listing(
    db, pf: PortalFilters, page: int, **scope: str
) -> tuple[int, Sequence[Any], int, bool]:
    """The page + the counter → ``(page, rows, found, timed_out)``.

    Beyond the last page — the LAST one (a re-request with its number), not a 404 and not an empty one: the same contract as in the cabinet (narucilac) and the feed. ``page`` in the response is the actual one; the canonical URL is built from it. A timeout does not re-request the page (the total cannot be found).
    """
    rows, found, timed_out = await _portal_query(db, pf, page, **scope)
    last = _total_pages(found)
    if page > last:
        if timed_out:
            return 1, rows, found, True
        return await _portal_listing(db, pf, last, **scope)
    return page, rows, found, timed_out


async def _portal_query(
    db, pf: PortalFilters, page: int, **scope: str
) -> tuple[Sequence[Any], int, bool]:
    """The page + the counter under ``statement_timeout`` → ``(rows, found, timed_out)``.

    A timeout is not a 500: the page is rendered with the hint “refine the query”. The count is skipped if the first page is incomplete (then N = len).
    """
    try:
        await db.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(PORTAL_STATEMENT_TIMEOUT_MS)},
        )
        rows = list(
            await repo.search_portal_tenders(
                db, pf, offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE, **scope
            )
        )
        if page == 1 and len(rows) < PAGE_SIZE:
            found = len(rows)
        else:
            found = await repo.count_portal_tenders(db, pf, **scope)
    except DBAPIError as exc:
        if not _is_query_canceled(exc):
            raise
        # PII / the request text is not written to the log — only the exception type.
        logger.warning("portal query timeout: %s", type(exc.orig or exc).__name__)
        # After 57014 the transaction is aborted — any next request of the session would fail with
        # InFailedSQLTransactionError. The rollback also resets the local timeout.
        await db.rollback()
        return [], 0, True
    return rows, found, False


def _total_pages(found: int) -> int:
    return max(1, -(-found // PAGE_SIZE))


def _filter_ctx(pf: PortalFilters, *, allow_region: bool) -> dict:
    """The context of the form / chips (shared by the hub and the sections)."""
    return {
        "pf": pf,
        "region_options": (
            [(code, okrug_label(code)) for code, _ in OKRUG_CHOICES]
            if allow_region
            else []
        ),
        "source_options": sorted(pf.source_labels.items(), key=lambda kv: kv[1]),
        "sort_keys": SORT_KEYS,
        # A page with filters is not a separate landing page: noindex (the canonical and
        # it points to the base path, the query is not included — seo_canonical_and_hreflang).
        "noindex": pf.active,
    }


def _hub_json_ld(base: str, t: dict) -> list[dict]:
    return [
        {
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": t["hub_title"],
            "url": f"{base}/tenderi",
        },
        _breadcrumb_list(base, t, []),
    ]


def _section_json_ld(
    base: str,
    t: dict,
    kind: str,
    code: str,
    title: str,
    open_rows,
    crumbs: list[tuple[str, str]] | None = None,
) -> list[dict]:
    url = f"{base}/tenderi/{kind}/{code}"
    collection = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": title,
        "url": url,
    }
    # Only the card URLs (TASK, step 4) — no fields of the tenant’s AI analysis.
    item_list = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "url": f"{base}/tender/{row.id}"}
            for i, row in enumerate(open_rows, start=1)
        ],
    }
    return [
        collection,
        item_list,
        _breadcrumb_list(base, t, [*(crumbs or []), (title, url)]),
    ]


@router.get("/tenderi", response_class=HTMLResponse)
async def geo_hub(
    request: Request,
    page: int = Depends(page_param),
    session=Depends(deps.get_optional_session),
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> HTMLResponse:
    ctx = public_context(request)
    t = GEO[ctx["lang"]]
    base = settings.cabinet_base_url.rstrip("/")
    open_rows: Sequence[Any] = []
    found = 0
    timed_out = False
    async with session_factory() as db:
        cpv_stats = _localize_cpv(await geo_cache.get_cpv_stats(db), ctx["lang"])
        reg_stats = await geo_cache.get_region_stats(db)
        pf = await _load_filters(request, db, allow_region=True)
        if pf.active:
            page, open_rows, found, timed_out = await _portal_listing(db, pf, page)
        else:
            # Without filters the hub does not page: page 1, the canonical without ?page=.
            page = 1
        sources_count = len(await geo_cache.get_source_labels(db))
    canonical, hreflang = seo_canonical_and_hreflang(
        request, settings.cabinet_base_url, page=page
    )
    return templates.TemplateResponse(
        request,
        "tenderi_hub.html",
        {
            **ctx,
            **_filter_ctx(pf, allow_region=True),
            "t": t,
            "tender_t": TENDER[ctx["lang"]],
            "open_rows": open_rows,
            "found": found,
            "timed_out": timed_out,
            "page": page,
            "total_pages": _total_pages(found),
            "section_path": "/tenderi",
            "authed": session is not None,
            "sources_count": sources_count,
            "cpv_sections": _cpv_sections(cpv_stats),
            "region_sections": _region_sections(reg_stats),
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
            "json_ld_list": _hub_json_ld(base, t),
        },
    )


async def _geo_cpv_group(
    request: Request,
    group: str,
    page: int,
    session,
    settings: CabinetSettings,
    session_factory: Callable,
) -> HTMLResponse:
    """The CPV group page (3 digits): the same template as the section’s.

    The group exists in the reference — always 200; into the index and the sitemap — only with ``total >= INDEX_CPV_GROUP_MIN_TENDERS`` (otherwise ``noindex,follow``).
    """
    ctx = public_context(request)
    t = GEO[ctx["lang"]]
    if not _cpv_group_valid(group):
        raise HTTPException(status_code=404)
    timed_out = False
    division = group[:2]

    async with session_factory() as db:
        groups = (await geo_cache.get_cpv_directory(db))["groups"]
        entry = groups.get(group) or {"total": 0, "open": 0, "open_sum_eur": None}
        pf = await _load_filters(request, db, allow_region=True)
        found = entry["open"]
        open_rows: Sequence[Any]
        closed_rows: Sequence[Any]
        if pf.active:
            page, open_rows, found, timed_out = await _portal_listing(
                db, pf, page, cpv_division=group
            )
            closed_rows = []
        else:
            page = min(page, _total_pages(found))
            open_rows = await repo.list_open_tenders_by_cpv(
                db, group, (page - 1) * PAGE_SIZE, PAGE_SIZE
            )
            closed_rows = (
                await repo.list_closed_tenders_by_cpv(db, group, CLOSED_LIMIT)
                if page == 1
                else []
            )

    lang = ctx["lang"]
    siblings = cpv_indexable_groups(groups)
    siblings = [g for g in siblings if g[:2] == division]
    prev_code, next_code = _neighbors([(g, {}) for g in siblings], group)
    prev_section = (
        (prev_code, cpv_label(f"{prev_code}00000", lang)) if prev_code else None
    )
    next_section = (
        (next_code, cpv_label(f"{next_code}00000", lang)) if next_code else None
    )

    base = settings.cabinet_base_url.rstrip("/")
    canonical, hreflang = seo_canonical_and_hreflang(
        request, settings.cabinet_base_url, page=page
    )
    title = cpv_label(f"{group}00000", lang) or group
    division_title = cpv_label(f"{division}000000", lang) or division
    crumbs = [
        ("/cpv-kodovi", t["crumb_cpv"]),
        (f"/tenderi/cpv/{division}", division_title),
    ]
    total_pages = _total_pages(found)

    return templates.TemplateResponse(
        request,
        "tenderi_section.html",
        {
            **ctx,
            **_filter_ctx(pf, allow_region=True),
            # A thin group (below the threshold) and output with filters — outside the index.
            "noindex": pf.active or entry["total"] < INDEX_CPV_GROUP_MIN_TENDERS,
            "found": found,
            "timed_out": timed_out,
            "t": t,
            "tender_t": TENDER[lang],
            "authed": session is not None,
            "kind": "cpv",
            "code": group,
            "title": title,
            "intro": t["section_group_intro"].format(
                code=group,
                interval=(await placeholders_for(session_factory, lang))["interval"],
            ),
            "stats": entry,
            "open_rows": open_rows,
            "closed_rows": closed_rows,
            "page": page,
            "total_pages": total_pages,
            "prev_section": prev_section,
            "next_section": next_section,
            "section_path": f"/tenderi/cpv/{group}",
            "crumbs": crumbs,
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
            "json_ld_list": _section_json_ld(
                base,
                t,
                "cpv",
                group,
                title,
                open_rows,
                crumbs=[(n, f"{base}{h}") for h, n in crumbs],
            ),
        },
    )


@router.get("/tenderi/cpv/{division}", response_class=HTMLResponse)
async def geo_cpv_section(
    request: Request,
    division: str,
    page: int = Depends(page_param),
    session=Depends(deps.get_optional_session),
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> HTMLResponse:
    if len(division) == 3:
        return await _geo_cpv_group(
            request, division, page, session, settings, session_factory
        )
    ctx = public_context(request)
    t = GEO[ctx["lang"]]
    timed_out = False

    async with session_factory() as db:
        stats = _localize_cpv(await geo_cache.get_cpv_stats(db), ctx["lang"])
        entry = stats.get(division)
        if entry is None or entry["total"] < MIN_TENDERS_FOR_PAGE:
            raise HTTPException(status_code=404)
        groups = (await geo_cache.get_cpv_directory(db))["groups"]
        pf = await _load_filters(request, db, allow_region=True)
        found = entry["open"]
        open_rows: Sequence[Any]
        closed_rows: Sequence[Any]
        if pf.active:
            page, open_rows, found, timed_out = await _portal_listing(
                db, pf, page, cpv_division=division
            )
            closed_rows = []
        else:
            page = min(page, _total_pages(found))
            open_rows = await repo.list_open_tenders_by_cpv(
                db, division, (page - 1) * PAGE_SIZE, PAGE_SIZE
            )
            closed_rows = (
                await repo.list_closed_tenders_by_cpv(db, division, CLOSED_LIMIT)
                if page == 1
                else []
            )

    sections = _cpv_sections(stats)
    prev_code, next_code = _neighbors(sections, division)
    prev_section = (prev_code, stats[prev_code]["label"]) if prev_code else None
    next_section = (next_code, stats[next_code]["label"]) if next_code else None

    base = settings.cabinet_base_url.rstrip("/")
    canonical, hreflang = seo_canonical_and_hreflang(
        request, settings.cabinet_base_url, page=page
    )
    title = entry["label"]
    total_pages = _total_pages(found)

    return templates.TemplateResponse(
        request,
        "tenderi_section.html",
        {
            **ctx,
            **_filter_ctx(pf, allow_region=True),
            "found": found,
            "timed_out": timed_out,
            "t": t,
            "tender_t": TENDER[ctx["lang"]],
            "authed": session is not None,
            "kind": "cpv",
            "code": division,
            "title": title,
            "intro": t["section_cpv_intro"].format(
                code=division,
                interval=(await placeholders_for(session_factory, ctx["lang"]))[
                    "interval"
                ],
            ),
            "stats": entry,
            "open_rows": open_rows,
            "closed_rows": closed_rows,
            "page": page,
            "total_pages": total_pages,
            "prev_section": prev_section,
            "next_section": next_section,
            "section_path": f"/tenderi/cpv/{division}",
            "crumbs": [("/cpv-kodovi", t["crumb_cpv"])],
            "child_groups": [
                (g, cpv_label(f"{g}00000", ctx["lang"]) or g, groups[g]["total"])
                for g in cpv_indexable_groups(groups)
                if g[:2] == division
            ],
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
            "json_ld_list": _section_json_ld(
                base,
                t,
                "cpv",
                division,
                title,
                open_rows,
                crumbs=[(t["crumb_cpv"], f"{base}/cpv-kodovi")],
            ),
        },
    )


@router.get("/cpv-kodovi", response_class=HTMLResponse)
async def cpv_directory_page(
    request: Request,
    session=Depends(deps.get_optional_session),
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> HTMLResponse:
    """The CPV directory: the tree of sections → groups + the client-side search over 9 454 codes.

    The tender counters come from ``geo_cache`` (TTL 1 h), not a query on render. The search is ``cpv_directory.js`` over ``cpv_codes.json``; the counters and the set of pages that can be linked to travel in the page as a JSON block (not executed — the CSP does not interfere).
    """
    ctx = public_context(request)
    lang = ctx["lang"]
    t = GEO[lang]
    async with session_factory() as db:
        cpv_stats = await geo_cache.get_cpv_stats(db)
        directory = await geo_cache.get_cpv_directory(db)
    groups = directory["groups"]
    linkable_groups = cpv_indexable_groups(groups)
    divisions = [d for d, _ in _cpv_sections(cpv_stats)]

    tree = cpv_directory.tree(lang)
    for division in tree:
        division["total"] = cpv_stats.get(division["code"], {}).get("total", 0)
        division["linked"] = division["code"] in divisions
        for g in division["groups"]:
            g["total"] = groups.get(g["code"], {}).get("total", 0)
            g["linked"] = g["code"] in linkable_groups

    base = settings.cabinet_base_url.rstrip("/")
    canonical, hreflang = seo_canonical_and_hreflang(request, settings.cabinet_base_url)
    terms = [
        {
            "@type": "DefinedTerm",
            "termCode": d["code"],
            "name": d["label"],
            "url": f"{base}/tenderi/cpv/{d['code']}",
        }
        for d in tree
        if d["linked"]
    ] + [
        {
            "@type": "DefinedTerm",
            "termCode": g["code"],
            "name": g["label"],
            "url": f"{base}/tenderi/cpv/{g['code']}",
        }
        for d in tree
        for g in d["groups"]
        if g["linked"]
    ]
    json_ld = [
        {
            "@context": "https://schema.org",
            "@type": "DefinedTermSet",
            "name": t["cpv_dir_title"],
            "url": f"{base}/cpv-kodovi",
            "inLanguage": lang,
            "hasDefinedTerm": terms,
        },
        _breadcrumb_list(base, t, [(t["cpv_dir_title"], f"{base}/cpv-kodovi")]),
    ]
    return templates.TemplateResponse(
        request,
        "cpv_directory.html",
        {
            **ctx,
            "t": t,
            "authed": session is not None,
            "tree": tree,
            "dir_data": {
                "n": directory["codes"],
                "g": linkable_groups,
                "d": divisions,
            },
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
            "json_ld_list": json_ld,
        },
    )


@router.get("/tenderi/region/{code}", response_class=HTMLResponse)
async def geo_region_section(
    request: Request,
    code: str,
    page: int = Depends(page_param),
    session=Depends(deps.get_optional_session),
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> HTMLResponse:
    ctx = public_context(request)
    t = GEO[ctx["lang"]]
    timed_out = False

    if code not in OKRUG_CODES:
        raise HTTPException(status_code=404)

    async with session_factory() as db:
        stats = await geo_cache.get_region_stats(db)
        entry = stats.get(code)
        if entry is None or entry["total"] < MIN_TENDERS_FOR_PAGE:
            raise HTTPException(status_code=404)
        pf = await _load_filters(request, db, allow_region=False)
        found = entry["open"]
        open_rows: Sequence[Any]
        closed_rows: Sequence[Any]
        if pf.active:
            page, open_rows, found, timed_out = await _portal_listing(
                db, pf, page, region_code=code
            )
            closed_rows = []
        else:
            page = min(page, _total_pages(found))
            open_rows = await repo.list_open_tenders_by_region(
                db, code, (page - 1) * PAGE_SIZE, PAGE_SIZE
            )
            closed_rows = (
                await repo.list_closed_tenders_by_region(db, code, CLOSED_LIMIT)
                if page == 1
                else []
            )

    sections = _region_sections(stats)
    prev_code, next_code = _neighbors(sections, code)
    prev_section = (prev_code, okrug_label(prev_code)) if prev_code else None
    next_section = (next_code, okrug_label(next_code)) if next_code else None

    base = settings.cabinet_base_url.rstrip("/")
    canonical, hreflang = seo_canonical_and_hreflang(
        request, settings.cabinet_base_url, page=page
    )
    title = okrug_label(code)
    total_pages = _total_pages(found)

    return templates.TemplateResponse(
        request,
        "tenderi_section.html",
        {
            **ctx,
            **_filter_ctx(pf, allow_region=False),
            # Without open tenders the section is an empty page: we do not index it.
            "noindex": pf.active or entry["open"] == 0,
            "found": found,
            "timed_out": timed_out,
            "t": t,
            "tender_t": TENDER[ctx["lang"]],
            "authed": session is not None,
            "kind": "region",
            "code": code,
            "title": title,
            "intro": t["section_region_intro"].format(
                name=title,
                interval=(await placeholders_for(session_factory, ctx["lang"]))[
                    "interval"
                ],
            ),
            "stats": entry,
            "open_rows": open_rows,
            "closed_rows": closed_rows,
            "page": page,
            "total_pages": total_pages,
            "prev_section": prev_section,
            "next_section": next_section,
            "section_path": f"/tenderi/region/{code}",
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
            "json_ld_list": _section_json_ld(base, t, "region", code, title, open_rows),
        },
    )
