"""Award Watch Notifier — push alerts about new wins of the subscribed PIBs (VG-06 E3b).

A standalone polling worker (model: subscription_reminders.py). Every 30 minutes it
looks for new pairs (subscription × award_id) that are not yet in award_watch_sent, and
sends the card to the subscriber in EVERY live channel:

  🔔 <b>Winner</b> (PIB xxx) — new win
  🏛 Customer: …
  📋 Tender title
  💶 N EUR  (if any)
  🗓 date
  + inline button “🏛 Who wins?” (an:cust:{tender_id})

The e-mail has no button (the messenger callback is unavailable) — its place is taken by a link
to the public tender card, as in :mod:`src.workers.adapters.email`.

‼️ Email is not “one more channel for completeness”: the Lite tier forcibly sets
``delivery_channels=[“email”]`` (:mod:`src.api.tiers`), therefore a Telegram-only
worker did not deliver award alerts to a Lite subscriber AT ALL. The mail contour is the
existing :mod:`src.api.emailer` (``EmailSender``), as in
:mod:`src.workers.channel_dead_notify`; there is no second mail contour.

A live channel = the freshest ``active`` row of ``user_channels`` with a non-empty
``external_id`` — the predicate is taken from routing (:func:`src.workers.matching.
channel_states`) and is not written a second time: if the selections diverge, the verdict “the channel is live” diverges too from where the delivery will actually go (the project rules). The addressee
is taken from there too: ``external_id`` — the chat_id for telegram and the address for email.

Fail-closed for mail (model — :mod:`src.workers.adapters.email`, #349):
the transport is passed EXPLICITLY, and ``None`` (“the mail env is not configured”) means “there is
nothing to send with”, not “send to nowhere”. There is no silent fallback to ``MockEmailSender`` —
it puts the e-mail into memory and raises no exception, that is, the worker would record
the pair in ``award_watch_sent`` for an unsent e-mail and the alert would be lost forever.

Idempotency — through award_watch_sent (UNIQUE subscription_id, award_id).
The key is NOT marked by channel: the record is made ONE per pair (subscription × award) and
only if at least one channel delivered. Therefore a repeated run does not double
with either one channel or two. The reverse side: TG delivered, and mail
failed → there will be no more e-mail for this pair. This is a deliberate trade-off:
the alternative (writing sent per channel) requires a migration of the key, and the subscriber got the alert anyway.

Watermark: when subscribing, all existing awards of the PIB are already recorded in
award_watch_sent by the handler on_watch_winner → the alerts are only about NEW wins.

Tier gate at sending: if the tenant is NOT Pro+ or is frozen — do NOT send
and do NOT write to sent (so that the alert arrives after a return to Pro+/unfreezing).
The subscription meanwhile stays active.

Privacy: the subscriber’s address never goes into the logs — PII. The log gets
sub_id/award_id/channel and the CLASS of the error: the text of an SMTP error contains the address
(``SMTPRecipientsRefused`` puts the recipient right into the exception arguments),
and ``logger.exception`` prints it in full into the traceback (#347/#349).

The worker goes to the DB privileged (without SET app.current_tenant_id) —
cross-tenant, the owner bypasses RLS (ARCHITECTURE §8).

Launch:
    python -m src.workers.award_watch_notifier          # polling loop
    python -m src.workers.award_watch_notifier --once   # one pass
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import text

from src.api.emailer import EmailSender
from src.db.session import session_scope
from src.domain.tiers import effective_tier
from src.i18n_common import common_t
from src.i18n_email import email_t, norm_locale
from src.workers.matching import channel_states

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 1800.0  # 30 minutes

# The base of the public tender card — the same as for the delivery adapters (ADR-026);
# prod sets the ENV CABINET_BASE_URL, the default contains no secrets.
DEFAULT_CABINET_BASE_URL = "https://nab.rs"

# The channels the worker can send through. A channel outside the list (viber — a stub)
# may be live, but the alert will not go through it.
SUPPORTED_CHANNELS = ("telegram", "email")

# The subject of the e-mail is limited: long Subjects are cut by mail clients (as in
# src.workers.adapters.email).
_SUBJECT_MAX_LEN = 120

# New pairs (subscription × award) that are not yet in award_watch_sent.
# LIMIT 500: anti-avalanche on the first run after a long downtime.
_NEW_PAIRS_SQL = text("""
    SELECT s.id        AS sub_id,
           CAST(s.tenant_id AS TEXT) AS tenant_id,
           s.winner_name,
           ta.id       AS award_id,
           CAST(ta.tender_id AS TEXT) AS tender_id,
           ta.winner_pib,
           ta.decision_date,
           ta.award_amount_eur,
           ta.customer_name,
           t.title
    FROM award_watch_subscriptions s
    JOIN tender_awards ta ON ta.winner_pib = s.winner_pib
    LEFT JOIN tenders_normalized t ON t.id = ta.tender_id
    WHERE s.is_active = true
      AND NOT EXISTS (
          SELECT 1 FROM award_watch_sent w
          WHERE w.subscription_id = s.id AND w.award_id = ta.id
      )
    LIMIT 500
    """)

_LOAD_TENANT_SQL = text("""
    SELECT tier, access_until, status FROM tenants WHERE id = CAST(:tenant_id AS UUID)
    """)

_RECORD_SENT_SQL = text("""
    INSERT INTO award_watch_sent (subscription_id, award_id)
    VALUES (:sub_id, :award_id)
    ON CONFLICT (subscription_id, award_id) DO NOTHING
    RETURNING id
    """)


def _is_pro_plus(tier: str | None) -> bool:
    """Pro+ := the tier on which the bid verdict is available (pro/business/enterprise).

    It expects the ALREADY effective tier (trial → pro is resolved by the caller via
    ``src.domain.tiers.effective_tier``), so trial subscribers do get the alerts."""
    from src.ai.usage import _TIER_LIMITS

    return _TIER_LIMITS.get(tier or "", {}).get("bid", 0) != 0


def _is_tenant_frozen(access_until: Any) -> bool:
    from src.domain.access import is_frozen

    return is_frozen(access_until, datetime.now(timezone.utc))


def _fmt_eur(value: Decimal | None) -> str | None:
    if value is None:
        return None
    try:
        n = int(value)
        return f"{n:,}".replace(",", " ")
    except (TypeError, ValueError):
        return None


def _fmt_decision_date(value: Any) -> str | None:
    """Decision date in dd.mm.yyyy; not a date — as is (as a string)."""
    if not value:
        return None
    try:
        return value.strftime("%d.%m.%Y")
    except AttributeError:
        return str(value)


@dataclass(frozen=True)
class AlertValues:
    """The raw (unescaped) values of the alert — common to the card and the e-mail.

    One source of facts for both channels: they differ only in the markup and in the way
    to open the tender (the inline button in TG, the link in the e-mail).
    """

    winner_name: str
    pib: str
    customer: str
    title: str
    tender_id: str
    eur: str | None
    decision_date: str | None


def alert_values(row: Any) -> AlertValues:
    """Takes the values of the alert from the row of :data:`_NEW_PAIRS_SQL`."""
    return AlertValues(
        winner_name=str(row.winner_name or row.winner_pib or "—"),
        pib=str(row.winner_pib or "—"),
        customer=str(row.customer_name or "—"),
        title=str(row.title or "—"),
        tender_id=str(row.tender_id or ""),
        eur=_fmt_eur(row.award_amount_eur),
        decision_date=_fmt_decision_date(row.decision_date),
    )


def build_alert_card(row: Any, locale: str | None = None) -> tuple[str, Any]:
    """Forms the HTML card of the alert + the inline button “🏛 Who wins?”.

    Returns (text, InlineKeyboardMarkup).

    ``locale`` — the language of the CHROME (``ChannelState.locale`` of the telegram channel: the language
    of the channel, otherwise the language of the cabinet); ``None``/unknown code → ru. The winner’s name, the PIB, the customer, the tender title, the sum and the date are DATA, not translated.
    The button label is taken from the same key as in the delivery card
    (``btn_customer_analytics``); ``callback_data`` does not depend on the language.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    v = alert_values(row)
    winner_name = html.escape(v.winner_name)
    pib = html.escape(v.pib)
    customer = html.escape(v.customer)
    title = html.escape(v.title)
    tender_id = v.tender_id

    lines = [
        f"🔔 <b>{winner_name}</b> (PIB {pib}) — {common_t(locale, 'aw_new_win')}",
        f"🏛 {common_t(locale, 'aw_customer')}: {customer}",
        f"📋 {title}",
    ]

    if v.eur:
        lines.append(f"💶 {v.eur} EUR")

    if v.decision_date:
        lines.append(f"🗓 {v.decision_date}")

    markup = (
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=common_t(locale, "btn_customer_analytics"),
                        callback_data=f"an:cust:{tender_id}",
                    )
                ]
            ]
        )
        if tender_id
        else None
    )

    return "\n".join(lines), markup


