"""The FastAPI dependencies of the cabinet (Sprint-19).

The providers by default give the live implementations; in tests they are substituted via ``app.dependency_overrides`` (ADR-013, without live external APIs). The session is a signed cookie (:mod:`src.api.security`); on every tenant request the route does ``set_tenant`` through the services (ADR-002).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, Response

from src.api import admin_access
from src.api import anti_abuse as aa
from src.api import security
from sqlalchemy import text

from src.api.config import CabinetSettings, get_cabinet_settings
from src.api.emailer import EmailSender, make_email_sender
from src.db.session import session_scope, set_current_tenant
from src.domain.access import is_frozen

logger = logging.getLogger(__name__)

SESSION_COOKIE = "tb_session"


def get_settings() -> CabinetSettings:
    return get_cabinet_settings()


def get_session_factory() -> Callable:
    """A factory of async sessions (by default — the shared ``session_scope``)."""
    return session_scope


_redis_client: Any | None = None


def get_redis() -> Any | None:
    """The shared async Redis client — the cache of the tender card translation (VG-26).

    One client per process (the connection pool is inside), created lazily: importing ``redis.asyncio`` is not needed by the tests that do not touch Redis.

    ``None`` (no ``REDIS_URL`` / the package is unavailable) is a legal outcome, not an error: translation without a cache is not done at all (it would burn tokens on every display); the card gives the original. There is no default to localhost — a silent connection into nowhere would take longer to cure than this log takes to read.
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = os.getenv("REDIS_URL")
    if not url:
        logger.warning(
            "REDIS_URL is not set — the card translation cache is disabled (VG-26)"
        )
        return None
    try:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(url)
    except Exception as exc:  # noqa: BLE001 — the cache is not critical, the card lives
        logger.warning(
            "The Redis client was not created (%s) — translation is disabled",
            type(exc).__name__,
        )
        return None
    return _redis_client


def get_turnstile(
    settings: CabinetSettings = Depends(get_settings),
) -> aa.TurnstileVerifier:
    return aa.LiveTurnstileVerifier(settings.turnstile_secret_key)


def get_email_validator(
    settings: CabinetSettings = Depends(get_settings),
) -> aa.EmailValidator:
    return aa.KickboxValidator(settings.kickbox_api_key)


def get_email_sender(
    settings: CabinetSettings = Depends(get_settings),
) -> EmailSender:
    """The mail transport by priority: SMTP → Postmark → Mock (STAGE 2)."""
    return make_email_sender(
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_user=settings.smtp_user,
        smtp_password=settings.smtp_password,
        smtp_from=settings.smtp_from,
        postmark_token=settings.postmark_server_token,
        email_from=settings.email_from,
    )


def get_optional_session(
    request: Request,
    settings: CabinetSettings = Depends(get_settings),
) -> Optional[dict]:
    """Decodes the session cookie. ``None`` — anonymous."""
    raw = request.cookies.get(SESSION_COOKIE)
    return security.verify_session(
        raw, settings.cabinet_secret_key, max_age=settings.session_max_age
    )


def set_session_cookie(
    response: Response,
    uid: str,
    tid: str,
    settings: CabinetSettings,
    *,
    locale: str | None = None,
) -> None:
    token = security.sign_session(uid, tid, settings.cabinet_secret_key, locale=locale)
    # Secure only on the public domain: on localhost (dev over http) a Secure cookie
    # would be dropped by the browser and would break the local login (STAGE 2).
    secure = settings.cookie_secure and settings.domain != "localhost"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=settings.session_max_age,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


ADMIN_LOGIN_PATH = "/admin/login"
ADMIN_HOME_PATH = "/admin"


async def resolve_platform_admin_flag(
    request: Request,
    session: Optional[dict] = Depends(get_optional_session),
    session_factory: Callable = Depends(get_session_factory),
    settings: CabinetSettings = Depends(get_settings),
) -> bool:
    """The operator flag: for the base.html header and for the :func:`require_admin` gate.

    It is attached at the application level, because the header is drawn by every cabinet page; FastAPI caches it within the request, so the resolution happens once per request. An anonymous user and an empty allowlist are cut off in :func:`admin_access.is_platform_admin` without a DB round-trip.

    A resolution error (DB unavailable) → False, not a 500: the dependency hangs on all routes, including the public ones that did not touch the DB for this before. For the gate, False means 403 — fail-closed is preserved.
    """
    try:
        flag = await admin_access.is_platform_admin(session, session_factory, settings)
    except Exception:  # noqa: BLE001 — the resolution must not bring down other pages
        logger.warning("platform-admin resolve failed, treating as non-operator")
        flag = False
    request.state.is_platform_admin = flag
    return flag


