"""Tender Change Notifier — "tender changed" to subscribers to whom it has already been delivered.

Standalone polling worker (model: :mod:`src.workers.award_watch_notifier`;
``delivery_outbox`` is tender-centric and ``ON CONFLICT (profile, tender, channel) DO
NOTHING`` would not skip a second message for the same pair — hence its own dedup key,
table ``tender_change_sent`` (0058), not an outbox row).

Where events come from: ingest writes a structural diff "was → became" into ``tender_changes``
(deadline, amount, documentation files, customer comment on a Correction — see
:mod:`src.domain.tender_changes`). Here events are merged into one message per
(tender, recipient) and go to ALL live channels of the recipient.

Rules (brief "AI analysis, step 3", item 3):

* recipient — a tenant to whom the tender was already DELIVERED (``delivery_outbox.status
  = 'sent'``) BEFORE the change: whoever gets the tender later will receive it already new;
* no more than once a day per (tender, recipient): events over a day accumulate and go
  as one message once the day has passed (``tender_change_sent.sent_at``);
* daily cap as for regular delivery — ``DELIVERY_DAILY_CAP`` (the same lever,
  :mod:`src.domain.delivery_cap`), here counted in NOTIFICATIONS per tenant per
  day; those who did not fit wait for the next pass (they are not written to sent);
* ``access_until`` expired (frozen) → do not send and do not write to sent: it will arrive after
  renewal; disabled/dead channels — the same ``channel_states`` predicate as routing and
  delivery (:data:`src.workers.matching.LIVE_CHANNELS_SQL`);
* the language of EACH channel is its own: Telegram — ``COALESCE(uc.locale, u.locale)``, email —
  ``u.locale`` (``ChannelState.user_locale`` / ``.locale``), see
  ``.claude/rules/i18n.md``. Catalogs: Telegram — ``common_t``, email — ``email_t``;
* the write to sent — on delivery to at least one channel (as with award alerts).

Fail-closed for email: transport ``None`` → nothing to send with, we do not write to sent.
PII (address/chat_id) does not go to the logs — only the id and the error class.

The worker accesses the DB privileged (superuser ``tendersrb``, cross-tenant).

Run:
    python -m src.workers.tender_change_notifier          # polling loop
    python -m src.workers.tender_change_notifier --once   # one pass
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text

from src.api.emailer import EmailSender
from src.db.session import session_scope
from src.domain.delivery_cap import daily_cap as daily_cap_from_env
from src.domain.tender_changes import merge_changes, render_lines
from src.i18n_common import common_t
from src.i18n_email import email_t, norm_locale
from src.workers.matching import channel_states

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 900.0  # 15 minutes
DEFAULT_CABINET_BASE_URL = "https://nab.rs"
SUPPORTED_CHANNELS = ("telegram", "email")
_SUBJECT_MAX_LEN = 120
#: Events older than the window are not sent (after a long downtime — no avalanche from the archive).
WINDOW_DAYS = 3

# Pairs (tender × recipient) with events not yet reported.
#  * recipient = a tenant to whom the tender was delivered (sent) in telegram/email BEFORE
#    the earliest unreported event;
#  * the tender is open (deadline has not passed or is not set);
#  * a day has passed since the last message for this pair (or there were no messages).
_PENDING_SQL = text("""
    SELECT c.tender_id,
           CAST(p.tenant_id AS TEXT) AS tenant_id,
           MAX(c.id)                 AS last_id,
           MIN(o.sent_at)            AS first_sent_at,
           tn.title,
           s.last_change_id          AS prev_id
    FROM tender_changes c
    JOIN tenders_normalized tn ON tn.id = c.tender_id
    JOIN delivery_outbox o ON o.tender_id = c.tender_id
                          AND o.status = 'sent'
                          AND o.channel IN ('telegram', 'email')
                          AND o.sent_at < c.detected_at
    JOIN profiles p ON p.id = o.profile_id
    LEFT JOIN tender_change_sent s
           ON s.tender_id = c.tender_id AND s.notified_tenant = p.tenant_id
    WHERE c.detected_at > now() - make_interval(days => :window_days)
      AND (tn.deadline_at IS NULL OR tn.deadline_at >= now())
      AND (s.last_change_id IS NULL OR c.id > s.last_change_id)
      AND (s.sent_at IS NULL OR s.sent_at <= now() - interval '1 day')
    GROUP BY c.tender_id, p.tenant_id, tn.title, s.last_change_id
    ORDER BY MIN(c.id)
    LIMIT 500
    """)

_EVENTS_SQL = text("""
    SELECT diff FROM tender_changes
    WHERE tender_id = :tender_id AND id > :after_id AND id <= :upto_id
      AND detected_at > :since
    ORDER BY id
    """)

_LOAD_TENANT_SQL = text("""
    SELECT access_until FROM tenants WHERE id = CAST(:tenant_id AS UUID)
    """)

_SENT_LAST_DAY_SQL = text("""
    SELECT count(*) AS n FROM tender_change_sent
    WHERE notified_tenant = CAST(:tenant_id AS UUID)
      AND sent_at > now() - interval '1 day'
    """)

_RECORD_SENT_SQL = text("""
    INSERT INTO tender_change_sent (tender_id, notified_tenant, last_change_id, sent_at)
    VALUES (:tender_id, CAST(:tenant_id AS UUID), :last_id, now())
    ON CONFLICT (tender_id, notified_tenant) DO UPDATE
    SET last_change_id = EXCLUDED.last_change_id, sent_at = EXCLUDED.sent_at
    """)


def _is_tenant_frozen(access_until: Any) -> bool:
    from src.domain.access import is_frozen

    return is_frozen(access_until, datetime.now(timezone.utc))


@dataclass(frozen=True)
class AlertTarget:
    """Addressee of one channel + the language in which they read notifications."""

    address: str
    locale: str | None = None


@dataclass(frozen=True)
class ChangeEmail:
    subject: str
    text_body: str
    html_body: str


def build_telegram_text(
    change: dict[str, Any],
    *,
    title: str,
    tender_id: str,
    cabinet_base_url: str,
    locale: str | None,
) -> str:
    """HTML message for Telegram. The chrome language — the ``common_t`` catalog of the channel."""
    base = (cabinet_base_url or DEFAULT_CABINET_BASE_URL).rstrip("/")
    url = html.escape(f"{base}/tender/{tender_id}", quote=True)
    esc = lambda v: html.escape(v, quote=False)  # noqa: E731 — text, not an attribute
    head = f"🔔 <b>{esc(common_t(locale, 'tc_title'))}</b>"
    lines = [head, f'📋 <a href="{url}">{esc(title or "—")}</a>']
    lines += [f"• {esc(x)}" for x in render_lines(change, locale, common_t)]
    return "\n".join(lines)


def build_email(
    change: dict[str, Any],
    *,
    title: str,
    tender_id: str,
    cabinet_base_url: str,
    locale: str | None,
) -> ChangeEmail:
    """Email. The chrome language — the ``email_t`` catalog and the recipient's ``users.locale``.

    Subject: the tender name — data from an external source, line breaks
    are collapsed (header injection), length is cut.
    """
    base = (cabinet_base_url or DEFAULT_CABINET_BASE_URL).rstrip("/")
    link = f"{base}/tender/{tender_id}"
    clean_title = " ".join((title or "—").split())
    subject = email_t(locale, "tc_subject").format(title=clean_title)
    if len(subject) > _SUBJECT_MAX_LEN:
        subject = subject[: _SUBJECT_MAX_LEN - 1].rstrip() + "…"
    greeting = email_t(locale, "greeting")
    intro = email_t(locale, "tc_intro")
    open_card = email_t(locale, "open_card")
    lines = render_lines(change, locale, email_t)

    text_body = "\n".join(
        [greeting, "", intro, "", clean_title, "", *[f"- {x}" for x in lines]]
        + ["", f"{open_card}: {link}", ""]
    )
    items = "".join(f"<li>{html.escape(x)}</li>" for x in lines)
    html_body = (
        "<html><body>"
        f"<p>{html.escape(greeting)}</p>"
        f"<p>{html.escape(intro)}</p>"
        f"<h2>🔔 {html.escape(clean_title)}</h2>"
        f"<ul>{items}</ul>"
        f'<p><a href="{html.escape(link, quote=True)}">{html.escape(open_card)}</a></p>'
        "</body></html>"
    )
    return ChangeEmail(subject=subject, text_body=text_body, html_body=html_body)


class TenderChangeNotifier:
    """Polling worker for "tender changed" notifications."""

    def __init__(
        self,
        *,
        bot: Any = None,
        email_sender: EmailSender | None = None,
        session_factory: Callable[[], Any] = session_scope,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        cabinet_base_url: str | None = None,
        daily_cap: int | None = None,
    ) -> None:
        self._bot = bot
        self._email = email_sender  # None = nothing to send with (fail-closed)
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        base = cabinet_base_url or os.getenv(
            "CABINET_BASE_URL", DEFAULT_CABINET_BASE_URL
        )
        self._cabinet_base_url = (base or "").rstrip("/")
        self._daily_cap = daily_cap_from_env() if daily_cap is None else daily_cap
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        logger.info(
            "tender_change_notifier: polling started (interval=%.0fs)",
            self._poll_interval,
        )
        while not self._stopped.is_set():
            try:
                sent = await self.poll_once()
                if sent:
                    logger.info("tender_change_notifier: notifications: %d", sent)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("tender_change_notifier: polling loop error")
            await self._sleep(self._poll_interval)

    async def poll_once(self) -> int:
        """One pass. Returns the number of delivered notifications (pairs tender × recipient)."""
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(_PENDING_SQL, {"window_days": WINDOW_DAYS}))
            )
        sent = 0
        tenant_cache: dict[str, dict[str, AlertTarget]] = {}
        used_today: dict[str, int] = {}
        for row in rows:
            tenant_id = str(row.tenant_id)
            if tenant_id not in tenant_cache:
                tenant_cache[tenant_id] = await self._check_tenant(tenant_id)
                used_today[tenant_id] = await self._sent_last_day(tenant_id)
            targets = tenant_cache[tenant_id]
            if not targets:
                continue
            if self._daily_cap > 0 and used_today[tenant_id] >= self._daily_cap:
                continue  # daily cap: not in sent — it will go the next day
            change = await self._merged_change(row)
            if change is None:
                # events exist, but "was → became" collapsed (the deadline was restored) —
                # there is nothing to report; we mark them as reported so as not to loop forever
                await self._record_sent(row)
                continue
            delivered = await self._send_all(row, change, targets)
            if not delivered:
                continue
            await self._record_sent(row)
            used_today[tenant_id] += 1
            sent += 1
            logger.info(
                "tender_change_notifier: tender=%s channels=%s",
                row.tender_id,
                ",".join(delivered),
            )
        return sent

    async def _merged_change(self, row: Any) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            result = await session.execute(
                _EVENTS_SQL,
                {
                    "tender_id": row.tender_id,
                    "after_id": row.prev_id or 0,
                    "upto_id": row.last_id,
                    "since": row.first_sent_at,
                },
            )
            diffs = [r.diff for r in result]
        return merge_changes(diffs)

    async def _check_tenant(self, tenant_id: str) -> dict[str, AlertTarget]:
        """Live channels of the tenant + channel language; frozen/no channels → ``{}``."""
        async with self._session_factory() as session:
            trow = (
                await session.execute(_LOAD_TENANT_SQL, {"tenant_id": tenant_id})
            ).first()
            if trow is None or _is_tenant_frozen(getattr(trow, "access_until", None)):
                return {}
            states = await channel_states(session, tenant_id)
        return {
            channel: AlertTarget(
                address=str(state.external_id).strip(),
                # email — the cabinet language (u.locale), Telegram — the channel language
                locale=state.user_locale if channel == "email" else state.locale,
            )
            for channel, state in states.items()
            if channel in SUPPORTED_CHANNELS and state.is_live
        }

    async def _sent_last_day(self, tenant_id: str) -> int:
        async with self._session_factory() as session:
            row = (
                await session.execute(_SENT_LAST_DAY_SQL, {"tenant_id": tenant_id})
            ).first()
        return int(row.n) if row else 0

    async def _send_all(
        self, row: Any, change: dict[str, Any], targets: dict[str, AlertTarget]
    ) -> list[str]:
        delivered: list[str] = []
        tender_id = str(row.tender_id)
        title = str(row.title or "")
        for channel in SUPPORTED_CHANNELS:
            target = targets.get(channel)
            if target is None or not target.address:
                continue
            if channel == "telegram":
                body = build_telegram_text(
                    change,
                    title=title,
                    tender_id=tender_id,
                    cabinet_base_url=self._cabinet_base_url,
                    locale=target.locale,
                )
                ok = await self._send_telegram(target.address, body)
            else:
                ok = await self._send_email(
                    target.address,
                    build_email(
                        change,
                        title=title,
                        tender_id=tender_id,
                        cabinet_base_url=self._cabinet_base_url,
                        locale=target.locale,
                    ),
                    target.locale,
                    tender_id,
                )
            if ok:
                delivered.append(channel)
        return delivered

    async def _send_telegram(self, chat_id: str, body: str) -> bool:
        if not self._bot:
            return False
        try:
            chat_int = int(chat_id)
        except (TypeError, ValueError):
            logger.warning("tender_change_notifier: invalid chat_id")
            return False
        try:
            await self._bot.send_message(
                chat_id=chat_int,
                text=body,
                parse_mode="HTML",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "tender_change_notifier: telegram failure error=%s", type(exc).__name__
            )
            return False

    async def _send_email(
        self, to: str, message: ChangeEmail, locale: str | None, tender_id: str
    ) -> bool:
        if self._email is None:
            logger.error(
                "tender_change_notifier: mail transport not configured tender=%s",
                tender_id,
            )
            return False
        try:
            await self._email.send(
                to=to,
                subject=message.subject,
                text_body=message.text_body,
                html_body=message.html_body,
                locale=norm_locale(locale),
            )
        except Exception as exc:  # noqa: BLE001 — a channel failure does not break the pass
            logger.error(
                "tender_change_notifier: email failure tender=%s error=%s",
                tender_id,
                type(exc).__name__,
            )
            return False
        return True

    async def _record_sent(self, row: Any) -> None:
        async with self._session_factory() as session:
            await session.execute(
                _RECORD_SENT_SQL,
                {
                    "tender_id": str(row.tender_id),
                    "tenant_id": str(row.tenant_id),
                    "last_id": row.last_id,
                },
            )
            await session.commit()

    async def _sleep(self, delay: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint
    import argparse

    from src.bots.telegram import build_bot
    from src.log_secrets import install_secret_log_filter
    from src.workers.adapters import email_sender_from_env

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()

    parser = argparse.ArgumentParser(description="Tender Change Notifier")
    parser.add_argument("--once", action="store_true", help="One pass and exit")
    args = parser.parse_args()

    notifier = TenderChangeNotifier(
        bot=build_bot(), email_sender=email_sender_from_env()
    )
    if args.once:
        sent = await notifier.poll_once()
        logger.info("tender_change_notifier --once: notifications %d", sent)
    else:
        await notifier.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