@dataclass(frozen=True)
class AlertTarget:
    """The addressee of one channel + the language in which they read the notifications.

    The language arrives from the common predicate of “where the delivery will go”
    (:data:`src.workers.matching.LIVE_CHANNELS_SQL` → :class:`ChannelState`), and
    not from a separate selection: a second query for the same meaning would diverge from
    the first. Each channel has ITS OWN — telegram and mail are different rows of
    ``user_channels``.
    """

    address: str
    locale: str | None = None


@dataclass(frozen=True)
class AlertEmail:
    """The rendered alert e-mail: the subject, the plain-text body and the HTML alternative."""

    subject: str
    text_body: str
    html_body: str


def build_alert_subject(row: Any, locale: str | None = None) -> str:
    """The subject of the e-mail: “New win: <who> (PIB xxx)”, cut by length.

    The winner’s name comes from an external source (eNabavke) → line breaks
    from it are collapsed into a space: a line break in the value of the Subject is
    a header injection (the same technique as in
    :func:`src.workers.adapters.email.build_subject`).

    ``locale`` — the language of the e-mail chrome (``ChannelState.locale`` of the email channel);
    ``None``/unknown code → ru (an explicit fallback ``email_t``).
    """
    v = alert_values(row)
    winner = " ".join(v.winner_name.split()) or "—"
    subject = email_t(locale, "aw_subject").format(winner=winner, pib=v.pib)
    if len(subject) > _SUBJECT_MAX_LEN:
        subject = subject[: _SUBJECT_MAX_LEN - 1].rstrip() + "…"
    return subject


