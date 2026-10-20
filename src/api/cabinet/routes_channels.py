"""Channel-binding routes of the cabinet (Chat 26, T3 — the cabinet↔bot stitching, C-04).

A logged-in subscriber clicks “Connect Telegram” → POST
``/cabinet/telegram/link`` generates a one-off bind token for his ``user_id`` and
shows the deep link ``https://t.me/<bot>?start=<token>``. The bot completes the binding at
``/start <token>``. The business logic is in
:mod:`src.api.cabinet.channel_service`; here only HTTP / render.

An anonymous user is redirected to ``/login`` (like the other cabinet routes).

As of owner 10.10.2026 the channels live as the “Kanali” section of the ``/cabinet/account`` hub
(``#kanali``): ``GET /cabinet/telegram`` and ``GET /cabinet/email`` are old addresses (the bot, emails, bookmarks); they answer 303 to the hub (not 301: it would be cached forever). The POST routes did not change; they return to ``/cabinet/account#kanali``.

A2: ``POST /cabinet/email/link|resend|unlink`` — the email channel without the bot: the subscriber enters the address himself.

owner (double opt-in): the entered address becomes ``pending`` and gets an email with the link
``GET /cabinet/email/confirm``; delivery goes only to a confirmed (``active``) address. ``POST /cabinet/email/resend`` sends the link again.
The confirmation does NOT create a session and does not give access to the account — it is the channel’s status, not a login.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api import deps, repo
from src.api.cabinet.i18n import resolve_authed_lang
from src.api.cabinet import channel_service as svc
from src.api.cabinet.routes_account import render_hub
from src.api.config import CabinetSettings
from src.api.emailer import EmailSender
from src.api.rate_limit import FixedWindowRateLimiter
from src.bots.config import get_bot_settings

router = APIRouter(tags=["cabinet"])

# Anti-abuse of confirmation emails (owner) — the same mechanism as magic-link / resend in
# routes_auth: an in-memory fixed window per api process. The key is
# ``user_id`` from the signed session (the routes are authed, so there is no enumeration here); the limit
# is shared between “bind” and “resend again”: both send an email, and counting them
# separately would double the real quota. 3 emails / 15 min.
_EMAIL_CONFIRM_PER_USER = FixedWindowRateLimiter(max_hits=3, window_sec=15 * 60)

# A KEY of the cabinet chrome catalog (not a ready text): the template localises it via ``t`` by the
# user’s locale (the two i18n axes are not mixed — CLAUDE.md).
_RATE_LIMIT_ERROR = "emp_err_rate_limit"


def _redirect_login(request: Request | None = None) -> RedirectResponse:
    """For login; on the GET page — with a return here (``deps.cabinet_login_url``)."""
    url = deps.cabinet_login_url(request) if request is not None else "/login"
    return RedirectResponse(url=url, status_code=303)


#: Where the channel POST routes and all old addresses return: the hub section.
HUB_CHANNELS_URL = "/cabinet/account#kanali"


def _hub_channels_redirect() -> RedirectResponse:
    return RedirectResponse(url=HUB_CHANNELS_URL, status_code=303)


def _hub_flash(kind: str, key: str) -> RedirectResponse:
    """A one-off flash in the hub: a catalog key in the query (``ch_err`` / ``ch_notice``).

    The hub renders only keys from the allow-list of prefixes (``routes_account``) — arbitrary text from the URL will not reach the page.
    """
    return RedirectResponse(
        url=f"/cabinet/account?{kind}={key}#kanali", status_code=303
    )


@router.get("/cabinet/telegram")
async def telegram_panel(request: Request, session=Depends(deps.get_optional_session)):
    """The old “My Telegram” address (the bot’s “Connect Telegram” button): 303 to the hub.

    Anonymous — to login with a return here; after login this redirect leads to the hub (the ``Location`` in the bot and ``channel_dead_notify`` were not changed).
    """
    if session is None:
        return _redirect_login(request)
    return _hub_channels_redirect()


@router.post("/cabinet/telegram/link", response_class=HTMLResponse)
async def link_telegram(
    request: Request,
    session=Depends(deps.require_active_access),
    settings: CabinetSettings = Depends(deps.get_settings),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """A one-off bind token → the deep link is shown IN THE HUB, in the “Kanali” section.

    The link cannot be returned by a redirect (the token in the URL is a secret in logs / history), so the response is the hub itself (200) with the ready link; ``#kanali`` in the form’s ``action`` scrolls to the section.
    """
    if session is None:
        return _redirect_login()

    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        already = await svc.has_active_telegram(db, session["uid"])
        raw = (
            None
            if already
            else await svc.create_bind_token(
                db, user_id=session["uid"], settings=settings
            )
        )
    bot_username = get_bot_settings().bot_username
    link = svc.build_deep_link(bot_username, raw) if raw else None
    return await render_hub(
        request,
        session,
        session_factory,
        tg_link=link,
        tg_already=already,
    )


# --------------------------------------------------------------------------- #
#  Email channel (A2): status / bind / unbind
# --------------------------------------------------------------------------- #


@router.get("/cabinet/email")
async def email_panel(request: Request, session=Depends(deps.get_optional_session)):
    """The old “My Email” address: 303 to the hub, the “Kanali” section."""
    if session is None:
        return _redirect_login(request)
    return _hub_channels_redirect()


@router.post("/cabinet/email/link", response_class=HTMLResponse)
async def link_email(
    request: Request,
    email: str = Form(""),
    settings: CabinetSettings = Depends(deps.get_settings),
    session=Depends(deps.require_active_access),
    session_factory: Callable = Depends(deps.get_session_factory),
    email_sender: EmailSender = Depends(deps.get_email_sender),
):
    """Request for an address: ``pending`` + an email with the link. Bad input → 200, not 500.

    The address is written to the ``user_id`` from the signed session — only the address itself is taken from the request. Delivery to it will not go out until the address is confirmed.
    """
    if session is None:
        return _redirect_login()

    address, error = svc.parse_email_address(email)
    if address is None:
        return _hub_flash("ch_err", error or "emp_err_form")

    if not _EMAIL_CONFIRM_PER_USER.hit(session["uid"]):
        return _hub_flash("ch_err", _RATE_LIMIT_ERROR)

    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        await svc.request_email_confirmation(
            db,
            user_id=session["uid"],
            address=address,
            settings=settings,
            email_sender=email_sender,
            # The email is in the language of the cabinet it was requested from.
            locale=resolve_authed_lang(
                request, getattr(request.state, "cabinet_user_locale", None)
            ),
        )
    # PRG: reloading the page does not repeat the binding (and does not send a second email).
    return _hub_channels_redirect()


@router.post("/cabinet/email/resend", response_class=HTMLResponse)
async def resend_email_confirmation(
    request: Request,
    settings: CabinetSettings = Depends(deps.get_settings),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    email_sender: EmailSender = Depends(deps.get_email_sender),
):
    """Resend the confirmation link to an already created ``pending`` address.

    The address is taken from the DB, not from the request: a repeat cannot substitute the recipient.
    """
    if session is None:
        return _redirect_login()

    if not _EMAIL_CONFIRM_PER_USER.hit(session["uid"]):
        return _hub_flash("ch_err", _RATE_LIMIT_ERROR)

    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        await svc.resend_email_confirmation(
            db,
            user_id=session["uid"],
            tenant_id=session["tid"],
            settings=settings,
            email_sender=email_sender,
            # The repeat email — in the same language as the first one.
            locale=resolve_authed_lang(
                request, getattr(request.state, "cabinet_user_locale", None)
            ),
        )
    return _hub_channels_redirect()


@router.get("/cabinet/email/confirm", response_class=HTMLResponse)
async def confirm_email(
    request: Request,
    token: str = Query(""),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """Confirming the address by the link from the email: ``pending`` → ``active``.

    Requires an active cabinet session — the token itself is NOT a login and does not open the account. The session is needed here not as a second factor, but to know WHICH ``pending`` row to check: the token is bound to the address via a hash (``_email_confirm_namespace``), and it can be verified only knowing the address.

    Idempotency (anti-prefetch): mail scanners / email previews may open the link first — confirm the address and burn the token; the real click arrives at the already burnt token. ``confirm_email`` returns ``False``, but the binding is in fact there, so before showing an error we check the FACT (:func:`svc.email_confirm_already`): the channel is already active AND the token was really issued to this address → we show SUCCESS, not an error. The error remains only for a really unknown / foreign token without a confirmed binding.

    An invalid token → a page with a clear error (200), not 500.
    """
    if session is None:
        return _redirect_login()

    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        ok = await svc.confirm_email(
            db,
            user_id=session["uid"],
            tenant_id=session["tid"],
            raw_token=token,
        )
        if not ok:
            ok = await svc.email_confirm_already(
                db,
                user_id=session["uid"],
                tenant_id=session["tid"],
                raw_token=token,
            )
    # The result — in the hub: catalog keys (``emp_confirm_*``) in a one-off flash, not a
    # separate page. The GET itself stays mutating — the address was not changed.
    if ok:
        return _hub_flash("ch_notice", "emp_confirm_ok")
    return _hub_flash("ch_err", "emp_confirm_invalid")


@router.post("/cabinet/email/unlink", response_class=HTMLResponse)
async def unlink_email(
    request: Request,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """Unbinding: active email rows → ``blocked``; delivery then has nowhere to go."""
    if session is None:
        return _redirect_login()

    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        await svc.unlink_email(db, user_id=session["uid"])
    return _hub_channels_redirect()
