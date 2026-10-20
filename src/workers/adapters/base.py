"""The contract of the channel delivery adapter (ARCHITECTURE §15.1, ADR-025).

An adapter encapsulates one channel (Telegram/Viber/Email/Web): it renders the ready
message from the delivery task and sends it to the recipient. The base layer does NOT
import aiogram/the messenger SDKs — it stays testable in isolation;
channel specifics (button types, exceptions) live in the concrete adapters.

Value objects:

- :class:`DeliveryJob` — one ``delivery_outbox`` row plus the tender fields and
  the channel target (``target``), assembled by a join in :mod:`src.workers.delivery`.
  The adapter does not go to the DB — it gets a ready job.
- :class:`FormattedMessage` — the rendered text + channel-specific markup
  (for Telegram — ``InlineKeyboardMarkup``; ``None`` for channels without buttons).
- :class:`DeliveryResult` — the outcome of sending: success/error, code, a sign of a block
  by the recipient (403) and a sign of a temporary (retryable) error.

Formatting helpers (pure, no network/DB) — for the rich card v6:
``format_amount`` / ``format_amount_pair`` / ``format_deadline`` /
``format_published`` / ``priority_stars`` / ``contract_type_label`` /
``procedure_type_label`` / ``regions_label`` / ``cpv_label`` /
``plan_period_label`` (the last two — from ``classifications``, Chat 44/45).

Helpers that return CHROME (the label of the subject type/procedure, the “estimated” mark)
accept ``locale`` — the interface language of the recipient (``job.locale`` =
``users.locale``); ``None`` → ru, so old calls without a language behave
as before. The single catalog of this vocabulary is :mod:`src.i18n_common` (the shared vocabulary
bot↔cabinet↔delivery). The tender data (numbers, currency, names) is NOT translated.

The optional ``send_text`` capability (a short plain text without a card/buttons)
was added for the reminder/summary workers (NEXT №1–2): the base method raises
``NotImplementedError``, the supporting channels (Telegram) override it. The ``format``/``send`` contract
of the delivery is NOT affected by this.

Digest mode (``profiles.delivery_schedule = digest_*``) is rendered by
:meth:`DeliveryAdapter.format_digest` — ONE message per batch of tenders of one
profile instead of N cards. The base implementation builds a plain-text list
(:func:`build_digest_text`) and suits any channel; Telegram and Email
override it for their own markup. The sending goes through the same ``send``: a digest is
another ``FormattedMessage``, not another transport.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from src.domain.delivery_cap import counter_text
from src.domain.tz import to_local
from src.dedup.also_on import also_on_text
from src.i18n_common import common_t
from src.i18n_email import digest_count_phrase, email_t
from src.workers.matching import MatchReason, is_english_tender, match_reason
from src.parsers.capabilities import deadline_has_time


@dataclass(frozen=True)
class DeliveryJob:
    """The delivery task: an outbox row + the tender fields (§2.10) + the channel target (§2.5).

    The tender fields — for rendering the message; ``target`` — the recipient address in the channel
    (``user_channels.external_id``: Telegram chat_id / Viber user_id / e-mail).
    ``channel_status`` — the status of the channel binding (``active`` / ``blocked``):
    the worker does not send to an inactive channel.

    The fields of the rich card v6 (``source_uid``..``classifications``) are added
    at the END of the dataclass with a default ``None`` — the order of the earlier positional fields
    does not change (frozen dataclass; :mod:`src.workers.delivery` creates the job by
    names). ``contract_type`` is extracted from ``classifications`` (see delivery);
    by the structure of the fact (#142 → normalize) it is not in the DB now — the card
    uses ``procedure_type`` as a temporary stand-in. ``classifications`` —
    a LIST of records `[{scheme,...}]` (cpv + the plan period, Chat 44/45); a dict
    is accepted for backward compatibility with a single record.
    """

    outbox_id: int
    tender_id: str
    channel: str
    requires_ai: bool
    # channel target
    user_channel_id: str | None
    target: str | None
    channel_status: str | None
    # tender content (for format)
    title: str | None
    source_url: str | None
    amount_eur: Decimal | float | None
    deadline_at: datetime | None
    # --- rich card v6 (NEXT №1): all Optional, default None, at the END ---
    source_uid: str | None = None
    source_key: str | None = None
    #: ``tenders_normalized.language`` — whether the tender is English-language (as in routing).
    language: str | None = None
    amount_original: Decimal | float | None = None
    currency_original: str | None = None
    is_amount_estimated: bool | None = None
    published_at: datetime | None = None
    procedure_type: str | None = None
    regions: list[str] | None = None
    contract_type: str | None = None
    classifications: dict | list | None = None
    # profile_id of the outbox row — for the daily card limit per profile
    # (delivery anti-spam, see src.workers.delivery). At the END of the frozen dataclass
    # with a default None: the order of the earlier positional fields does not change.
    profile_id: str | None = None
    # --- profile label + debug mode (EPIC 2) ---
    # profile_name — the human-readable name of the template (profiles.name); None → fallback.
    profile_name: str | None = None
    # debug_match — the debug-mode flag of the profile (profiles.debug_match).
    debug_match: bool = False
    # Profile fields for on-the-fly computation of the match reason (only when debug_match).
    profile_keywords_include: Any = None
    profile_keywords_exclude: list[str] | None = None
    profile_classifications: dict | None = None
    # The profile’s dictionary mode (profiles.keyword_dict): words found through
    # the dictionary must get into “Matched” — otherwise the row loses the client’s word.
    profile_keyword_dict: bool = False
    # haystack of the tender (title_normalized + description_normalized) for computing
    # the match reason by keywords when debug_match=true.
    tender_haystack: str | None = None
    # The interface language of the recipient (users.locale) — the language of the e-mail CHROME (subject-
    # prefix, card body, digest). Not the language of the AI content (profiles.
    # target_language). NULL/unknown → ru (see src.i18n_email.norm_locale).
    # At the END of the frozen dataclass with a default None: the order of the positional fields is unchanged.
    locale: str | None = None
    # The human-readable name of the source (``sources.display_name``) — what
    # the client sees instead of the key ``jnportal`` (SRC-0d). NULL/empty → fallback to
    # ``source_key`` (see :func:`src.domain.sources.source_label_of`).
    source_label: str | None = None
    # The cross-source cluster (0047): the link verdict of THIS row (``dup`` /
    # ``probable`` / None) and the number of OTHER sources of the cluster — for the mark
    # “the same tender is also on other sources” (:mod:`src.dedup.also_on`).
    canonical_confidence: str | None = None
    also_on_n: int = 0
    # The daily delivery limit (src.domain.delivery_cap): how many tenders of the template
    # go out today INCLUDING this send, and what the limit is. Set by the worker
    # (dataclasses.replace); 0/0 = “we don’t know” → the counter row is not shown.
    daily_sent: int = 0
    daily_cap: int = 0


@dataclass(frozen=True)
class FormattedMessage:
    """The rendered message for the channel."""

    text: str
    reply_markup: object | None = None


@dataclass(frozen=True)
class DeliveryResult:
    """The outcome of a delivery attempt (written to ``delivery_attempts`` §2.15).

    Attributes:
        ok: sent successfully.
        response_code: HTTP/Bot-API code (200/403/429/…), ``None`` if none.
        error_text: the error text for the attempt log.
        blocked: the recipient blocked the bot / the chat is not found (403) — the channel
            is marked ``blocked``, further attempts stop (§15.5).
        retryable: a temporary error (429/5xx/network) — the row returns to the
            ready status and will be retried on the next polling cycle.
    """

    ok: bool
    response_code: int | None = None
    error_text: str | None = None
    blocked: bool = False
    retryable: bool = False


def _format_int(value: Decimal | float | int | str | None) -> str | None:
    """An integer sum with a space as the thousands separator, or ``None``.

    ``12345`` → ``"12 345"``; non-numeric/``None`` → ``None`` (decimals are dropped — tender sums are large, the cents are not needed in the card).
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"{number:,.0f}".replace(",", " ")


def format_amount(amount_eur: Decimal | float | None) -> str | None:
    """A human-readable sum in EUR with thousands separators, or ``None``.

    ``12345`` → ``"12 345 EUR"``; ``None`` → ``None`` (the amount line
    is not added to the message).
    """
    number = _format_int(amount_eur)
    return f"{number} EUR" if number is not None else None


# Priority thresholds (stars) by the EUR sum — module constants (edited here).
PRIORITY_STAR_THRESHOLDS: tuple[int, int, int] = (50_000, 250_000, 1_000_000)


def priority_stars(amount_eur: Decimal | float | None) -> str:
    """Priority stars by the EUR sum (thresholds — :data:`PRIORITY_STAR_THRESHOLDS`).

    ``None``/``0``/non-numeric → ``""``; ``<50k`` → ``"⭐"``; ``50k–250k`` →
    ``"⭐⭐"``; ``250k–1M`` → ``"⭐⭐⭐"``; ``≥1M`` → ``"⭐⭐⭐⭐"``.
    """
    if amount_eur is None:
        return ""
    try:
        value = float(amount_eur)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    t1, t2, t3 = PRIORITY_STAR_THRESHOLDS
    if value < t1:
        return "⭐"
    if value < t2:
        return "⭐⭐"
    if value < t3:
        return "⭐⭐⭐"
    return "⭐⭐⭐⭐"


def format_amount_pair(
    amount_original: Decimal | float | None,
    currency_original: str | None,
    amount_eur: Decimal | float | None,
    is_amount_estimated: bool | None = None,
    locale: str | None = None,
) -> str | None:
    """The sum in the original currency + the EUR equivalent, or ``None`` if there are no sums.

    Examples: ``“12 345 678 RSD (~105 200 EUR)”`` (both sums); only EUR →
    ``“105 200 EUR”``; only the original → ``“12 345 678 RSD”``. EUR is a conversion,
    hence always with ``~``. ``is_amount_estimated`` → the prefix ``~`` before
    the original and the “estimated” mark at the end — the only CHROME in the sum line,
    so it is taken from the catalog by ``locale`` (``None`` → ru, the behavior
    unchanged). The numbers themselves and the currency code are data, not translated.
    """
    orig_number = _format_int(amount_original)
    eur_number = _format_int(amount_eur)
    currency = (currency_original or "").strip().upper() or None
    estimated = bool(is_amount_estimated)

    orig_part: str | None = None
    if orig_number is not None and currency is not None:
        prefix = "~" if estimated else ""
        orig_part = f"{prefix}{orig_number} {currency}"
    eur_part = f"{eur_number} EUR" if eur_number is not None else None

    if orig_part and eur_part:
        text = f"{orig_part} (~{eur_number} EUR)"
    elif orig_part:
        text = orig_part
    elif eur_part:
        text = eur_part
    else:
        return None

    if estimated:
        text = f"{text} {common_t(locale, 'amount_estimated')}"
    return text


def format_deadline(
    deadline_at: datetime | None,
    *,
    source_key: str | None = None,
    note: str | None = None,
) -> str | None:
    """Deadline in the format ``DD.MM.YYYY HH:MM`` Belgrade time, or ``None``.

    The single display point of the deadline for the bot and the e-mail (called by ``telegram.py``,
    ``email.py``, ``reminders.py``, ``summary.py``). The deadline is stored in UTC in the DB;
    here is the display boundary, hence the conversion to ``Europe/Belgrade``
    (:func:`to_local`) before ``strftime``. A naive input is treated as UTC.

    A source without a closing hour (``deadline_time=False``, CFKU): only the date and
    the ``note`` — a localised mark, passed by the caller in its own language (the e-mail and Telegram take it from different catalogs).
    """
    if deadline_at is None:
        return None
    local = to_local(deadline_at)
    if not deadline_has_time(source_key):
        date_text = local.strftime("%d.%m.%Y")
        return f"{date_text} ({note})" if note else date_text
    return local.strftime("%d.%m.%Y %H:%M")


def format_published(published_at: datetime | None) -> str | None:
    """Publication date in the format ``DD.MM.YYYY`` (without time), or ``None``.

    The format is date-only, with no hours, so we do NOT convert to local time: a shift within the day does not change the date, while PublishDate for some tenders is a midnight placeholder (see `src.domain.tz.is_midnight`), which a local conversion would move to the neighbouring day.
    """
    if published_at is None:
        return None
    return published_at.strftime("%d.%m.%Y")


# Contract subject type → KEY of the shared-vocabulary catalog (ct_goods/ct_services/
# ct_works). Machine values (en) + the Serbian text from the card header (#142, on
# case where contract_type starts being mapped onto NormalizedTender). Here only
# the mapping “source code → meaning”; the human label is in src.i18n_common
# (one label for all channels and languages). An unknown value → as is.
_CONTRACT_TYPE_KEYS: dict[str, str] = {
    "goods": "ct_goods",
    "supplies": "ct_goods",
    "supply": "ct_goods",
    "services": "ct_services",
    "service": "ct_services",
    "works": "ct_works",
    "work": "ct_works",
    "dobra": "ct_goods",
    "usluge": "ct_services",
    "radovi": "ct_works",
}


def contract_type_label(
    contract_type: str | None, locale: str | None = None
) -> str | None:
    """Machine/Serbian value of the subject type → the label in the recipient’s language.

    ``locale`` — the language of the CHROME (``users.locale``); ``None``/unknown code → ru
    (:func:`src.i18n_common.common_t`), so a call without a language behaves as
    before. An unknown value is returned as is (we do not invent a label);
    ``None``/empty → ``None``.
    """
    if contract_type is None:
        return None
    raw = str(contract_type).strip()
    if not raw:
        return None
    key = _CONTRACT_TYPE_KEYS.get(raw.lower())
    return common_t(locale, key) if key else raw


# Procedure type (enum NormalizedTender.procedure_type) → KEY of the shared
# vocabulary catalog (pt_*). It serves as a temporary stand-in for “Subject type”, while
# contract_type does not get into the DB (see delivery._contract_type_from_
# classifications). Unknown → as is.
_PROCEDURE_TYPE_KEYS: dict[str, str] = {
    "open": "pt_open",
    "restricted": "pt_restricted",
    "negotiated_with_call": "pt_negotiated_with_call",
    "negotiated_without_call": "pt_negotiated_without_call",
    "framework": "pt_framework",
    "dps": "pt_dps",
    "competitive_dialogue": "pt_competitive_dialogue",
    "innovation_partnership": "pt_innovation_partnership",
    "design_contest": "pt_design_contest",
    "pin": "pt_pin",
    "qualification": "pt_qualification",
    "small_value": "pt_small_value",
}


def procedure_type_label(
    procedure_type: str | None, locale: str | None = None
) -> str | None:
    """Machine value of the procedure → the label in the recipient’s language.

    ``locale`` see :func:`contract_type_label`; unknown → as is; None→None.
    """
    if procedure_type is None:
        return None
    raw = str(procedure_type).strip()
    if not raw:
        return None
    key = _PROCEDURE_TYPE_KEYS.get(raw.lower())
    return common_t(locale, key) if key else raw


def regions_label(regions: list[str] | None) -> str | None:
    """A list of regions → a string joined by ``", "``; empty/``None`` → ``None``."""
    if not regions:
        return None
    parts = [str(r).strip() for r in regions if r is not None and str(r).strip()]
    if not parts:
        return None
    return ", ".join(parts)


# --- classifications → card labels (cpv + plan period, Chat 44/45) ----- #
def _iter_classifications(classifications: Any) -> list[dict[str, Any]]:
    """Normalises the input to a list of dict records: None→[]; dict→[dict]; list→dicts."""
    if classifications is None:
        return []
    if isinstance(classifications, dict):
        return [classifications]
    if isinstance(classifications, list):
        return [c for c in classifications if isinstance(c, dict)]
    return []


def _find_classification(classifications: Any, scheme: str) -> dict[str, Any] | None:
    """The first record with ``scheme`` (case-insensitive) or ``None``."""
    target = scheme.strip().lower()
    for c in _iter_classifications(classifications):
        value = c.get("scheme")
        if isinstance(value, str) and value.strip().lower() == target:
            return c
    return None


def cpv_label(classifications: Any) -> str | None:
    """CPV record (`scheme=CPV`) → “45000000 — Građevinski radovi”, or ``None``.

    Accepts a list of classifications records OR a single dict OR None. If
    there is only a code / only a name — returns what is there; no CPV record or
    empty → None. The separator is an em dash “—”.
    """
    record = _find_classification(classifications, "CPV")
    if record is None:
        return None
    code = record.get("code")
    label = record.get("label")
    code = str(code).strip() if code not in (None, "") else None
    label = str(label).strip() if label not in (None, "") else None
    if code and label:
        return f"{code} — {label}"
    if code:
        return code
    if label:
        return label
    return None


def plan_period_label(classifications: Any) -> str | None:
    """PLAN record (`scheme=PLAN`) → “2. kvartal 2026” / “2. kvartal” / “2026”, or None.

    Accepts a list of classifications records OR a single dict OR None.
    Glues the non-empty ``quarter`` and ``year`` with a space (None/empty
    are skipped). No PLAN record / both empty → None.
    """
    record = _find_classification(classifications, "PLAN")
    if record is None:
        return None
    quarter = record.get("quarter")
    year = record.get("year")
    quarter = str(quarter).strip() if quarter not in (None, "") else None
    year = str(year).strip() if year not in (None, "") else None
    parts = [p for p in (quarter, year) if p]
    if not parts:
        return None
    return " ".join(parts)


# --- digest: one message per batch of tenders of one profile -------------- #
# The forms “N new tenders” and the declension live in the NEUTRAL catalog
# :mod:`src.i18n_email` (the single source of the e-mail chrome; see its docstring).
# ``locale`` in all digest helpers defaults to ``ru`` — this keeps the
# previous (ru-only) behavior of the Telegram adapter, which calls them without a language;
# the Email adapter passes the recipient’s language (``job.locale``).


def digest_heading(count: int, locale: str | None = None) -> str:
    """The digest header: “📬 Digest: 3 new tenders” (by the recipient’s language)."""
    prefix = email_t(locale, "digest_heading_prefix")
    return f"{prefix}: {digest_count_phrase(count, locale)}"


def digest_facts(job: DeliveryJob, locale: str | None = None) -> str | None:
    """A short line of tender facts for the digest: “Amount: … · Submissions due: …”.

    The digest is a list, not a card: we take only two fields, by which the subscriber decides whether to open the tender. No facts at all → ``None``
    (the line is not added to the digest). The chrome labels — by the recipient’s language;
    the values (sums/dates) are data and are not translated.
    """
    facts: list[str] = []
    amount = format_amount_pair(
        job.amount_original,
        job.currency_original,
        job.amount_eur,
        job.is_amount_estimated,
        locale,
    )
    if amount:
        facts.append(f"{email_t(locale, 'lbl_amount')}: {amount}")
    deadline = format_deadline(
        job.deadline_at,
        source_key=job.source_key,
        note=email_t(locale, "deadline_hour_unknown"),
    )
    if deadline:
        facts.append(f"{email_t(locale, 'lbl_deadline')}: {deadline}")
    also_on = also_on_text(email_t, locale, job.canonical_confidence, job.also_on_n)
    if also_on:
        facts.append(f"🔁 {also_on}")
    return " · ".join(facts) if facts else None


# Limits of the “Why this tender is for you” row: beyond them — “+N”, the card does not bloat.
WHY_MAX_WORDS = 3
WHY_MAX_CPV = 2


def match_reason_of(job: DeliveryJob) -> MatchReason:
    """The match reason by the job’s fields (the template words + the tender’s CPV).

    A pure function, no DB. Called ONCE per job, and the result is passed
    further: the “Matched” row and the Telegram debug block take the same object.
    """
    return match_reason(
        job.profile_keywords_include,
        job.profile_keywords_exclude,
        job.tender_haystack or "",
        job.classifications,
        job.profile_classifications,
        keyword_dict_enabled=job.profile_keyword_dict,
        # The same predicate as in routing/preview: the tender's language OR the source.
        english=is_english_tender(job.language, job.source_key),
    )


def _capped(items: list[str], limit: int) -> str:
    """“a, b, c +N”: the first ``limit`` values and the counter of the remainder."""
    rest = len(items) - limit
    shown = ", ".join(items[:limit])
    return f"{shown} +{rest}" if rest > 0 else shown


def why_matched_pair(
    reason: MatchReason, t: Callable[..., str], locale: str | None
) -> tuple[str, str] | None:
    """A pair (“Matched”, “gips · CPV 45421000”) or ``None`` if there is no reason.

    ``None`` — when there are neither words nor CPV (match_all or only the region): the line
    is not shown. The label is chrome by the recipient’s language; the words are the originals from the template,
    the codes are from the tender, both are not translated. Escaping is on the channel’s side.
    """
    parts: list[str] = []
    if reason.matched_keywords:
        parts.append(_capped(reason.matched_keywords, WHY_MAX_WORDS))
    if reason.matched_tender_cpv:
        parts.append("CPV " + _capped(reason.matched_tender_cpv, WHY_MAX_CPV))
    if not parts:
        return None
    return t(locale, "why_matched"), " · ".join(parts)


def job_counter_line(job: DeliveryJob, t: Callable[..., str]) -> str:
    """The line “Sent today N of M for template …” for the footer; ``""`` below the threshold."""
    name = job.profile_name or t(job.locale, "no_profile_name")
    return counter_text(t, job.locale, job.daily_sent, job.daily_cap, name)


def build_digest_text(
    jobs: list[DeliveryJob],
    *,
    link_for: Callable[[DeliveryJob], str | None],
    escape: Callable[[str], str] | None = None,
    locale: str | None = None,
    reasons: list[MatchReason] | None = None,
) -> str:
    """The plain text of the digest: header, a numbered list of tenders, the template label.

    ``link_for`` — the link to the tender for the channel (for Telegram the primary source, as in
    the card; for Email — the card in the cabinet); ``None``/empty → the link line is not
    added. ``escape`` — escaping of DYNAMIC text for channels with markup
    (Telegram, ``parse_mode=HTML``); by default the text as is.
    ``locale`` — the language of the chrome (header, facts, the template label); default ``ru``.
    ``reasons`` — the match reasons, parallel to ``jobs`` (the caller, who needs them and for something else too, computes them once); ``None`` → computed here.
    All jobs of the batch belong to one profile (grouping in
    :mod:`src.workers.delivery`), so the template label is one per message.
    """
    esc = escape or (lambda value: value)
    lines: list[str] = [esc(digest_heading(len(jobs), locale)), ""]
    no_title = email_t(locale, "no_title")
    for index, job in enumerate(jobs, start=1):
        title = (job.title or "").strip() or no_title
        stars = priority_stars(job.amount_eur)
        head = f"{index}. " + (f"{stars} " if stars else "") + esc(title)
        lines.append(head)
        facts = digest_facts(job, locale)
        if facts:
            lines.append(f"   {esc(facts)}")
        reason = reasons[index - 1] if reasons else match_reason_of(job)
        pair = why_matched_pair(reason, email_t, locale)
        if pair:
            lines.append(f"   {esc(f'{pair[0]}: {pair[1]}')}")
        link = link_for(job)
        if link:
            lines.append(f"   {esc(link)}")
        lines.append("")
    profile_name = jobs[0].profile_name if jobs else None
    label = email_t(locale, "found_by_template")
    no_name = email_t(locale, "no_profile_name")
    lines.append(esc(f"{label}: {profile_name or no_name}"))
    counter = job_counter_line(jobs[-1], email_t) if jobs else ""
    if counter:
        lines.append(esc(counter))
    return "\n".join(lines)


class DeliveryAdapter(ABC):
    """The base channel adapter. ``channel`` — the machine key of the channel."""

    channel: str = ""

    def supports_buttons(self) -> bool:
        """Whether the channel supports inline buttons (Telegram/Viber — yes)."""
        return False

    def is_configured(self) -> bool:
        """Whether the channel has a working transport (for the worker’s start-up diagnostics).

        The transport of most channels is passed to the constructor explicitly, and without it
        the adapter cannot be created — the base ``True`` is enough for them. Channels whose transport
        is taken from the env and may be absent (Email) override it:
        an unconfigured adapter must honestly fail in ``send``, not report success.
        """
        return True

    @abstractmethod
    def format(self, job: DeliveryJob) -> FormattedMessage:
        """Renders the message from the delivery task (no network calls)."""
        raise NotImplementedError

    def format_digest(self, jobs: list[DeliveryJob]) -> FormattedMessage:
        """Renders ONE digest message for a batch of tenders of one profile.

        Called by the delivery worker for profiles with ``delivery_schedule=digest_*``
        instead of N calls to :meth:`format`; it is sent by the same :meth:`send`
        (the first job of the batch carries ``target``). The base implementation is plain text with
        a link to the primary source; channels with markup override it.
        """
        return FormattedMessage(
            text=build_digest_text(jobs, link_for=lambda job: job.source_url)
        )

    @abstractmethod
    async def send(self, job: DeliveryJob, message: FormattedMessage) -> DeliveryResult:
        """Sends the message to the recipient ``job.target``. Returns the result."""
        raise NotImplementedError

    async def send_text(
        self,
        target: str,
        text: str,
        locale: str | None = None,
        subject: str | None = None,
    ) -> DeliveryResult:
        """Sends a short plain text to the recipient ``target`` (without a card/buttons).

        An optional channel capability for the reminder/summary workers (NEXT
        №1–2): it does not require :class:`DeliveryJob`/``format``. The base implementation
        raises ``NotImplementedError`` (the channel cannot do plain text); channels
        with support (Telegram) override it. The ``format``/``send`` contract
        of the delivery is NOT affected by this method.
        """
        raise NotImplementedError(f"{self.channel}: send_text not implemented")
