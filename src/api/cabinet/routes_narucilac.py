"""Route of the “Customer analytics” page in the cabinet (A3, VG-06 lite).

``GET /cabinet/narucilac`` — choosing a customer (search by name) and their profile: who wins, at what prices, how concentrated the market is. All the logic is in :mod:`src.api.cabinet.narucilac_page_service`; here only HTTP / render.

An anonymous user is redirected to ``/login`` (like the other cabinet routes). The Pro+ gate and the freeze are inside the service, by the same predicate as the bot and the tender page: the page always renders (200), and the gate decides what to show — the profile or an empty state with a CTA to the plans. There is no redirect to /plans here: the customer-analytics section on the tender page behaves the same way.
"""

from __future__ import annotations

import re

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.api import deps
from src.api.cabinet import narucilac_page_service as svc
from src.api.cabinet.i18n import resolve_authed_lang
from src.api.templating import templates

router = APIRouter(tags=["cabinet"])


def _not_found(request: Request) -> HTMLResponse:
    # KEYS of the catalog (message.html localises them via ``t``), not a ready text.
    return templates.TemplateResponse(
        request,
        "message.html",
        {
            "title_key": "msg_export_empty_title",
            "body_key": "msg_export_empty_body",
            "success": False,
            "home_href": "/app",
        },
        status_code=404,
    )


@router.get("/cabinet/narucilac", response_class=HTMLResponse)
async def narucilac_page(
    request: Request,
    q: str = "",
    name: str = "",
    sort: str = "",
    page: str = "1",
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """The “Customer analytics” page (ru-only, like the whole cabinet).

    The length of ``q`` / ``name`` is NOT validated on input (no ``Query(max_length=...)``): a 422 JSON instead of a page is a bad response to what the user pasted into the search — a long name. The service cuts it, the page stays a page. The same for ``sort`` (unknown → the default in the service) and ``page`` (<1 → 1).
    """
    if session is None:
        return RedirectResponse(url="/login", status_code=303)

    async with session_factory() as db:
        view = await svc.get_narucilac_page(
            db,
            tid=session["tid"],
            term=q,
            customer_name=name,
            sort=sort,
            # ASCII and at most 6 digits: "²" gave a ValueError, a huge number — OFFSET > bigint (500).
            page=int(page) if re.fullmatch(r"\d{1,6}", page.strip(), re.ASCII) else 1,
        )
    return templates.TemplateResponse(request, "narucilac.html", {"page": view})


@router.get("/cabinet/narucilac.csv")
async def narucilac_csv(
    request: Request,
    name: str = "",
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """CSV export of the profile of the selected customer (C4).

    A direct download by link (not HTMX); the contract is the same as for the export from the tender page (``/app/tenders/{id}/analytics.csv``): anonymous → /login (303), the gate and freeze → a redirect to /plans (a CTA, not a bare 403 — the user needs a path), no customer / no decisions → 404. The gate and ``set_tenant`` are inside the service, by the same predicate as the page: no own threshold is invented here.
    """
    if session is None:
        return RedirectResponse(url="/login", status_code=303)

    async with session_factory() as db:
        export = await svc.get_narucilac_export(
            db,
            tid=session["tid"],
            customer_name=name,
            locale=resolve_authed_lang(
                request, getattr(request.state, "cabinet_user_locale", None)
            ),
        )

    if export.status == "success":
        # The file name is ASCII-safe (see _safe_ascii in analytics_export).
        return Response(
            content=export.content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{export.filename}"'
            },
        )
    if export.status in ("gate_pro", "frozen"):
        return RedirectResponse(url="/plans", status_code=303)
    # no_customer | no_data — nothing to export.
    return _not_found(request)
