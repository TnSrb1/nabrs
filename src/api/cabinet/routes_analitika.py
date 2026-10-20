"""Routes of “Analitika” — the report builder with Excel export (R4).

``GET /app/analitika`` — the page: anonymous → 303 /login; the page is always 200, the gate (Business / Enterprise or trial, freeze first) decides the content — the report or an empty state with a CTA to the plans. The contract is the same as for ``/cabinet/narucilac`` (routes_narucilac).

``GET /app/analitika.xlsx`` — direct download: gate / freeze → 303 /plans, a request timeout → 303 to the page with ``timeout=1`` (a hint without a repeated request). All logic is in :mod:`src.api.cabinet.analitika_service`.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.api import deps
from src.api.cabinet import analitika_service as svc
from src.api.cabinet.i18n import resolve_authed_lang
from src.api.templating import templates

router = APIRouter(tags=["cabinet"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _lang(request: Request) -> str:
    return resolve_authed_lang(
        request, getattr(request.state, "cabinet_user_locale", None)
    )


@router.get("/app/analitika", response_class=HTMLResponse)
async def analitika_page(
    request: Request,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return RedirectResponse(url="/login", status_code=303)
    async with session_factory() as db:
        view = await svc.get_analitika_page(
            db,
            tid=session["tid"],
            params=dict(request.query_params),
            lang=_lang(request),
        )
    return templates.TemplateResponse(request, "analitika.html", {"page": view})


@router.get("/app/analitika.xlsx")
async def analitika_xlsx(
    request: Request,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return RedirectResponse(url="/login", status_code=303)
    params = dict(request.query_params)
    async with session_factory() as db:
        export = await svc.get_analitika_export(
            db, tid=session["tid"], params=params, lang=_lang(request)
        )
    if export.status == "success":
        return Response(
            content=export.content,
            media_type=XLSX_MEDIA_TYPE,
            headers={
                "Content-Disposition": f'attachment; filename="{export.filename}"'
            },
        )
    if export.status == "timeout":
        # Flag timeout=1: the page will show a hint without restarting the request.
        return RedirectResponse(
            url="/app/analitika?" + urlencode({**params, "timeout": "1"}),
            status_code=303,
        )
    return RedirectResponse(url="/plans", status_code=303)
