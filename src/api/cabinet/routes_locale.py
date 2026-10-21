"""Interface language switcher of the cabinet (Phase 1.1).

``POST /cabinet/locale`` — the form from the sticky header (three pills SR / RU / EN). It writes
the choice to ``users.locale`` (persistent, takes priority over the cookie) AND to the ``lang`` cookie
(so the language is picked up both before the next DB read and on the public site), then
returns to the same page — the chrome is re-rendered entirely in the new language.

Progressive enhancement: a plain POST form, works WITHOUT JS (the browser does a full navigation by itself via a 303 redirect). An htmx request is sent to the same address and redirected via
``HX-Redirect`` — without it htmx would swap in a fragment, while the language change touches the whole page.

RLS: ``set_tenant`` FIRST as the transaction’s first action (otherwise FORCE RLS on ``users``
returns 0 rows). The user edits ONLY their own row — by ``session["uid"]``,
never by a parameter from the request.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from src.api import deps, repo, security
from src.api.cabinet.i18n import LANG_COOKIE, SUPPORTED

router = APIRouter(tags=["cabinet"])

# 1 year — the same as the language cookie on the public site (routes_public._LANG_COOKIE_MAX_AGE).
_LANG_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# CAST per the asyncpg invariant (the project rules): only explicit CAST in text().
_UPDATE_LOCALE_SQL = text(
    "UPDATE users SET locale = CAST(:locale AS text), updated_at = now() "
    "WHERE id = CAST(:uid AS uuid)"
)


@router.post("/cabinet/locale")
async def set_locale(
    request: Request,
    lang: str = Form(...),
    return_to: str = Form("/app"),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings=Depends(deps.get_settings),
) -> RedirectResponse:
    """Save the user’s interface language and return to the same page."""
    if session is None:
        # An anonymous user has no users row to write to — to login (like the other cabinet routes).
        return RedirectResponse(url="/login", status_code=303)

    # Code validation: only a known language. A tampered / broken POST — a no-op redirect
    # back, without writing to the DB and without a cookie.
    if lang not in SUPPORTED:
        return RedirectResponse(
            url=security.safe_next_path(return_to) or "/app", status_code=303
        )

    async with session_factory() as db:
        # RLS: set_tenant FIRST — before writing tenant data.
        await repo.set_tenant(db, session["tid"])
        # We edit strictly OWN row — by uid from the signed session, not from the form.
        await db.execute(_UPDATE_LOCALE_SQL, {"locale": lang, "uid": session["uid"]})
        await db.commit()

    target = security.safe_next_path(return_to) or "/app"
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        LANG_COOKIE,
        lang,
        max_age=_LANG_COOKIE_MAX_AGE,
        samesite="lax",
        path="/",
    )
    # We re-issue the session cookie with the new locale: the chrome reads the language from the
    # signed session (deps.resolve_cabinet_locale), so without re-issuing the language change
    # would not be picked up until the next login.
    deps.set_session_cookie(
        response, session["uid"], session["tid"], settings, locale=lang
    )
    # htmx: a full reload instead of a fragment swap (the language changes the whole chrome).
    if request.headers.get("hx-request"):
        response.headers["HX-Redirect"] = target
    return response
