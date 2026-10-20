"""The Telegram delivery adapter (aiogram 3, ARCHITECTURE §15, ADR-025).

Renders the rich tender card v6 (priority stars, tender No., region, subject type,
CPV, the amount ORIG+EUR, publication date, the plan period, submissions due,
the source, link) with the buttons “📂 Open in the cabinet” / “⭐ Watch” / “✖ Hide” and
“🧠 AI analysis ▾” (expands 'ai:deep:{tender_id}' and others; see
:func:`src.bots.telegram.build_card_keyboard`) and sends it through the passed instance of
:class:`aiogram.Bot` (the same bot from :mod:`src.bots.telegram`,
``parse_mode=HTML``). The presses of the funnel buttons are handled by
:func:`src.bots.telegram.on_tender_status`, the deep button — by on_deep_analysis.

The AI buttons are shown to EVERYONE (the plan gate — on press), but they are hidden behind
“🧠 AI analysis ▾”: on a phone four buttons in a row were cut off.

The button “📂 Open in the cabinet” leads to ``/app/tenders/{id}?profile_id=`` of the cabinet
(``CABINET_BASE_URL``, prod — app.nab.rs); without a profile it is absent (the page
requires ``profile_id``). An unlogged user gets ``/login?next=…`` and after sign-in
lands on the same card. The method `_tender_url` is the historical ``/tender/{id}``,
not used in the card (kept for the unit test of the base URL).

The card is assembled from the fields of :class:`DeliveryJob` (a join in delivery): a row
is added ONLY if the value is there. ``contract_type`` by the structure of the fact
(#142 → normalize) does not get into the DB — when it is absent, ``procedure_type`` is shown
with the label “Procedure: …” as a temporary stand-in. The CPV and the plan period
are taken from ``classifications`` (``cpv_label``/``plan_period_label``,
Chat 44/45).

The LANGUAGE of the CARD is the interface language of the recipient (``users.locale`` → ``DeliveryJob.
locale``, a join in :mod:`src.workers.delivery`): the row labels, the labels of the subject
type/procedure and the button labels are taken from the shared catalog
:mod:`src.i18n_common` (``common_t``), ``None``/unknown code → ru. Only the CHROME is translated:
the tender's title, region, CPV, sums and dates are the source's data and
go as is (the second language axis — ``profiles.target_language`` — does not take part here).
``callback_data`` of the buttons does not depend on the language.

Besides the card (``format``/``send``) the adapter supports ``send_text`` —
a short plain-text message without a card/buttons (the reminder worker,
NEXT №1). Both ``send`` and ``send_text`` go through the common :meth:`_send_raw`
(a single mapping of Bot API errors), therefore the delivery contract does not change.

Mapping of Bot API errors to :class:`DeliveryResult` (§15.5):

- ``TelegramForbiddenError`` (403, “bot was blocked by the user”) and
  ``TelegramBadRequest`` with “chat not found” / “user is deactivated” → ``blocked``:
  the channel is marked ``blocked``, the attempts stop.
- ``TelegramRetryAfter`` (429) and other ``TelegramAPIError`` (5xx/network) →
  ``retryable``: the row returns to the queue and will be retried.
- other ``TelegramBadRequest`` (e.g. a broken markup) → a permanent fail.
"""

from __future__ import annotations

import html
import logging
import os

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.dedup.also_on import also_on_text
from src.domain.sources import source_label_of
from src.bots.sr_latin import latinize_markup
from src.domain.text_norm import sr_display
from src.i18n_common import common_t
from src.i18n_email import norm_locale
from src.workers.adapters.base import (
    DeliveryAdapter,
    DeliveryJob,
    DeliveryResult,
    FormattedMessage,
    build_digest_text,
    job_counter_line,
    contract_type_label,
    cpv_label,
    format_amount_pair,
    format_deadline,
    format_published,
    match_reason_of,
    plan_period_label,
    priority_stars,
    procedure_type_label,
    regions_label,
    why_matched_pair,
)

logger = logging.getLogger(__name__)

# The cabinet base for the deep-link (historically — the “Open tender” button). In prod
# the value is the ENV CABINET_BASE_URL (domain nab.rs, ADR-026); the default fallback does not
# contain secrets. The button was removed (Chat 45), but the base URL/method are kept.
DEFAULT_CABINET_BASE_URL = "https://nab.rs"

# Substrings of the Bot API response equivalent to “the recipient is unreachable” (§15.5).
_BLOCKED_MARKERS = (
    "bot was blocked",
    "chat not found",
    "user is deactivated",
    "bot can't initiate conversation",
)


