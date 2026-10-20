"""FastAPI application (ADR-006).

Mounts the web cabinet (signup + magic-link + profiles + the Telegram channel binding, ADR-007), the public showcase (the landing ``/`` with multi-language sr / ru / en, SITE-01) and the public tender card ``/tender/{id}`` (Sprint-19). The health-check is preserved.

The branded 404 / 500 error pages (SITE-06) — for the browser; API clients (Accept without ``text/html``) get JSON, so as not to break the programmatic endpoints.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.ai import translation
from src.api import deps, security
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
from src.api.config import get_cabinet_settings
from src.api.public.i18n import public_context
from src.api.public import (
    routes_contact,
    routes_geo,
    routes_news,
    routes_public,
    routes_seo,
    routes_tender,
)
from src.api.static_assets import STATIC_DIR, VersionedStaticFiles
from src.api.templating import templates

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Fail-hard on a default / empty CABINET_SECRET_KEY in production (AUD-003 / FIX-04).
    # The call here = at uvicorn process start, before the first request is accepted.
    get_cabinet_settings()
    # Warm-up of the AI client: the SDK import (~3 s) is paid at start, not at the first click on “Translate”.
    # A warm-up error (no key, no package) must not bring down the start — translations just
    # go the ordinary way. Only the exception type goes to the log (we do not write PII / secrets).
    try:
        translation.warm_default_client()
    except Exception as exc:  # noqa: BLE001 — the warm-up is optional
        logger.warning("ai warm-up skipped (%s)", type(exc).__name__)
    yield


# /docs, /redoc, /openapi.json are closed in production (audit 07.10 SEC-02): the schema
# reveals /admin*, the billing webhook and checkout. For the dev stand — the flag
# API_DOCS_ENABLED=1 (default OFF). app.openapi() in the code remains available.
_DOCS_ON = os.environ.get("API_DOCS_ENABLED", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

app = FastAPI(
    title="TenderSRB API",
    version="0.2.0",
    lifespan=_lifespan,
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url="/redoc" if _DOCS_ON else None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
)


# The public showcase that HEAD serves as GET (read-only, with no side effects).
# An allow-list, not “all GETs”: the cabinet and auth GET routes CHANGE state
# (they burn the magic token, verify the email, end the session in /logout), and a mail
# scanner trying the link with HEAD would burn it before the client’s click. For them HEAD
# stays 405. The full list of GET routes with side effects is in
# docs/reports/TESTING_public-nav-small.md (section “GET audit”).
_HEAD_AS_GET_PATHS = re.compile(
    r"/"
    r"|/help|/izvori|/statistika|/novosti|/novosti/[^/]+|/kontakt|/plans|/tenderi"
    r"|/tenderi/cpv/[^/]+|/tenderi/region/[^/]+"
    r"|/tender/[^/]+|/legal/[^/]+"
    r"|/healthz|/robots\.txt|/llms\.txt|/favicon\.ico"
    r"|/security\.txt|/\.well-known/security\.txt"
    r"|/sitemap\.xml|/sitemap-pages\.xml|/sitemap-tenders-\d+\.xml"
)


class _HeadAsGet:
    """HEAD on the public showcase pages — as GET, but without a body (audit 07.10).

    FastAPI does not add HEAD to ``@router.get`` — production gave ``HEAD /`` → 405, although under HTTP a HEAD on a GET resource must give the same headers without a body. We rewrite the method to GET and strip the body here, at the ASGI level, ONLY for the paths of ``_HEAD_AS_GET_PATHS``. The other HEADs go as is (405 on the routes with a mutating GET, native handling in StaticFiles).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "HEAD"
            or not _HEAD_AS_GET_PATHS.fullmatch(scope["path"])
        ):
            await self.app(scope, receive, send)
            return

        async def send_without_body(message: Message) -> None:
            if message["type"] == "http.response.body":
                message = {**message, "body": b""}
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, send_without_body)


app.add_middleware(_HeadAsGet)

STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", VersionedStaticFiles(directory=str(STATIC_DIR)), name="static")

# The operator flag for the base.html header (the “Admin” link) — the resolution requires
# an async request of the email from the DB, so this is a dependency, not a context processor.
# We attach it EXACTLY to the routers whose templates inherit base.html: the showcase
# (public_base.html) draws the cabinet header for a logged-in user — the flag and whoami come for him
# from the separate _PUBLIC_DEPS (below), not from this bundle: an anonymous user makes no DB request.
# The criterion when adding a router — whether its template inherits base.html
# (regression: tests/api/test_admin_login.py::test_*_routers_resolve_admin_flag).
_CABINET_DEPS = [
    Depends(deps.resolve_platform_admin_flag),
    # The cabinet i18n chrome (Phase 1.1): resolves users.locale and marks the render as
    # cabinet (request.state.cabinet_user_locale). The same criterion as the admin flag:
    # we attach it to the routers whose templates inherit base.html.
    Depends(deps.resolve_cabinet_locale),
    # The identification of the current session in the header (whoami, owner 20.09.2026): email +
    # the tenant name. The same criterion as the admin flag and the locale — the routers
    # whose templates inherit base.html.
    Depends(deps.resolve_cabinet_whoami),
]