def build_alert_email(
    row: Any, *, cabinet_base_url: str, locale: str | None = None
) -> AlertEmail:
    """Assembles the alert e-mail: the same facts as in the TG card, but without the button.

    Instead of the inline button “🏛 Who wins?” — a link to the public tender card:
    there is also the customer analysis, and everything else (as in
    :class:`src.workers.adapters.email.EmailAdapter`).

    ``locale`` — the language of the e-mail CHROME, the catalog :mod:`src.i18n_email` (the Telegram card has its own: the channel and the mail are different rows of ``user_channels``, each with its own language). The alert facts are data and are not translated.
    """
    v = alert_values(row)
    base = (cabinet_base_url or DEFAULT_CABINET_BASE_URL).rstrip("/")

    facts: list[tuple[str, str]] = [
        (email_t(locale, "aw_customer"), v.customer),
        (email_t(locale, "aw_tender"), v.title),
    ]
    if v.eur:
        facts.append((email_t(locale, "lbl_amount"), f"{v.eur} EUR"))
    if v.decision_date:
        facts.append((email_t(locale, "aw_decision_date"), v.decision_date))

    heading = f"{v.winner_name} (PIB {v.pib}) — {email_t(locale, 'aw_new_win')}"
    greeting = email_t(locale, "greeting")
    intro = email_t(locale, "aw_intro")
    open_card = email_t(locale, "open_card")
    footer = email_t(locale, "aw_footer")

    text_lines = [
        greeting,
        "",
        f"{intro} {heading}",
        "",
    ]
    text_lines.extend(f"{label}: {value}" for label, value in facts)
    if v.tender_id:
        text_lines.extend(["", f"{open_card}: {base}/tender/{v.tender_id}"])
    text_lines.extend(["", footer, ""])

    facts_html = "".join(
        f"<li><b>{html.escape(label)}:</b> {html.escape(value)}</li>"
        for label, value in facts
    )
    link_html = ""
    if v.tender_id:
        url = html.escape(f"{base}/tender/{v.tender_id}", quote=True)
        link_html = f'<p><a href="{url}">{html.escape(open_card)}</a></p>'
    html_body = (
        "<html><body>"
        f"<p>{html.escape(greeting)}</p>"
        f"<p>{html.escape(intro)}</p>"
        f"<h2>🔔 {html.escape(heading)}</h2>"
        f"<ul>{facts_html}</ul>"
        f"{link_html}"
        f"<p>{html.escape(footer)}</p>"
        "</body></html>"
    )

    return AlertEmail(
        subject=build_alert_subject(row, locale),
        text_body="\n".join(text_lines),
        html_body=html_body,
    )


