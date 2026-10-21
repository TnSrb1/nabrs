"""The trial-access service for the cabinet (STAGE T).

Three tasks:

- :func:`get_access_status` — the tenant’s access status for the dashboard: reads ``tenants.access_until`` and the latest ``trial_requests`` request (one round-trip per table), derives a human-readable label and the ``active`` / ``can_request`` flags.
- :func:`create_trial_request` — creates a ``pending`` request, but ONLY if the tenant has no other ``pending`` AND no active access (``access_until`` NULL = permanently active → we do not open a new request). Otherwise — the refusal reason.
- :func:`notify_admin` — best-effort server-side notification of the owner in Telegram (the Bot API directly via httpx, ``TG_BOT_TOKEN``) with inline decision buttons. A send failure → a log entry, the request stays ``pending`` (the safety net — ``/trials`` in the bot).

All functions take an ``AsyncSession`` — tested on a mock session (ADR-013), without network and a real DB. Since VG-01 (0021) ``api`` runs under the role ``tendersrb_app`` (NOSUPERUSER, FORCE RLS), therefore ``repo.set_tenant`` is MANDATORY (the RLS contract; without it the query fails with ``UndefinedObjectError``) AND, as an extra layer, an explicit ``WHERE tenant_id`` in the queries (as :mod:`src.api.repo`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from src.api import repo
from src.domain.access import access_active
from src.plans.catalog import UPGRADE_REQUEST_PREFIX

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

# The access status + the latest request in one query: EXISTS(pending) and the value of
# the tenant’s access_until (+ an explicit WHERE tenant_id as an extra layer on top of RLS).
_ACCESS_SQL = text("""
    SELECT
        (SELECT access_until FROM tenants WHERE id = CAST(:tid AS uuid))
            AS access_until,
        (SELECT status FROM trial_requests
          WHERE tenant_id = CAST(:tid AS uuid)
          ORDER BY requested_at DESC LIMIT 1) AS last_status
    """)

# Anti-duplicate: whether the tenant has a pending request + the current access_until.
_GUARD_SQL = text("""
    SELECT
        EXISTS(
            SELECT 1 FROM trial_requests
             WHERE tenant_id = CAST(:tid AS uuid) AND status = 'pending'
        ) AS has_pending,
        (SELECT access_until FROM tenants WHERE id = CAST(:tid AS uuid))
            AS access_until
    """)

_INSERT_SQL = text("""
    INSERT INTO trial_requests (tenant_id, comment)
    VALUES (CAST(:tid AS uuid), :comment)
    RETURNING id
    """)

# The initiator’s email (the session’s login user) — for the admin notification text.
_EMAIL_SQL = text("SELECT email FROM users WHERE id = CAST(:uid AS uuid)")


def _fmt_date(value: datetime | None) -> str | None:
    """``datetime`` → ``DD.MM.YYYY`` (or ``None``)."""
    return value.strftime("%d.%m.%Y") if value is not None else None


@dataclass(frozen=True)
class AccessStatus:
    """The tenant’s access status for the dashboard.

    state: ``active`` (there is access) / ``pending`` (the request awaits a decision) / ``expired`` (access has expired) / ``rejected`` (the latest request was rejected, no access) / ``none`` (no access, no requests were made).
    active: the delivery is running (``access_until`` NULL or in the future).
    can_request: a request can be created (no pending AND no active access).
    until: the date of the end of access ``DD.MM.YYYY`` (or ``None`` — unlimited / none).

    There is deliberately NO ready status label here: the access label is localised in the template by ``state`` (+ ``until`` for the variant “access until …”) from the ``cabinet.i18n`` catalog (keys ``access_*``). The service does not carry the locale — otherwise the English cockpit would keep a Russian label (the owner’s complaint, 23.07).
    """

    state: str
    active: bool
    can_request: bool
    until: str | None


@dataclass(frozen=True)
class CreateResult:
    """The result of :func:`create_trial_request`."""

    ok: bool
    reason: str | None = None
    request_id: str | None = None
    email: str | None = None


async def get_access_status(db, tenant_id: str) -> AccessStatus:
    """The tenant’s access status (for the cabinet dashboard).

    Requires the RLS context (we set ``app.current_tenant_id``) + an explicit ``WHERE tenant_id`` in the query. ``access_until`` NULL is treated as permanent active access (demo / test tenants).
    """
    await repo.set_tenant(db, tenant_id)
    row = (await db.execute(_ACCESS_SQL, {"tid": str(tenant_id)})).first()
    access_until = row.access_until if row is not None else None
    last_status = row.last_status if row is not None else None
    now = datetime.now(timezone.utc)

    has_pending = last_status == "pending"
    active = access_active(access_until, now)  # SSOT predicate (FIX-SG-01)
    until = _fmt_date(access_until)

    if has_pending:
        return AccessStatus("pending", active, False, until)
    if active:
        # The label (“access until {until}” / “…without a time limit”) is assembled by the
        # template from state + until — the service gives only facts.
        return AccessStatus("active", True, False, until)
    # no access (expired or never issued), and no pending either → a request can be made.
    if last_status == "rejected":
        return AccessStatus("rejected", False, True, None)
    if access_until is not None:
        return AccessStatus("expired", False, True, None)
    return AccessStatus("none", False, True, None)


async def create_trial_request(
    db, *, tenant_id: str, user_id: str, comment: str | None
) -> CreateResult:
    """Creates a pending request if there is no other pending AND no active access.

    Returns :class:`CreateResult`: ``ok=True`` + ``request_id`` + the initiator’s ``email`` (for the admin notification), or ``ok=False`` + ``reason`` (``pending`` — the request is already under review; ``active`` — access is still active).

    The exception — the UPGRADE requests of the showcase (``comment`` starts with ``upgrade_request:``): they are created EVEN with an active ``access_until``. Otherwise a trial user (whose access_until is active) would silently lose the subscription request — the admin gets nothing, while the user sees “sent” (promise without implementation).
    An extension request (``extension_request``, the banner “subscription ended”) goes the ordinary way: ``active`` blocks (there is nothing to extend), ``pending`` does not allow duplicates. We keep the anti-spam: a duplicate is still blocked by the existing ``pending``.
    The behavior of the ordinary trial-access request from the dashboard does NOT change.
    """
    is_upgrade = comment is not None and comment.startswith(UPGRADE_REQUEST_PREFIX)
    await repo.set_tenant(db, tenant_id)
    guard = (await db.execute(_GUARD_SQL, {"tid": str(tenant_id)})).first()
    has_pending = bool(guard.has_pending) if guard is not None else False
    access_until = guard.access_until if guard is not None else None
    now = datetime.now(timezone.utc)

    if has_pending:
        return CreateResult(False, reason="pending")
    if not is_upgrade and access_active(
        access_until, now
    ):  # SSOT predicate (FIX-SG-01)
        return CreateResult(False, reason="active")

    new_id = (
        await db.execute(_INSERT_SQL, {"tid": str(tenant_id), "comment": comment})
    ).scalar_one()
    email = (await db.execute(_EMAIL_SQL, {"uid": str(user_id)})).scalar_one_or_none()
    await db.commit()
    return CreateResult(True, request_id=str(new_id), email=email)


def _admin_keyboard(request_id: str) -> dict:
    """The inline decision keyboard for the Bot API (the same callback format as the bot).

    callback_data: ``tr:<request_id>:<3|15|30|deny>``. The cabinet does not pull in aiogram, so we assemble reply_markup as a raw dict for ``sendMessage``.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "✅ 3 дня", "callback_data": f"tr:{request_id}:3"},
                {"text": "✅ 15 дней", "callback_data": f"tr:{request_id}:15"},
                {"text": "✅ 30 дней", "callback_data": f"tr:{request_id}:30"},
            ],
            [{"text": "❌ Отклонить", "callback_data": f"tr:{request_id}:deny"}],
        ]
    }