# require_active_access closes the CONTENT (the feed, tender cards, “My tenders”,
# customer analytics, the library, all their exports) ONLY on the user’s deletion
# (users.deleted_at) and on a DB error. The freeze (access_until expired) does not close the content
# (owner 10.10) — see the docstring of deps.require_active_access.
# It is NOT attached to the return paths — login, subscription, trial, language, referrals, billing,
# the showcase, admin: the frozen user must have a way to pay. The dashboard `/app`
# (routes_profiles) also stays open — it draws the banner “access expired”, and
# the gate there hangs point-wise on `/app/feed` (the feed = content), see routes_profiles.
_CONTENT_DEPS = _CABINET_DEPS + [Depends(deps.require_active_access)]

# The showcase draws the cabinet header for a logged-in user (#576 / #580): whoami + “Admin”.
# The dependency resolves ONLY with a session — an anonymous user makes no DB request.
_PUBLIC_DEPS = [Depends(deps.resolve_public_authed_header)]

app.include_router(routes_public.router, dependencies=_PUBLIC_DEPS)
app.include_router(routes_contact.router, dependencies=_PUBLIC_DEPS)
app.include_router(routes_news.router, dependencies=_PUBLIC_DEPS)
app.include_router(routes_seo.router)
app.include_router(routes_auth.router, dependencies=_CABINET_DEPS)
app.include_router(routes_profiles.router, dependencies=_CABINET_DEPS)
# routes_my_tenders — BEFORE routes_tender_ai: GET /app/tenders/calendar.ics must
# reach its own route, not /app/tenders/{tender_id} (the first match wins).
app.include_router(routes_my_tenders.router, dependencies=_CONTENT_DEPS)
app.include_router(routes_tender_ai.router, dependencies=_CONTENT_DEPS)
app.include_router(routes_narucilac.router, dependencies=_CONTENT_DEPS)
app.include_router(routes_analitika.router, dependencies=_CONTENT_DEPS)
app.include_router(routes_library.router, dependencies=_CONTENT_DEPS)
app.include_router(routes_channels.router, dependencies=_CABINET_DEPS)
app.include_router(routes_trial.router, dependencies=_CABINET_DEPS)
app.include_router(routes_subscription.router, dependencies=_CABINET_DEPS)
app.include_router(routes_account.router, dependencies=_CABINET_DEPS)
app.include_router(routes_locale.router, dependencies=_CABINET_DEPS)
app.include_router(routes_tender.router, dependencies=_PUBLIC_DEPS)
app.include_router(routes_geo.router, dependencies=_PUBLIC_DEPS)
app.include_router(routes_admin.router, dependencies=_CABINET_DEPS)
app.include_router(routes_billing.router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe — used by Caddy / monitoring and the docker healthcheck."""
    return {"status": "ok"}


def _wants_html(request: Request) -> bool:
    """A browser (Accept contains ``text/html``) → the branded page; otherwise JSON."""
    return "text/html" in request.headers.get("accept", "")


def _error_page_context(request: Request, title_key: str, body_key: str) -> dict:
    """The context of the branded error page: the language and the “home” link.

    On an error the route dependencies may not have run (404), or may have run and managed to set the language (500 in the middle of a cabinet route) — therefore we read the session ourselves. With a session: the language is from the signed session (``users.locale``), otherwise the dependency managed to set it; the link goes to the cabinet ``/app``. Without a session (and without a language) — the SHOWCASE contour (``?lang=`` → cookie → Accept-Language → sr), the link — ``/`` (the showcase home, F-13: “Na početnu” led to the login). Previously ``cabinet_user_locale`` was overwritten to ``None`` unconditionally, and a 500 for a logged-in Russian-speaking user was in Serbian.
    """
    settings = get_cabinet_settings()
    session = security.verify_session(
        request.cookies.get(deps.SESSION_COOKIE),
        settings.cabinet_secret_key,
        max_age=settings.session_max_age,
    )
    locale = None
    if session is not None:
        locale = session.get("locale") or getattr(
            request.state, "cabinet_user_locale", None
        )
    request.state.cabinet_user_locale = locale
    request.state.cabinet_anon_public_lang = locale is None
    ctx = {
        "title_key": title_key,
        "body_key": body_key,
        "success": False,
        "home_href": "/app" if session is not None else "/",
    }
    if session is None:
        # An anonymous user sees the header and the footer of the showcase (Tenders / Plans / Sources / Contact),
        # rather than a cabinet header with only “Login / Sign up” (audit 07.10).
        ctx.update(public_context(request), public_chrome=True)
    return ctx


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Exception):
    """404 on public / cabinet paths — the branded page (SITE-06).

    To programmatic clients (Accept without ``text/html``) we give JSON, so as not to break the API endpoints. The stack trace is not exposed.
    """
    if _wants_html(request):
        # message.html inherits base.html, and its chrome now goes through the i18n
        # catalog (`t`). On a 404 the route dependencies do not run, so we mark
        # the render as cabinet manually — otherwise the header / footer would come out without strings.
        return templates.TemplateResponse(
            request,
            "message.html",
            _error_page_context(request, "msg_404_title", "msg_404_body"),
            status_code=404,
        )
    return JSONResponse({"detail": "Not Found"}, status_code=404)


@app.exception_handler(Exception)
async def server_error_handler(request: Request, exc: Exception):
    """An unhandled error → 500 without a stack trace (SITE-06).

    To the browser — the branded page; to the programmatic client — JSON. The concrete HTTP errors (401 / 403 / 422) are handled by their own handlers and do not reach here.
    """
    if _wants_html(request):
        # See the 404 handler: we mark the render as cabinet, so that the header / footer
        # base.html get the i18n catalog `t`.
        return templates.TemplateResponse(
            request,
            "message.html",
            _error_page_context(request, "msg_500_title", "msg_500_body"),
            status_code=500,
        )
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