class TelegramAdapter(DeliveryAdapter):
    """Delivery to Telegram through aiogram ``Bot.send_message``."""

    channel = "telegram"

    def __init__(self, bot: Bot, *, cabinet_base_url: str | None = None) -> None:
        self._bot = bot
        base = cabinet_base_url or os.getenv(
            "CABINET_BASE_URL", DEFAULT_CABINET_BASE_URL
        )
        self._cabinet_base_url = (base or "").rstrip("/")

    def supports_buttons(self) -> bool:
        return True

    def _tender_url(self, tender_id: str) -> str:
        """Deep-link to the cabinet for the tender card (review focus §3).

        A historical helper: the “Open tender” button was removed (Chat 45; the cabinet is not on the domain).
        Kept for the unit test of the base URL; not called in the card.
        """
        return f"{self._cabinet_base_url}/tender/{tender_id}"

    def format(self, job: DeliveryJob) -> FormattedMessage:
        """Assembles the rich HTML card v6 + the inline buttons.

        A title with the priority stars, then the card rows (each ONLY when a value is present): tender No., region, subject type (or the procedure as a stand-in), CPV, the amount ORIG+EUR, publication date, the plan period, submissions due,
        the source, link. All dynamic text is escaped
        (``parse_mode=HTML``).
        Buttons:
          - row 1: 📂 Open in the cabinet (if there is a profile) · ⭐ Watch · ✖ Hide;
          - row 2: 🧠 AI analysis ▾ (expands the AI buttons; gate — on press);
          - (optional, debug): ⚙️ Configure the template — as a separate row.
        """
        # Lazy import: the single source of the funnel keyboards — src.bots.telegram,
        # we import it here so as not to pull the bot module when the adapter is imported.
        from src.bots.telegram import build_card_keyboard, cabinet_tender_url

        # The language of the card’s CHROME — the interface language of the recipient (users.locale, arrives
        # at the adapter as the field DeliveryJob.locale). The tender data (title, region,
        # CPV, sums, dates) is another axis, not translated.
        loc = job.locale

        title = html.escape(job.title or common_t(loc, "no_title"))
        stars = priority_stars(job.amount_eur)
        header = f"{stars} <b>{title}</b>" if stars else f"<b>{title}</b>"

        # The detailed card rows — added ONLY when a value is present.
        details: list[str] = []

        if job.source_uid:
            details.append(f"🔢 № {html.escape(str(job.source_uid))}")

        region = regions_label(job.regions)
        if region:
            details.append(f"📍 {common_t(loc, 'lbl_region')}: {html.escape(region)}")

        # Subject type (Supply/Services/Works). contract_type is currently not in the DB
        # (#142 → normalize does not map it) → fallback: the procedure as the label.
        type_label = contract_type_label(job.contract_type, loc)
        if type_label:
            details.append(f"📦 {common_t(loc, 'lbl_type')}: {html.escape(type_label)}")
        else:
            proc_label = procedure_type_label(job.procedure_type, loc)
            if proc_label:
                details.append(
                    f"📦 {common_t(loc, 'lbl_procedure')}: {html.escape(proc_label)}"
                )

        # CPV classification (Chat 44) — from classifications (scheme=CPV).
        cpv = cpv_label(job.classifications)
        if cpv:
            details.append(f"🏷 {common_t(loc, 'lbl_cpv')}: {html.escape(cpv)}")

        amount = format_amount_pair(
            job.amount_original,
            job.currency_original,
            job.amount_eur,
            job.is_amount_estimated,
            loc,
        )
        if amount:
            details.append(f"💰 {html.escape(amount)}")

        published = format_published(job.published_at)
        if published:
            details.append(
                f"📅 {common_t(loc, 'lbl_published')}: {html.escape(published)}"
            )

        # The plan period (Chat 45) — from classifications (scheme=PLAN).
        plan_period = plan_period_label(job.classifications)
        if plan_period:
            details.append(
                f"📅 {common_t(loc, 'lbl_plan')}: {html.escape(plan_period)}"
            )

        deadline = format_deadline(
            job.deadline_at,
            source_key=job.source_key,
            note=common_t(loc, "deadline_hour_unknown"),
        )
        if deadline:
            details.append(
                f"🗓 {common_t(loc, 'lbl_deadline')}: {html.escape(deadline)}"
            )

        platform = source_label_of(job.source_label, job.source_key)
        if platform:
            details.append(
                f"🏛 {common_t(loc, 'lbl_platform')}: {html.escape(platform)}"
            )
        also_on = also_on_text(common_t, loc, job.canonical_confidence, job.also_on_n)
        if also_on:
            details.append(f"🔁 {html.escape(also_on)}")

        if job.source_url:
            url = html.escape(job.source_url, quote=True)
            details.append(
                f'🔗 <a href="{url}">{html.escape(common_t(loc, "source"))}</a>'
            )

        profile_name_display = (
            html.escape(job.profile_name)
            if job.profile_name
            else html.escape(common_t(loc, "no_profile_name"))
        )
        # The match reason is computed ONCE: both the “Matched” row and the debug take it.
        reason = match_reason_of(job)
        pair = why_matched_pair(reason, common_t, loc)
        if pair:
            details.append(f"🎯 {html.escape(pair[0])}: {html.escape(pair[1])}")

        details.append(
            f"🔎 {common_t(loc, 'found_by_template')}: «{profile_name_display}»"
        )

        if job.debug_match:
            kw_text = (
                ", ".join(reason.matched_keywords) if reason.matched_keywords else "—"
            )
            cpv_text = ", ".join(reason.matched_cpv) if reason.matched_cpv else "—"
            details.append(
                f"🐞 {common_t(loc, 'debug_match')}:\n"
                f"   • {common_t(loc, 'debug_keywords')}: {html.escape(kw_text)}\n"
                f"   • {common_t(loc, 'debug_cpv')}: {html.escape(cpv_text)}"
            )

        # Daily limit: the counter row at the bottom — only from the threshold (80%); below it we stay silent.
        counter = job_counter_line(job, common_t)
        if counter:
            details.append(f"📊 {html.escape(counter)}")

        # An empty separator line — only if there are details (the minimum = the header).
        parts = [header]
        if details:
            parts.append("")
            parts.extend(details)
        text = "\n".join(parts)

        # The card buttons: ≤2 rows of ≤3 — [Open in the cabinet] [Watch] [Hide] and
        # [🧠 AI analysis ▾] (expands 🧠/🎯/📋/📝/🏛 with a separate markup edit).
        # “Open in the cabinet” — only when the profile is known: the tender page in the
        # cabinet without profile_id returns 404. The labels — by the recipient’s language;
        # callback_data does NOT depend on the language.
        cabinet_url = (
            cabinet_tender_url(self._cabinet_base_url, job.tender_id, job.profile_id)
            if job.profile_id
            else None
        )
        card_kb = build_card_keyboard(
            job.tender_id,
            status="new",
            locale=loc,
            cabinet_url=cabinet_url,
            ai_full=bool(job.profile_id),
        )
        rows: list = list(card_kb.inline_keyboard) if card_kb is not None else []

        if job.debug_match and job.profile_id:
            configure_url = f"{self._cabinet_base_url}/profiles/{job.profile_id}/edit"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=common_t(loc, "btn_configure_profile"), url=configure_url
                    )
                ]
            )

        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        return FormattedMessage(text=text, reply_markup=markup)

    async def _send_raw(
        self,
        target: str | None,
        text: str,
        reply_markup: object | None = None,
        locale: str | None = None,
    ) -> DeliveryResult:
        """Sends the text to the chat ``target`` (Telegram chat_id) with the mapping of errors §15.5.

        The common core for :meth:`send` (the card with buttons) and :meth:`send_text`
        (a short reminder without buttons). The behavior and the result codes are as before —
        ``send`` and ``send_text`` only assemble ``text``/``reply_markup`` differently.
        """
        if not target:
            return DeliveryResult(
                ok=False, error_text="нет chat_id (external_id пуст)", blocked=True
            )
        try:
            chat_id = int(target)
        except (TypeError, ValueError):
            return DeliveryResult(
                ok=False,
                error_text=f"некорректный chat_id: {target!r}",
                blocked=True,
            )

        # owner 10.10.2026: sr — Latin only (the tender data is stored in Cyrillic).
        lang = norm_locale(locale)
        text = sr_display(text, lang)
        reply_markup = latinize_markup(reply_markup, lang)
        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
            )
            return DeliveryResult(ok=True, response_code=200)
        except TelegramForbiddenError as exc:
            return DeliveryResult(
                ok=False, response_code=403, error_text=str(exc), blocked=True
            )
        except TelegramRetryAfter as exc:
            return DeliveryResult(
                ok=False, response_code=429, error_text=str(exc), retryable=True
            )
        except TelegramBadRequest as exc:
            msg = str(exc).lower()
            blocked = any(marker in msg for marker in _BLOCKED_MARKERS)
            return DeliveryResult(
                ok=False, response_code=400, error_text=str(exc), blocked=blocked
            )
        except TelegramAPIError as exc:  # 5xx and other server-side — retryable
            return DeliveryResult(ok=False, error_text=str(exc), retryable=True)

    def format_digest(self, jobs: list[DeliveryJob]) -> FormattedMessage:
        """Digest mode: one list message instead of N cards.

        There are no buttons (they are channel-specific for one tender) — instead of them, as in
        the card, a link to each tender’s primary source. All dynamic
        text is escaped: the bot’s messages go with ``parse_mode=HTML``.

        The chrome language — ``jobs[0].locale``: the batch is assembled over ONE channel of one
        recipient (grouping in :mod:`src.workers.delivery`), so its language is one. An empty batch → ``None`` → ru (as in the Email adapter).
        """
        return FormattedMessage(
            text=build_digest_text(
                jobs,
                link_for=lambda job: job.source_url,
                escape=html.escape,
                locale=jobs[0].locale if jobs else None,
            )
        )

    async def send(self, job: DeliveryJob, message: FormattedMessage) -> DeliveryResult:
        """Sends the card to the chat ``job.target`` (Telegram chat_id)."""
        return await self._send_raw(
            job.target, message.text, message.reply_markup, job.locale
        )

    async def send_text(
        self,
        target: str,
        text: str,
        locale: str | None = None,
        subject: str | None = None,
    ) -> DeliveryResult:
        """Sends a short plain-text message (a reminder) without buttons."""
        return await self._send_raw(target, text, locale=locale)
