"""Route for requesting a trial access from the cabinet (STAGE T).

POST ``/cabinet/trial/request`` — creates a ``pending`` request (anti-duplicate in :mod:`src.api.cabinet.trial_service`) and sends a best-effort notification to the owner in Telegram. Anonymous → ``/login``. After processing — a redirect to ``/app``: the status of the request (awaiting approval / the refusal reason) is visible in the “Trial access” block of the dashboard. The status itself is shown in :func:`src.api.cabinet.routes_profiles.dashboard`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from src.api import admin_notify, deps, repo
from src.api.cabinet import trial_service
from src.api.emailer import EmailSender
from src.domain.access import EXTENSION_REQUEST_PREFIX, is_extension_request

router = APIRouter(tags=["cabinet"])


@router.post("/cabinet/trial/request")
async def request_trial(
    request: Request,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings=Depends(deps.get_settings),
    email_sender: EmailSender = Depends(deps.get_email_sender),
):
    if session is None:
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    comment = (form.get("comment") or "").strip() or None
    if form.get("kind") == "extension":
        # The banner button “subscription ended” (owner 10.10): a free renewal.
        # A marker in the comment instead of a migration; we do not lose the user’s text.
        comment = EXTENSION_REQUEST_PREFIX + (f": {comment}" if comment else "")
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        result = await trial_service.create_trial_request(
            db, tenant_id=session["tid"], user_id=session["uid"], comment=comment
        )
    # Admin notification on a successful creation: Telegram (if the bot is configured) and
    # email to the operators — independently of each other (a failure of one channel must not
    # cancel the second; both are best-effort, the route always redirects to /app).
    if result.ok:
        if settings.tg_bot_token and settings.admin_chat_id:
            await trial_service.notify_admin(
                token=settings.tg_bot_token,
                admin_chat_id=settings.admin_chat_id,
                request_id=result.request_id,
                email=result.email,
                comment=comment,
            )
        when = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        admin_url = settings.cabinet_base_url.rstrip("/") + "/admin/requests"
        # The renewal — its own heading: otherwise the operator reads it as a new trial access.
        title = (
            "Запрос продления"
            if is_extension_request(comment)
            else "Новая заявка на тестовый доступ"
        )
        await admin_notify.notify_admins_email(
            email_sender=email_sender,
            settings=settings,
            subject=title,
            text_body="\n".join(
                [
                    f"{title}.",
                    f"Email заявителя: {result.email or '—'}",
                    f"Комментарий: {comment or '—'}",
                    f"Время: {when}",
                    f"Заявки: {admin_url}",
                ]
            ),
        )
    return RedirectResponse(url="/app", status_code=303)