async def notify_admin(
    *,
    token: str,
    admin_chat_id: int,
    request_id: str,
    email: str | None,
    comment: str | None,
    timeout: float = 10.0,
) -> bool:
    """Best-effort notification of the owner in Telegram about a new request (the Bot API directly).

    Returns ``True`` on a successful send, ``False`` on any failure (network / HTTP / missing token). On failure the request stays ``pending`` — the owner will see it with the ``/trials`` command. It never raises outwards (it does not break the cabinet’s POST).
    """
    if not token or not admin_chat_id:
        return False
    when = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    lines = [
        "🆕 Запрос тестового доступа",
        f"Email: {email or '—'}",
        f"Комментарий: {comment or '—'}",
        f"Дата: {when}",
    ]
    payload = {
        "chat_id": admin_chat_id,
        "text": "\n".join(lines),
        "reply_markup": _admin_keyboard(request_id),
    }
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    try:
        import httpx  # lazy

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        return True
    except Exception:  # noqa: BLE001 — best-effort, fallback /trials
        logger.warning(
            "notify_admin: the notification could not be sent", exc_info=True
        )
        return False


async def send_admin_text(
    *,
    token: str,
    admin_chat_id: int,
    text: str,
    timeout: float = 10.0,
) -> bool:
    """Best-effort ordinary message to the service chat (HTML parse_mode, without a keyboard).

    ``text`` — already ESCAPED by the caller (``html.escape`` for any user input). PII: only the error class goes to the log (the URL with the bot token and the request body can get into an httpx exception, so neither ``exc_info`` nor ``str(exc)``).
    It never raises outwards.
    """
    if not token or not admin_chat_id:
        return False
    payload = {"chat_id": admin_chat_id, "text": text, "parse_mode": "HTML"}
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    try:
        import httpx  # lazy

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("send_admin_text: not sent (%s)", type(exc).__name__)
        return False
