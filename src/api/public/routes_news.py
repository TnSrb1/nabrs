"""Product news: GET /novosti, GET /novosti/{slug}, POST /novosti/procitano.

The content is :mod:`src.api.public.news` (no DB). “Read” is the cookie ``news_seen`` with the date of the freshest viewed news item (no table): it is set by viewing ``/novosti`` and by the “close” button on the dashboard banner (a plain form, no inline JS — CSP). The “Novo” mark in the cabinet menu is computed by ``templating._auth_context``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api import deps
from src.api.config import CabinetSettings
from src.api.public import news
from src.api.public.i18n import NOVOSTI, public_context, seo_canonical_and_hreflang
from src.api.public.i18n import persist_lang as _persist_lang
from src.api.templating import templates

router = APIRouter(tags=["public"])


def _mark_seen(response: Response) -> None:
    response.set_cookie(
        news.SEEN_COOKIE,
        news.latest_date().isoformat(),
        max_age=news.SEEN_COOKIE_MAX_AGE,
        samesite="lax",
        httponly=True,
        path="/",
    )


@router.get("/novosti", response_class=HTMLResponse)
async def novosti_list(
    request: Request,
    settings: CabinetSettings = Depends(deps.get_settings),
) -> HTMLResponse:
    ctx = public_context(request)
    canonical, hreflang = seo_canonical_and_hreflang(request, settings.cabinet_base_url)
    response = templates.TemplateResponse(
        request,
        "novosti.html",
        {
            **ctx,
            "t": NOVOSTI[ctx["lang"]],
            "items": news.NEWS,
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
        },
    )
    _persist_lang(request, response)
    _mark_seen(response)
    return response


@router.get("/novosti/{slug}", response_class=HTMLResponse)
async def novosti_item(
    request: Request,
    slug: str,
    settings: CabinetSettings = Depends(deps.get_settings),
) -> HTMLResponse:
    item = news.NEWS_BY_SLUG.get(slug)
    if item is None:
        raise HTTPException(status_code=404)
    ctx = public_context(request)
    lang = ctx["lang"]
    base = settings.cabinet_base_url.rstrip("/")
    canonical, hreflang = seo_canonical_and_hreflang(request, base)
    response = templates.TemplateResponse(
        request,
        "novosti_item.html",
        {
            **ctx,
            "t": NOVOSTI[lang],
            "item": item,
            "item_title": item.title[lang],
            "item_body": item.body[lang],
            "item_link_label": item.link_label[lang],
            "seo_canonical_url": canonical,
            "seo_hreflang_links": hreflang,
            "json_ld_list": [
                {
                    "@context": "https://schema.org",
                    "@type": "NewsArticle",
                    "headline": item.title[lang],
                    "datePublished": item.published.isoformat(),
                    "inLanguage": lang,
                    "mainEntityOfPage": canonical,
                    "publisher": {"@type": "Organization", "name": "TenderSRB"},
                }
            ],
        },
    )
    _persist_lang(request, response)
    _mark_seen(response)
    return response


def _safe_return(path: str) -> str:
    """Only a local path (not ``//host`` and not a scheme) — otherwise /app."""
    if path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/app"


@router.post("/novosti/procitano")
async def novosti_mark_read(return_to: str = Form("/app")) -> RedirectResponse:
    """Close the “Novo” banner: the “read” cookie and back to the same page."""
    response = RedirectResponse(_safe_return(return_to), status_code=303)
    _mark_seen(response)
    return response
