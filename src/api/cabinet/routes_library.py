"""Routes of the search-template library (EPIK 4).

GET /library — the showcase of ready-made filter sets: “show all”, then the industries (:mod:`library_sectors`: the industry package first, narrow templates after it). GET /library/{package|template}/{key} — the composition of the set. Each card has a LIVE figure “≈ N tenders in 30 days”, counted by the same predicate that the profile form preview will show (:mod:`src.api.cabinet.template_prefill` — one payload for both places). Anonymous → /login. The button leads to /profiles/new with the form already filled in.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api import deps, repo, tiers
from src.api.cabinet import library_counts
from src.api.cabinet import library_sectors
from src.api.cabinet import preview_service
from src.api.cabinet import profile_service
from src.api.cabinet import subscription_service
from src.api.cabinet import template_library_service as svc
from src.api.cabinet import template_prefill
from src.api.cabinet import vertical_packages
from src.api.cabinet.i18n import resolve_authed_lang
from src.api.templating import templates
from src.domain.text_norm import cyr_to_lat
from src.api.cabinet.source_choices import source_choice_rows
from src.domain.sources import default_source_keys_for_new_profile

logger = logging.getLogger(__name__)

router = APIRouter(tags=["cabinet"])


def _redirect_login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


async def showcase(db, tid: str, lang: str, q: str = "") -> dict:
    """Showcase data: industries (package + narrow templates), live figures, profile limit.

    Shared by ``/library`` and the first screen “Čime se bavite?” (``/profiles/new``):
    the same set of payloads in the same order — so the same :mod:`library_counts` cache key and the same figures on the package tile in both places.
    The layout by industries is :mod:`library_sectors` (one dictionary for both screens; the order does not depend on the language). ``q`` — search by name / words (the layout only; the figures are counted over the full set, the cache key does not change).
    """
    packages = vertical_packages.display_packages(lang)

    # template_library (0016) is not under FORCE RLS — set_tenant is not mandatory for
    # the current query, but we set it for uniformity / resilience to reordering.
    await repo.set_tenant(db, tid)
    # A template that duplicates a package (Serbian Latin “Medicinska oprema” ≈ its Cyrillic spelling) is not
    # shown: the data is not touched, the filter is only in the showcase. The filtered one does not get
    # into the counter payloads either — the order of the figures = the order of the cards.
    items = [
        t
        for t in await svc.list_public_templates(db)
        if not svc.duplicates_of_packages(t, vertical_packages.PACKAGES)
    ]

    # The profile limit BEFORE the refusal (owner 22.09.2026): the same logic as on
    # /cabinet/account — “X of Y in use” is visible BEFORE the click on a card, not only
    # after the create_profile refusal (tiers.PROFILE_LIMITS is not changed,
    # only shown).
    tier, status = await subscription_service.get_tenant_tier(db, tid)
    profile_used = len(await profile_service.list_profiles(db, tid))

    # The order of the payloads: “without a filter”, then packages, then templates. We count in ONE call
    # (one window scan for the whole showcase) and hand the figures out in this same
    # order; the layout by industries comes afterwards, on the ready cards.
    # The card figure is counted over the same sources the new-profile form will tick
    # (the `sources` registry, SRC-0a) — otherwise it would diverge from the preview.
    source_keys = await default_source_keys_for_new_profile(db)
    payloads = [template_prefill.unfiltered_payload(source_keys=source_keys)]
    payloads += [
        template_prefill.from_package(p, source_keys=source_keys)
        for p in vertical_packages.PACKAGES
    ]
    payloads += [
        template_prefill.from_template(t, source_keys=source_keys) for t in items
    ]
    counts = await library_counts.counts_for(db, payloads)

    def _count(index: int):
        """The card figure or None (counter unavailable — shown without a figure)."""
        return counts[index] if counts is not None else None

    # Effective plan (trial = Pro rights), as on the dashboard / account.
    profile_limit = tiers.profile_limit(tiers.effective_tier(status, tier))
    profile_limit_reached = profile_limit is not None and profile_used >= profile_limit

    for i, pkg in enumerate(packages):
        pkg["count"] = _count(1 + i)

    views = []
    for i, t in enumerate(items):
        view = svc.localized_view(t, lang)
        view["count"] = _count(1 + len(vertical_packages.PACKAGES) + i)
        # The industry is by the ORIGINAL column, not by the translation: otherwise the layout and the
        # order would depend on the interface language.
        view["source_kind"] = t.activity_kind
        views.append(view)

    return {
        "sectors": library_sectors.build_sectors(packages, views, q=q),
        # VG-14: vertical packages are data-as-code; the showcase is ru/sr/en by the cabinet locale.
        "packages": packages,
        # owner: an explicit entry into the “show all” state — the product could always do it,
        # but there was no entry point in the interface.
        "unfiltered_count": _count(0),
        "templates_total": len(items),
        "q": q,
        "profile_used": profile_used,
        "profile_limit": profile_limit,
        "profile_limit_reached": profile_limit_reached,
    }


@router.get("/library", response_class=HTMLResponse)
async def library(
    request: Request,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    q: str = "",
):
    if session is None:
        return _redirect_login()

    # The cabinet interface language (the same axis that the context processor _auth_context resolves
    # for t / lang of the base template) — previously it was hard-coded to "ru".
    lang = resolve_authed_lang(
        request, getattr(request.state, "cabinet_user_locale", None)
    )
    async with session_factory() as db:
        ctx = await showcase(db, session["tid"], lang, q=q[:100])
    return templates.TemplateResponse(request, "library.html", ctx)


#: How many fresh tenders the composition page shows (owner brief: 3).
DETAIL_EXAMPLES = 3


def _keyword_groups(groups) -> list[list[str]]:
    """OR groups as a list of strings: each string is the words that are ALL needed."""
    out = []
    for group in groups or []:
        terms = [t for t in (group or {}).get("all_of") or [] if t]
        if terms:
            out.append(terms)
    return out


def _cpv_title(code: str, lang: str) -> str | None:
    """The CPV name; in sr the reference is Cyrillic (`l_sr`) — shown in Latin on display."""
    label = library_sectors.cpv_label(code, lang)
    return cyr_to_lat(label) if label and lang == "sr" else label


def _cpv_rows(classifications, lang: str) -> list[dict]:
    cls = classifications if isinstance(classifications, dict) else {}
    rows = []
    for entry in cls.get("include") or []:
        code = (entry or {}).get("code") if isinstance(entry, dict) else None
        if code:
            rows.append({"code": code, "label": _cpv_title(str(code), lang)})
    return rows


@router.get("/library/{kind}/{key}", response_class=HTMLResponse)
async def library_detail(
    request: Request,
    kind: str,
    key: str,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """Composition of the template / package: all the words, CPV with names, stop words, region,
    “≈N in 30 days” and 3 fresh tenders.

    The figure and the examples are ONE pass of :func:`preview_service.preview_count` over the
    payload of :mod:`template_prefill` (the same as the card and the form): an example cannot fall outside the figure. The buttons lead to the existing prefill —
    “based on” goes straight into the full form, “as is” — to the confirmation screen.
    """
    if session is None:
        return _redirect_login()
    lang = resolve_authed_lang(
        request, getattr(request.state, "cabinet_user_locale", None)
    )
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        source_keys = await default_source_keys_for_new_profile(db)
        # The sources of the composition form: the same checkboxes as in the template form; RS are ticked
        # (as for a new profile), the rest — the subscriber switches on himself (owner 30.09).
        sources = await source_choice_rows(db, source_keys)
        if kind == "package":
            pkg = vertical_packages.get_package(key)
            if pkg is None:
                raise HTTPException(status_code=404)
            view = next(
                p for p in vertical_packages.display_packages(lang) if p["slug"] == key
            )
            payload = template_prefill.from_package(pkg, lang, source_keys=source_keys)
            item = {
                "name": view["name"],
                "description": view["description"],
                "sector": library_sectors.sector_of_package(key),
                # Readable package words, not the search stems (display_words).
                "groups": [[w] for w in view["keywords"]],
                # Catalog codes, not the payload prefixes (“72*”): we show
                # what the client will see in the CPV widget of the form.
                "cpv": _cpv_rows({"include": [{"code": c} for c in pkg.cpv]}, lang),
                "stop_words": list(pkg.keywords_exclude),
                "query": f"from_package={key}",
                "params": {"from_package": key},
            }
        elif kind == "template":
            tpl = await svc.get_template(db, key)
            if tpl is None:
                raise HTTPException(status_code=404)
            view = svc.localized_view(tpl, lang)
            payload = template_prefill.from_template(tpl, source_keys=source_keys)
            item = {
                "name": view["name"],
                "description": view["description"],
                "sector": library_sectors.sector_of_kind(tpl.activity_kind),
                # Words — in the SEARCH language, like the card tags (localized_view).
                "groups": _keyword_groups(view["keywords_include"]),
                "cpv": _cpv_rows(tpl.classifications, lang),
                # template_library templates have no stop words (there is no such column).
                "stop_words": [],
                "query": f"from_template={tpl.id}",
                "params": {"from_template": tpl.id},
            }
        else:
            raise HTTPException(status_code=404)

        tier, status = await subscription_service.get_tenant_tier(db, session["tid"])
        profile_used = len(await profile_service.list_profiles(db, session["tid"]))
        try:
            preview = await preview_service.preview_count(
                db, payload, examples_limit=DETAIL_EXAMPLES
            )
            count, examples = preview.count, preview.examples
        except Exception as exc:  # the composition page matters more than the counter
            logger.warning("library_detail preview failed: %s", type(exc).__name__)
            count, examples = None, []

    profile_limit = tiers.profile_limit(tiers.effective_tier(status, tier))
    return templates.TemplateResponse(
        request,
        "library_detail.html",
        {
            "item": item,
            "kind": kind,
            "source_choices": sources,
            "checked_keys": source_keys,
            "count": count,
            "examples": examples,
            "regions": payload.get("regions") or [],
            "profile_used": profile_used,
            "profile_limit": profile_limit,
            "profile_limit_reached": profile_limit is not None
            and profile_used >= profile_limit,
        },
    )
