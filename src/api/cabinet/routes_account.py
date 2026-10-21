"""The “My account” section of the cabinet (owner 20.09.2026, additional owner 22.09.2026).

As of owner 10.10.2026 — the “Moj kabinet” hub: sections with anchors ``#status`` · ``#kanali`` · ``#sabloni`` · ``#pretplata`` · ``#nalog``. The channels (``/cabinet/telegram``, ``/cabinet/email``) and the subscription (``/cabinet/subscription``) — previously separate pages — are now sections here; the old addresses answer 303 to the anchors.

It gathers in one place what used to be spread over five screens: “who am I” (the cabinet header, #527) · the plan and its term · search profiles (``/app``) · delivery channels · packages (``/library``). The reason — a real dead end for a client: the Pro plan (profile limit 3) ran into the refusal “Profile limit reached”, and the product had no “my profiles” page with a delete action at all (``GET /profiles`` does not exist — 405).

The data is READ through the existing services (``subscription_service``, ``trial_service``, ``profile_service``, ``channel_service``) — not a single new SQL query, except those that ``/cabinet/subscription`` and ``/app`` already make separately. ``whoami_email`` / ``whoami_tenant_name`` are not read here at all: this is the global context of all cabinet templates (``templating.py:_auth_context``, deps.resolve_cabinet_whoami), not a new query.

Managing a template in the row — three separate actions, three existing routes (``routes_profiles``), with no new mechanism: “Disable” / “Enable” (``POST /profiles/{id}/deactivate`` / ``/activate``, reversible, the ``is_active`` axis) and “Delete” (``POST /profiles/{id}/delete``, ``profile_service.soft_delete_profile``, irreversible, ``deleted_at``). Both toggles get a hidden field ``next=/cabinet/account``, to return here and not to the dashboard.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.api import deps, repo, security, tiers
from src.api.cabinet import account_delete_service as delete_svc
from src.api.cabinet import account_export_service as export_svc
from src.api.cabinet import channel_service as channel_svc
from src.api.cabinet import profile_service as svc
from src.api.cabinet import trial_service
from src.api.cabinet.routes_subscription import build_subscription_context
from src.api.config import CabinetSettings
from src.api.emailer import EmailSender
from src.api.rate_limit import FixedWindowRateLimiter
from src.api.templating import templates
from src.bots.config import get_bot_settings
from src.i18n_email import email_t
from src.plans.catalog import get_plan, tier_to_plan_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["cabinet"])

#: Export “Download my data”: the key is user_id (like the login key by email, but
#: session-based: anonymous users do not reach here). Building the file — ~20 queries, do not hammer.
_EXPORT_PER_USER = FixedWindowRateLimiter(max_hits=5, window_sec=60 * 60)


def _redirect_login(request: Request | None = None) -> RedirectResponse:
    """For login; on the GET page — with a return here (``deps.cabinet_login_url``)."""
    url = deps.cabinet_login_url(request) if request is not None else "/login"
    return RedirectResponse(url=url, status_code=303)


@dataclass(frozen=True)
class ProfileRow:
    """A profile projection for the list row (presentation, not ORM)."""

    id: str
    name: str | None
    is_active: bool
    match_all: bool
    keyword_terms: list[str]
    keyword_extra: int
    regions: list[str]


def _profile_row(profile) -> ProfileRow:
    """A short summary of the profile parameters for the list row.

    Keywords — the first terms from the FIRST two OR-groups (``all_of``), with the remainder as a count (``keyword_extra``), not the full list: the list row must hold on one line (the screen-length audit invariant); the full set is seen at ``/profiles/{id}``.
    """
    terms: list[str] = []
    for group in (getattr(profile, "keywords_include", None) or [])[:2]:
        terms.extend((group or {}).get("all_of") or [])
    shown, extra = terms[:5], max(0, len(terms) - 5)
    return ProfileRow(
        id=str(profile.id),
        name=profile.name or None,
        is_active=bool(profile.is_active),
        match_all=bool(getattr(profile, "match_all", False)),
        keyword_terms=shown,
        keyword_extra=extra,
        regions=list(getattr(profile, "regions", None) or []),
    )


def resolve_plan_key(subscription, tier: str | None, is_trial: bool) -> str:
    """The showcase key (free / pro / business / enterprise) — a mirror of ``build_subscription_context`` (``current_key``).

    The same input (subscription / tier / status) must give the same plan on “My subscription” and here — otherwise the client would see a mismatch between the two screens. Trial access is treated as Pro (ARCHITECTURE §4).
    """
    if is_trial:
        return "pro"
    if subscription is not None and subscription.plan_key:
        plan = get_plan(subscription.plan_key)
        if plan is not None:
            return plan.key
    return tier_to_plan_key(tier)


async def render_hub(
    request: Request,
    session: dict,
    session_factory: Callable,
    **extra,
):
    """The “Moj kabinet” hub: status · channels · templates · subscription · account.

    The common render for ``GET /cabinet/account`` and ``POST /cabinet/telegram/link`` (there ``extra`` carries the deep link). The plan and “access until” are computed by ONE resolver (:func:`routes_subscription.build_subscription_context` + ``resolve_plan_key``), otherwise the plan name and the date would diverge between the Status and Pretplata sections.
    """
    tid = session["tid"]
    async with session_factory() as db:
        # RLS: set_tenant FIRST — as in all cabinet routes.
        await repo.set_tenant(db, tid)
        sub_ctx = await build_subscription_context(db, tid)
        access_status = await trial_service.get_access_status(db, tid)
        # list_profiles already filters deleted_at IS NULL — the same predicate
        # as the limit (_count_profiles), so len(profiles) = “X in use”
        # without a second query.
        profiles = await svc.list_profiles(db, tid)
        telegram_state = await channel_svc.telegram_channel_state(db, tid)
        email_state = await channel_svc.email_channel_state(db, tid)
        warning = await channel_svc.delivery_warning(db, tid, profiles)

    tier, status = sub_ctx["tier"], sub_ctx["status"]
    # ‼️ The limit is from the EFFECTIVE plan (``effective_tier``), as the
    # profile creation itself counts it (profile_service._tenant_tier). By the RAW tier the
    # page lied exactly where the client runs into it: a tenant on trial
    # access (tier='lite', status='trial') got "1 of 1 in use — limit
    # reached", although create_profile happily creates three (trial = Pro rights,
    # ARCHITECTURE §4). The same method now applies on the dashboard and in the enable gate.
    limit = tiers.profile_limit(tiers.effective_tier(status, tier))
    used = len(profiles)
    plan_key = resolve_plan_key(sub_ctx["subscription"], tier, sub_ctx["is_trial"])

    rows = [_profile_row(p) for p in profiles]
    active_rows = [r for r in rows if r.is_active]
    inactive_rows = [r for r in rows if not r.is_active]

    email_linked = email_state is not None and email_state.is_live
    email_pending = (
        email_state is not None
        and email_state.status == channel_svc.CHANNEL_STATUS_PENDING
    )

    ctx = {
        **sub_ctx,
        "access_status": access_status,
        "delivery_warning": warning,
        "plan_key": plan_key,
        "profile_used": used,
        "profile_limit": limit,
        "profile_limit_reached": limit is not None and used >= limit,
        "active_rows": active_rows,
        "inactive_rows": inactive_rows,
        "telegram_linked": telegram_state is not None and telegram_state.is_live,
        "email_linked": email_linked,
        "email_pending": email_pending,
        "email_masked": (
            channel_svc.mask_email(email_state.external_id)
            if email_state is not None and (email_linked or email_pending)
            else None
        ),
        "bot_username": get_bot_settings().bot_username,
        "tg_link": None,
        "tg_already": False,
        # The flashes of the channel POST routes are CATALOG KEYS; the template renders only keys
        # with the prefixes ``emp_err_`` / ``emp_confirm_`` (the text from the URL is not rendered).
        "channel_error_key": request.query_params.get("ch_err", ""),
        "channel_notice_key": request.query_params.get("ch_notice", ""),
        # The refusal to enable a template over the limit at ``next=/cabinet/account``
        # (POST /profiles/{id}/activate): the same flag and the same banner as
        # on the dashboard (dash_limit_title / dash_limit_body) — the refusal must
        # be visible where the button was pressed, not only on /app.
        "delete_error_key": _DELETE_ERRORS.get(request.query_params.get("del_err", "")),
        "export_error_key": _EXPORT_ERRORS.get(request.query_params.get("exp_err", "")),
        "profile_limit_notice": (
            request.query_params.get(svc.PROFILE_LIMIT_FLAG) == svc.PROFILE_LIMIT_VALUE
        ),
    }
    ctx.update(extra)
    return templates.TemplateResponse(request, "account.html", ctx)


@router.get("/cabinet/account", response_class=HTMLResponse)
async def account_page(
    request: Request,
    session=Depends(deps.require_active_access),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """“Moj kabinet” — one page instead of four (owner 10.10.2026)."""
    if session is None:
        return _redirect_login(request)
    return await render_hub(request, session, session_factory)


#: Refusal codes → catalog key (``?del_err=`` — a one-off flag, like ``limit=profiles``).
_DELETE_ERRORS = {
    delete_svc.EMAIL_MISMATCH: "del_err_mismatch",
    delete_svc.ACTIVE_SUBSCRIPTION: "del_err_paid",
    "csrf": "del_err_csrf",
}


#: ``?exp_err=`` → catalog key (the text from the URL is not rendered).
_EXPORT_ERRORS = {
    "rate": "exp_err_rate",
    "csrf": "del_err_csrf",
    "fail": "exp_err_fail",
}


@router.post("/cabinet/account/export")
async def account_export(
    csrf: str = Form(default=""),
    session=Depends(deps.require_active_access),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings: CabinetSettings = Depends(deps.get_settings),
):
    """“Download my data”: one JSON file with THEIR OWN data (LEG-06).

    POST + CSRF (like deletion), not GET: a file with personal data must not be served by prefetch / HEAD, and our GET sometimes mutates. Only the own pair ``uid`` / ``tid`` from the signed session — not a single id from the request. To the log — ``user_id`` and the exception type, no addresses or content.
    """
    if session is None:
        return _redirect_login()
    if not security.verify_csrf(csrf, session, settings.cabinet_secret_key):
        return RedirectResponse(
            url="/cabinet/account?exp_err=csrf#acct-export", status_code=303
        )
    uid = session["uid"]
    if not _EXPORT_PER_USER.hit(str(uid)):
        return RedirectResponse(
            url="/cabinet/account?exp_err=rate#acct-export", status_code=303
        )
    try:
        async with session_factory() as db:
            data = await export_svc.build_account_export(
                db, tenant_id=session["tid"], user_id=uid
            )
    except (
        Exception
    ) as exc:  # noqa: BLE001 — a flash to the client, only the type to the log
        logger.warning("account-export failed user=%s: %s", uid, type(exc).__name__)
        return RedirectResponse(
            url="/cabinet/account?exp_err=fail#acct-export", status_code=303
        )
    if data is None:
        gone = _redirect_login()
        deps.clear_session_cookie(gone)
        return gone
    logger.info("account-export ok user=%s", uid)
    return Response(
        content=export_svc.export_bytes(data),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{export_svc.export_filename()}"'
            ),
            "Cache-Control": "no-store",
        },
    )


@router.post("/cabinet/account/delete", response_class=HTMLResponse)
async def account_delete(
    request: Request,
    confirm_email: str = Form(default=""),
    csrf: str = Form(default=""),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings: CabinetSettings = Depends(deps.get_settings),
    email_sender: EmailSender = Depends(deps.get_email_sender),
):
    """Self-deletion of the account: CSRF → email confirmation → three axes + anonymisation.

    Success: the confirmation email (``users.locale``), the cookie is cleared, the “Account deleted” page. Refusal: a return to “My account” with a one-off error flag, nothing is written. ‼️ This is NOT ``/profiles/{id}/delete`` (that one deletes a template).
    """
    if session is None:
        return _redirect_login(request)
    if not security.verify_csrf(csrf, session, settings.cabinet_secret_key):
        return RedirectResponse(
            url=f"/cabinet/account?del_err={'csrf'}#acct-delete", status_code=303
        )
    async with session_factory() as db:
        result = await delete_svc.delete_own_account(
            db,
            tenant_id=session["tid"],
            user_id=session["uid"],
            confirm_email=confirm_email,
        )
    if result.outcome == delete_svc.NOT_FOUND:
        # Already deleted (double click / second tab): the session is dead.
        gone = _redirect_login()
        deps.clear_session_cookie(gone)
        return gone
    if result.outcome != delete_svc.OK:
        return RedirectResponse(
            url=f"/cabinet/account?del_err={result.outcome}#acct-delete",
            status_code=303,
        )
    if result.email:
        try:
            await email_sender.send(
                to=result.email,
                subject=email_t(result.locale, "account_deleted_subject"),
                text_body=email_t(result.locale, "account_deleted_body"),
                locale=result.locale,
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 — the account is already deleted, the email is secondary
            # We do not write PII to the log — only the exception type.
            logger.warning(
                "account-delete confirmation mail failed: %s", type(exc).__name__
            )
    response = templates.TemplateResponse(
        request,
        "message.html",
        {"title_key": "del_done_title", "body_key": "del_done_body", "home_href": "/"},
    )
    deps.clear_session_cookie(response)
    return response
