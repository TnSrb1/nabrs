"""Administrative cabinet (EPIC 6).

Access only for platform operators from the allowlist `CABINET_ADMIN_EMAILS`
(gate `deps.require_admin`).
All pages: /admin/users, /admin/users/{uid}, /admin/templates, /admin/moderation.

Operator login — /admin/login: a separate URL and branding on top of the COMMON passwordless
contour of the cabinet (`routes_auth`). There is no second authentication contour here
and there must not be: the page only draws the form of the existing login with a
return parameter to the admin.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api import deps, security
from src.api.admin import admin_service as svc
from src.api.admin import sources_admin as src_svc
from src.api.cabinet import template_library_service as lib_svc
from src.api.templating import templates
from src.api.tiers import TIER_ORDER

router = APIRouter(prefix="/admin", tags=["admin"])

_GRANT_DAYS_OPTIONS = [3, 7, 14, 30, 90, 365]

# Ceiling of the period issued together with the return of a disabled subscriber.
# The same order as the maximum variant of “Extend access”: a typo
# in the field must not give years of access with one click (bypassing the grant buttons).
_MAX_ENABLE_DAYS = 365

# Limit of the operator’s comment on the decision (protection against a huge textarea → email/cabinet).
_MAX_DECISION_COMMENT = 1000


def _form_comment(raw) -> str | None:
    """The operator’s comment on the decision from the form: empty → None, trimmed to the limit."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    return value[:_MAX_DECISION_COMMENT]


