"""Billing Notifications Worker — payment lifecycle e-mails (blocker #3).

The payment gateway's webhook silently changed the DB (payment passed / failed / canceled /
renewed) — the subscriber did not know the status of their money (LAUNCH_AUDIT §3). The core
:func:`src.billing.service.apply_billing_event` now puts an e-mail into the queue
``billing_notification_outbox`` (migration 0033) IN THE SAME TRANSACTION as the
dedup-claim of the webhook; this polling worker picks it up and sends the e-mail.

E-mails by ``kind`` (= ``BillingEvent.type``), locale **ru** (the invariant of the project rules:
the bot and the cabinet are ru only), a service tone:

* ``activated`` — “Payment received, subscription is active”.
* ``renewed``   — “Subscription renewed”.
* ``past_due``  — “Payment failed — access is kept until <access_until>”.
* ``canceled``  — “Subscription canceled — access until <access_until>”.

‼️ ``canceled``/``past_due`` do NOT cut access (the core does not apply them to the tenant —
it expires by itself by ``tenants.access_until``), therefore the text says “access until
<date>”, and NOT “access disabled”.

Idempotency — claim-then-send through ``sent_at`` (double protection):

1. **A double webhook = one e-mail.** The enqueue is idempotent by ``(provider,
   event_id)`` (ON CONFLICT DO NOTHING in the core) — the second webhook does not
   double the row.
2. **A double run of the worker = one e-mail.** The claim of the row —
   ``app_billing_mark_notification_sent`` (``UPDATE ... WHERE sent_at IS NULL
   RETURNING id``): the id returns only to the first; a race/repeat gets nothing.
3. **A channel failure does NOT roll the mark back.** The order: mark (a short transaction),
   then send. A rollback would return ``sent_at=NULL`` and loop the sending every cycle
   (anti-spam — as in ``subscription_reminders``/``channel_dead_notify``).
4. **No valid e-mail — we do not claim, we do not fail.** The address check goes BEFORE the claim:
   the e-mail will still go out if the address is fixed (the magic-link login requires an e-mail,
   so the case is rare).

The DB access contour: the queue is a platform table WITHOUT a direct grant to the app role
(REVOKE ALL in 0033, the model ``admin_audit_log`` 0030). The worker reads/writes it
ONLY through the SECURITY DEFINER functions ``app_billing_*`` (the owner-superuser bypasses the grant control of the table). There is no direct SELECT/UPDATE under the app role.

Privacy: the logs get only ``tenant_id``/``kind``/id. The subscriber's e-mail is NEVER logged (neither in INFO nor in exception) — PII (``SMTPRecipientsRefused``
puts the recipient right into the arguments, ``logger.exception`` prints them in full —
we log only the CLASS of the error).

The dependencies are injected (the e-mail sender, the session factory, the interval, the base URL
of the cabinet, the batch size) — unit tests without a real PostgreSQL/network (ADR-013).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import text

from src.api.emailer import EmailSender, MockEmailSender
from src.db.session import session_scope, set_current_tenant
from src.i18n_email import email_t

logger = logging.getLogger(__name__)

DEFAULT_CABINET_BASE_URL = "https://nab.rs"
DEFAULT_TZ = "Europe/Belgrade"
CABINET_PATH = "/app"  # Cabinet: the subscriber’s dashboard (routes_profiles).
BATCH_LIMIT = 200  # how many unsent e-mails to take in one pass.

# Unsent e-mails with the addressee already resolved (SECURITY DEFINER 0033/0035):
# the app role has no direct access to the queue. plan/decision_comment are filled only
# for the decision e-mails on a request (manual_approved/manual_rejected, 0035); for e-mails
# of the payment lifecycle — NULL (their texts ignore these columns).
_PENDING_SQL = text(
    "SELECT id, tenant_id, kind, email, email_status, access_until, tz, "
    "plan, decision_comment "
    "FROM app_billing_pending_notifications(CAST(:limit AS int))"
)

# The CABINET language of the recipient (``users.locale``); the tenant is set before the request.
_LOCALE_SQL = text(
    "SELECT locale FROM users "
    "WHERE tenant_id = CAST(:tid AS uuid) AND deleted_at IS NULL "
    "AND lower(btrim(email)) = lower(btrim(CAST(:email AS text))) "
    "ORDER BY created_at, id LIMIT 1"
)

# Atomic claim of the row: the id returns only on the first NULL→now() (0033).
_MARK_SENT_SQL = text(
    "SELECT app_billing_mark_notification_sent(CAST(:id AS bigint)) AS id"
)


def _resolve_tz(tz_name: str | None) -> Any:
    name = (tz_name or "").strip() or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return timezone.utc


def build_cabinet_url(cabinet_base_url: str) -> str:
    """A direct link to the subscriber’s dashboard in the cabinet."""
    base = (cabinet_base_url or DEFAULT_CABINET_BASE_URL).rstrip("/")
    return f"{base}{CABINET_PATH}"


def _fmt_date(access_until: datetime | None, tz: Any) -> str:
    """The access-end date in the tenant’s time zone; None → empty (a safety net)."""
    if access_until is None:
        return ""
    return access_until.astimezone(tz).strftime("%d.%m.%Y")


_KINDS = (
    "activated",
    "renewed",
    "past_due",
    "canceled",
    "manual_approved",
    "manual_rejected",
)


def build_email_subject(kind: str, locale: str | None = None) -> str:
    """The subject of the e-mail by the event type in the language of ``users.locale`` (None → sr).

    ``manual_approved``/``manual_rejected`` — the reply to the subscriber about the decision on a request
    (0035, a channel independent of Telegram); the others — the payment lifecycle.
    """
    return email_t(locale, f"bn_subject_{kind if kind in _KINDS else 'default'}")


def _comment_block(decision_comment: str | None, locale: str | None = None) -> str:
    """The block of the operator’s comment for the decision e-mails (empty → no block)."""
    text_value = (decision_comment or "").strip()
    if not text_value:
        return ""
    return "\n\n" + email_t(locale, "bn_comment").format(text=text_value)


def build_email_body(
    kind: str,
    *,
    access_until: datetime | None,
    tz: Any,
    cabinet_url: str,
    plan: str | None = None,
    decision_comment: str | None = None,
    locale: str | None = None,
) -> str:
    """The e-mail text by the event type in the language of ``users.locale`` (None → sr).

    ``past_due``/``canceled`` report “access is kept until <date>”, and NOT
    “access disabled”: the core does NOT apply these events to access — it expires by itself
    by ``tenants.access_until``.

    ``manual_approved``/``manual_rejected`` (0035) — the reply to the subscriber about the decision on
    a request: ``plan`` names the activated subscription (None → trial access),
    ``decision_comment`` — the operator’s comment (if any). A channel independent of Telegram
    (e-mail + the cabinet): the subscriber learns the decision even if Telegram is not linked or has dropped.
    """
    date_str = _fmt_date(access_until, tz)
    until = email_t(locale, "bn_until").format(date=date_str) if date_str else ""
    footer = "\n\n" + email_t(locale, "bn_footer").format(url=cabinet_url)
    key = f"bn_body_{kind if kind in _KINDS else 'default'}"
    body = email_t(locale, key)
    if kind == "manual_approved":
        if plan:
            what_key = "bn_what_plan_until" if until else "bn_what_plan"
            what = email_t(locale, what_key).format(plan=plan.capitalize(), until=until)
        else:
            what_key = "bn_what_trial_until" if until else "bn_what_trial"
            what = email_t(locale, what_key).format(until=until)
        return (
            body.format(what=what) + _comment_block(decision_comment, locale) + footer
        )
    if kind == "manual_rejected":
        return body + _comment_block(decision_comment, locale) + footer
    # An unknown kind → a neutral notification (key ``default``).
    return body.format(until=until) + footer


class BillingNotificationsWorker:
    """The polling worker of the payment lifecycle e-mails (claim-then-send, idempotent)."""

    def __init__(
        self,
        *,
        email_sender: EmailSender | None = None,
        session_factory: Callable[[], Any] = session_scope,
        poll_interval: float = 60.0,
        cabinet_base_url: str = DEFAULT_CABINET_BASE_URL,
        batch_limit: int = BATCH_LIMIT,
    ) -> None:
        self._email = email_sender or MockEmailSender()
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        self._cabinet_url = build_cabinet_url(cabinet_base_url)
        self._batch_limit = batch_limit
        self._stopped = asyncio.Event()

    # -- public API ----------------------------------------------------- #

    def stop(self) -> None:
        """Asks the worker to finish after the current iteration."""
        self._stopped.set()

    async def run(self) -> None:
        logger.info(
            "billing_notifications: polling started (interval=%.0fs, batch=%d)",
            self._poll_interval,
            self._batch_limit,
        )
        while not self._stopped.is_set():
            try:
                sent = await self.poll_once()
                if sent:
                    logger.info(
                        "billing_notifications: e-mails sent per cycle: %d", sent
                    )
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ):  # noqa: BLE001 — a cycle failure does not break the worker
                logger.exception("billing_notifications: polling cycle error")
            await self._sleep(self._poll_interval)

    async def poll_once(self) -> int:
        """One pass: queue → address → claim → send. Returns the number of e-mails."""
        sent = 0
        for row in await self._load_pending():
            # We check the address BEFORE the claim: a claim without the ability to send would mark
            # the e-mail as sent forever, and the subscriber would never learn the status
            # even after the address is fixed (as in channel_dead_notify).
            if not self._can_email(row):
                logger.warning(
                    "billing_notifications: the e-mail exists, but there is nowhere to send it "
                    "(no valid e-mail) tenant=%s kind=%s id=%s",
                    row.tenant_id,
                    row.kind,
                    row.id,
                )
                continue
            if not await self._claim(row.id):
                continue  # already sent / taken by another run — we do not duplicate
            if await self._send_email(row):
                sent += 1
        return sent

    # -- internal -------------------------------------------------------- #

    @staticmethod
    def _can_email(row: Any) -> bool:
        """Is there anywhere to send: the address is set and not rejected as invalid.

        Mirrors the branch “the address is valid” in the ORDER BY of the function
        ``app_billing_pending_notifications`` (it lifts the suitable user to the top) — the predicates must match.
        """
        return bool((row.email or "").strip()) and row.email_status != "bounced"

    async def _load_pending(self) -> list[Any]:
        async with self._session_factory() as session:
            result = await session.execute(_PENDING_SQL, {"limit": self._batch_limit})
            return list(result)

    async def _claim(self, notification_id: Any) -> bool:
        """Atomically marks the e-mail as sent. True — ours, we send it.

        The mark goes BEFORE the sending: a channel failure is not rolled back (otherwise a retry every
        cycle). An empty RETURNING id → the row was already taken by another run/a repeat.
        """
        async with self._session_factory() as session:
            result = await session.execute(_MARK_SENT_SQL, {"id": notification_id})
            await session.commit()
        row = result.first()
        return bool(row is not None and row[0] is not None)

    async def _load_locale(self, row: Any) -> str | None:
        """The ``users.locale`` of the recipient (the CABINET language → the e-mail language).

        The SECURITY DEFINER function of the queue (0033/0035) does not return the locale column, and the migration for it is RED, so the language is read by a separate query under the tenant of the row (FORCE RLS: ``set_tenant`` first). The same addressee as in the queue (by e-mail), deterministically (ORDER BY). Any failure → None (→ sr): the e-mail language must not break the sending. Only the error class goes into the log.
        """
        try:
            async with self._session_factory() as session:
                await set_current_tenant(session, row.tenant_id)
                result = await session.execute(
                    _LOCALE_SQL, {"tid": str(row.tenant_id), "email": row.email}
                )
                found = result.first()
        except (
            Exception
        ) as exc:  # noqa: BLE001 — the e-mail language must not break send
            logger.warning(
                "billing_notifications: locale lookup: %s", type(exc).__name__
            )
            return None
        return found[0] if found is not None else None

    async def _send_email(self, row: Any) -> bool:
        """Sends the e-mail. The mark is NOT rolled back on failure (see the module docstring).

        The addressee has already been checked by :meth:`_can_email` before the claim. The subscriber's address does not get into the logs — PII.
        """
        tz = _resolve_tz(row.tz)
        locale = await self._load_locale(row)
        try:
            await self._email.send(
                to=row.email,
                subject=build_email_subject(row.kind, locale),
                text_body=build_email_body(
                    row.kind,
                    access_until=row.access_until,
                    tz=tz,
                    cabinet_url=self._cabinet_url,
                    plan=getattr(row, "plan", None),
                    decision_comment=getattr(row, "decision_comment", None),
                    locale=locale,
                ),
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 — a channel failure does not roll the mark back
            # ‼️ NOT logger.exception and NOT %s of exc: the subscriber’s address is PII, and the
            # text of the SMTP error contains it. We log only the CLASS of the error.
            logger.error(
                "billing_notifications: e-mail failure tenant=%s kind=%s id=%s error=%s",
                row.tenant_id,
                row.kind,
                row.id,
                type(exc).__name__,
            )
            return False
        logger.info(
            "billing_notifications: e-mail sent tenant=%s kind=%s id=%s",
            row.tenant_id,
            row.kind,
            row.id,
        )
        return True

    async def _sleep(self, delay: float) -> None:
        """An interruptible sleep: reacts to stop() while waiting."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the payment e-mails worker (the container via Compose)."""
    from src.api.emailer import make_email_sender
    from src.log_secrets import install_secret_log_filter

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()
    email_sender = make_email_sender(
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", ""),
        postmark_token=os.getenv("POSTMARK_SERVER_TOKEN", ""),
        email_from=os.getenv("EMAIL_FROM", "noreply@nab.rs"),
    )
    worker = BillingNotificationsWorker(
        email_sender=email_sender,
        cabinet_base_url=os.getenv("CABINET_BASE_URL", DEFAULT_CABINET_BASE_URL),
    )
    await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