_USER_LOCALE_SQL = text(
    "SELECT locale FROM users WHERE id = CAST(:uid AS uuid) AND deleted_at IS NULL"
)


async def resolve_cabinet_locale(
    request: Request,
    session: Optional[dict] = Depends(get_optional_session),
    session_factory: Callable = Depends(get_session_factory),
) -> None:
    """Puts the saved ``users.locale`` into ``request.state`` for the cabinet’s i18n chrome.

    It hangs next to :func:`resolve_platform_admin_flag` on the routers whose templates inherit ``base.html`` (``_CABINET_DEPS``): the cabinet chrome catalog is substituted by the context processor :func:`src.api.templating._auth_context`, which needs the user’s language with priority over the cookie (``resolve_authed_lang``).

    The locale is carried by the signed session ITSELF (``session["locale"]``, set at login and at a language change) — it can be trusted the same way as uid / tid. We go to the DB ONLY for old cookies issued before this field (there is no ``locale`` key in the payload): we read from the DB under the GUC of one’s own tenant (ADR-002: ``set_tenant`` FIRST as an action); backward compatibility is mandatory. Anonymous / DB error / empty field → ``None``: the resolver then falls back to the cookie → Accept-Language → ``ru``. The presence of the attribute ``request.state.cabinet_user_locale`` is the marker that this is a cabinet render: only then does the processor inject ``t`` / ``lang`` (otherwise it would override the public ``t`` that the showcase passes from its own catalog).
    """
    locale: str | None = None
    if session is not None:
        locale = session.get("locale")
        # An old cookie without the locale field → a one-off fallback to the DB (not on every render:
        # fresh sessions carry the locale in the signature and do not come here).
        if locale is None:
            try:
                async with session_factory() as db:
                    await set_current_tenant(db, session["tid"])
                    row = (
                        await db.execute(_USER_LOCALE_SQL, {"uid": session["uid"]})
                    ).first()
                    locale = getattr(row, "locale", None) if row is not None else None
            except (
                Exception
            ):  # noqa: BLE001 — the language resolution must not bring down the page
                logger.warning("cabinet locale resolve failed, falling back to request")
                locale = None
    request.state.cabinet_user_locale = locale


_WHOAMI_SQL = text(
    "SELECT u.email AS whoami_email, t.display_name AS whoami_tenant_name, "
    "t.access_until AS whoami_access_until, "
    "t.tier AS whoami_tier, t.status AS whoami_status, "
    # The “renewal pending” banner — only for the renewal request (marker
    # extension_request, see domain.access), not for any pending one (a plan is pending too).
    "EXISTS(SELECT 1 FROM trial_requests r WHERE r.tenant_id = t.id "
    "AND r.status = 'pending' AND r.comment LIKE 'extension_request%') "
    "AS whoami_has_pending "
    "FROM users u JOIN tenants t ON t.id = u.tenant_id "
    "WHERE u.id = CAST(:uid AS uuid) AND t.id = CAST(:tid AS uuid) "
    "AND u.deleted_at IS NULL LIMIT 1"
)


async def resolve_cabinet_whoami(
    request: Request,
    session: Optional[dict] = Depends(get_optional_session),
    session_factory: Callable = Depends(get_session_factory),
) -> None:
    """Puts the email + (if any) the tenant name of the current session into ``request.state``.

    The cabinet header (``base.html``) shows “who am I” — the owner’s complaint of 20.09.2026: a client logs in under several accounts and cannot check which one they are in the cabinet right now. The session carries only ``uid`` / ``tid`` (not the email), so one SELECT under the GUC of one’s own tenant is needed (ADR-002: ``set_tenant`` FIRST).

    Anonymous / DB error → both fields ``None``, the header does not draw the identity line (fail-open on display, not on access — this is a display of what is already known, not a new way to identify the user).
    """
    email: str | None = None
    tenant_name: str | None = None
    frozen = False
    pending = False
    nav_access = None
    if session is not None:
        try:
            async with session_factory() as db:
                await set_current_tenant(db, session["tid"])
                row = (
                    await db.execute(
                        _WHOAMI_SQL, {"uid": session["uid"], "tid": session["tid"]}
                    )
                ).first()
                if row is not None:
                    email = row.whoami_email
                    tenant_name = row.whoami_tenant_name
                    frozen = is_frozen(
                        row.whoami_access_until, datetime.now(timezone.utc)
                    )
                    pending = bool(row.whoami_has_pending)
                    # The status badge and the plan marks in the menu — from the same gates
                    # that close the pages (nav_access), without a second query.
                    from src.api.nav_access import resolve_nav_access

                    nav_access = resolve_nav_access(
                        row.whoami_status,
                        row.whoami_tier,
                        row.whoami_access_until,
                        datetime.now(timezone.utc),
                    )
        except (
            Exception
        ):  # noqa: BLE001 — the identification must not bring down the page
            logger.warning("cabinet whoami resolve failed, header omits identity")
    request.state.cabinet_whoami_email = email
    request.state.cabinet_whoami_tenant_name = tenant_name
    # The banner “subscription ended” on all cabinet pages (owner 10.10): access has expired →
    # the cabinet is open, paid features are closed. A resolution error = no banner (this is a display,
    # not access: the paid gates count is_frozen themselves).
    request.state.cabinet_access_frozen = frozen
    request.state.cabinet_extension_pending = pending
    request.state.cabinet_nav_access = nav_access


