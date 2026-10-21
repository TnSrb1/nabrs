"""The public marketing and legal routes of the showcase (TASK 1/2, SITE-01/04/05).

The landing GET / and the legal pages GET /legal/{name} with multi-language sr / ru / en.
GET /plans — the public showcase of the plans (VG-12, step 2).
POST /plans/upgrade — a pending request for an upgrade (VG-12, step 3).
GET /help — the help center of the showcase (VG-30a), multi-language sr / ru / en.
GET /izvori — the showcase of the sources: what each enabled source gives.
GET /statistika — public procurement statistics (aggregates, 1 h cache).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from src.api import admin_notify, deps
from src.api.cabinet import trial_service
from src.api.config import CabinetSettings
from src.api.emailer import EmailSender
from src.api.public.i18n import (
    HELP,
    LANDING,
    PLANS,
    SOURCES,
    STATS,
    TENDER,
    public_context,
    seo_canonical_and_hreflang,
)
from src.api.public.i18n import persist_lang as _persist_lang
from src.api.public import geo_cache
from src.api.public.home_ai_example import HOME_AI_EXAMPLE
from src.api.public.sources_page import SourceCard, get_source_cards
from src.api.public import stats_page
from src.api.public.trial_text import fill_trial
from src.api.static_assets import static_url
from src.api.public.sources_view import (
    SourcesView,
    fill_sources,
    get_sources_view,
    placeholders_for,
)
from src.api.public.legal_texts import LEGAL, get_doc
from src.api.templating import templates
from src.api.cabinet.library_sectors import cpv_label
from src.domain.text_norm import cyr_to_lat
from src.api.tiers import effective_tier as _effective_tier
from src.billing.registry import billing_configured
from src.domain.tz import calendar_days_until
from src.i18n_common import COMMON
from src.plans.catalog import (
    ENTERPRISE_CONTACT_EMAIL,
    comparison_matrix,
    display_plans,
    enterprise_plan,
    showcase_feature_matrix,
    tier_to_plan_key,
)

# The registry of the legal document names — a predicate over the catalog (src/api/public/legal_texts),
# not a separate list: /sitemap-pages.xml reuses this same source.
LEGAL_DOC_NAMES: tuple[str, ...] = tuple(LEGAL.keys())

#: The public link to the product bot (the JSON-LD ``sameAs`` of the home page).
TELEGRAM_BOT_URL = "https://t.me/nabrs_bot"


def _organization_and_website_json_ld(base: str) -> list[dict]:
    """Organization + WebSite (the landing) — only real fields: favicon.svg as the logo (an existing asset), the e-mail — the only public contact address in the catalog (ENTERPRISE_CONTACT_EMAIL)."""
    return [
        {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": "TenderSRB",
            "url": base,
            "logo": f"{base}{static_url('favicon.svg')}",
            "email": ENTERPRISE_CONTACT_EMAIL,
            # The product bot — docs/REGISTRY.md (@nabrs_bot; the username in Telegram
            # is case-insensitive, BOT_USERNAME=Nabrs_bot — the same account).
            "sameAs": [TELEGRAM_BOT_URL],
        },
        {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "TenderSRB",
            "url": base,
        },
    ]


def _plans_json_ld(base: str, lang: str) -> list[dict]:
    """SoftwareApplication with an Offer by plans — the prices from the catalog, not as a literal.

    Enterprise (``price_eur is None``, “on request”) does not get into ``offers`` — it has no real price, rather than an invented number. Free (internal, not on the showcase) does not get an Offer: the trial of N days is in the ``description``, not a “free price €0” (owner’s decision 30.09.2026).
    """
    offers = [
        {
            "@type": "Offer",
            "name": plan.key,
            "price": str(plan.price_eur),
            "priceCurrency": "EUR",
            "url": f"{base}/plans",
        }
        for plan in display_plans()
        if isinstance(plan.price_eur, int)
    ]
    return [
        {
            "@context": "https://schema.org",
            "@type": "SoftwareApplication",
            "name": "TenderSRB",
            "applicationCategory": "BusinessApplication",
            "url": f"{base}/plans",
            "description": fill_trial(PLANS[lang]["trial_banner"], lang),
            "offers": offers,
        }
    ]


def _help_faq_json_ld(
    lang: str, names: str = "", placeholders: dict[str, str] | None = None
) -> list[dict]:
    """FAQPage — only if HELP really has a section ``faq`` with questions."""
    sections = cast(
        list[dict],
        fill_trial(fill_sources(HELP[lang]["sections"], names, placeholders), lang),
    )
    for section in sections:
        if section.get("id") != "faq":
            continue
        items = section.get("faq_items") or []
        if not items:
            return []
        return [
            {
                "@context": "https://schema.org",
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": item["q"],
                        "acceptedAnswer": {"@type": "Answer", "text": item["a"]},
                    }
                    for item in items
                ],
            }
        ]
    return []


_TENANT_TIER_SQL = text(
    "SELECT tier, status FROM tenants WHERE id = CAST(:tid AS uuid)"
)

_UPGRADEABLE_PLANS = frozenset({"pro", "business"})

router = APIRouter(tags=["public"])


logger = logging.getLogger(__name__)


def _count_label(n: int, lang: str, key: str) -> str:
    """“N sources” / “N countries” by the language rule: ru / sr — 1 / 2–4 / other, en — 1 / other."""
    if lang == "en":
        form = "one" if n == 1 else "many"
    elif 11 <= n % 100 <= 14:
        form = "many"
    elif n % 10 == 1:
        form = "one"
    elif 2 <= n % 10 <= 4:
        form = "few"
    else:
        form = "many"
    return str(LANDING[lang][f"{key}_{form}"]).format(n=n)


@router.get("/", response_class=HTMLResponse)
async def landing(
    request: Request,
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> HTMLResponse:
    """The showcase’s marketing landing (multi-language, default sr).

    The facts block (open now / updated / source) and the card example — from the cache of the ``/tenderi`` hub (:func:`geo_cache.get_landing_facts`). A DB error does not bring down the home page: the blocks are simply not drawn.
    """
    ctx = public_context(request)
    canonical, hreflang = seo_canonical_and_hreflang(request, settings.cabinet_base_url)
    facts: dict | None = None
    sources_view = SourcesView()
    try:
        async with session_factory() as db:
            sources_view = await get_sources_view(db)
            facts = await geo_cache.get_landing_facts(db)
    except Exception as exc:  # noqa: BLE001 — the home page lives without the figures
        logger.warning("landing facts unavailable: %s", type(exc).__name__)
    updated_at = facts.get("updated_at") if facts else None
    facts_updated_today = updated_at is not None and (
        calendar_days_until(updated_at, datetime.now(timezone.utc)) == 0
    )
    response = templates.TemplateResponse(
        request,
        "landing.html",
        {
            **ctx,
            "t": fill_trial(
                fill_sources(
                    LANDING[ctx["lang"]],
                    sources_view.names,
                    sources_view.placeholders(ctx["lang"]),
                ),
                ctx["lang"],
            ),
            "tender_t": TENDER[ctx["lang"]],
            "facts": facts,
            # The coverage in the facts row: “K izvora · M zemalja” — no data → None, no fragment.
            "facts_sources_text": (
                _count_label(facts["sources_count"], ctx["lang"], "facts_sources")
                if facts and facts.get("sources_count")
                else None
            ),
            "facts_countries_text": (
                _count_label(facts["countries_count"], ctx["lang"], "facts_countries")
                if facts and facts.get("countries_count")
                else None
            ),
            "ai_example": HOME_AI_EXAMPLE,
            "facts_updated_today": facts_updated_today,
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
            "json_ld_list": _organization_and_website_json_ld(
                settings.cabinet_base_url.rstrip("/")
            ),
        },
    )
    _persist_lang(request, response)
    return response


@router.get("/help", response_class=HTMLResponse)
async def help_center(
    request: Request,
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> HTMLResponse:
    """The showcase help center (VG-30a): available both to the anonymous user and from the cabinet.

    The list of sources (the “Izvori” section and ``{sources}`` in the texts) — from the ``sources`` table; a DB error does not bring down the help: the list is simply not drawn.
    """
    ctx = public_context(request)
    sources_view = SourcesView()
    try:
        async with session_factory() as db:
            sources_view = await get_sources_view(db)
    except (
        Exception
    ) as exc:  # noqa: BLE001 — the help lives without the list of sources
        logger.warning("help sources unavailable: %s", type(exc).__name__)
    canonical, hreflang = seo_canonical_and_hreflang(request, settings.cabinet_base_url)
    response = templates.TemplateResponse(
        request,
        "help.html",
        {
            **ctx,
            "t": fill_trial(
                fill_sources(
                    HELP[ctx["lang"]],
                    sources_view.names,
                    sources_view.placeholders(ctx["lang"]),
                ),
                ctx["lang"],
            ),
            "sources_view": sources_view,
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
            "json_ld_list": _help_faq_json_ld(
                ctx["lang"],
                sources_view.names,
                sources_view.placeholders(ctx["lang"]),
            ),
        },
    )
    _persist_lang(request, response)
    return response


@router.get("/izvori", response_class=HTMLResponse)
async def sources_page(
    request: Request,
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> HTMLResponse:
    """The showcase of the sources: the enabled sources and what each gives (owner 02.10.2026).

    A DB error does not bring down the page — an honest empty state is drawn (as for the help).
    """
    ctx = public_context(request)
    cards: tuple[SourceCard, ...] = ()
    try:
        async with session_factory() as db:
            cards = await get_source_cards(db)
    except Exception as exc:  # noqa: BLE001 — the page lives without the list
        logger.warning("izvori sources unavailable: %s", type(exc).__name__)
    canonical, hreflang = seo_canonical_and_hreflang(request, settings.cabinet_base_url)
    response = templates.TemplateResponse(
        request,
        "izvori.html",
        {
            **ctx,
            "t": SOURCES[ctx["lang"]],
            # The labels of the capabilities and countries — the common catalog with the profile form.
            "ct": COMMON[ctx["lang"]],
            "cards": cards,
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
        },
    )
    _persist_lang(request, response)
    return response


def _cpv_division_name(division: str, lang: str) -> str:
    """The name of the CPV section (the root ``XX000000``) in the showcase language.

    The ``cpv_codes.json`` reference gives sr in Cyrillic; the showcase sr is in Latin. Not in the reference → the section code as is (the fallback is explicit).
    """
    name = cpv_label(f"{division}000000", lang)
    if not name:
        return division
    return cyr_to_lat(name) if lang == "sr" else name


@router.get("/statistika", response_class=HTMLResponse)
async def statistika_page(
    request: Request,
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
) -> HTMLResponse:
    """Public procurement statistics (R3 ANALYTICS_PRODUCT, owner 10.10.2026).

    The aggregates come from the cache of :mod:`stats_page` (TTL 1 h, under ``statement_timeout``); a DB failure does not bring down the page — an honest empty state is drawn.
    """
    ctx = public_context(request)
    lang = ctx["lang"]
    stats = await stats_page.get_public_stats(session_factory)
    cpv_names = {}
    if stats is not None:
        cpv_names = {r.key: _cpv_division_name(r.key, lang) for r in stats.cpv_top}
    canonical, hreflang = seo_canonical_and_hreflang(request, settings.cabinet_base_url)
    response = templates.TemplateResponse(
        request,
        "statistika.html",
        {
            **ctx,
            "t": fill_trial(STATS[lang], lang),
            "stats": stats,
            "cpv_names": cpv_names,
            # The labels of the procedures — the common vocabulary pt_<code> (as in the email / Telegram),
            # src.workers.adapters.base.procedure_type_label); an unknown one — as is.
            "proc_label": lambda code: (
                COMMON[lang].get(f"pt_{code.lower()}", code)
                if code
                else STATS[lang]["proc_none"]
            ),
            "fmt_eur": stats_page.fmt_eur,
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
        },
    )
    _persist_lang(request, response)
    return response


@router.get("/legal/{name}", response_class=HTMLResponse)
async def legal(
    request: Request,
    name: str,
    settings: CabinetSettings = Depends(deps.get_settings),
) -> HTMLResponse:
    """The legal page (privacy / terms / impressum) in the chosen language of the showcase."""
    ctx = public_context(request)
    doc = get_doc(name, ctx["lang"])
    if doc is None:
        raise HTTPException(status_code=404)
    canonical, hreflang = seo_canonical_and_hreflang(request, settings.cabinet_base_url)
    response = templates.TemplateResponse(
        request,
        "legal.html",
        {
            **ctx,
            "doc": doc,
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
        },
    )
    _persist_lang(request, response)
    return response


@router.get("/plans", response_class=HTMLResponse)
async def plans_page(
    request: Request,
    session: Optional[dict] = Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings: CabinetSettings = Depends(deps.get_settings),
) -> HTMLResponse:
    """The showcase of the plan plans (public, multi-language sr / ru / en)."""
    ctx = public_context(request)
    lang = ctx["lang"]
    canonical, hreflang = seo_canonical_and_hreflang(request, settings.cabinet_base_url)

    current_plan_key: Optional[str] = None
    if session is not None:
        try:
            async with session_factory() as db:
                row = (
                    await db.execute(_TENANT_TIER_SQL, {"tid": str(session["tid"])})
                ).first()
            if row is not None:
                et = _effective_tier(row.status, row.tier)
                current_plan_key = tier_to_plan_key(et)
        except Exception:
            pass

    upgrade_status = request.query_params.get("upgrade")

    response = templates.TemplateResponse(
        request,
        "plans.html",
        {
            **ctx,
            "t": fill_trial(
                fill_sources(
                    PLANS[lang], "", await placeholders_for(session_factory, lang)
                ),
                lang,
            ),
            "plans": display_plans(),
            "enterprise": enterprise_plan(),
            "feature_matrix": showcase_feature_matrix,
            "comparison_rows": comparison_matrix(),
            "current_plan_key": current_plan_key,
            "plans_authed": session is not None,
            "billing_configured": billing_configured(settings),
            "enterprise_email": ENTERPRISE_CONTACT_EMAIL,
            "upgrade_status": upgrade_status,
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
            "json_ld_list": _plans_json_ld(settings.cabinet_base_url.rstrip("/"), lang),
        },
    )
    _persist_lang(request, response)
    return response


@router.post("/plans/upgrade")
async def plans_upgrade(
    request: Request,
    session: Optional[dict] = Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings=Depends(deps.get_settings),
    email_sender: EmailSender = Depends(deps.get_email_sender),
) -> RedirectResponse:
    """A pending request for a plan upgrade (test mode, no payment)."""
    if session is None:
        return RedirectResponse("/signup", status_code=303)

    form = await request.form()
    plan = (form.get("plan") or "").strip().lower()
    if plan not in _UPGRADEABLE_PLANS:
        return RedirectResponse("/plans", status_code=303)

    comment = f"upgrade_request:{plan}"
    async with session_factory() as db:
        result = await trial_service.create_trial_request(
            db, tenant_id=session["tid"], user_id=session["uid"], comment=comment
        )

    # Admin notification on a successful creation: Telegram (if the bot is configured) and
    # email to the operators — independently (a failure of one channel does not cancel the second).
    if result.ok:
        if settings.tg_bot_token and settings.admin_chat_id:
            await trial_service.notify_admin(
                token=settings.tg_bot_token,
                admin_chat_id=settings.admin_chat_id,
                request_id=result.request_id,
                email=result.email,
                comment=comment,
            )
        when = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        admin_url = settings.cabinet_base_url.rstrip("/") + "/admin/requests"
        await admin_notify.notify_admins_email(
            email_sender=email_sender,
            settings=settings,
            subject="Новая заявка на апгрейд плана",
            text_body="\n".join(
                [
                    f"Новая заявка на апгрейд плана: {plan}.",
                    f"Email заявителя: {result.email or '—'}",
                    f"Комментарий: {comment or '—'}",
                    f"Время: {when}",
                    f"Заявки: {admin_url}",
                ]
            ),
        )

    status = "requested" if result.ok else result.reason
    return RedirectResponse(f"/plans?upgrade={status}", status_code=303)
