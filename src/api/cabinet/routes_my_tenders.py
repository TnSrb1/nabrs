"""Workflow / retention routes of the cabinet (VG-17): folders, notes, watching.

- ``GET  /app/my`` — the “My tenders” page: folders with tenders + the deadline funnel, grouped by stage (``?stage=`` — a filter to one stage). Folder management (create / delete) — with plain POST forms.
- ``GET  /app/my/export.csv`` — the same selection in CSV (F2), assembled by the shared contour :mod:`src.api.cabinet.my_tenders_export`.
- HTMX actions from the feed / card (swap container ``_tender_actions.html``, one id per tender — ``#actions-{tender_id}``):
    * ``POST /app/tenders/{tender_id}/watch``  (form ``watch=on|off``) — watching;
    * ``POST /app/tenders/{tender_id}/status`` (form ``status``) — the funnel stage (all 7 from ``STATUSES``, parity with the bot: one table for both);
    * ``POST /app/tenders/{tender_id}/note``   (form ``body``) — a note;
    * ``POST /app/tenders/{tender_id}/folders/{folder_id}/add|remove`` — a folder.
  Each POST mutates and returns the re-rendered partial (the current state). The actions are tender-centric — ``profile_id`` is not needed (unlike the AI artifacts).

Require-session (like ``routes_tender_ai``): anonymous on GET → /login; on POST via HTMX → 401 + ``HX-Redirect``. All the logic is in
:mod:`src.api.cabinet.workflow_service` (RLS via ``repo.set_tenant``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.api import deps, repo
from src.api.config import CabinetSettings
from src.api.cabinet.i18n import resolve_authed_lang
from src.api.cabinet.tender_path import canonical_tender_id
from src.domain import ics
from src.domain.ids import canonical_uuid
from src.domain.seo_text import clean_seo_text
from src.parsers.capabilities import deadline_has_time
from src.api.cabinet import my_tenders_export as csv_export
from src.api.cabinet import workflow_service as svc
from src.api.templating import templates
from src.db.models.tender_status import STATUSES

router = APIRouter(tags=["cabinet"])


def _redirect_login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def _hx_login() -> HTMLResponse:
    """Anonymous on an HTMX request: 401 + HX-Redirect to /login."""
    resp = HTMLResponse("", status_code=401)
    resp.headers["HX-Redirect"] = "/login"
    return resp


async def _render_actions(
    request: Request,
    *,
    session_factory: Callable,
    tid: str,
    tender_id: str,
) -> HTMLResponse:
    """Re-renders ``_tender_actions.html`` with the tender’s current state."""
    async with session_factory() as db:
        await repo.set_tenant(db, tid)
        state = await svc.get_tender_state(db, tenant_id=tid, tender_id=tender_id)
        folders = await svc.list_folders(db, tid)
    return templates.TemplateResponse(
        request,
        "_tender_actions.html",
        {"tender_id": tender_id, "state": state, "folders": folders},
    )


# --------------------------------------------------------------------------- #
# The “My tenders” page
# --------------------------------------------------------------------------- #


@router.get("/app/my", response_class=HTMLResponse)
async def my_tenders(
    request: Request,
    stage: str | None = Query(None),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """“My tenders” + a filter by funnel stage (``?stage=active``).

    The filter chips are counted over the FULL aggregate (otherwise, after filtering, the subscriber would lose the links to the other stages), and the list is over the filtered one.
    """
    if session is None:
        return _redirect_login()
    tid = session["tid"]
    async with session_factory() as db:
        await repo.set_tenant(db, tid)
        data = await svc.get_my_tenders(db, tid)
    active_stage = stage if stage in STATUSES else None
    filtered = svc.filter_by_stage(data, active_stage)
    return templates.TemplateResponse(
        request,
        "my_tenders.html",
        {
            "data": filtered,
            # The funnel board (Stage 2) — a presentation slice of the same aggregate,
            # a pure function: the page makes no new DB query.
            "board": svc.build_funnel_board(filtered, active_stage),
            "all_stages": data.stages,
            "active_stage": active_stage,
            # We count over the FULL aggregate: the export gives everything, not the current
            # filter — otherwise on an empty stage the link would vanish, although there is
            # something to export.
            "has_any": bool(data.folders or data.funnel),
        },
    )


@router.get("/app/my/export.csv")
async def my_tenders_csv(
    request: Request,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """CSV export of “My tenders” (F2) — the same set that is visible on /app/my.

    A direct download by link (not HTMX), so anonymous → /login (303), as on the page itself. Tenant scope — ``set_tenant`` as the first action inside the service; the export can give out nothing but one’s own tenders. An empty list is a valid CSV of headers, not a 404 / 500.
    """
    if session is None:
        return _redirect_login()
    async with session_factory() as db:
        rows = await svc.get_my_tenders_export(db, session["tid"])
    # The file goes to the client → the header and stages are in the language of their cabinet (the same axis
    # that the chrome resolves).
    lang = resolve_authed_lang(
        request, getattr(request.state, "cabinet_user_locale", None)
    )
    return Response(
        content=csv_export.build_my_tenders_csv(rows, lang),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{csv_export.CSV_FILENAME}"'
        },
    )


@router.get("/app/tenders/calendar.ics")
async def my_tenders_calendar(
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings: CabinetSettings = Depends(deps.get_settings),
):
    """“Svi rokovi (.ics)”: active / watched tenders with a future deadline.

    A plain GET. A direct download link — anonymous → /login (303), as for the CSV.
    ‼️ Declared in this module, and the module is mounted ABOVE routes_tender_ai
    (main.py): otherwise ``/app/tenders/{tender_id}`` would swallow ``calendar.ics``.
    """
    if session is None:
        return _redirect_login()
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        rows = await svc.get_calendar_deadlines(db, session["tid"], now)
    base = settings.cabinet_base_url.rstrip("/")
    events = [
        ics.TenderEvent(
            tender_id=r.tender_id,
            title=clean_seo_text(r.title),
            deadline=r.deadline_at,
            url=f"{base}/tender/{r.tender_id}",
            customer=clean_seo_text(r.customer_name) or None,
            has_time=deadline_has_time(r.source_key),
        )
        for r in rows
    ]
    return Response(
        content=ics.build_calendar(events, now).encode("utf-8"),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="tenders-rokovi.ics"',
            "X-Robots-Tag": "noindex",
        },
    )


# --------------------------------------------------------------------------- #
# Folder management (plain forms from the “My tenders” page)
# --------------------------------------------------------------------------- #


@router.post("/app/folders")
async def create_folder(
    request: Request,
    name: str = Form(""),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _redirect_login()
    async with session_factory() as db:
        await svc.create_folder(db, tenant_id=session["tid"], name=name)
    return RedirectResponse(url="/app/my", status_code=303)


@router.post("/app/folders/{folder_id}/delete")
async def delete_folder(
    request: Request,
    folder_id: str,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _redirect_login()
    async with session_factory() as db:
        await svc.delete_folder(db, tenant_id=session["tid"], folder_id=folder_id)
    return RedirectResponse(url="/app/my", status_code=303)


# --------------------------------------------------------------------------- #
# HTMX actions from the feed / card
# --------------------------------------------------------------------------- #


@router.post("/app/tenders/{tender_id}/watch", response_class=HTMLResponse)
async def toggle_watch(
    request: Request,
    tender_id: str,
    watch: str = Form("on"),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _hx_login()
    # POST: 301 won’t do (the browser would repeat the form as a GET) — the canonical is taken here.
    canon = canonical_tender_id(tender_id)
    if canon is None:
        return Response(status_code=404)
    tender_id = canon
    tid = session["tid"]
    async with session_factory() as db:
        await svc.set_watch(
            db, tenant_id=tid, tender_id=tender_id, watch=(watch == "on")
        )
    return await _render_actions(
        request, session_factory=session_factory, tid=tid, tender_id=tender_id
    )


@router.post("/app/tenders/{tender_id}/status", response_class=HTMLResponse)
async def set_status(
    request: Request,
    tender_id: str,
    status: str = Form(...),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """Sets the funnel stage from the cabinet — the same 7 as in the bot (VG-17w).

    The neighbouring ``/watch`` remains the “follow” toggle (watching↔new) and works as before; this route is an explicit choice of the stage, including ``active``, under which the reminders worker sends reminders to Telegram.

    A value outside ``STATUSES`` → 400 without a write (the service checks it): it cannot be put into the form, so it is either a broken client or an attempt to slip in garbage.
    """
    if session is None:
        return _hx_login()
    # POST: 301 won’t do (the browser would repeat the form as a GET) — the canonical is taken here.
    canon = canonical_tender_id(tender_id)
    if canon is None:
        return Response(status_code=404)
    tender_id = canon
    tid = session["tid"]
    async with session_factory() as db:
        ok = await svc.set_status(db, tenant_id=tid, tender_id=tender_id, status=status)
    if not ok:
        return HTMLResponse("Неизвестная стадия", status_code=400)
    return await _render_actions(
        request, session_factory=session_factory, tid=tid, tender_id=tender_id
    )


@router.post("/app/tenders/{tender_id}/note", response_class=HTMLResponse)
async def save_note(
    request: Request,
    tender_id: str,
    body: str = Form(""),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _hx_login()
    # POST: 301 won’t do (the browser would repeat the form as a GET) — the canonical is taken here.
    canon = canonical_tender_id(tender_id)
    if canon is None:
        return Response(status_code=404)
    tender_id = canon
    tid = session["tid"]
    async with session_factory() as db:
        await svc.save_note(db, tenant_id=tid, tender_id=tender_id, body=body)
    return await _render_actions(
        request, session_factory=session_factory, tid=tid, tender_id=tender_id
    )


@router.post(
    "/app/tenders/{tender_id}/folders/{folder_id}/add", response_class=HTMLResponse
)
async def add_to_folder(
    request: Request,
    tender_id: str,
    folder_id: str,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _hx_login()
    # POST: 301 won’t do (the browser would repeat the form as a GET) — the canonical is taken here.
    canon = canonical_tender_id(tender_id)
    if canon is None:
        return Response(status_code=404)
    tender_id = canon
    # folder_id — the same class: raw into CAST(... AS uuid) → 500 (#632).
    folder_canon = canonical_uuid(folder_id)
    if folder_canon is None:
        return Response(status_code=404)
    tid = session["tid"]
    async with session_factory() as db:
        await svc.add_to_folder(
            db, tenant_id=tid, folder_id=folder_canon, tender_id=tender_id
        )
    return await _render_actions(
        request, session_factory=session_factory, tid=tid, tender_id=tender_id
    )


@router.post(
    "/app/tenders/{tender_id}/folders/{folder_id}/remove",
    response_class=HTMLResponse,
)
async def remove_from_folder(
    request: Request,
    tender_id: str,
    folder_id: str,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _hx_login()
    # POST: 301 won’t do (the browser would repeat the form as a GET) — the canonical is taken here.
    canon = canonical_tender_id(tender_id)
    if canon is None:
        return Response(status_code=404)
    tender_id = canon
    # folder_id — the same class: raw into CAST(... AS uuid) → 500 (#632).
    folder_canon = canonical_uuid(folder_id)
    if folder_canon is None:
        return Response(status_code=404)
    tid = session["tid"]
    async with session_factory() as db:
        await svc.remove_from_folder(
            db, tenant_id=tid, folder_id=folder_canon, tender_id=tender_id
        )
    return await _render_actions(
        request, session_factory=session_factory, tid=tid, tender_id=tender_id
    )