async def resolve_public_authed_header(
    request: Request,
    session: Optional[dict] = Depends(get_optional_session),
    session_factory: Callable = Depends(get_session_factory),
    settings: CabinetSettings = Depends(get_settings),
) -> None:
    """whoami + “Admin” in the cabinet header on the public pages for a logged-in user (#580).

    The showcase (``public_base.html``) draws for a logged-in user the same header as the cabinet (``_cabinet_nav.html``, #576), and the data for it — email / tenant and the operator flag — used to be resolved only on the ``_CABINET_DEPS`` routers. Here — the same resolvers, but ONLY when a session is present: an anonymous user (all search and advertising traffic) goes through without a single DB query. The ``cabinet_user_locale`` marker is NOT set — it switches ``t`` to the cabinet catalog and would break the public pages (see :func:`resolve_cabinet_locale`).
    """
    if session is None:
        return
    await resolve_platform_admin_flag(request, session, session_factory, settings)
    await resolve_cabinet_whoami(request, session, session_factory)


async def use_public_lang_for_anon(request: Request) -> None:
    """Marks the screen as “entered from the showcase”: for an ANONYMOUS user the language is resolved as on the showcase.

    It hangs on the signup / login / resend forms (``routes_auth``). The client comes to them directly from the public pages, so the default must match: the cabinet ``ru`` default gave a gap — the showcase gave ``sr`` for the same request, while the signup form gave ``ru`` (audit 2026-08-02, HIGH-2; confirmed on production).

    ONLY the language detection changes — the string catalog stays the cabinet one (``sgn_*`` / ``log_*`` / ``rsn_*``). A logged-in user is not touched: for them the language is taken by ``users.locale`` / the signed session, and the priority is the same as before (see :func:`src.api.templating._auth_context`).

    ``async`` on purpose: a synchronous FastAPI dependency is moved to the threadpool on EVERY request, and the work here is one assignment.
    """
    request.state.cabinet_anon_public_lang = True


def cabinet_login_url(request: Request) -> str:
    """The cabinet login URL with a return to the requested page (``?next=``).

    Without it the “Connect email” / “Create template” link of a logged-out user led to ``/login``, and after login to ``/app``: the target was lost. The sample is ``/cabinet/telegram`` and :func:`admin_login_url`. The return is made by the browser as a GET → a non-GET request has no return. The open redirect is closed twice: here (:func:`security.safe_next_path` — only a relative path) and on the intake of ``/login`` / ``/auth/verify`` by the same function.
    """
    if request.method != "GET":
        return "/login"
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    safe = security.safe_next_path(target)
    if not safe:
        return "/login"
    return f"/login?next={quote(safe, safe='')}"


def admin_login_url(request: Request) -> str:
    """The operator login URL with a return to the requested admin path.

    The return is made by the browser as a GET, so the path of a non-GET request cannot be the return (there is no GET handler for /admin/users/{uid}/grant — it would return to a 405). Such a request is redirected to the admin root.
    """
    if request.method != "GET":
        return f"{ADMIN_LOGIN_PATH}?next={quote(ADMIN_HOME_PATH, safe='')}"
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    target = security.safe_next_path(target) or ADMIN_HOME_PATH
    return f"{ADMIN_LOGIN_PATH}?next={quote(target, safe='')}"


async def require_admin(
    request: Request,
    session: Optional[dict] = Depends(get_optional_session),
    is_admin: bool = Depends(resolve_platform_admin_flag),
    settings: CabinetSettings = Depends(get_settings),
) -> dict:
    """Allows access only to platform operators from the allowlist.

    Three different outcomes — do not confuse them:
    - no session → 303 to ``/admin/login`` with a return (a bare 403 read as “broken”, not as “log in”);
    - a session exists, but the email is not in the allowlist → 403. We do not reveal the existence of the admin to an outsider and do not call login;
    - the allowlist is empty → fail-closed, 403 to everyone, including an anonymous user.

    An anonymous HTMX request stays 403: XHR expects a fragment, not the HTML of the login form.

    ``is_admin`` is resolved by the same dependency that draws the link in the header — FastAPI caches it within the request, so there is no second DB trip.
    """
    if not admin_access.parse_allowlist(settings):
        raise HTTPException(status_code=403, detail="Admin access required")
    if session is None:
        if request.headers.get("hx-request"):
            raise HTTPException(status_code=403, detail="Authentication required")
        raise HTTPException(
            status_code=303,
            detail="Authentication required",
            headers={"Location": admin_login_url(request)},
        )
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return session


