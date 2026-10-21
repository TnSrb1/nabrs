"""Cabinet authentication routes (ARCHITECTURE §4, ADR-018 revised).

Passwordless flow: signup (+ anti-abuse) → email verify → magic-link login.
The business logic is in :mod:`src.api.cabinet.auth_service`; here only HTTP,
render and cookie. External collaborators are injected via :mod:`src.api.deps`.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api import admin_notify
from src.api import anti_abuse as aa
from src.api import deps, security
from src.api.cabinet import auth_service as svc
from src.api.cabinet import trial_service
from src.api.config import CabinetSettings
from src.api.emailer import EmailSender
from src.api.public.i18n import LANDING, persist_lang, resolve_lang
from src.api.public.trial_text import fill_trial
from src.api.rate_limit import FixedWindowRateLimiter
from src.api.templating import templates

router = APIRouter(tags=["auth"])

# The login forms are the client’s first screen: they arrive at them straight from the showcase.
# Therefore the language here is resolved by the SHOWCASE contour (resolve_lang, default sr), not
# the cabinet fallback ru (audit 2026-08-02, HIGH-2): previously the showcase gave sr for the
# same request, while /signup gave ru. The string catalog stays
# the cabinet one; only the language detection changes. The language choice in the pill arrives
# as ?lang= (GET links, as on the showcase) and is remembered by persist_lang.
_PUBLIC_LANG = [Depends(deps.use_public_lang_for_anon)]

# Anti-abuse for the magic-link request (STAGE 2, security minimum): in-memory,
# per api process. Limits independently by email and by IP; when exceeded no email is sent,
# but the response is the same (anti-enumeration). 3 per 15 min per email, 10 per 15 min per IP.
_MAGIC_LINK_PER_EMAIL = FixedWindowRateLimiter(max_hits=3, window_sec=15 * 60)
_MAGIC_LINK_PER_IP = FixedWindowRateLimiter(max_hits=10, window_sec=15 * 60)

# Re-registration request (#658): without a limit the form is a generator of emails to the operator.
_REREGISTER_PER_EMAIL = FixedWindowRateLimiter(max_hits=3, window_sec=15 * 60)
_REREGISTER_PER_IP = FixedWindowRateLimiter(max_hits=10, window_sec=15 * 60)

_RESEND_VERIFY_PER_EMAIL = FixedWindowRateLimiter(max_hits=3, window_sec=15 * 60)
_RESEND_VERIFY_PER_IP = FixedWindowRateLimiter(max_hits=10, window_sec=15 * 60)


def _link_not_accepted(
    request: Request,
    *,
    session: dict | None,
    target: str,
    title_key: str,
    body_key: str,
    next_path: str | None = None,
):
    """A token is not accepted (burnt / expired / someone else’s) — what to show the client.

    Single-use of the token remains; ONLY the outcome of the display changes (production 04.08.2026: the client logged in via the link, reopened the email and saw “Link invalid”, although the session in the browser was alive; re-opening is also done by mail scanners / preview bots).

    Two branches, both without an error page:
    - a live session in this browser → 303 to the cabinet: the person is already inside, we cannot show them a dead end;
    - anonymous → a page with a FORM to request a new link (submit to the existing POST /login), the text honest: the link is single-use and already used.

    A common point for BOTH routes (/auth/verify and /auth/magic) — the predicate “every point where a token failure draws a page must give one behavior”, otherwise half the scenario gets fixed and half does not.
    """
    if session is not None:
        return RedirectResponse(url=target, status_code=303)
    response = templates.TemplateResponse(
        request,
        "auth_link_used.html",
        {"title_key": title_key, "body_key": body_key, "next_path": next_path},
    )
    persist_lang(request, response)
    return response


def _trial_note(request: Request) -> str:
    """Promise line above the form: “N days of Pro free · email only, no card”."""
    lang = resolve_lang(request)
    return fill_trial(LANDING[lang]["trial_note"], lang)


@router.get("/signup", response_class=HTMLResponse, dependencies=_PUBLIC_LANG)
async def signup_form(
    request: Request, settings: CabinetSettings = Depends(deps.get_settings)
):
    response = templates.TemplateResponse(
        request,
        "signup.html",
        {
            "turnstile_site_key": settings.turnstile_site_key,
            "trial_note": _trial_note(request),
        },
    )
    persist_lang(request, response)
    return response


@router.post("/signup", response_class=HTMLResponse, dependencies=_PUBLIC_LANG)
async def signup_submit(
    request: Request,
    email: str = Form(""),
    cf_turnstile_response: str = Form("", alias="cf-turnstile-response"),
    consent: str = Form(""),
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
    turnstile: aa.TurnstileVerifier = Depends(deps.get_turnstile),
    email_validator: aa.EmailValidator = Depends(deps.get_email_validator),
    email_sender: EmailSender = Depends(deps.get_email_sender),
):
    # SITE-05: mandatory consent to the Privacy Policy / Terms — a server-side check
    # BEFORE any processing. It does not weaken anti-abuse (it cuts the request off before
    # the signup logic) and is not an enumeration vector (it does not depend on the email).
    if consent != "yes":
        response = templates.TemplateResponse(
            request,
            "signup.html",
            {
                "turnstile_site_key": settings.turnstile_site_key,
                "trial_note": _trial_note(request),
                "consent_error": True,
                "email": email,
            },
            status_code=400,
        )
        persist_lang(request, response)
        return response
    client_ip = request.client.host if request.client else None
    # The language of the chrome email and the locale of the new account follow the language choice on the site
    # (anonymous: ?lang= / cookie / Accept-Language; default sr, as on the showcase). The same
    # resolver as the page chrome uses — otherwise the account would be created with a language
    # different from the one the client saw the form in.
    lang = resolve_lang(request)
    async with session_factory() as session:
        result = await svc.signup(
            session,
            email=email,
            turnstile_token=cf_turnstile_response,
            turnstile=turnstile,
            email_validator=email_validator,
            email_sender=email_sender,
            settings=settings,
            remoteip=client_ip,
            locale=lang,
        )
    if result.outcome == svc.REJECTED_TURNSTILE:
        # A verification failure — the same form with the saved email, not a separate
        # page: the entered address is not lost, a retry is one click.
        response = templates.TemplateResponse(
            request,
            "signup.html",
            {
                "turnstile_site_key": settings.turnstile_site_key,
                "trial_note": _trial_note(request),
                "turnstile_error": True,
                "email": email,
            },
            status_code=400,
        )
        persist_lang(request, response)
        return response
    if result.outcome == svc.REJECTED_DUPLICATE:
        # “Address already taken” — the same for a live trial and a deleted account
        # (anti-enumeration, as before). New: the path forward — a request to the operator.
        response = templates.TemplateResponse(
            request,
            "signup_reregister.html",
            {"turnstile_site_key": settings.turnstile_site_key, "email": email},
        )
        persist_lang(request, response)
        return response
    success = result.outcome in (svc.TRIAL_STARTED, svc.VERIFICATION_REQUIRED)
    response = templates.TemplateResponse(
        request,
        "signup_result.html",
        {"message": result.message, "success": success, "outcome": result.outcome},
    )
    persist_lang(request, response)
    return response


@router.post(
    "/signup/reregister", response_class=HTMLResponse, dependencies=_PUBLIC_LANG
)
async def signup_reregister(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(""),
    cf_turnstile_response: str = Form("", alias="cf-turnstile-response"),
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
    turnstile: aa.TurnstileVerifier = Depends(deps.get_turnstile),
    email_sender: EmailSender = Depends(deps.get_email_sender),
):
    """A re-registration request for the address of a deleted account (owner 10.10, #658).

    The response is the SAME whether the request was created or not (the address was not deleted / a request is already waiting / the limit): otherwise by brute-forcing addresses one could learn which accounts were deleted. The operator notification runs in the background: a delay in the response must not reveal that a request was created (the same rule as the response being identical in text).
    """
    client_ip = request.client.host if request.client else None
    lang = resolve_lang(request)
    if not await turnstile.verify(cf_turnstile_response, remoteip=client_ip):
        response = templates.TemplateResponse(
            request,
            "signup_reregister.html",
            {
                "turnstile_site_key": settings.turnstile_site_key,
                "email": email,
                "turnstile_error": True,
            },
            status_code=400,
        )
        persist_lang(request, response)
        return response
    norm_email = security.normalize_email(email)
    email_ok = _REREGISTER_PER_EMAIL.hit(norm_email)
    ip_ok = _REREGISTER_PER_IP.hit(client_ip or "unknown")
    request_id = None
    if email_ok and ip_ok:
        async with session_factory() as session:
            request_id = await svc.request_reregistration(
                session, email=email, locale=lang
            )
    if request_id is not None:
        background_tasks.add_task(
            _notify_operator_reregister,
            settings=settings,
            email_sender=email_sender,
            email=norm_email,
            lang=lang,
        )
    response = templates.TemplateResponse(
        request, "signup_reregister.html", {"sent": True}
    )
    persist_lang(request, response)
    return response


async def _notify_operator_reregister(
    *, settings: CabinetSettings, email_sender: EmailSender, email: str, lang: str
) -> None:
    """Service notification to the operator (ru): Telegram and email independently, best-effort."""
    when = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    if settings.tg_bot_token and settings.admin_chat_id:
        await trial_service.send_admin_text(
            token=settings.tg_bot_token,
            admin_chat_id=settings.admin_chat_id,
            text=(
                "🆕 Запрос повторной регистрации\n"
                f"Email: {html.escape(email)}\n"
                f"Язык: {html.escape(lang)}\n"
                f"Время: {when}\n"
                "Решение — в админке: /admin/requests"
            ),
        )
    admin_url = settings.cabinet_base_url.rstrip("/") + "/admin/requests"
    await admin_notify.notify_admins_email(
        email_sender=email_sender,
        settings=settings,
        subject="Запрос повторной регистрации",
        text_body="\n".join(
            [
                "Запрос повторной регистрации после самоудаления аккаунта.",
                f"Email: {email}",
                f"Язык: {lang}",
                f"Время: {when}",
                f"Заявки: {admin_url}",
            ]
        ),
    )


@router.get("/auth/resend", response_class=HTMLResponse, dependencies=_PUBLIC_LANG)
async def resend_verification_form(request: Request):
    response = templates.TemplateResponse(request, "resend_verification.html", {})
    persist_lang(request, response)
    return response


@router.post("/auth/resend", response_class=HTMLResponse, dependencies=_PUBLIC_LANG)
async def resend_verification_submit(
    request: Request,
    email: str = Form(""),
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
    email_sender: EmailSender = Depends(deps.get_email_sender),
):
    client_ip = request.client.host if request.client else "unknown"
    norm_email = security.normalize_email(email)
    email_ok = _RESEND_VERIFY_PER_EMAIL.hit(norm_email)
    ip_ok = _RESEND_VERIFY_PER_IP.hit(client_ip)
    if email_ok and ip_ok:
        lang = resolve_lang(request)
        async with session_factory() as session:
            await svc.resend_verification(
                session,
                email=email,
                email_sender=email_sender,
                settings=settings,
                locale=lang,
            )
    # Anti-enumeration: always the same response.
    response = templates.TemplateResponse(
        request,
        "message.html",
        {
            "title_key": "authm_check_mail_title",
            "body_key": "authm_resend_sent_body",
            "success": True,
        },
    )
    persist_lang(request, response)
    return response


@router.get("/auth/verify", response_class=HTMLResponse, dependencies=_PUBLIC_LANG)
async def verify_email(
    request: Request,
    token: str = "",
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
    browser_session: dict | None = Depends(deps.get_optional_session),
    email_sender: EmailSender = Depends(deps.get_email_sender),
):
    async with session_factory() as session:
        sess = await svc.verify_email_token(
            session, raw_token=token, email_sender=email_sender
        )
        # Idempotency (anti-prefetch): a mail scanner may have opened the link first —
        # burnt the token and confirmed the account. The real click arrives at the
        # burnt token (sess=None), but the account is already confirmed → to the anonymous user we say
        # “e-mail already confirmed”, not “the link is single-use”.
        already = (
            await svc.email_verify_already_done(session, raw_token=token)
            if sess is None
            else False
        )
    if sess is None:
        # The confirmation link carries no return target (there is none in the email) — the live session
        # is sent to the dashboard.
        return _link_not_accepted(
            request,
            session=browser_session,
            target="/app",
            title_key=(
                "authm_verify_already_title" if already else "authm_link_used_title"
            ),
            body_key=(
                "authm_verify_already_body"
                if already
                else "authm_verify_link_expired_body"
            ),
        )
    response = RedirectResponse(url="/app", status_code=303)
    deps.set_session_cookie(
        response, sess["uid"], sess["tid"], settings, locale=sess.get("locale")
    )
    return response


@router.get("/login", response_class=HTMLResponse, dependencies=_PUBLIC_LANG)
async def login_form(
    request: Request, next_param: str = Query(default="", alias="next")
):
    response = templates.TemplateResponse(
        request, "login.html", {"next_path": security.safe_next_path(next_param)}
    )
    persist_lang(request, response)
    return response


@router.post("/login", response_class=HTMLResponse, dependencies=_PUBLIC_LANG)
async def login_submit(
    request: Request,
    email: str = Form(""),
    next_param: str = Form("", alias="next"),
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
    email_sender: EmailSender = Depends(deps.get_email_sender),
):
    # Rate limit (STAGE 2): both axes are always counted (no short-circuit), so that
    # one axis does not mask the other. When exceeded — no email is sent.
    client_ip = request.client.host if request.client else "unknown"
    norm_email = security.normalize_email(email)
    email_ok = _MAGIC_LINK_PER_EMAIL.hit(norm_email)
    ip_ok = _MAGIC_LINK_PER_IP.hit(client_ip)
    if email_ok and ip_ok:
        lang = resolve_lang(request)
        async with session_factory() as session:
            await svc.request_magic_link(
                session,
                email=email,
                email_sender=email_sender,
                settings=settings,
                next_path=security.safe_next_path(next_param),
                locale=lang,
            )
    # Anti-enumeration: always the same response (including on limit).
    response = templates.TemplateResponse(
        request,
        "message.html",
        {
            "title_key": "authm_check_mail_title",
            "body_key": "authm_login_sent_body",
            "success": True,
        },
    )
    persist_lang(request, response)
    return response


@router.get("/auth/magic", response_class=HTMLResponse, dependencies=_PUBLIC_LANG)
async def magic_login(
    request: Request,
    token: str = "",
    next_param: str = Query(default="", alias="next"),
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
    browser_session: dict | None = Depends(deps.get_optional_session),
):
    async with session_factory() as session:
        sess = await svc.consume_magic_link(session, raw_token=token)
    # Re-validation of the return value: it came from the email URL, so it could have been substituted on the way. The default is the cabinet dashboard, NOT the admin. The same verified path
    # travels as a hidden field of the new-link form: the client will return
    # to where they were going.
    # Anti-enumeration: always the same response.
    safe_next = security.safe_next_path(next_param)
    target = safe_next or "/app"
    if sess is None:
        return _link_not_accepted(
            request,
            session=browser_session,
            target=target,
            title_key="authm_link_used_title",
            body_key="authm_magic_link_expired_body",
            next_path=safe_next,
        )
    response = RedirectResponse(url=target, status_code=303)
    deps.set_session_cookie(
        response, sess["uid"], sess["tid"], settings, locale=sess.get("locale")
    )
    return response


@router.get("/logout", response_class=HTMLResponse)
async def logout_page(
    request: Request, session: dict | None = Depends(deps.get_optional_session)
):
    """GET does NOT log out (prefetch / link scanner / a foreign site’s ``<img src>``): only
    a page with a POST button-form. Anonymous — straight to login."""
    if session is None:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "logout.html", {})


@router.post("/logout")
async def logout(
    csrf: str = Form(default=""),
    session: dict | None = Depends(deps.get_optional_session),
    settings: CabinetSettings = Depends(deps.get_settings),
):
    """Logout: POST + the session’s CSRF token. Without a session — simply to login (idempotent).
    A wrong token — 403, and the session stays: a foreign site cannot log the user out.
    """
    response = RedirectResponse(url="/login", status_code=303)
    if session is None:
        deps.clear_session_cookie(response)
        return response
    if not security.verify_csrf(csrf, session, settings.cabinet_secret_key):
        raise HTTPException(status_code=403, detail="invalid csrf token")
    deps.clear_session_cookie(response)
    return response