class AwardWatchNotifier:
    """The polling worker of the alerts about new wins of the subscribed PIBs (30-min interval)."""

    def __init__(
        self,
        *,
        bot: Any = None,
        email_sender: EmailSender | None = None,
        session_factory: Callable[[], Any] = session_scope,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        cabinet_base_url: str | None = None,
    ) -> None:
        self._bot = bot
        # Fail-closed: None = “nothing to send with”. There is no fallback to MockEmailSender —
        # it would report success for an unsent e-mail (see the module docstring).
        self._email = email_sender
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        base = cabinet_base_url or os.getenv(
            "CABINET_BASE_URL", DEFAULT_CABINET_BASE_URL
        )
        self._cabinet_base_url = (base or "").rstrip("/")
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        logger.info(
            "award_watch_notifier: polling started (interval=%.0fs)",
            self._poll_interval,
        )
        while not self._stopped.is_set():
            try:
                sent = await self.poll_once()
                if sent:
                    logger.info("award_watch_notifier: alerts sent: %d", sent)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("award_watch_notifier: polling cycle error")
            await self._sleep(self._poll_interval)

    async def poll_once(self) -> int:
        """One pass: new pairs → gate → send to all live channels → record.

        Returns the number of DELIVERED alerts (pairs subscription × award), not of
        e-mails/messages: a pair with two live channels is one alert.
        """
        rows = await self._load_new_pairs()
        if not rows:
            return 0

        sent = 0
        # A cache of the gate checks by tenant_id — so as not to go to the DB for each row
        tenant_cache: dict[str, tuple[bool, dict[str, AlertTarget]]] = (
            {}
        )  # tenant_id → (ok, {channel: addressee + their language})

        for row in rows:
            tenant_id = str(row.tenant_id)

            if tenant_id not in tenant_cache:
                tenant_cache[tenant_id] = await self._check_tenant(tenant_id)
            ok, targets = tenant_cache[tenant_id]

            if not ok or not targets:
                continue

            delivered = await self._send_all(row, targets)
            if not delivered:
                continue
            # The record — one per pair, and only on the fact of delivery to at least one
            # channel: the pair will no longer return to the selection (see the module docstring).
            await self._record_sent(row.sub_id, row.award_id)
            sent += 1
            logger.info(
                "award_watch_notifier: alert sub=%s award=%s channels=%s",
                row.sub_id,
                row.award_id,
                ",".join(delivered),
            )

        return sent

    async def _send_all(self, row: Any, targets: dict[str, AlertTarget]) -> list[str]:
        """Sends the alert to all live channels. Returns those where it was delivered.

        The language is its OWN for EACH channel (:attr:`AlertTarget.locale`): telegram and mail
        are different ``user_channels`` rows, and the subscriber may read the bot in sr,
        and the e-mails in ru.

        ‼️ The addressee (chat_id/email) is not passed into the log: an e-mail is PII.
        """
        delivered: list[str] = []
        for channel in SUPPORTED_CHANNELS:
            target = targets.get(channel)
            if target is None or not target.address:
                continue
            if channel == "telegram":
                text_msg, markup = build_alert_card(row, target.locale)
                ok = await self._send_telegram(target.address, text_msg, markup)
            else:
                ok = await self._send_email(row, target.address, target.locale)
            if ok:
                delivered.append(channel)
        return delivered

    async def _load_new_pairs(self) -> list[Any]:
        async with self._session_factory() as session:
            result = await session.execute(_NEW_PAIRS_SQL)
            return list(result)

    async def _check_tenant(
        self, tenant_id: str
    ) -> tuple[bool, dict[str, AlertTarget]]:
        """Checks the gate (Pro+, not frozen) and resolves the tenant’s addressees.

        Returns (gate_ok, {channel: addressee + their language}) over the LIVE channels from
        :func:`src.workers.matching.channel_states` — the same predicate as routing/delivery. If the gate is not passed or there are no live channels → (False, {}).
        The gate on an expired plan: do NOT send and do NOT write to sent (the alert will come
        after a return to Pro+/unfreezing).

        ‼️ An own SQL for telegram here was a third predicate for the same meaning and
        differed from the delivery by the filter ``u.deleted_at IS NULL``, which is absent from
        both ``LIVE_CHANNELS_SQL`` and ``delivery._LOAD_JOBS_SQL``: a soft-deleted user’s channel
        got the tenders, but the award alerts did not. Now the predicate is one; if a filter by
        ``deleted_at`` is needed — it is needed by ALL three and is edited in the common
        selection, not here.
        """
        async with self._session_factory() as session:
            trow = (
                await session.execute(_LOAD_TENANT_SQL, {"tenant_id": tenant_id})
            ).first()
            if trow is None:
                return False, {}
            if _is_tenant_frozen(getattr(trow, "access_until", None)):
                return False, {}
            # The effective tier: trial → the rights of Pro (ARCHITECTURE §4), AFTER the frozen gate.
            status_val = (
                str(trow.status) if trow and getattr(trow, "status", None) else None
            )
            raw_tier = str(trow.tier) if trow and trow.tier else None
            tier = effective_tier(status_val, raw_tier)
            if not _is_pro_plus(tier):
                return False, {}

            states = await channel_states(session, tenant_id)

        targets = {
            channel: AlertTarget(
                address=str(state.external_id).strip(), locale=state.locale
            )
            for channel, state in states.items()
            if channel in SUPPORTED_CHANNELS and state.is_live
        }
        if not targets:
            return False, {}
        return True, targets

    async def _send_telegram(self, chat_id: str, text: str, markup: Any) -> bool:
        """Sends the HTML card of the alert to Telegram. Returns True on success."""
        if not self._bot:
            return False
        try:
            chat_int = int(chat_id)
        except (TypeError, ValueError):
            logger.warning("award_watch_notifier: incorrect chat_id=%s", chat_id)
            return False
        try:
            await self._bot.send_message(
                chat_id=chat_int,
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return True
        except Exception:  # noqa: BLE001
            logger.exception("award_watch_notifier: send failure chat=%s", chat_id)
            return False

    async def _send_email(
        self, row: Any, target: str, locale: str | None = None
    ) -> bool:
        """Sends the alert e-mail. Returns True on success.

        Fail-closed: no transport → False (the pair is NOT marked as sent and will
        go out when the mail env is set up). Success here = a silent loss of the alert.

        ‼️ Neither the address nor the traceback goes into the log — PII (see the module docstring).
        """
        if self._email is None:
            logger.error(
                "award_watch_notifier: the mail transport is not configured, "
                "alert not sent sub=%s award=%s",
                row.sub_id,
                row.award_id,
            )
            return False
        message = build_alert_email(
            row, cabinet_base_url=self._cabinet_base_url, locale=locale
        )
        try:
            await self._email.send(
                to=target,
                subject=message.subject,
                text_body=message.text_body,
                html_body=message.html_body,
                locale=norm_locale(locale),
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 — a channel failure does not break the pass
            logger.error(
                "award_watch_notifier: e-mail failure sub=%s award=%s error=%s",
                row.sub_id,
                row.award_id,
                type(exc).__name__,
            )
            return False
        return True

    async def _record_sent(self, sub_id: int, award_id: int) -> None:
        async with self._session_factory() as session:
            await session.execute(
                _RECORD_SENT_SQL, {"sub_id": sub_id, "award_id": award_id}
            )
            await session.commit()

    async def _sleep(self, delay: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the alerts worker (the container via Compose)."""
    import argparse

    from src.bots.telegram import build_bot
    from src.log_secrets import install_secret_log_filter
    from src.workers.adapters import email_sender_from_env

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()

    parser = argparse.ArgumentParser(description="Award Watch Notifier")
    parser.add_argument("--once", action="store_true", help="One pass and exit")
    args = parser.parse_args()

    bot = build_bot()
    # email_sender_from_env() → None if the mail is not configured: the same fail-closed
    # transport as for the delivery (#349), without a second mail contour.
    notifier = AwardWatchNotifier(bot=bot, email_sender=email_sender_from_env())

    if args.once:
        sent = await notifier.poll_once()
        logger.info("award_watch_notifier --once: sent %d alerts", sent)
    else:
        await notifier.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