def _client_ip(request: Request) -> str | None:
    """Client IP for the audit: the first hop of X-Forwarded-For (behind Caddy) or the peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip() or None
    return request.client.host if request.client else None


async def _actor_email(db, session: dict) -> str:
    """Operator email for the audit. The session carries only uid/tid — we resolve the email
    cross-tenant through SECURITY DEFINER app_admin_get_user (get_user). Fallback
    to uid if the user is not found (we never hardcode an email)."""
    admin = await svc.get_user(db, session.get("uid", ""))
    if admin is not None and admin.email:
        return admin.email
    return session.get("uid", "unknown")


@router.get("/login", response_class=HTMLResponse)
async def admin_login_form(
    request: Request,
    next_param: str = Query(default="", alias="next"),
    is_admin: bool = Depends(deps.resolve_platform_admin_flag),
):
    """Login for the platform operator.

    Its own form — but the POST goes to the common /login, the link arrives by email through the same
    magic-link contour. The difference from the cabinet is only in the URL, the heading, and in that the
    default return leads to the admin.

    An operator with a live session does not see the form — they go straight inside.
    """
    target = security.safe_next_path(next_param) or deps.ADMIN_HOME_PATH
    if is_admin:
        return RedirectResponse(url=target, status_code=303)
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {"next_path": target},
    )


@router.get("")
@router.get("/")
async def admin_index(
    session: dict = Depends(deps.require_admin),
):
    """Bare /admin (and /admin/) → the operator’s workplace (“Users”).

    owner 04.08 did not find user management: the index led to “Health”,
    and a second link to “Users” got lost in the navigation. “Health”
    remains an ordinary navigation item, just not the starting screen.
    Under the same require_admin gate: an anonymous/non-admin gets 403, not 404.
    """
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    page: int = Query(default=1, ge=1),
    q: str = Query(default=""),
    tier: str = Query(default=""),
    sort: str = Query(default=""),
    direction: str = Query(default="", alias="dir"),
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    # Search (email/tenant) and plan filter — read-only, applied in
    # admin_service.list_users on top of the SECURITY DEFINER selection (without migrations).
    # sort/dir — normalize_sort whitelist: garbage → default, without 422.
    sort_key, sort_dir = svc.normalize_sort(sort, direction)
    async with session_factory() as db:
        users, total = await svc.list_users(
            db, page=page, search=q, tier=tier, sort=sort_key, direction=sort_dir
        )
        stats = await svc.get_platform_stats(db)
        silent = await src_svc.silent_sources(db)
    page_size = svc.PAGE_SIZE
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "active_nav": "users",
            "silent_sources": silent,
            "users": users,
            "stats": stats,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "q": q,
            "tier": tier,
            "sort": sort_key,
            "sort_dir": sort_dir,
            "tiers": list(TIER_ORDER),
        },
    )


@router.get("/users/{uid}", response_class=HTMLResponse)
async def admin_user_detail(
    uid: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    async with session_factory() as db:
        user = await svc.get_user(db, uid)
        if user is None:
            return HTMLResponse("Пользователь не найден", status_code=404)
        delivered_count = await svc.get_user_delivered_count(db, user.tenant_id)
        tenant_profiles = await svc.list_profiles(db, tenant_id=user.tenant_id)
        user_channels = await svc.list_user_channels(db, user.id)
    # profile_count and len(tenant_profiles) count the same thing: all non-deleted
    # profiles of the tenant (active + disabled). The breakdown is for display only.
    profiles_active = [p for p in tenant_profiles if p.is_active]
    profiles_inactive = [p for p in tenant_profiles if not p.is_active]
    return templates.TemplateResponse(
        request,
        "admin_user_detail.html",
        {
            "active_nav": "users",
            "user": user,
            "delivered_count": delivered_count,
            "profiles_active": profiles_active,
            "profiles_inactive": profiles_inactive,
            "user_channels": user_channels,
            "tiers": list(TIER_ORDER),
            "grant_days_options": _GRANT_DAYS_OPTIONS,
        },
    )


@router.post("/users/{uid}/grant")
async def admin_grant_access(
    uid: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    form = await request.form()
    try:
        days = int(form.get("days", 30))
    except (ValueError, TypeError):
        days = 30
    async with session_factory() as db:
        user = await svc.get_user(db, uid)
        if user is None:
            return HTMLResponse("Пользователь не найден", status_code=404)
        actor = await _actor_email(db, session)
        await svc.grant_access(
            db,
            admin_uid=session.get("uid", "unknown"),
            tenant_id=user.tenant_id,
            days=days,
            # The base of the extension AND the before audit record — the current period from the same
            # selection that draws the card. Without it the extension would again become
            # a setting now()+days (cutting paid access).
            before_until=user.access_until,
            actor_email=actor,
            target_user=user.id,
            ip=_client_ip(request),
        )
    return RedirectResponse(url=f"/admin/users/{uid}", status_code=303)


@router.post("/users/{uid}/revoke")
async def admin_revoke_access(
    uid: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    async with session_factory() as db:
        user = await svc.get_user(db, uid)
        if user is None:
            return HTMLResponse("Пользователь не найден", status_code=404)
        actor = await _actor_email(db, session)
        await svc.revoke_access(
            db,
            admin_uid=session.get("uid", "unknown"),
            tenant_id=user.tenant_id,
            actor_email=actor,
            target_user=user.id,
            ip=_client_ip(request),
            before_until=user.access_until,
        )
    return RedirectResponse(url=f"/admin/users/{uid}", status_code=303)


@router.post("/users/{uid}/disable")
async def admin_disable_subscriber(
    uid: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """Disable a subscriber in full: access + Telegram channels + cabinet login.

    Soft and reversible (``/enable``), there is no physical deletion. A separate button
    from “Freeze” (``/revoke``) deliberately: freezing is about the access period,
    disabling is about “this subscriber is no longer in the service”.
    """
    async with session_factory() as db:
        user = await svc.get_user(db, uid)
        if user is None:
            return HTMLResponse("Пользователь не найден", status_code=404)
        actor = await _actor_email(db, session)
        await svc.disable_subscriber(
            db,
            admin_uid=session.get("uid", "unknown"),
            tenant_id=user.tenant_id,
            actor_email=actor,
            target_user=user.id,
            ip=_client_ip(request),
            before_until=user.access_until,
        )
    return RedirectResponse(url=f"/admin/users/{uid}", status_code=303)


@router.post("/users/{uid}/enable")
async def admin_enable_subscriber(
    uid: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """Restore a disabled subscriber: login + channels, and with ``days>0`` also the period.

    ``days`` from the form; empty/garbage/negative → 0 (the period is not touched, it is
    issued by “Extend access”). The upper bound is the same as for grant: without it a typo
    in the field would give years of access with one click.
    """
    form = await request.form()
    # str() around form.get: starlette types the value as ``UploadFile | str``,
    # and without the cast the file’s mypy debt would grow by this line (the ratchet
    # tests/quality/test_mypy_debt.py catches exactly this).
    raw_days = str(form.get("days") or "").strip()
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = 0
    if days < 0 or days > _MAX_ENABLE_DAYS:
        days = 0
    async with session_factory() as db:
        user = await svc.get_user(db, uid)
        if user is None:
            return HTMLResponse("Пользователь не найден", status_code=404)
        actor = await _actor_email(db, session)
        await svc.enable_subscriber(
            db,
            admin_uid=session.get("uid", "unknown"),
            tenant_id=user.tenant_id,
            days=days,
            actor_email=actor,
            target_user=user.id,
            ip=_client_ip(request),
            before_until=user.access_until,
        )
    return RedirectResponse(url=f"/admin/users/{uid}", status_code=303)


@router.post("/users/{uid}/set-tier")
async def admin_set_tier(
    uid: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    form = await request.form()
    tier = (form.get("tier") or "").strip().lower()
    if tier not in TIER_ORDER:
        return HTMLResponse("Неверный тариф", status_code=422)
    async with session_factory() as db:
        user = await svc.get_user(db, uid)
        if user is None:
            return HTMLResponse("Пользователь не найден", status_code=404)
        actor = await _actor_email(db, session)
        await svc.set_tier(
            db,
            admin_uid=session.get("uid", "unknown"),
            tenant_id=user.tenant_id,
            tier=tier,
            actor_email=actor,
            target_user=user.id,
            ip=_client_ip(request),
            before_tier=user.tier,
        )
    return RedirectResponse(url=f"/admin/users/{uid}", status_code=303)


@router.get("/templates", response_class=HTMLResponse)
async def admin_templates(
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    async with session_factory() as db:
        profiles = await svc.list_profiles(db)
    return templates.TemplateResponse(
        request,
        "admin_templates.html",
        {"active_nav": "templates", "profiles": profiles},
    )


@router.get("/health", response_class=HTMLResponse)
async def admin_health(
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    async with session_factory() as db:
        health = await svc.get_ingest_health(db)
        delivery = await svc.get_delivery_health(db)
        silent = await src_svc.silent_sources(db)
    return templates.TemplateResponse(
        request,
        "admin_health.html",
        {
            "active_nav": "health",
            "health": health,
            "delivery": delivery,
            "silent_sources": silent,
        },
    )


@router.get("/sources", response_class=HTMLResponse)
async def admin_sources(
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """“Izvori”: sources — on/off, last pass, counters, errors, silence."""
    async with session_factory() as db:
        sources = await src_svc.list_sources(db)
    return templates.TemplateResponse(
        request,
        "admin_sources.html",
        {
            "active_nav": "sources",
            "sources": sources,
            "silent_sources": [s for s in sources if s.silent],
        },
    )


@router.post("/sources/{source_key}/enable")
async def admin_source_enable(
    source_key: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    return await _toggle_source(source_key, True, request, session, session_factory)


@router.post("/sources/{source_key}/disable")
async def admin_source_disable(
    source_key: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    return await _toggle_source(source_key, False, request, session, session_factory)


async def _toggle_source(
    source_key: str,
    enable: bool,
    request: Request,
    session: dict,
    session_factory: Callable,
):
    """Common on/off POST: the ``require_admin`` guard has already run, the audit is in the service."""
    async with session_factory() as db:
        actor = await _actor_email(db, session)
        result = await src_svc.set_source_enabled(
            db,
            source_key=source_key,
            enable=enable,
            actor_email=actor,
            ip=_client_ip(request),
        )
    if not result.ok:
        return HTMLResponse("Источник не найден", status_code=404)
    return RedirectResponse(url="/admin/sources", status_code=303)


@router.get("/audit", response_class=HTMLResponse)
async def admin_audit(
    request: Request,
    page: int = Query(default=1, ge=1),
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """Immutable journal of privileged actions (VG-29.4), read-only.

    The cross-tenant global journal — read through SECURITY DEFINER
    app_admin_list_audit / app_admin_audit_total (0030)."""
    async with session_factory() as db:
        rows, total = await svc.list_audit(db, page=page)
    page_size = svc.AUDIT_PAGE_SIZE
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    return templates.TemplateResponse(
        request,
        "admin_audit.html",
        {
            "active_nav": "audit",
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )


@router.get("/requests", response_class=HTMLResponse)
async def admin_requests(
    request: Request,
    page: int = Query(default=1, ge=1),
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """Pending requests for access/subscription (sub-approval): approve/reject.

    The list is cross-tenant through SECURITY DEFINER (0034); approving an upgrade request
    issues the plan through the SINGLE billing path (provider='manual')."""
    async with session_factory() as db:
        rows, total = await svc.list_trial_requests(db, page=page)
    page_size = svc.REQUESTS_PAGE_SIZE
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    return templates.TemplateResponse(
        request,
        "admin_requests.html",
        {
            "active_nav": "requests",
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "grant_days_options": _GRANT_DAYS_OPTIONS,
        },
    )


@router.post("/requests/{request_id}/approve")
async def admin_approve_request(
    request_id: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings=Depends(deps.get_settings),
    email_sender=Depends(deps.get_email_sender),
):
    form = await request.form()
    raw_days = form.get("days")
    days = int(raw_days) if isinstance(raw_days, str) and raw_days.isdigit() else 30
    if days <= 0:
        days = 30
    comment = _form_comment(form.get("comment"))
    async with session_factory() as db:
        actor = await _actor_email(db, session)
        await svc.approve_request(
            db,
            admin_uid=session.get("uid", "unknown"),
            request_id=request_id,
            days=days,
            comment=comment,
            actor_email=actor,
            ip=_client_ip(request),
            email_sender=email_sender,
            settings=settings,
        )
    return RedirectResponse(url="/admin/requests", status_code=303)


@router.post("/requests/{request_id}/reject")
async def admin_reject_request(
    request_id: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings=Depends(deps.get_settings),
    email_sender=Depends(deps.get_email_sender),
):
    form = await request.form()
    comment = _form_comment(form.get("comment"))
    async with session_factory() as db:
        actor = await _actor_email(db, session)
        await svc.reject_request(
            db,
            admin_uid=session.get("uid", "unknown"),
            request_id=request_id,
            comment=comment,
            actor_email=actor,
            ip=_client_ip(request),
            email_sender=email_sender,
            settings=settings,
        )
    return RedirectResponse(url="/admin/requests", status_code=303)


@router.get("/moderation", response_class=HTMLResponse)
async def admin_moderation(
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    async with session_factory() as db:
        pending = await lib_svc.list_pending(db)
    return templates.TemplateResponse(
        request,
        "admin_moderation.html",
        {"active_nav": "moderation", "pending": pending},
    )


@router.post("/moderation/{template_id}/approve")
async def admin_approve_template(
    template_id: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    async with session_factory() as db:
        await lib_svc.approve_template(
            db,
            admin_uid=session.get("uid", "unknown"),
            template_id=template_id,
        )
    return RedirectResponse(url="/admin/moderation", status_code=303)


@router.post("/moderation/{template_id}/reject")
async def admin_reject_template(
    template_id: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    async with session_factory() as db:
        await lib_svc.reject_template(
            db,
            admin_uid=session.get("uid", "unknown"),
            template_id=template_id,
        )
    return RedirectResponse(url="/admin/moderation", status_code=303)


@router.post("/moderation/{template_id}/edit")
async def admin_edit_pending(
    template_id: str,
    request: Request,
    session: dict = Depends(deps.require_admin),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    form = await request.form()
    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip() or None

    # Showcase translations (0036): the operator fills them in right here, where they edit the Russian
    # text. Empty fields are not saved — the render honestly falls back.
    # ``form.get`` is typed as ``UploadFile | str`` — we cast explicitly, otherwise
    # the mypy debt grows (the guard tests/quality/test_mypy_debt.py catches it).
    def _field(key: str) -> str:
        return str(form.get(key) or "").strip()

    i18n = {
        lang: {
            "name": _field(f"name_{lang}"),
            "description": _field(f"description_{lang}"),
        }
        for lang in ("sr", "en")
    }
    if not name:
        return RedirectResponse(url="/admin/moderation", status_code=303)
    async with session_factory() as db:
        await lib_svc.update_pending(
            db,
            admin_uid=session.get("uid", "unknown"),
            template_id=template_id,
            name=name,
            description=description,
            i18n=i18n,
        )
    return RedirectResponse(url="/admin/moderation", status_code=303)