# Where a revoked / deleted subscriber goes. NOT the cabinet hub: the hub is itself closed
# ``require_active_access`` → a 303 to the hub would loop the revoked user (until owner 10.10
# here stood ``/cabinet/subscription``, which was not gated itself; now it is a 303 to the
# hub). The public ``/plans`` requires no session and gives no loop.
SUBSCRIPTION_PATH = "/plans"

# One SELECT for both axes of access revocation: the freeze of the tenant (``tenants.access_until``)
# and the soft deletion of the user (``users.deleted_at``, which the login resolvers already respect —
# ``app_resolve_user_for_auth`` — but nobody checked an issued cookie).
# A JOIN without a link condition is deliberate: the user is looked up by its own id from the signature
# of the session, the tenant by its own; there will be no rows if either side is invisible under the
# GUC (RLS) or physically deleted — that is fail-closed, see below. asyncpg → CAST.
_ACCESS_GATE_SQL = text("""
    SELECT t.access_until AS access_until, u.deleted_at AS deleted_at
    FROM tenants t
    JOIN users u ON u.id = CAST(:uid AS uuid)
    WHERE t.id = CAST(:tid AS uuid)
    LIMIT 1
    """)


async def require_active_access(
    request: Request,
    session: Optional[dict] = Depends(get_optional_session),
    session_factory: Callable = Depends(get_session_factory),
) -> Optional[dict]:
    """Closes the CABINET CONTENT to a revoked / deleted subscriber; for a frozen one — it stays OPEN.

    The owner’s decision of 10.10.2026: after the access has ended (``access_until`` expired, ``is_frozen``) the delivery stops, but the entry to the cabinet remains — the feed, cards, “My tenders”, templates, the account are open, and on every page hangs a banner “subscription ended” with a button for a free renewal (``base.html``). The paid / AI paths are closed by their own gates on ``is_frozen`` (``matching``, ``tender_ai_generate``, ``customer_analytics_service``…) — this change does NOT touch them (they spend the API key). Until 10.10 a frozen user here got a 303 to ``/cabinet/subscription``.

    The session is self-sufficient (HMAC, 14 days), nobody went to the DB for the status, and there was nothing to revoke an issued cookie with, except changing ``CABINET_SECRET_KEY`` (which logs out EVERYONE). This resolution IS the status check on a live cookie — for a disconnected subscriber (``users.deleted_at``) and for a vanished user / tenant pair.

    Outcomes — three, not two:
    - no session (anonymous) → ``None`` and NOT A SINGLE request to the DB: we do not build a second authorization contour; the anonymous user gets /login from the route itself;
    - the user is deleted / disconnected (``users.deleted_at IS NOT NULL``) or the pair is absent → 303 to ``/plans`` (the mechanism as in :func:`require_admin`); for an HTMX request — 401 + ``HX-Redirect`` to the same path, exactly as ``routes_tender_ai._hx_login`` does for an anonymous user: XHR expects a fragment, not the body of a redirect;
    - otherwise (the access is alive OR frozen) → the session, and the route proceeds.

    A DB error → close (fail-closed). This is DELIBERATELY stricter than :func:`resolve_platform_admin_flag` (there an error = “not an operator”, and it hangs on the public routes): opening the content because of an unavailable database is worse than showing the subscription page.

    RLS: ``set_current_tenant`` FIRST as the transaction’s action (ADR-002). We hold the DB session for EXACTLY one SELECT — the gate hangs on every content route, and holding the connection for the duration of the render would exhaust the pool.
    """
    if session is None:
        return None
    try:
        async with session_factory() as db:
            await set_current_tenant(db, session["tid"])
            row = (
                await db.execute(
                    _ACCESS_GATE_SQL,
                    {"tid": session["tid"], "uid": session["uid"]},
                )
            ).first()
        allowed = row is not None and getattr(row, "deleted_at", None) is None
    except Exception:  # noqa: BLE001 — fail-closed: an error does not open the content
        logger.warning("access gate resolve failed, closing cabinet content")
        allowed = False
    if allowed:
        return session
    if request.headers.get("hx-request"):
        raise HTTPException(
            status_code=401,
            detail="Access expired",
            headers={"HX-Redirect": SUBSCRIPTION_PATH},
        )
    raise HTTPException(
        status_code=303,
        detail="Access expired",
        headers={"Location": SUBSCRIPTION_PATH},
    )
