"""The Email delivery adapter for tenders (ARCHITECTURE §15.1, ADR-025).

The Lite tier promises the subscriber delivery by e-mail (``src.api.tiers`` forcibly
sets ``delivery_channels=[“email”]``), but the channel was a stub — e-mails were not
sent at all. Here the channel is implemented on top of the ALREADY existing mail
contour :mod:`src.api.emailer` (``EmailSender`` / ``SmtpSender`` /
``PostmarkSender`` / ``MockEmailSender``) — there is no second mail contour in the project
and there must not be one.

The e-mail content is the same tender card as in
:class:`src.workers.adapters.telegram.TelegramAdapter`, but without inline buttons
(``supports_buttons()`` → False): the funnel/AI buttons require a messenger callback,
so in the e-mail their place is taken by LINKS — to the public tender card
``{CABINET_BASE_URL}/tender/{id}`` (all actions are available there) and to the
primary source (``source_url``). The e-mail body is given in two parts: plain text and
a simple HTML (``EmailSender.send`` puts the HTML in as an alternative).

``format`` returns :class:`EmailFormattedMessage` — ``FormattedMessage`` plus
the subject and the HTML part (``reply_markup`` of an e-mail is always ``None``). A subclass, not a
new contract: the delivery worker's ``send`` gets the object as is.

Privacy: the recipient address (``job.target``) is NEVER logged — PII.
The log/``delivery_attempts`` get ``outbox_id``/``user_channel_id`` and the CLASS of the
error: the text of an SMTP error contains the address (``SMTPRecipientsRefused`` puts
the recipient right into the exception arguments), and ``logger.exception`` prints it
in full in the traceback.

Fail-closed: the transport is passed to the constructor EXPLICITLY, and ``None`` (the mail env
is not configured — see :func:`src.workers.adapters.email_sender_from_env`) means “there is
nothing to send with”, not “send to nowhere”. There is no silent fallback to ``MockEmailSender`` here:
it puts the e-mail into a list in memory and raises no exception, that is, ``send``
would report ``ok=True`` for an unsent e-mail, and the outbox row would be
marked delivered — the e-mail is silently lost. An explicitly passed
``MockEmailSender`` (tests) is a legitimate transport and works as before.

Mapping of errors to :class:`DeliveryResult` (§15.5):

- the recipient was rejected by the server (``SMTPRecipientsRefused``) → ``blocked``: the address
  is dead, the channel is marked ``blocked``, the attempts stop;
- the transport is not configured → ``retryable``: the row stays in the queue and will go out when
  the env is set up — without code changes;
- other failures (network, 5xx, timeout) → ``retryable``: the row returns to the queue
  and will be retried.

An exception is never let out in any case — the delivery worker gets
a ``DeliveryResult``.
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
from dataclasses import dataclass

from src.api.emailer import EmailSender
from src.dedup.also_on import also_on_text
from src.workers.matching import MatchReason
from src.domain.sources import source_label_of
from src.i18n_email import email_t, norm_locale
from src.workers.adapters.base import (
    job_counter_line,
    DeliveryAdapter,
    DeliveryJob,
    DeliveryResult,
    FormattedMessage,
    build_digest_text,
    contract_type_label,
    cpv_label,
    digest_count_phrase,
    digest_facts,
    digest_heading,
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

# The base of the public tender card (the same as in the Telegram adapter): in prod
# ENV CABINET_BASE_URL (ADR-026); the default fallback contains no secrets.
DEFAULT_CABINET_BASE_URL = "https://nab.rs"

# The subject of the e-mail is limited: long Subjects are cut by mail clients.
_SUBJECT_MAX_LEN = 120


@dataclass(frozen=True)
class EmailFormattedMessage(FormattedMessage):
    """The rendered e-mail: ``text`` — the plain-text body, plus the subject and the HTML part."""

    subject: str = ""
    html_body: str | None = None


def build_digest_subject(jobs: list[DeliveryJob]) -> str:
    """The subject of the digest e-mail: “Digest: 3 new tenders” (the recipient’s language).

    The language of the chrome — ``jobs[0].locale`` (a batch has one recipient/channel). There are no dynamics
    from an external source in the subject (only a number) — header injection of the e-mail
    is impossible here by construction.
    """
    locale = jobs[0].locale if jobs else None
    prefix = email_t(locale, "digest_prefix")
    return f"{prefix}: {digest_count_phrase(len(jobs), locale)}"


def build_subject(job: DeliveryJob) -> str:
    """The subject of the e-mail: “New tender: <title>” (chrome — by ``job.locale``).

    The prefix chrome is localised by the recipient’s language; the TITLE of the tender is data
    (eNabavke), not translated. The title comes from an external source →
    line breaks from it are collapsed into a space: a line break in the value
    of a Subject header would be a header injection.
    """
    title = " ".join((job.title or "").split()) or email_t(job.locale, "no_title")
    subject = f"{email_t(job.locale, 'subject_new')}: {title}"
    if len(subject) > _SUBJECT_MAX_LEN:
        subject = subject[: _SUBJECT_MAX_LEN - 1].rstrip() + "…"
    return subject


class EmailAdapter(DeliveryAdapter):
    """Delivery of the tender card by e-mail through :class:`EmailSender`."""

    channel = "email"

    def __init__(
        self,
        email_sender: EmailSender | None = None,
        *,
        cabinet_base_url: str | None = None,
    ) -> None:
        self._email = email_sender
        base = cabinet_base_url or os.getenv(
            "CABINET_BASE_URL", DEFAULT_CABINET_BASE_URL
        )
        self._cabinet_base_url = (base or "").rstrip("/")

    def supports_buttons(self) -> bool:
        return False

    def is_configured(self) -> bool:
        """Whether there is a transport. ``False`` → ``send`` honestly fails into retryable."""
        return self._email is not None

    def _tender_url(self, tender_id: str) -> str:
        """A link to the public tender card (a replacement for the inline buttons)."""
        return f"{self._cabinet_base_url}/tender/{tender_id}"

    def _details(self, job: DeliveryJob, reason: MatchReason) -> list[tuple[str, str]]:
        """The card rows as “label → value” pairs (only non-empty ones).

        The same set of fields and the same logic of stand-ins as in the Telegram card
        (``contract_type`` does not get into the DB → the procedure label), but without markup:
        the text and the HTML are assembled from the same pairs. The LABELS — the chrome — by the recipient’s language
        (``job.locale``); the VALUES are tender data, not translated.
        """
        loc = job.locale
        details: list[tuple[str, str]] = []

        if job.source_uid:
            details.append((email_t(loc, "lbl_number"), str(job.source_uid)))

        region = regions_label(job.regions)
        if region:
            details.append((email_t(loc, "lbl_region"), region))

        type_label = contract_type_label(job.contract_type, loc)
        if type_label:
            details.append((email_t(loc, "lbl_type"), type_label))
        else:
            proc_label = procedure_type_label(job.procedure_type, loc)
            if proc_label:
                details.append((email_t(loc, "lbl_procedure"), proc_label))

        cpv = cpv_label(job.classifications)
        if cpv:
            details.append((email_t(loc, "lbl_cpv"), cpv))

        amount = format_amount_pair(
            job.amount_original,
            job.currency_original,
            job.amount_eur,
            job.is_amount_estimated,
            loc,
        )
        if amount:
            details.append((email_t(loc, "lbl_amount"), amount))

        published = format_published(job.published_at)
        if published:
            details.append((email_t(loc, "lbl_published"), published))

        plan_period = plan_period_label(job.classifications)
        if plan_period:
            details.append((email_t(loc, "lbl_plan"), plan_period))

        deadline = format_deadline(
            job.deadline_at,
            source_key=job.source_key,
            note=email_t(loc, "deadline_hour_unknown"),
        )
        if deadline:
            details.append((email_t(loc, "lbl_deadline"), deadline))

        platform = source_label_of(job.source_label, job.source_key)
        if platform:
            details.append((email_t(loc, "lbl_platform"), platform))

        pair = why_matched_pair(reason, email_t, loc)
        if pair:
            details.append(pair)

        details.append(
            (
                email_t(loc, "found_by_template"),
                job.profile_name or email_t(loc, "no_profile_name"),
            ),
        )
        return details

    def format(self, job: DeliveryJob) -> EmailFormattedMessage:
        """Assembles the e-mail: the subject, the plain-text body and the HTML alternative.

        Instead of inline buttons — links: the tender card in the cabinet (there is also
        the funnel of statuses and the AI analysis) and the primary source, if it is known.
        """
        loc = job.locale
        no_title = email_t(loc, "no_title")
        title = (job.title or no_title).strip() or no_title
        stars = priority_stars(job.amount_eur)
        heading = f"{stars} {title}".strip()
        details = self._details(job, match_reason_of(job))
        tender_url = self._tender_url(job.tender_id)
        open_card = email_t(loc, "open_card")

        text_lines = [
            email_t(loc, "greeting"),
            "",
            email_t(loc, "intro_single"),
            "",
            heading,
            "",
        ]
        text_lines.extend(f"{label}: {value}" for label, value in details)
        also_on = also_on_text(email_t, loc, job.canonical_confidence, job.also_on_n)
        if also_on:
            text_lines.append(f"🔁 {also_on}")
        text_lines.extend(["", f"{open_card}: {tender_url}"])
        if job.source_url:
            text_lines.append(f"{email_t(loc, 'source')}: {job.source_url}")
        text_lines.extend(["", email_t(loc, "footer_actions")])
        counter = job_counter_line(job, email_t)
        if counter:
            text_lines.extend(["", counter])
        text_lines.append("")
        text_body = "\n".join(text_lines)

        html_details = "".join(
            f"<li><b>{html.escape(label)}:</b> {html.escape(value)}</li>"
            for label, value in details
        )
        also_on_html = f"<p>🔁 {html.escape(also_on)}</p>" if also_on else ""
        source_html = ""
        if job.source_url:
            source_url = html.escape(job.source_url, quote=True)
            source_html = f'<p><a href="{source_url}">{html.escape(email_t(loc, "source"))}</a></p>'
        counter_html = (
            f'<p style="color:#555;font-size:90%">{html.escape(counter)}</p>'
            if counter
            else ""
        )
        html_body = (
            "<html><body>"
            f"<p>{html.escape(email_t(loc, 'greeting'))}</p>"
            f"<p>{html.escape(email_t(loc, 'intro_single'))}</p>"
            f"<h2>{html.escape(heading)}</h2>"
            f"<ul>{html_details}</ul>"
            f"{also_on_html}"
            f'<p><a href="{html.escape(tender_url, quote=True)}">'
            f"{html.escape(open_card)}</a></p>"
            f"{source_html}"
            f"<p>{html.escape(email_t(loc, 'footer_actions'))}</p>"
            f"{counter_html}"
            "</body></html>"
        )

        return EmailFormattedMessage(
            text=text_body,
            reply_markup=None,
            subject=build_subject(job),
            html_body=html_body,
        )

    def format_digest(self, jobs: list[DeliveryJob]) -> EmailFormattedMessage:
        """Digest mode: ONE e-mail with a list of tenders instead of N e-mails.

        A list, not N cards: each tender has the title, the amount/deadline and
        a link to the card in the cabinet (there is also the funnel of statuses and the AI analysis), as
        in a single e-mail. The body is plain text (:func:`build_digest_text`) plus
        an HTML alternative from the same data.
        """
        loc = jobs[0].locale if jobs else None
        no_title = email_t(loc, "no_title")
        # The match reason — once per tender: both the text and the HTML take this list.
        reasons = [match_reason_of(job) for job in jobs]
        text_body = build_digest_text(
            jobs,
            link_for=lambda job: self._tender_url(job.tender_id),
            locale=loc,
            reasons=reasons,
        )
        items = []
        for job, reason in zip(jobs, reasons):
            title = (job.title or "").strip() or no_title
            url = html.escape(self._tender_url(job.tender_id), quote=True)
            facts = digest_facts(job, loc)
            facts_html = f"<br>{html.escape(facts)}" if facts else ""
            pair = why_matched_pair(reason, email_t, loc)
            why_html = f"<br>{html.escape(f'{pair[0]}: {pair[1]}')}" if pair else ""
            items.append(
                f'<li><a href="{url}">{html.escape(title)}</a>{facts_html}{why_html}</li>'
            )
        profile_name = jobs[0].profile_name if jobs else None
        found_label = email_t(loc, "found_by_template")
        no_name = email_t(loc, "no_profile_name")
        digest_counter = job_counter_line(jobs[-1], email_t) if jobs else ""
        digest_counter_html = (
            f'<p style="color:#555;font-size:90%">{html.escape(digest_counter)}</p>'
            if digest_counter
            else ""
        )
        html_body = (
            "<html><body>"
            f"<p>{html.escape(email_t(loc, 'greeting'))}</p>"
            f"<h2>{html.escape(digest_heading(len(jobs), loc))}</h2>"
            f"<ol>{''.join(items)}</ol>"
            f"<p>{html.escape(found_label)}: "
            f"{html.escape(profile_name or no_name)}</p>"
            f"<p>{html.escape(email_t(loc, 'footer_actions_digest'))}</p>"
            f"{digest_counter_html}"
            "</body></html>"
        )
        return EmailFormattedMessage(
            text=text_body,
            reply_markup=None,
            subject=build_digest_subject(jobs),
            html_body=html_body,
        )

    async def send(self, job: DeliveryJob, message: FormattedMessage) -> DeliveryResult:
        """Sends the e-mail to ``job.target`` (the e-mail from ``user_channels.external_id``).

        A sending error is not let out — it is mapped to ``DeliveryResult``
        (§15.5). The recipient address does not get into the log or into ``error_text`` (PII).
        """
        if self._email is None:
            # There is no transport (the env is not configured) — the row stays in the queue and
            # will go out when the env appears. Success here = a silent loss of the e-mail.
            logger.error(
                "delivery/email: the mail transport is not configured, "
                "the e-mail is not sent outbox=%s user_channel=%s",
                job.outbox_id,
                job.user_channel_id,
            )
            return DeliveryResult(
                ok=False,
                error_text="почтовый транспорт не настроен",
                retryable=True,
            )

        target = (job.target or "").strip()
        if not target:
            return DeliveryResult(
                ok=False, error_text="нет email (external_id пуст)", blocked=True
            )

        subject = getattr(message, "subject", "") or build_subject(job)
        html_body = getattr(message, "html_body", None)
        try:
            await self._email.send(
                to=target,
                subject=subject,
                text_body=message.text,
                html_body=html_body,
                locale=norm_locale(job.locale),
            )
        except smtplib.SMTPRecipientsRefused as exc:
            # The address was rejected by the server — the channel is dead, retrying is pointless.
            self._log_failure(job, exc)
            return DeliveryResult(
                ok=False,
                error_text=f"адрес отвергнут ({type(exc).__name__})",
                blocked=True,
            )
        except Exception as exc:  # noqa: BLE001 — we do not let the exception out
            self._log_failure(job, exc)
            return DeliveryResult(
                ok=False,
                error_text=f"сбой отправки email ({type(exc).__name__})",
                retryable=True,
            )
        return DeliveryResult(ok=True, response_code=200)

    async def send_text(
        self,
        target: str,
        text: str,
        locale: str | None = None,
        subject: str | None = None,
    ) -> DeliveryResult:
        """A short summary e-mail (the delivery limit) without a card; errors — into the result."""
        if self._email is None:
            return DeliveryResult(
                ok=False, error_text="почтовый транспорт не настроен", retryable=True
            )
        try:
            await self._email.send(
                to=target.strip(),
                subject=subject or email_t(locale, "subject_new"),
                text_body=text,
                html_body=f"<html><body><p>{html.escape(text)}</p></body></html>",
                locale=norm_locale(locale),
            )
        except Exception as exc:  # noqa: BLE001 — we do not put the address into the log/error (PII)
            logger.error(
                "delivery/email: the summary e-mail failed error=%s", type(exc).__name__
            )
            return DeliveryResult(
                ok=False,
                error_text=f"сбой отправки email ({type(exc).__name__})",
                retryable=True,
            )
        return DeliveryResult(ok=True, response_code=200)

    @staticmethod
    def _log_failure(job: DeliveryJob, exc: Exception) -> None:
        """Logs the failure WITHOUT the address and WITHOUT a traceback (see the module docstring)."""
        logger.error(
            "delivery/email: the send failed outbox=%s user_channel=%s error=%s",
            job.outbox_id,
            job.user_channel_id,
            type(exc).__name__,
        )
