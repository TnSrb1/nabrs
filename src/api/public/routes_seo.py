"""GEO / SEO foundation: robots.txt, sitemap.xml (+ chunks), llms.txt, favicon.

Before this task all four were 404 (JSON ``{"detail":"Not Found"}``), and the 55128 rows of ``tenders_normalized`` (each open to an anonymous user with a 200) were not linked from any public page and were not listed in any sitemap — the crawler physically could not find them (TASK geo_base, fact 26.09.2026).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import cast
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from src.api import deps, repo
from src.api.config import CabinetSettings
from src.api.public import geo_cache
from src.api.public.i18n import HELP, LANDING, PLANS, SUPPORTED
from src.api.public.sources_view import SourcesView, fill_sources, get_sources_view
from src.api.public.trial_text import fill_trial
from src.api.public.news import NEWS
from src.api.public.routes_geo import (
    _cpv_sections,
    _region_sections,
    cpv_indexable_groups,
)
from src.api.public.routes_public import LEGAL_DOC_NAMES
from src.domain.text_norm import sr_display
from src.plans.catalog import ENTERPRISE_CONTACT_EMAIL, display_plans, enterprise_plan

router = APIRouter(tags=["public"])

# The sitemap.xml protocol limits a file to 50 000 URLs (sitemaps.org).
TENDER_CHUNK_SIZE = 50_000

# The public pages of the showcase (not the tenders) — a predicate by fact: the landing + the legal
# documents from the LEGAL catalog (a single source with /legal/{name}, not a separate
# list). /plans/upgrade is a POST handler, not a page for the listing.
_PUBLIC_PAGE_PATHS: tuple[str, ...] = (
    "/",
    "/plans",
    "/help",
    "/izvori",
    "/statistika",
    "/cpv-kodovi",
    "/novosti",
    *(f"/novosti/{n.slug}" for n in NEWS),
    *(f"/legal/{name}" for name in LEGAL_DOC_NAMES),
)

# Explicit private routes beyond the predicate below (without "hard" links in the router registry —
# POST-only, they draw no pages, but it is better to enumerate them explicitly, since
# the TASK names them).
_EXPLICIT_DISALLOW: tuple[str, ...] = (
    "/api",
    "/plans/upgrade",
    # The FastAPI autodoc: not pages for search (insurance in case /docs is closed).
    "/docs",
    "/redoc",
    "/openapi.json",
)


def _private_route_prefixes() -> list[str]:
    """The Disallow prefixes — a predicate over the REGISTRY OF ROUTERS, not a handwritten list.

    The cabinet / auth / admin / billing routers are attached in ``main.py`` with ``_CABINET_DEPS`` / ``_CONTENT_DEPS`` (the auth gate) — ``routes_public`` / ``routes_tender`` remain without them (the ``public`` tag). A new private route in one of the listed modules will get into the Disallow automatically by the first segment of its path — nothing needs to be added by hand.
    """
    from src.api.admin import routes_admin
    from src.api.billing import routes_billing
    from src.api.cabinet import (
        routes_account,
        routes_analitika,
        routes_auth,
        routes_channels,
        routes_library,
        routes_locale,
        routes_my_tenders,
        routes_narucilac,
        routes_profiles,
        routes_subscription,
        routes_tender_ai,
        routes_trial,
    )

    private_routers = (
        routes_auth.router,
        routes_profiles.router,
        routes_tender_ai.router,
        routes_my_tenders.router,
        routes_narucilac.router,
        routes_analitika.router,
        routes_library.router,
        routes_channels.router,
        routes_trial.router,
        routes_subscription.router,
        routes_account.router,
        routes_locale.router,
        routes_admin.router,
        routes_billing.router,
    )
    prefixes: set[str] = set(_EXPLICIT_DISALLOW)
    for r in private_routers:
        base = getattr(r, "prefix", "") or ""
        for route in r.routes:
            path = base + getattr(route, "path", "")
            segment = path.strip("/").split("/")[0].split("{")[0]
            if segment:
                prefixes.add("/" + segment)
    return sorted(prefixes)


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt(
    settings: CabinetSettings = Depends(deps.get_settings),
) -> PlainTextResponse:
    base = settings.cabinet_base_url.rstrip("/")
    lines = ["User-agent: *"]
    lines += [f"Disallow: {p}" for p in _private_route_prefixes()]
    lines += ["", f"Sitemap: {base}/sitemap.xml"]
    body = "\n".join(lines) + "\n"
    return PlainTextResponse(
        body,
        media_type="text/plain",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _url_entry(loc: str, lastmod: str | None = None) -> str:
    parts = [f"  <url>\n    <loc>{xml_escape(loc)}</loc>"]
    if lastmod:
        parts.append(f"    <lastmod>{lastmod}</lastmod>")
    parts.append("  </url>")
    return "\n".join(parts)


@router.get("/sitemap.xml")
async def sitemap_index(
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> Response:
    """The sitemap index: the pages + N chunks of tenders (≤50000 URLs each)."""
    base = settings.cabinet_base_url.rstrip("/")
    async with session_factory() as db:
        total = await repo.count_tenders(db)
    chunks = max(1, -(-total // TENDER_CHUNK_SIZE))  # ceil div, at least 1 file

    entries = [f"  <sitemap>\n    <loc>{base}/sitemap-pages.xml</loc>\n  </sitemap>"]
    for n in range(chunks):
        entries.append(
            f"  <sitemap>\n    <loc>{base}/sitemap-tenders-{n}.xml</loc>\n  </sitemap>"
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</sitemapindex>\n"
    )
    return Response(
        body,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def _geo_page_paths(session_factory: Callable) -> list[str]:
    """``/tenderi`` + the sections that passed the threshold :data:`routes_geo.MIN_TENDERS_FOR_PAGE`.

    The same aggregates that the hub renders (the in-process TTL cache ``geo_cache``) — a page is not in the sitemap if it itself gives a 404 (TASK geo_landings, step 4).
    """
    async with session_factory() as db:
        cpv_stats = await geo_cache.get_cpv_stats(db)
        reg_stats = await geo_cache.get_region_stats(db)
        cpv_groups = (await geo_cache.get_cpv_directory(db))["groups"]
    paths = ["/tenderi"]
    paths += [f"/tenderi/cpv/{code}" for code, _ in _cpv_sections(cpv_stats)]
    # The groups — by the same predicate as the page’s robots (the threshold INDEX_CPV_GROUP_MIN_TENDERS).
    paths += [f"/tenderi/cpv/{g}" for g in cpv_indexable_groups(cpv_groups)]
    paths += [f"/tenderi/region/{code}" for code, _ in _region_sections(reg_stats)]
    return paths


@router.get("/sitemap-pages.xml")
async def sitemap_pages(
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> Response:
    """The public pages × sr / ru / en (the landing, the plans, help, the legal documents, GEO)."""
    base = settings.cabinet_base_url.rstrip("/")
    entries = []
    for path in (*_PUBLIC_PAGE_PATHS, *(await _geo_page_paths(session_factory))):
        for lang in SUPPORTED:
            loc = f"{base}{path}" if lang == "sr" else f"{base}{path}?lang={lang}"
            entries.append(_url_entry(loc))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    return Response(
        body,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/sitemap-tenders-{n}.xml")
async def sitemap_tenders(
    n: int,
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> Response:
    base = settings.cabinet_base_url.rstrip("/")
    offset = n * TENDER_CHUNK_SIZE
    async with session_factory() as db:
        total = await repo.count_tenders(db)
        if n < 0 or offset >= max(total, 1):
            raise HTTPException(status_code=404)
        rows = await repo.list_tenders_for_sitemap(db, offset, TENDER_CHUNK_SIZE)

    entries = []
    for tender_id, last_changed_at in rows:
        lastmod = None
        if last_changed_at is not None:
            dt = last_changed_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            lastmod = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries.append(_url_entry(f"{base}/tender/{tender_id}", lastmod))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    return Response(
        body,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _llms_txt_body(base: str, view: SourcesView | None = None) -> str:
    """Markdown per llmstxt.org — from the existing i18n catalogs, without invention.

    ``view`` — the enabled sources (the ``sources`` table): the names for ``{sources}``, the frequency for ``{interval}`` / ``{interval_note}`` in the text of the catalogs.
    """
    view = view or SourcesView()
    sr = fill_trial(
        fill_sources(LANDING["sr"], view.names, view.placeholders("sr")), "sr"
    )
    en = fill_trial(
        fill_sources(LANDING["en"], view.names, view.placeholders("en")), "en"
    )
    sr_plan_names = PLANS["sr"]["plans"]
    plans_lines = []
    for plan in display_plans():
        name = sr_plan_names[plan.key]["name"]
        price = (
            f"{plan.price_eur} EUR/mes."
            if isinstance(plan.price_eur, int)
            else "po dogovoru"
        )
        plans_lines.append(f"- {name}: {price}")
    ent = enterprise_plan()
    plans_lines.append(
        f"- {sr_plan_names[ent.key]['name']}: po dogovoru ({ENTERPRISE_CONTACT_EMAIL})"
    )
    sr_sections = cast(
        list[dict],
        fill_trial(
            fill_sources(HELP["sr"]["sections"], view.names, view.placeholders("sr")),
            "sr",
        ),
    )
    help_sections = ", ".join(s["title"] for s in sr_sections)

    return (
        "# TenderSRB (nab.rs)\n\n"
        f"> {sr['hero_subtitle']}\n\n"
        f"{sr['icp_text']}\n\n"
        "## Šta radimo\n"
        + "\n".join(
            f"- {f['title']}: {f['text']}" for f in cast(list[dict], sr["ai_features"])
        )
        + "\n\n"
        "## Cenovnik (EUR)\n" + "\n".join(plans_lines) + "\n\n"
        f"## Pomoć\nTeme: {help_sections}\n\n"
        "## Linkovi\n"
        f"- Pomoć: {base}/help\n"
        f"- Cenovnik: {base}/plans\n"
        f"- Izvori: {base}/izvori\n"
        f"- Statistika: {base}/statistika\n"
        f"- Sitemap: {base}/sitemap.xml\n\n"
        "## English\n"
        f"{en['hero_subtitle']} {en['icp_text']}\n"
        "Pricing: "
        + ", ".join(
            (
                f"{PLANS['en']['plans'][p.key]['name']} {p.price_eur} EUR/mo"
                if isinstance(p.price_eur, int)
                else f"{PLANS['en']['plans'][p.key]['name']} on request"
            )
            for p in (*display_plans(), enterprise_plan())
        )
        + f". Help: {base}/help · Pricing: {base}/plans\n"
    )


@router.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt(
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> PlainTextResponse:
    base = settings.cabinet_base_url.rstrip("/")
    view = SourcesView()
    try:
        async with session_factory() as db:
            view = await get_sources_view(db)
    except Exception:  # noqa: BLE001 — llms.txt lives without the list of sources
        pass
    return PlainTextResponse(
        # The sr + en document must not contain Cyrillic (owner 10.10.2026) —
        # the source names come from the DB in Cyrillic.
        sr_display(_llms_txt_body(base, view), "sr"),
        media_type="text/markdown",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# RFC 9116. The “build date” = the moment of the module import: a deployment restarts the process.
# Expires = +365 days from it. Contact — info@nab.rs (MX nab.rs = Proton).
_BUILT_AT = datetime.now(timezone.utc)
_SECURITY_CONTACT = f"mailto:{ENTERPRISE_CONTACT_EMAIL}"


def _security_txt_body(base: str, built_at: datetime) -> str:
    expires = (built_at + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"Contact: {_SECURITY_CONTACT}\n"
        f"Expires: {expires}\n"
        "Preferred-Languages: sr, en, ru\n"
        f"Canonical: {base}/.well-known/security.txt\n"
    )


@router.get("/.well-known/security.txt", response_class=PlainTextResponse)
@router.get("/security.txt", response_class=PlainTextResponse)
async def security_txt(
    settings: CabinetSettings = Depends(deps.get_settings),
) -> PlainTextResponse:
    base = settings.cabinet_base_url.rstrip("/")
    return PlainTextResponse(
        _security_txt_body(base, _BUILT_AT),
        media_type="text/plain",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/favicon.ico")
async def favicon_redirect() -> RedirectResponse:
    """The browser default path → the existing SVG (301, a permanent redirect)."""
    return RedirectResponse("/static/favicon.svg", status_code=301)
