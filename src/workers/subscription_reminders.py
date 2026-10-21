"""Subscription Reminder Worker — reminders about the subscription expiry.

A polling worker: once a day by the tenant's local time it sends the subscriber a
reminder about the approaching date of the access expiry (``tenants.access_until``)
for :data:`THRESHOLDS_DAYS` days (by default 7 and 1). Channels: Telegram
(if there is an active binding) AND e-mail (if there is a valid address). Both
channels are tried for one claim.

The second pass of the same cycle — the notification “access has already expired” (the threshold
:data:`EXPIRED_THRESHOLD`): to the tenants whose ``access_until`` is in the past,
but not older than :data:`EXPIRED_GRACE_HOURS`. The matching (``src/workers/matching.py``)
stops the delivery at the same moment the access expires — without this
notification the client sees only silence and does not link it with the payment.

Idempotency — claim-then-send through ``subscription_reminder_log``
(migration 0017):

1. **One threshold — once for one expiry date.** The UNIQUE
   ``(tenant_id, threshold_days, access_until_snapshot)`` guarantees
   “do not send twice”. On a **renewal** (a new ``access_until``) the
   snapshot changes → the UNIQUE is different → the thresholds fire again. The same
   works for ``EXPIRED_THRESHOLD`` — it has the same ``threshold_days``,
   just outside the set :data:`THRESHOLDS_DAYS`.
2. **A channel failure does NOT roll the log back.** The order: claim (a short
   transaction, commit), then send. If one of the channels failed — the log is
   NOT rolled back (anti-spam: rollback → a repeated claim → a repeated sending).
3. **NULL access_until — do not send.** Both selections filter
   ``access_until IS NOT NULL``; the window is split between ``_TENANTS_SQL``
   (``> now()``, thresholds 7/1) and ``_EXPIRED_TENANTS_SQL`` (``<= now()`` and
   not older than :data:`EXPIRED_GRACE_HOURS`) — the ones expired LONGER ago than the window are not touched
   (see the warning at :data:`EXPIRED_GRACE_HOURS`).

RISK / the decision “no channel at all”: if the tenant has no active
TG channel AND no valid e-mail — the claim is NOT made, and the log is not written.
If a channel appears in the future, the reminder can still go out (if
access_until is still within the threshold). This means that in a rare scenario
“the channel appeared the same day” the reminder will be sent. The alternative
(to make the claim at once but not send) would create a “dead” record without a send
and lose the chance to send through the new channel. The choice: anti-spam matters more than
a possible delay of one day — similarly to the summary (we do not write an empty summary
to the log).

The worker goes to the DB **privileged** (without ``SET app.current_tenant_id``):
a cross-tenant server operation, the owner bypasses RLS (ARCHITECTURE §8).

The dependencies are injected (the adapter, the e-mail sender, the session, the interval,
the thresholds, the “current time”) — for unit tests without a real PostgreSQL/network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import text

from src.api.emailer import EmailSender, MockEmailSender
from src.db.session import session_scope
from src.i18n_common import common_t, days_phrase
from src.i18n_email import email_t
from src.workers.adapters.base import DeliveryAdapter

logger = logging.getLogger(__name__)

DEFAULT_TZ = "Europe/Belgrade"
SEND_HOUR = (
    9  # we send not earlier than 9:00 in the tenant’s tz (the analogue of SUMMARY_HOUR)
)
THRESHOLDS_DAYS: tuple[int, ...] = (7, 1)

# The threshold “access has already expired” — a log marker, separate from THRESHOLDS_DAYS
# (0 is not in 7/1 and cannot match them in days_until_access).
EXPIRED_THRESHOLD = 0

# ‼️ The cutoff window is MANDATORY: without it the very first run in prod would send a
# notification to ALL tenants with an expired access_until, including those expired months
# ago (2 tenants on 06.09.2026 — 396f699b since 06.08, e968c1ff since 04.08). This is the
# same trap that gave 765 extra messages in the backfill in July.
EXPIRED_GRACE_HOURS = 48

SUPPORT_CONTACT = "support@nab.rs"

# The tenants with a non-zero and not expired access_until. LEFT JOIN to user_channels:
# we take one TG channel per tenant (the freshest linked_at). The e-mail is taken from
# users. The worker goes privileged — without SET tenant.
#
# ‼️ TWO channels — TWO languages, and this is not duplication but different axes. The e-mail goes to
# the address from ``users`` → its language = the language of the CABINET (``u.locale``). Telegram goes to
# the bound channel → its language = the language of the CHANNEL, with the fallback to the cabinet
# (``COALESCE(uc.locale, u.locale)``, migration 0038 — the same as for delivery).
# The client normally has a cabinet in ru and Telegram in sr; to merge them into one column
# would mean lying to one of the channels.
_TENANTS_SQL = text("""
    SELECT DISTINCT ON (ten.id)
           ten.id                        AS tenant_id,
           ten.access_until              AS access_until,
           COALESCE(ten.tz, :default_tz) AS tz,
           u.email                       AS email,
           u.email_status                AS email_status,
           u.locale                      AS email_locale,
           uc.external_id                AS tg_chat_id,
           COALESCE(uc.locale, u.locale) AS tg_locale
    FROM tenants ten
    JOIN users u          ON u.tenant_id = ten.id
                         AND u.deleted_at IS NULL
    LEFT JOIN user_channels uc ON uc.user_id = u.id
                              AND uc.channel = 'telegram'
                              AND uc.status = 'active'
    WHERE ten.access_until IS NOT NULL
      AND ten.access_until > now()
    ORDER BY ten.id, uc.linked_at DESC NULLS LAST
    """)

# The tenants whose access_until is ALREADY IN THE PAST, but not older than the cutoff window
# EXPIRED_GRACE_HOURS. A mirror of _TENANTS_SQL (the same JOIN/channels/DISTINCT ON),
# differs only in the condition on access_until — see the docstring of EXPIRED_GRACE_HOURS.
_EXPIRED_TENANTS_SQL = text("""
    SELECT DISTINCT ON (ten.id)
           ten.id                        AS tenant_id,
           ten.access_until              AS access_until,
           COALESCE(ten.tz, :default_tz) AS tz,
           u.email                       AS email,
           u.email_status                AS email_status,
           u.locale                      AS email_locale,
           uc.external_id                AS tg_chat_id,
           COALESCE(uc.locale, u.locale) AS tg_locale
    FROM tenants ten
    JOIN users u          ON u.tenant_id = ten.id
                         AND u.deleted_at IS NULL
    LEFT JOIN user_channels uc ON uc.user_id = u.id
                              AND uc.channel = 'telegram'
                              AND uc.status = 'active'
    WHERE ten.access_until IS NOT NULL
      AND ten.access_until <= now()
      AND ten.access_until > now() - (CAST(:grace_hours AS int) * INTERVAL '1 hour')
    ORDER BY ten.id, uc.linked_at DESC NULLS LAST
    """)

# Claim-then-send: RETURNING returns a row only on the first claim (ON CONFLICT
# DO NOTHING). UNIQUE (tenant_id, threshold_days, access_until_snapshot) —
# “one threshold for one expiry date — once”).
_CLAIM_SQL = text("""
    INSERT INTO subscription_reminder_log
           (tenant_id, threshold_days, access_until_snapshot)
    VALUES (:tenant_id, :threshold, :access_until)
    ON CONFLICT (tenant_id, threshold_days, access_until_snapshot) DO NOTHING
    RETURNING id
    """)


def _resolve_tz(tz_name: str | None) -> Any:
    name = (tz_name or "").strip() or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return timezone.utc


def days_until_access(access_until: datetime, *, now: datetime, tz: Any) -> int:
    """The whole calendar days to access_until in the zone tz (floor by dates)."""
    until_date = access_until.astimezone(tz).date()
    now_date = now.astimezone(tz).date()
    return (until_date - now_date).days


def build_tg_text(
    threshold_days: int, access_until: datetime, tz: Any, locale: str | None = None
) -> str:
    """The text of the reminder in Telegram; ``locale`` — the language of the CHANNEL (``tg_locale``).

    The catalog of the shared vocabulary (:mod:`src.i18n_common`) — the same as for the delivery card
    in the bot. ``None``/unknown code → ru (an explicit fallback ``common_t``).
    """
    date_str = access_until.astimezone(tz).strftime("%d.%m.%Y")
    expiring = common_t(locale, "sub_expiring").format(
        days=days_phrase(threshold_days, locale), date=date_str
    )
    renew = common_t(locale, "sub_renew").format(contact=SUPPORT_CONTACT)
    return f"⏰ {expiring}\n{renew}"


def build_email_subject(threshold_days: int, locale: str | None = None) -> str:
    """The e-mail subject; ``locale`` — the language of the CABINET (``email_locale``), the e-mail catalog."""
    return email_t(locale, "sub_subject").format(
        days=days_phrase(threshold_days, locale)
    )


def build_email_body(
    threshold_days: int, access_until: datetime, tz: Any, locale: str | None = None
) -> str:
    """The e-mail body; ``locale`` — the language of the CABINET (``email_locale``), the e-mail catalog."""
    date_str = access_until.astimezone(tz).strftime("%d.%m.%Y")
    expiring = email_t(locale, "sub_expiring").format(
        days=days_phrase(threshold_days, locale), date=date_str
    )
    renew = email_t(locale, "sub_renew").format(contact=SUPPORT_CONTACT)
    return f"{expiring}\n{renew}"


def build_tg_expired_text(
    _threshold_days: int, access_until: datetime, tz: Any, locale: str | None = None
) -> str:
    """The text of “access has already expired” in Telegram; ``locale`` — the language of the CHANNEL.

    ``_threshold_days`` is not used (the signature matches
    :func:`build_tg_text` for the sake of the common sending code in :class:`SubscriptionReminderWorker`).
    """
    date_str = access_until.astimezone(tz).strftime("%d.%m.%Y")
    expired = common_t(locale, "sub_expired").format(date=date_str)
    renew = common_t(locale, "sub_expired_renew").format(contact=SUPPORT_CONTACT)
    return f"⏰ {expired}\n{renew}"


def build_email_expired_subject(_threshold_days: int, locale: str | None = None) -> str:
    """The e-mail subject “access has already expired”; ``locale`` — the language of the CABINET."""
    return email_t(locale, "sub_expired_subject")


def build_email_expired_body(
    _threshold_days: int, access_until: datetime, tz: Any, locale: str | None = None
) -> str:
    """The e-mail body “access has already expired”; ``locale`` — the language of the CABINET."""
    date_str = access_until.astimezone(tz).strftime("%d.%m.%Y")
    expired = email_t(locale, "sub_expired").format(date=date_str)
    renew = email_t(locale, "sub_expired_renew").format(contact=SUPPORT_CONTACT)
    return f"{expired}\n{renew}"


class SubscriptionReminderWorker:
    """The polling worker of the subscription-expiry reminders (thresholds 7/1, idempotent)."""

    def __init__(
        self,
        *,
        tg_adapter: DeliveryAdapter | None = None,
        email_sender: EmailSender | None = None,
        session_factory: Callable[[], Any] = session_scope,
        poll_interval: float = 3600.0,
        thresholds: tuple[int, ...] = THRESHOLDS_DAYS,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._tg = tg_adapter
        self._email = email_sender or MockEmailSender()
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        self._thresholds = tuple(thresholds)
        self._thresholds_set = set(self._thresholds)
        self._now = now_factory or (lambda: datetime.now(timezone.utc))
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        logger.info(
            "subscription_reminders: polling started (thresholds=%s, hour=%d, interval=%.0fs)",
            self._thresholds,
            SEND_HOUR,
            self._poll_interval,
        )
        while not self._stopped.is_set():
            try:
                sent = await self.poll_once()
                if sent:
                    logger.info(
                        "subscription_reminders: reminders sent per cycle: %d",
                        sent,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("subscription_reminders: polling cycle error")
            await self._sleep(self._poll_interval)

    async def poll_once(self) -> int:
        """One pass: tenants → thresholds → claim → send. Returns the number of claims."""
        sent = 0
        now = self._now()
        for row in await self._load_tenants():
            tz = _resolve_tz(row.tz)
            local_now = now.astimezone(tz)
            if local_now.hour < SEND_HOUR:
                continue
            days = days_until_access(row.access_until, now=now, tz=tz)
            if days not in self._thresholds_set:
                continue

            # We check the presence of at least one channel BEFORE the claim.
            # If there is neither TG nor e-mail — we do not claim: if a channel appears
            # in the future, the reminder can still go out (see the RISK in the module docstring).
            has_tg = bool(row.tg_chat_id)
            has_email = bool(row.email) and row.email_status != "bounced"
            if not has_tg and not has_email:
                continue

            if not await self._claim(row.tenant_id, days, row.access_until):
                continue

            if await self._notify(row, days, tz):
                sent += 1

        for row in await self._load_expired_tenants():
            tz = _resolve_tz(row.tz)
            local_now = now.astimezone(tz)
            if local_now.hour < SEND_HOUR:
                continue

            has_tg = bool(row.tg_chat_id)
            has_email = bool(row.email) and row.email_status != "bounced"
            if not has_tg and not has_email:
                continue

            if not await self._claim(
                row.tenant_id, EXPIRED_THRESHOLD, row.access_until
            ):
                continue

            if await self._notify(
                row,
                EXPIRED_THRESHOLD,
                tz,
                tg_builder=build_tg_expired_text,
                subject_builder=build_email_expired_subject,
                body_builder=build_email_expired_body,
            ):
                sent += 1
        return sent

    async def _load_tenants(self) -> list[Any]:
        async with self._session_factory() as session:
            result = await session.execute(_TENANTS_SQL, {"default_tz": DEFAULT_TZ})
            return list(result)

    async def _load_expired_tenants(self) -> list[Any]:
        async with self._session_factory() as session:
            result = await session.execute(
                _EXPIRED_TENANTS_SQL,
                {"default_tz": DEFAULT_TZ, "grace_hours": EXPIRED_GRACE_HOURS},
            )
            return list(result)

    async def _claim(
        self, tenant_id: Any, threshold: int, access_until: datetime
    ) -> bool:
        """The atomic claim in subscription_reminder_log. True — ours, we send."""
        async with self._session_factory() as session:
            result = await session.execute(
                _CLAIM_SQL,
                {
                    "tenant_id": tenant_id,
                    "threshold": threshold,
                    "access_until": access_until,
                },
            )
            await session.commit()
        return bool(list(result))

    async def _notify(
        self,
        row: Any,
        threshold: int,
        tz: Any,
        *,
        tg_builder: Callable[[int, datetime, Any, str | None], str] = build_tg_text,
        subject_builder: Callable[[int, str | None], str] = build_email_subject,
        body_builder: Callable[
            [int, datetime, Any, str | None], str
        ] = build_email_body,
    ) -> bool:
        """Sends the reminder to TG and/or e-mail. The log is NOT rolled back on failure.

        A failure of one channel does not roll back the claim and does not block the other channel:
        we try to send to all available. ``*_builder`` — by default the texts
        of the threshold reminder (7/1); the call with ``build_*_expired_*`` — the same
        sending mechanics for the notification “access has already expired”.
        """
        tg_ok = await self._send_tg(row, threshold, tz, tg_builder)
        email_ok = await self._send_email(
            row, threshold, tz, subject_builder, body_builder
        )
        if tg_ok or email_ok:
            logger.info(
                "subscription_reminders: tenant=%s threshold=%s tg=%s email=%s",
                row.tenant_id,
                threshold,
                tg_ok,
                email_ok,
            )
            return True
        logger.warning(
            "subscription_reminders: all channels refused tenant=%s threshold=%s",
            row.tenant_id,
            threshold,
        )
        return False

    async def _send_tg(
        self,
        row: Any,
        threshold: int,
        tz: Any,
        tg_builder: Callable[[int, datetime, Any, str | None], str] = build_tg_text,
    ) -> bool:
        if not row.tg_chat_id or self._tg is None:
            return False
        # The language of the CHANNEL (getattr: the old test fakes without the column do not break).
        text = tg_builder(
            threshold, row.access_until, tz, getattr(row, "tg_locale", None)
        )
        try:
            result = await self._tg.send_text(
                row.tg_chat_id, text, locale=getattr(row, "tg_locale", None)
            )
        except Exception:  # noqa: BLE001 — a channel failure does not roll the log back
            logger.exception(
                "subscription_reminders: TG failure tenant=%s threshold=%s",
                row.tenant_id,
                threshold,
            )
            return False
        if not result.ok:
            logger.warning(
                "subscription_reminders: TG did not accept tenant=%s threshold=%s code=%s",
                row.tenant_id,
                threshold,
                result.response_code,
            )
            return False
        return True

    async def _send_email(
        self,
        row: Any,
        threshold: int,
        tz: Any,
        subject_builder: Callable[[int, str | None], str] = build_email_subject,
        body_builder: Callable[
            [int, datetime, Any, str | None], str
        ] = build_email_body,
    ) -> bool:
        if not row.email or row.email_status == "bounced":
            return False
        # The CABINET language — the e-mail has its own, not the language setting of the TG channel.
        locale = getattr(row, "email_locale", None)
        try:
            await self._email.send(
                to=row.email,
                subject=subject_builder(threshold, locale),
                text_body=body_builder(threshold, row.access_until, tz, locale),
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 — a channel failure does not roll the log back
            # ‼️ NOT logger.exception and NOT %s of exc: the subscriber’s address is PII, and the
            # the text of the SMTP error contains it (`SMTPRecipientsRefused` puts the
            # recipient right into the arguments, the traceback prints them in full).
            # We log only the CLASS of the error — that is enough for diagnostics.
            logger.error(
                "subscription_reminders: e-mail failure tenant=%s threshold=%s error=%s",
                row.tenant_id,
                threshold,
                type(exc).__name__,
            )
            return False
        return True

    async def _sleep(self, delay: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the subscription reminder worker (the container via Compose)."""
    import os

    from src.api.emailer import make_email_sender
    from src.bots.telegram import build_bot
    from src.log_secrets import install_secret_log_filter
    from src.workers.adapters import default_adapters

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()
    bot = build_bot()
    adapters = default_adapters(bot)
    email_sender = make_email_sender(
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", ""),
        postmark_token=os.getenv("POSTMARK_SERVER_TOKEN", ""),
        email_from=os.getenv("EMAIL_FROM", "noreply@nab.rs"),
    )
    worker = SubscriptionReminderWorker(
        tg_adapter=adapters.get("telegram"),
        email_sender=email_sender,
    )
    await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
