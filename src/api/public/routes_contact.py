"""The feedback form: GET / POST /kontakt — the ONLY receiver for all forms.

The form (the partial ``_contact_form.html``) sits on /kontakt, in “Help” (/help) and in the cabinet hub (/cabinet/account); the “Contact us” button on /plans is a link here too. The hidden field ``source`` (help | cabinet | plans | kontakt) — where the request came from — goes into the operator notification text and chooses the “back” on the “thank you” screen.

Previously the button was a ``mailto:`` and without a mail client (web mail, a phone without a configured mail) it did nothing. Now the request goes to the operators by email (``notify_admins_email``) and to the service Telegram chat; we do not write it to the DB (there is no table for it, and a migration would be RED). The address ``info@nab.rs`` stays as text under the form.

Protection: a honeypot field (a silent “success” without sending), rate-limit by IP and by email, a check of Origin / Referer on POST (there are no CSRF tokens in the project — SameSite=lax + CSP ``form-action self``). ‼️ PII: we do not write the address and the message text to the log.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from src.api import admin_notify, deps
from src.api.cabinet import trial_service
from src.api.emailer import EmailSender
from src.api.public.i18n import CONTACT, CONTACT_MESSAGE_MAX, public_context
from src.api.public.i18n import persist_lang as _persist_lang
from src.api.rate_limit import FixedWindowRateLimiter
from src.api.templating import templates

MESSAGE_MAX = CONTACT_MESSAGE_MAX
NAME_MAX = 200
EMAIL_MAX = 254
SOURCES = ("help", "cabinet", "plans", "kontakt")
DEFAULT_SOURCE = "kontakt"
# Where “back” leads on the “thank you” screen: (address, the CONTACT catalog key).
_BACK = {
    "help": ("/help", "back_help"),
    "cabinet": ("/cabinet/account", "back_cabinet"),
}
_BACK_DEFAULT = ("/plans", "back")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# in-memory, per api process (as for the magic link): 3 per hour per email, 10 per hour per IP.
_CONTACT_PER_EMAIL = FixedWindowRateLimiter(max_hits=3, window_sec=60 * 60)
_CONTACT_PER_IP = FixedWindowRateLimiter(max_hits=10, window_sec=60 * 60)

_TG_MESSAGE_MAX = 600

logger = logging.getLogger(__name__)

router = APIRouter(tags=["public"])


def _client_ip(request: Request) -> str:
    """The first hop of X-Forwarded-For (Caddy sets it) or the peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff and xff.split(",")[0].strip():
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _source(raw: object) -> str:
    value = str(raw or "").strip()
    return value if value in SOURCES else DEFAULT_SOURCE


def _render(
    request: Request,
    *,
    source: str = DEFAULT_SOURCE,
    status_code: int = 200,
    sent_to: str | None = None,
    error: str | None = None,
    values: dict | None = None,
    prefill_email: str = "",
) -> HTMLResponse:
    ctx = public_context(request)
    t = CONTACT[ctx["lang"]]
    vals = {"email": prefill_email, "name": "", "message": ""}
    vals.update(values or {})
    back_href, back_key = _BACK.get(source, _BACK_DEFAULT)
    response = templates.TemplateResponse(
        request,
        "contact.html",
        {
            **ctx,
            "t": t,
            "sent_to": sent_to,
            "error": error,
            "values": vals,
            "form_source": source,
            "back_href": back_href,
            "back_label": t[back_key],
        },
        status_code=status_code,
    )
    _persist_lang(request, response)
    return response


@router.get("/kontakt", response_class=HTMLResponse)
async def contact_page(
    request: Request,
    _whoami: None = Depends(deps.resolve_cabinet_whoami),
) -> HTMLResponse:
    return _render(
        request,
        prefill_email=getattr(request.state, "cabinet_whoami_email", "") or "",
    )


def _same_origin(request: Request) -> bool:
    """A POST from a foreign site is rejected; with no headers (old clients) — we let it through."""
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src:
        return True
    host = urlsplit(src).netloc
    return host == (request.headers.get("host") or "")


@router.post("/kontakt", response_class=HTMLResponse)
async def contact_submit(
    request: Request,
    settings=Depends(deps.get_settings),
    email_sender: EmailSender = Depends(deps.get_email_sender),
) -> HTMLResponse:
    lang = public_context(request)["lang"]
    t = CONTACT[lang]
    form = await request.form()
    source = _source(form.get("source"))
    if not _same_origin(request):
        return _render(request, source=source, status_code=403, error=t["err_origin"])

    email = str(form.get("email") or "").strip()[:EMAIL_MAX]
    name = str(form.get("name") or "").strip()[:NAME_MAX]
    message = str(form.get("message") or "").strip()
    values = {"email": email, "name": name, "message": message}

    # Honeypot: a human does not see the field. We do not inform the bot — the same “success”.
    if str(form.get("website") or "").strip():
        return _render(request, source=source, sent_to=email or "—")

    if not _EMAIL_RE.match(email):
        return _render(
            request, source=source, status_code=422, error=t["err_email"], values=values
        )
    if not message:
        return _render(
            request,
            source=source,
            status_code=422,
            error=t["err_message"],
            values=values,
        )
    if len(message) > MESSAGE_MAX:
        return _render(
            request,
            source=source,
            status_code=422,
            error=t["err_too_long"],
            values=values,
        )

    # The limit is applied AFTER validation (422s do not eat the window); both axes without short-circuit.
    # The IP — the first hop of X-Forwarded-For: behind Caddy request.client is the proxy’s address,
    # and the limit “per IP” was shared by all visitors (the owner’s review of 02.10, api logs:
    # 49/49 requests from 172.18.0.10).
    ip_ok = _CONTACT_PER_IP.hit(_client_ip(request))
    email_ok = _CONTACT_PER_EMAIL.hit(email.lower())
    if not (ip_ok and email_ok):
        return _render(
            request, source=source, status_code=429, error=t["err_rate"], values=values
        )

    when = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    tg_ok = False
    if settings.tg_bot_token and settings.admin_chat_id:
        e = html.escape
        tg_ok = await trial_service.send_admin_text(
            token=settings.tg_bot_token,
            admin_chat_id=settings.admin_chat_id,
            text="\n".join(
                [
                    "✉️ Сообщение с формы «Связаться с нами»",
                    f"Источник: {e(source)}",
                    f"Email: {e(email)}",
                    f"Имя/компания: {e(name) or '—'}",
                    f"Время: {when}",
                    "",
                    # 2000 characters after html.escape may exceed the limit
                    # of Telegram 4096 → 400 and a silent loss; the full text is in the email.
                    e(message[:_TG_MESSAGE_MAX]),
                ]
            ),
        )
    mail_sent = await admin_notify.notify_admins_email(
        email_sender=email_sender,
        settings=settings,
        subject="Сообщение с формы «Связаться с нами»",
        text_body="\n".join(
            [
                f"Источник: {source}",
                f"Email: {email}",
                f"Имя/компания: {name or '—'}",
                f"Время: {when}",
                "",
                message,
            ]
        ),
    )
    if not (tg_ok or mail_sent):
        # No channel accepted the request (TG and CABINET_ADMIN_EMAILS are not configured):
        # we do not write it to the DB, so a “thank you” would be a lie. The log — without the address and the text.
        logger.error(
            "contact: the request was not delivered by any channel (source=%s)", source
        )
        return _render(
            request,
            source=source,
            status_code=503,
            error=t["err_send"],
            values=values,
        )
    return _render(request, source=source, sent_to=email)
