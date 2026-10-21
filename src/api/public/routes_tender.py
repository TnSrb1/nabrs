"""The public tender card ``/tender/{id}`` (ARCHITECTURE §5.2).

It closes the deep link from the delivery worker (the Telegram button “Open tender” → ``{CABINET_BASE_URL}/tender/{tender_id}``, see src/workers/adapters/telegram.py). Tenders are public data (NOT tenant-scoped, no RLS, DB_SCHEMA §4). The AI analysis is shown only to an authorized user and only for his tenant (RLS isolation through the profile).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.api import deps, repo
from src.api.config import CabinetSettings
from src.api.public import geo_cache
from src.api.public.i18n import GEO, TENDER, public_context
from src.api.public.routes_geo import MIN_TENDERS_FOR_PAGE
from src.api.templating import templates
from src.api.public.sources_view import fill_sources
from src.api.public.trial_text import fill_trial
from src.domain.actuality import no_deadline_is_fresh
from src.domain.classifications import cpv_division
from src.domain.ids import canonical_uuid
from src.domain import ics
from src.domain.indexing import is_indexable
from src.domain.regions import okrug_label
from src.domain.seo_text import clean_seo_text
from src.domain.sources import HOME_COUNTRY, source_label
from src.domain.tz import to_local
from src.parsers.capabilities import deadline_has_time

router = APIRouter(tags=["public"])


def _tender_json_ld(tender, canonical_url: str) -> dict | None:
    """JSON-LD ``GovernmentService`` for the tender card — only the filled fields.

    Previously this was an ``Offer``: the customer was passed off as the ``seller``, the estimated value as the ``price``, the submission deadline as ``validThrough``. That is wrong semantics (the customer does not sell, the estimate is not the price of an offer, the submission deadline is not the validity term of a price). Only what ``Service`` has by meaning goes into the markup: the name, the card address, the id, the CPV category. The amount and the deadline remain visible facts on the page, but they are not passed off as properties of the service. The tenant’s AI analysis (``analysis``) never gets here — it is private and tenant-scoped (RLS).
    """
    name = clean_seo_text(tender.title)
    if not name:
        return None
    # The description does NOT go here: the raw tender.description carries the service
    # marker `### file name` (see src/domain/description_render.py) and is
    # not HTML-escaped — the trap for an XSS through a document’s file name lives there too
    # (tests/api/test_tender_description_markers.py). These fields
    # already have their own safe render path (description_html) — a second, raw one,
    # we do not create in the JSON-LD.
    data: dict = {
        "@context": "https://schema.org",
        "@type": "GovernmentService",
        "name": name,
        "url": canonical_url,
        "identifier": str(tender.id),
    }
    if tender.classifications:
        for c in tender.classifications:
            if isinstance(c, dict) and c.get("scheme") == "CPV" and c.get("code"):
                label = c.get("label")
                data["category"] = f"{c['code']} {label}" if label else c["code"]
                break
    return data


def _tender_breadcrumb(base: str, lang: str, name: str, canonical_url: str) -> dict:
    """``BreadcrumbList`` Home → Tenderi → tender (TASK geo_landings, step 5).

    The same ``GEO`` catalog / the same path ``/tenderi`` as the hub and the sections (:mod:`src.api.public.routes_geo`) — a single chain across the whole public site.
    """
    t = GEO[lang]
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
        {"@type": "ListItem", "position": 3, "name": name, "item": canonical_url},
    ]
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": items,
    }


async def _related_links(db, tender, base: str, lang: str) -> list[dict]:
    """Interlinking to the pages of step 2 (TASK geo_landings) — only to the sections that really exist: a section / region below the threshold :data:`MIN_TENDERS_FOR_PAGE` gives a 404, and a link to it would be a defect rather than interlinking."""
    t = TENDER[lang]
    cpv_stats = await geo_cache.get_cpv_stats(db)
    reg_stats = await geo_cache.get_region_stats(db)
    links: list[dict] = []
    seen_div: set[str] = set()
    for c in tender.classifications or []:
        if not isinstance(c, dict) or c.get("scheme") != "CPV":
            continue
        code = c.get("code")
        label = c.get("label")
        division = cpv_division(code) if code else None
        if not division or division in seen_div or not label:
            continue
        entry = cpv_stats.get(division)
        if entry is None or entry["total"] < MIN_TENDERS_FOR_PAGE:
            continue
        seen_div.add(division)
        links.append(
            {
                "url": f"{base}/tenderi/cpv/{division}",
                "label": t["more_cpv"].format(name=label),
            }
        )
    for code in tender.regions or []:
        entry = reg_stats.get(code)
        if entry is None or entry["total"] < MIN_TENDERS_FOR_PAGE:
            continue
        links.append(
            {
                "url": f"{base}/tenderi/region/{code}",
                "label": t["more_region"].format(name=okrug_label(code)),
            }
        )
    return links


def _canonical_redirect(request: Request, path: str) -> RedirectResponse:
    """301 to the canonical address; the query string is kept (language, UTM)."""
    query = request.url.query
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=301)


@router.get("/tender/{tender_id}.ics")
async def tender_ics(
    tender_id: str,
    session_factory: Callable = Depends(deps.get_session_factory),
    settings: CabinetSettings = Depends(deps.get_settings),
):
    """“Add to calendar”: the bid deadline of one tender as .ics (RFC 5545).

    A plain GET: writes nothing. Public data — no session needed. Declared BEFORE ``/tender/{tender_id}``: that one would catch ``<id>.ics`` as an HTML page.
    404 — no tender, not a UUID, or no deadline stated (the event has no time to be at).
    """
    canon = canonical_uuid(tender_id)
    if canon is None:
        raise HTTPException(status_code=404)
    async with session_factory() as db:
        tender = await repo.get_tender(db, canon)
    if tender is None or tender.deadline_at is None:
        raise HTTPException(status_code=404)
    base = settings.cabinet_base_url.rstrip("/")
    event = ics.TenderEvent(
        tender_id=str(tender.id),
        title=clean_seo_text(tender.title),
        deadline=tender.deadline_at,
        url=f"{base}/tender/{tender.id}",
        customer=clean_seo_text(tender.customer_name) or None,
        has_time=deadline_has_time(tender.source_key),
    )
    body = ics.build_calendar([event], datetime.now(timezone.utc))
    return Response(
        content=body.encode("utf-8"),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="tender-{tender.id}.ics"',
            # The deadline file is not a page for search (a public URL without X-Robots-Tag).
            "X-Robots-Tag": "noindex",
        },
    )


@router.get("/tender/{tender_id}", response_class=HTMLResponse)
async def tender_page(
    request: Request,
    tender_id: str,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings: CabinetSettings = Depends(deps.get_settings),
):
    ctx = public_context(request)
    t = TENDER[ctx["lang"]]
    base = settings.cabinet_base_url.rstrip("/")
    # One canonical address of the card (SEO): urn:uuid/{…}/UPPER → 301 to str(UUID).
    # A non-UUID — 404 without going to the DB (``CAST`` would fall into a 500, production 07.10).
    canon = canonical_uuid(tender_id)
    if canon is not None and canon != tender_id:
        return _canonical_redirect(request, f"/tender/{canon}")
    async with session_factory() as db:
        tender = await repo.get_tender(db, canon) if canon is not None else None
        if tender is None:
            return templates.TemplateResponse(
                request,
                "message.html",
                {
                    **ctx,
                    "title": t["not_found_title"],
                    "message": t["not_found_text"],
                    "success": False,
                },
                status_code=404,
            )
        alts = await repo.count_canonical_alts(
            db, tender.canonical_tender_id, tender.id
        )
        analysis = None
        if session is not None:
            await repo.set_tenant(db, session["tid"])
            analysis = await repo.get_tender_ai_analysis(db, tender.id, session["tid"])
        related_links = await _related_links(db, tender, base, ctx["lang"])
        # The award decision — only a date that has passed (today in Belgrade time).
        awards = await repo.get_tender_awards(
            db, tender.id, to_local(datetime.now(timezone.utc)).date()
        )
        source_name = await source_label(db, getattr(tender, "source_key", None))

    # Canonical / og:url — from CABINET_BASE_URL, NOT from request.url_for: behind the proxy
    # uvicorn sees http, while production serves https (the project rules, fact 26.09.2026).
    canonical_url = f"{base}/tender/{tender.id}"
    offer = _tender_json_ld(tender, canonical_url)
    json_ld_list = (
        [offer, _tender_breadcrumb(base, ctx["lang"], offer["name"], canonical_url)]
        if offer
        else []
    )

    return templates.TemplateResponse(
        request,
        "tender.html",
        {
            **ctx,
            # "Data from the official source (<name>)" — by the source of THIS tender.
            "t": fill_trial(fill_sources(t, source_name), ctx["lang"]),
            "tender": tender,
            "source_name": source_name,
            # The country is shown only for foreign platforms: “Srbija” is the background.
            "foreign_country": (
                country
                if (country := (getattr(tender, "country_code", None) or "").strip())
                and country != HOME_COUNTRY
                else ""
            ),
            "alts": alts,
            # A tender without a deadline: “current” only if younger than 30 days (the same rule as in the feed).
            "no_deadline_fresh": no_deadline_is_fresh(
                getattr(tender, "published_at", None),
                getattr(tender, "first_seen_at", None),
                datetime.now(timezone.utc),
            ),
            "analysis": analysis,
            "authed": session is not None,
            "canonical_url": canonical_url,
            "json_ld": json_ld_list,
            "related_links": related_links,
            "awards": awards,
            # Closed > 90 days ago (and without dates) — outside the index, but the links lead further.
            "robots_noindex": not is_indexable(
                getattr(tender, "deadline_at", None),
                getattr(tender, "published_at", None),
                datetime.now(timezone.utc),
            ),
        },
    )
