"""Delivery Worker — sends tenders from ``delivery_outbox`` to the channels.

ARCHITECTURE §8 (the outbox pattern), §15 (delivery channels), ADR-025.
DB_SCHEMA §2.14 (delivery_outbox), §2.15 (delivery_attempts), §2.5 (user_channels).

Pipeline (the channels Telegram and Email; Viber — a stub):

1. **poll** — every ``poll_interval`` takes a batch of ready outbox rows
   (``status IN ('pending','ai_done')``, the channel from ``enabled_channels``), except
   the rows of profiles whose digest window has not come yet (see below).
2. **dispatch** — for each row resolves the channel adapter, renders the message
   and sends it. The rows of a digest profile within the window go out as ONE message per
   profile+channel (``adapter.format_digest``), not as a card per tender.
3. **finalize** — writes ``delivery_attempts`` and moves the outbox status
   (``sent`` / ``failed`` / back to the ready state for a retry).

Delivery schedule (``profiles.delivery_schedule``; the cabinet offers it from the start —
“Immediately” / “Morning” / “Evening” / “Custom schedule (Pro+)”):

- ``immediate`` / ``NULL`` — as before: a card for each tender at once.
- ``digest_morning`` / ``digest_evening`` — accumulate and send in one batch in a fixed window
  (:data:`DIGEST_MORNING_HOUR` / :data:`DIGEST_EVENING_HOUR`, the hour in the tenant's tz).
- ``digest_custom`` — the window by ``profiles.digest_cron`` (see :func:`digest_window`).

**The rows are not lost, they wait:** outside the window the profile simply does not take part in the pick-up
(``_CLAIM_SQL`` excludes its ``profile_id``) — the row status does not change, it
goes out in the nearest window. The ``FOR UPDATE SKIP LOCKED`` contour and the status change
are untouched.

**The idempotency of the digest — without a new table:** “sent today” is visible from
the outbox itself (``max(sent_at)`` of the profile's rows, the date in the tenant's tz). The digest of
a profile goes out once a day: after the sending the rows are already in ``sent``, and the profile
until the end of the day again counts as “outside the window” — a repeated run will not send a second
digest; the new tenders accumulate until the next window.

The price of “one digest a day”: no more than ``batch_size``
rows (default 20) go into the batch, and the tail beyond that waits for the NEXT window — the rows are not
lost, but arrive a day later. The daily cap of the profile (``daily_cap``,
default 30 tenders) cuts such bursts anyway, so at the current scale the tail is unreachable; if
digests start hitting 20 rows — raise ``batch_size``, do not fix the window (otherwise a second digest a day would come back).

Reliability (the review focus of the task):

1. **Idempotency.** The rows are taken by an atomic ``UPDATE ... WHERE id IN
   (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`` into the transit status
   ``delivering`` (the field ``status`` is a free TEXT without CHECK, so no migration
   is needed). Parallel workers will not take the same row (``SKIP LOCKED`` +
   the status change); a repeated run will not send a ``sent`` row (its status is no longer
   in ``pending``/``ai_done``). Hung ``delivering`` rows (a worker crash before finalisation) are revived at start-up by :meth:`recover_stale` — they return to the
   ready status by ``requires_ai`` (``ai_done`` if AI was required, otherwise ``pending``).
2. **403 “bot was blocked”.** The adapter returns ``DeliveryResult(blocked=True)``
   → ``user_channels.status='blocked'``, ``delivery_outbox.status='failed'``,
   a record in ``delivery_attempts``; further attempts stop (the status
   ``failed`` is terminal, the channel is blocked).
3. **The message format** — in the channel adapter: the rich card v6 with buttons in
   :class:`src.workers.adapters.telegram.TelegramAdapter`, the same card as an e-mail
   (buttons → links) in :class:`src.workers.adapters.email.EmailAdapter`.
   ``_load_jobs`` pulls all the card fields from ``tenders_normalized`` (§2.10) and
   puts them into :class:`DeliveryJob`.

Anti-spam of delivery (protection against a salvo of the daily eNabavke batch):

- **Pacing.** Between the dispatches of the jobs in one cycle a pause of
  ``send_delay`` seconds is kept (a constructor parameter; env ``DELIVERY_SEND_DELAY_SEC``,
  default ``1.0``; ``0`` — no pause) — it smooths the salvo and does not hit
  the flood limits of Telegram.
- **The daily cap per profile — in TENDERS.** No more than ``daily_cap`` DIFFERENT
  tenders per profile for the current UTC day (a constructor parameter; env
  ``DELIVERY_DAILY_CAP``, default ``30``; ``0`` — the cap is off). An outbox row
  is written per channel, so we count ``DISTINCT tender_id``: a tender in Telegram and by e-mail spends ONE unit (owner 16.10.2026). The cap cuts only
  the sending; the site shows all matches (the feed also reads ``suppressed``).
  Beyond the cap the row moves to the terminal ``suppressed`` (a free TEXT, no migration needed) with a record in ``delivery_attempts`` (``err='daily_cap'``),
  and the message is NOT sent. On the first suppression of a profile for the day, a one-off localised summary with links to the feed and the template goes out
  in the CHANNEL (``adapter.send_text``). From the 80% threshold (:mod:`src.domain.delivery_cap`) a line “Sent today N of M” is added to the messages. Invariant:
  ``suppressed`` is terminal, ``_CLAIM_SQL`` takes only
  ``pending``/``ai_done``, ``recover_stale`` — only ``delivering``, so the suppressed rows are not re-sent. Rows with ``profile_id IS NULL`` do not fall under the cap.

The dependencies are injected (the session factory, the adapter registry, the batch size,
the interval) — for unit tests with a mocked DB and a fake adapter. Like
the routing worker, it goes to the DB over a privileged connection (without
``SET app.current_tenant_id``): RLS on ``delivery_outbox`` / ``user_channels``
is enabled, but the owner of the tables bypasses it — a cross-tenant sending is
a server operation (ARCHITECTURE §8, DB_SCHEMA §4).

Not done (separate sprints): quiet hours, retry-backoff/DLQ, Viber/Web
(stubs), wiring the webhook into FastAPI (Track F).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import text

from src.db.session import session_scope
from src.dedup.also_on import also_on_count_sql
from src.domain.delivery_cap import DEFAULT_DAILY_CAP
from src.domain.delivery_cap import daily_cap as daily_cap_from_env
from src.i18n_common import common_t
from src.i18n_email import email_t
from src.workers.adapters import DeliveryAdapter, DeliveryJob, DeliveryResult
from src.workers.adapters.email import DEFAULT_CABINET_BASE_URL
from src.workers.matching import is_english_tender, match_haystack

logger = logging.getLogger(__name__)

# Ready-to-send statuses (DB_SCHEMA §2.14). 'ai_pending' is NOT taken —
# it waits for the AI worker (Sprint-18). The transit status of the pick-up is 'delivering'.
_READY_STATUSES = ("pending", "ai_done")
_CLAIMED_STATUS = "delivering"

# Atomic pick-up of a batch: SKIP LOCKED excludes the worker race, the status change —
# a repeated pick-up. We select only the channels we can deliver.
# :hold — digest profiles OUTSIDE the window: their rows are not taken (the status is not touched,
# the row waits for its window). An empty list — we hold nobody. The check
# profile_id IS NULL is mandatory: `NULL = ANY(...)` gives NULL, and NOT NULL is also
# NULL, therefore without it the rows WITHOUT a profile would silently drop out of the pick-up altogether.
_CLAIM_SQL = text("""
    UPDATE delivery_outbox
    SET status = :claimed
    WHERE id IN (
        SELECT id FROM delivery_outbox
        WHERE status = ANY(:ready)
          AND channel = ANY(:channels)
          AND (profile_id IS NULL
               OR NOT (profile_id = ANY(CAST(:hold AS uuid[]))))
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT :batch
    )
    RETURNING id
    """)

# Revival of hung 'delivering' (a crash before finalisation) — at the worker's start-up.
_RECOVER_SQL = text("""
    UPDATE delivery_outbox
    SET status = CASE WHEN requires_ai THEN 'ai_done' ELSE 'pending' END
    WHERE status = :claimed
    """)

# Loading of the jobs by id: outbox + the channel target (user_channels) + the tender fields
# (the rich card v6: §2.10 — sums, dates, region, subject type, source).
# DISTINCT ON (o.id) protects against fan-out with several user/channels of the tenant
# (on MVP the link is 1:1): we take the active channel, otherwise the freshest.
_LOAD_JOBS_SQL = text("""
    SELECT DISTINCT ON (o.id)
           o.id                 AS outbox_id,
           o.tender_id          AS tender_id,
           o.channel            AS channel,
           o.requires_ai        AS requires_ai,
           o.profile_id         AS profile_id,
           uc.id                AS user_channel_id,
           uc.external_id       AS target,
           uc.status            AS channel_status,
           -- Язык карточки: язык КАНАЛА (0038), иначе язык кабинета. NULL в
           -- uc.locale = «наследовать users.locale» (см. миграцию 0038), а не
           -- «язык неизвестен» — поэтому COALESCE, а не CASE по каналу.
           COALESCE(uc.locale, u.locale) AS locale,
           t.title              AS title,
           t.source_url         AS source_url,
           t.amount_eur         AS amount_eur,
           t.deadline_at        AS deadline_at,
           t.source_uid         AS source_uid,
           t.source_key         AS source_key,
           t.language           AS language,
           NULLIF(s.display_name, '') AS source_label,
           -- Пометка «тот же тендер на других площадках» (src.dedup.also_on).
           t.canonical_confidence AS canonical_confidence,
           {also_on_n} AS also_on_n,
           t.amount_original    AS amount_original,
           t.currency_original  AS currency_original,
           t.is_amount_estimated AS is_amount_estimated,
           t.published_at       AS published_at,
           t.procedure_type     AS procedure_type,
           t.contract_type      AS contract_type,
           t.regions            AS regions,
           t.classifications    AS classifications,
           p.name               AS profile_name,
           p.debug_match        AS debug_match,
           p.keywords_include   AS profile_keywords_include,
           p.keywords_exclude   AS profile_keywords_exclude,
           p.keyword_dict       AS profile_keyword_dict,
           p.classifications    AS profile_classifications,
           -- Текст матчинга собирает match_haystack (зеркало routing): английский
           -- тендер — только заголовок, поэтому отдаём части раздельно.
           t.title_normalized   AS tender_title,
           t.description_normalized AS tender_description
    FROM delivery_outbox o
    JOIN profiles p       ON p.id = o.profile_id
    JOIN users u          ON u.tenant_id = p.tenant_id
    JOIN user_channels uc ON uc.user_id = u.id AND uc.channel = o.channel
    JOIN tenders_normalized t ON t.id = o.tender_id
    LEFT JOIN sources s   ON s.source_key = t.source_key
    WHERE o.id = ANY(:ids)
      -- Удалённый (users.deleted_at) не получает доставку даже по живой строке
      -- user_channels: старая cookie/канал не должны пережить удаление (10.10).
      AND u.deleted_at IS NULL
    ORDER BY o.id, (uc.status = 'active') DESC, uc.linked_at DESC NULLS LAST
    """.format(also_on_n=also_on_count_sql("t")))

_INSERT_ATTEMPT_SQL = text("""
    INSERT INTO delivery_attempts (outbox_id, attempted_at, response_code, error_text)
    VALUES (:outbox_id, NOW(), :code, :err)
    """)
_SET_SENT_SQL = text(
    "UPDATE delivery_outbox SET status='sent', sent_at=NOW() WHERE id=:id"
)
_SET_FAILED_SQL = text(
    "UPDATE delivery_outbox SET status='failed', failed_at=NOW() WHERE id=:id"
)
# Return to the ready status for a retry (a temporary error): by the requires_ai of the row.
_REVERT_READY_SQL = text("""
    UPDATE delivery_outbox
    SET status = CASE WHEN requires_ai THEN 'ai_done' ELSE 'pending' END
    WHERE id=:id
    """)
_BLOCK_CHANNEL_SQL = text("UPDATE user_channels SET status='blocked' WHERE id=:id")

# --- Anti-spam: the daily limit of TENDERS per profile (env DELIVERY_DAILY_CAP) --- #
# The cap cuts only Telegram/e-mail and is counted in TENDERS: an outbox row is written
# for EACH channel (matching.WRITE_OUTBOX_SQL), therefore with TG+email a tender = two
# rows, not two units of the limit. The selection gives the pairs (profile, tender) for
# the current UTC day (DISTINCT collapses the channels). The timezone of the postgres container is UTC,
# so date_trunc('day', now()) = the start of the UTC day. profile_id = ANY(:pids).
_PROFILE_DAILY_TENDERS_SQL = text("""
    SELECT DISTINCT profile_id, tender_id
    FROM delivery_outbox
    WHERE profile_id = ANY(CAST(:pids AS uuid[]))
      AND status = 'sent'
      AND sent_at >= date_trunc('day', now())
    """)
# Suppression beyond the cap: the terminal status 'suppressed' (a free TEXT,
# no migration needed). The row is not picked up again (_CLAIM_SQL takes only
# pending/ai_done; recover_stale — only delivering).
_SET_SUPPRESSED_SQL = text(
    "UPDATE delivery_outbox SET status='suppressed', failed_at=NOW() WHERE id=:id"
)
# Whether this profile has already been suppressed IN THIS CHANNEL for the current day (except the current
# row) — so as to send the warning summary EXACTLY once a day per
# profile and channel (the e-mail and Telegram have their own message).
_SUPPRESS_EXISTS_SQL = text("""
    SELECT EXISTS(
        SELECT 1 FROM delivery_outbox
        WHERE profile_id = :pid
          AND channel = :channel
          AND status = 'suppressed'
          AND failed_at >= date_trunc('day', now())
          AND id <> :id
    )
    """)


# --- digest mode: the profile’s schedule (profiles.delivery_schedule) -------- #
# The tz contour as in the neighbouring workers (summary/reminders/subscription_reminders):
# the tenant’s zone, default Europe/Belgrade, an unknown zone → UTC (we do not fall over).
DEFAULT_TZ = "Europe/Belgrade"
# The hours of the fixed windows (the tenant’s local time): morning — before the start of the working
# day, evening — after it. Edited here; not moved to env (Compose is RED).
DIGEST_MORNING_HOUR = 9
DIGEST_EVENING_HOUR = 18
# The digest schedules (the values of the cabinet, src.api.forms._SCHEDULES). The rest
# (immediate / NULL / unknown) — immediate delivery, as before.
_DIGEST_SCHEDULES: tuple[str, ...] = (
    "digest_morning",
    "digest_evening",
    "digest_custom",
)

# The profiles with a digest schedule + the tenant’s zone + the time of the last send per
# profile (by it “the digest for today has already gone out” is counted — the digest has no
# table of its own; the state is visible from the outbox itself).
_DIGEST_PROFILES_SQL = text("""
    SELECT p.id                          AS profile_id,
           p.delivery_schedule           AS delivery_schedule,
           p.digest_cron                 AS digest_cron,
           COALESCE(ten.tz, :default_tz) AS tz,
           (SELECT max(o.sent_at)
              FROM delivery_outbox o
             WHERE o.profile_id = p.id
               AND o.status = 'sent')    AS last_sent_at
    FROM profiles p
    JOIN tenants ten ON ten.id = p.tenant_id
    WHERE p.delivery_schedule = ANY(:schedules)
    """)


def _resolve_tz(tz_name: str | None) -> Any:
    """The IANA zone name → :class:`ZoneInfo`; unknown/empty → default/UTC."""
    name = (tz_name or "").strip() or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — a broken zone must not break delivery
        return timezone.utc


def _cron_field(field: str, low: int, high: int) -> set[int] | None:
    """One cron field → a set of values: ``*``, ``5``, ``1,3``, ``1-5``, ``*/2``.

    ``None`` — the field is unclear (or out of range): we do not guess, the caller
    falls back. ``digest_cron`` comes from the cabinet form as free text
    (there is no validation), so the parser must be distrustful.
    """
    values: set[int] = set()
    for part in (field or "").split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, _, step_raw = part.partition("/")
            part = part.strip()
            step_raw = step_raw.strip()
            if not step_raw.isdigit() or int(step_raw) < 1:
                return None
            step = int(step_raw)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_raw, _, end_raw = part.partition("-")
            if not (start_raw.strip().isdigit() and end_raw.strip().isdigit()):
                return None
            start, end = int(start_raw), int(end_raw)
        elif part.isdigit():
            start = end = int(part)
        else:
            return None
        if start > end or start < low or end > high:
            return None
        values.update(range(start, end + 1, step))
    return values or None


def digest_window(
    schedule: str | None, digest_cron: str | None
) -> tuple[int, set[int] | None] | None:
    """The digest window of the profile: ``(hour, ISO weekdays | None)``.

    ``None`` — the schedule is not a digest (``immediate``/``NULL``/unknown) →
    immediate delivery. Weekdays ``None`` — there is no restriction by day.

    ``digest_custom`` is read from ``digest_cron`` (``min hour dom month dow``), but
    NOT the whole cron is supported: the digest window is hourly, so only
    the hour field is taken (for a list of hours — the earliest: one batch a day) and the weekday field;
    minutes/day of month/month are ignored. An unparsable cron → the morning
    window (losing a delivery is worse than sending it in the morning).
    """
    key = (schedule or "").strip()
    if key == "digest_morning":
        return (DIGEST_MORNING_HOUR, None)
    if key == "digest_evening":
        return (DIGEST_EVENING_HOUR, None)
    if key != "digest_custom":
        return None

    parts = (digest_cron or "").split()
    if len(parts) != 5:
        return (DIGEST_MORNING_HOUR, None)
    hours = _cron_field(parts[1], 0, 23)
    if hours is None:
        return (DIGEST_MORNING_HOUR, None)
    dow_raw = _cron_field(parts[4], 0, 7)
    # cron: 0 and 7 — Sunday; datetime.isoweekday(): Mon=1..Sun=7.
    dow = None if dow_raw is None else {7 if v == 0 else v for v in dow_raw}
    if dow is not None and dow >= {1, 2, 3, 4, 5, 6, 7}:
        dow = None  # no restriction by day — we do not carry the full set
    return (min(hours), dow)


def digest_due(
    window: tuple[int, set[int] | None],
    *,
    tz: Any,
    now: datetime,
    last_sent_at: datetime | None,
) -> bool:
    """Whether the digest window has come and whether it has already gone out today (the tenant’s tz).

    “Not earlier than the window hour”, not “exactly at the hour”: the worker may have been down or busy —
    a missed window is caught up the same day, and not lost for a day.
    ``last_sent_at`` — the last send of ANY row of the profile (the digest
    sends the whole batch at once, so “already sent today” = the digest went out).
    """
    hour, dows = window
    local_now = now.astimezone(tz)
    if dows is not None and local_now.isoweekday() not in dows:
        return False
    if local_now.hour < hour:
        return False
    if last_sent_at is None:
        return True
    if (
        last_sent_at.tzinfo is None
    ):  # for the case of a naive time from fakes/the driver
        last_sent_at = last_sent_at.replace(tzinfo=timezone.utc)
    return last_sent_at.astimezone(tz).date() < local_now.date()


def _profile_key(profile_id: Any) -> str | None:
    """The profile key for matching the job against the plans: UUID/str → str, ``None`` → None."""
    return None if profile_id is None else str(profile_id)


def cabinet_base_url() -> str:
    """The base of the cabinet links in the messages (the same env/default as in the adapters)."""
    base = os.getenv("CABINET_BASE_URL", DEFAULT_CABINET_BASE_URL)
    return (base or "").rstrip("/")


class DeliveryWorker:
    """The polling delivery worker from ``delivery_outbox`` to the channels."""

    def __init__(
        self,
        *,
        adapters: dict[str, DeliveryAdapter],
        session_factory: Callable[[], Any] = session_scope,
        enabled_channels: tuple[str, ...] = ("telegram", "email"),
        batch_size: int = 20,
        poll_interval: float = 2.0,
        send_delay: float = 1.0,
        daily_cap: int = DEFAULT_DAILY_CAP,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapters = adapters
        self._session_factory = session_factory
        self._enabled_channels = list(enabled_channels)
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        # Anti-spam: the pause between sends (sec) and the daily card limit per
        # profile (0 — off). See the module docstring / env in main().
        self._send_delay = send_delay
        self._daily_cap = daily_cap
        # “The current time” is injected — the digest windows are tested without sleep.
        self._now = now_factory or (lambda: datetime.now(timezone.utc))
        self._stopped = asyncio.Event()

    # -- public API ----------------------------------------------------- #

    def stop(self) -> None:
        """Asks the worker to finish after the current iteration."""
        self._stopped.set()

    async def run(self) -> None:
        """The main cycle: revival of hung rows → polling until stopped."""
        await self.recover_stale()
        logger.info("delivery: polling started (channels=%s)", self._enabled_channels)
        self._warn_unconfigured_channels()
        while not self._stopped.is_set():
            try:
                processed = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ):  # noqa: BLE001 — a cycle failure does not break the worker
                logger.exception("delivery: polling cycle error")
                processed = 0
            # if the batch is empty — we sleep the interval; if it is full — straight to the next one
            if processed < self._batch_size:
                await self._sleep(self._poll_interval)

    def _warn_unconfigured_channels(self) -> None:
        """One line visible in ``docker logs`` per channel without a transport.

        Such a channel does not lose rows (``send`` → retryable, the row waits in the
        queue), but it does not deliver either — this must be visible at once, not dug out of ``delivery_attempts``.
        """
        for channel in self._enabled_channels:
            adapter = self._adapters.get(channel)
            if adapter is not None and not adapter.is_configured():
                logger.warning(
                    "delivery: канал %s включён, но транспорт не настроен — "
                    "доставка будет откладываться (retryable), пока не задан env",
                    channel,
                )

    async def recover_stale(self) -> int:
        """Returns the hung ``delivering`` rows to the ready status (anti-loss at start-up)."""
        async with self._session_factory() as session:
            result = await session.execute(_RECOVER_SQL, {"claimed": _CLAIMED_STATUS})
            await session.commit()
        count = getattr(result, "rowcount", 0) or 0
        if count:
            logger.info("delivery: revived hung rows: %d", count)
        return count

    async def poll_once(self) -> int:
        """One cycle: schedules → pick-up of the batch → loading of the jobs → dispatch.

        Returns the size of the taken batch. The digest profiles outside the window do not take part
        in the pick-up (their rows wait); inside the window they go out as one message per
        profile+channel; ``immediate`` goes by a card per tender, as before.
        """
        async with self._session_factory() as session:
            due_by_profile = await self._load_digest_plans(session)
            hold = sorted(pid for pid, due in due_by_profile.items() if not due)
            claimed = await self._claim_batch(session, hold)
            await session.commit()
            if not claimed:
                return 0
            jobs = await self._load_jobs(session, claimed)

        loaded_ids = {job.outbox_id for job in jobs}
        # claimed without a loadable target (no active channel binding) — fail.
        for missing in [cid for cid in claimed if cid not in loaded_ids]:
            await self._fail_no_target(missing)

        # We take the sets of TENDERS already sent today, per the profiles of the batch.
        sent_today = await self._load_profile_tenders(jobs)
        due_pids = {pid for pid, due in due_by_profile.items() if due}
        units = self._split_units(jobs, due_pids)
        for index, (is_digest, unit) in enumerate(units):
            # Pacing: the pause BETWEEN sends (not before the first). The unit
            # of sending — a card (immediate) or the digest batch of the profile.
            if self._send_delay > 0 and index > 0:
                await self._sleep(self._send_delay)
            allowed: list[DeliveryJob] = []
            unit_new: dict[str, set[str]] = {}
            for job in unit:
                if self._cap_reached(job, sent_today, unit_new):
                    # The profile’s limit is exhausted — we suppress (we do not send).
                    await self._suppress_capped(job)
                    continue
                pid = _profile_key(job.profile_id)
                if pid is not None:
                    unit_new.setdefault(pid, set()).add(str(job.tender_id))
                allowed.append(job)
            if not allowed:
                continue
            allowed = self._with_counter(allowed, sent_today, unit_new)
            if is_digest:
                result = await self._dispatch_digest(allowed)
            else:
                result = await self._dispatch_one(allowed[0])
            pid = _profile_key(allowed[0].profile_id)
            if result.ok and pid is not None:
                sent_today.setdefault(pid, set()).update(unit_new.get(pid, set()))
        return len(claimed)

    # -- internal -------------------------------------------------------- #

    def _split_units(
        self, jobs: list[DeliveryJob], due_pids: set[str]
    ) -> list[tuple[bool, list[DeliveryJob]]]:
        """The batch of jobs → the sending units ``(digest?, jobs)``.

        The ``immediate`` profiles (and rows without a profile) — one unit per job,
        in the previous order; the jobs of a digest profile within the window — one unit per
        profile+channel (a batch has one channel: it has its own address and render).
        """
        units: list[tuple[bool, list[DeliveryJob]]] = []
        groups: dict[tuple[str, str], list[DeliveryJob]] = {}
        for job in jobs:
            pid = _profile_key(job.profile_id)
            if pid is None or pid not in due_pids:
                units.append((False, [job]))
                continue
            group = groups.setdefault((pid, job.channel), [])
            if not group:
                units.append((True, group))
            group.append(job)
        return units

    def _cap_reached(
        self,
        job: DeliveryJob,
        sent_today: dict[str, set[str]],
        unit_new: dict[str, set[str]],
    ) -> bool:
        """Whether the profile’s daily limit in TENDERS is exhausted.

        A tender that already went out today to another channel (or is counted in this unit)
        does not spend the limit again: the second row of the same pair (profile, tender) is
        the same tender. A new tender is cut when there are already ``daily_cap`` different ones.
        """
        pid = _profile_key(job.profile_id)
        if self._daily_cap <= 0 or pid is None:
            return False
        taken = sent_today.get(pid, set()) | unit_new.get(pid, set())
        if str(job.tender_id) in taken:
            return False
        return len(taken) >= self._daily_cap

    def _with_counter(
        self,
        allowed: list[DeliveryJob],
        sent_today: dict[str, set[str]],
        unit_new: dict[str, set[str]],
    ) -> list[DeliveryJob]:
        """Sets in the jobs “how many tenders of the profile went out today” (for the footer).

        The number — including THIS send; the adapter shows the counter line only
        from the threshold :func:`src.domain.delivery_cap.should_show_counter`.
        """
        pid = _profile_key(allowed[0].profile_id)
        if self._daily_cap <= 0 or pid is None:
            return allowed
        total = len(sent_today.get(pid, set()) | unit_new.get(pid, set()))
        return [
            replace(job, daily_sent=total, daily_cap=self._daily_cap) for job in allowed
        ]

    async def _load_digest_plans(self, session: Any) -> dict[str, bool]:
        """The verdict “the window has come” for the digest profiles: ``{profile_id: due}``.

        Only the profiles with a digest schedule are in the dict — ``immediate``/``NULL``
        do not get here and go the previous way (the immediate card).
        """
        result = await session.execute(
            _DIGEST_PROFILES_SQL,
            {"default_tz": DEFAULT_TZ, "schedules": list(_DIGEST_SCHEDULES)},
        )
        now = self._now()
        due_by_profile: dict[str, bool] = {}
        for row in result:
            window = digest_window(
                row.delivery_schedule, getattr(row, "digest_cron", None)
            )
            if (
                window is None
            ):  # The SQL has already filtered, but the verdict is by the code
                continue
            due_by_profile[str(row.profile_id)] = digest_due(
                window,
                tz=_resolve_tz(getattr(row, "tz", None)),
                now=now,
                last_sent_at=getattr(row, "last_sent_at", None),
            )
        return due_by_profile

    async def _claim_batch(
        self, session: Any, hold: list[str] | None = None
    ) -> list[int]:
        result = await session.execute(
            _CLAIM_SQL,
            {
                "claimed": _CLAIMED_STATUS,
                "ready": list(_READY_STATUSES),
                "channels": self._enabled_channels,
                "batch": self._batch_size,
                "hold": list(hold or []),
            },
        )
        return [row.id for row in result]

    async def _load_jobs(self, session: Any, ids: list[int]) -> list[DeliveryJob]:
        result = await session.execute(_LOAD_JOBS_SQL, {"ids": ids})
        jobs: list[DeliveryJob] = []
        for row in result:
            regions = getattr(row, "regions", None)
            is_estimated = getattr(row, "is_amount_estimated", None)
            classifications = getattr(row, "classifications", None)
            jobs.append(
                DeliveryJob(
                    outbox_id=row.outbox_id,
                    tender_id=str(row.tender_id),
                    channel=row.channel,
                    requires_ai=bool(row.requires_ai),
                    user_channel_id=(
                        str(row.user_channel_id)
                        if row.user_channel_id is not None
                        else None
                    ),
                    target=row.target,
                    channel_status=row.channel_status,
                    title=row.title,
                    source_url=row.source_url,
                    amount_eur=row.amount_eur,
                    deadline_at=row.deadline_at,
                    # --- the rich card v6 ---
                    source_uid=getattr(row, "source_uid", None),
                    source_key=getattr(row, "source_key", None),
                    language=getattr(row, "language", None),
                    source_label=getattr(row, "source_label", None),
                    canonical_confidence=getattr(row, "canonical_confidence", None),
                    also_on_n=int(getattr(row, "also_on_n", 0) or 0),
                    amount_original=getattr(row, "amount_original", None),
                    currency_original=getattr(row, "currency_original", None),
                    is_amount_estimated=(
                        bool(is_estimated) if is_estimated is not None else None
                    ),
                    published_at=getattr(row, "published_at", None),
                    procedure_type=getattr(row, "procedure_type", None),
                    regions=(list(regions) if regions is not None else None),
                    # contract_type — a direct column of tenders_normalized (0011);
                    # the subject type (goods/services/works) from the card enrichment.
                    contract_type=getattr(row, "contract_type", None),
                    # classifications (JSONB §2.10) — a LIST of records
                    # [{scheme,...}] (cpv + plan period, Chat 44/45). The list is
                    # the main format; a dict is accepted for compatibility with
                    # the old/single records. Anything else (None/scalar) → None.
                    classifications=(
                        classifications
                        if isinstance(classifications, (dict, list))
                        else None
                    ),
                    # profile_id — for the daily limit per profile (getattr:
                    # the old test fakes without the field do not break).
                    profile_id=getattr(row, "profile_id", None),
                    profile_name=getattr(row, "profile_name", None),
                    debug_match=bool(getattr(row, "debug_match", False)),
                    profile_keywords_include=getattr(
                        row, "profile_keywords_include", None
                    ),
                    profile_keywords_exclude=getattr(
                        row, "profile_keywords_exclude", None
                    ),
                    profile_classifications=getattr(
                        row, "profile_classifications", None
                    ),
                    # getattr: the old test fakes without the field do not break (False).
                    profile_keyword_dict=bool(
                        getattr(row, "profile_keyword_dict", False)
                    ),
                    tender_haystack=match_haystack(
                        getattr(row, "tender_title", None),
                        getattr(row, "tender_description", None),
                        english=is_english_tender(
                            getattr(row, "language", None),
                            getattr(row, "source_key", None),
                        ),
                    ),
                    # The language of the card chrome = the language of the CHANNEL, otherwise the language of the cabinet
                    # (COALESCE in _LOAD_JOBS_SQL; NULL → ru in the adapter).
                    # getattr: the old test fakes without the field do not break.
                    locale=getattr(row, "locale", None),
                )
            )
        return jobs

    async def _dispatch_one(self, job: DeliveryJob) -> DeliveryResult:
        """Resolves the adapter, renders and sends one job; finalises the status.

        Returns the :class:`DeliveryResult` of the sending — for the increment of the daily
        counter of the profile in :meth:`poll_once`.
        """
        adapter = self._adapters.get(job.channel)
        if adapter is None:  # should not happen — poll filters the channels
            result = DeliveryResult(ok=False, error_text=f"нет адаптера: {job.channel}")
            await self._finalize(job, result)
            return result

        if not job.target or (job.channel_status not in (None, "active")):
            # the channel is inactive / no target — we do not send, mark fail (the channel is already blocked)
            result = DeliveryResult(ok=False, error_text="канал неактивен или нет цели")
            await self._finalize(job, result, block_channel=False)
            return result

        try:
            message = adapter.format(job)
            result = await adapter.send(job, message)
        except NotImplementedError as exc:
            result = DeliveryResult(ok=False, error_text=f"канал не реализован: {exc}")
        except Exception as exc:  # noqa: BLE001 — one error does not break the batch
            logger.exception("delivery: send failure outbox=%s", job.outbox_id)
            result = DeliveryResult(ok=False, error_text=str(exc), retryable=True)

        await self._finalize(job, result)
        return result

    async def _dispatch_digest(self, jobs: list[DeliveryJob]) -> DeliveryResult:
        """Sends ONE digest message for the batch of jobs of one profile+channel.

        The batch lives as one whole: the address and the render are taken from the first job
        (``target``/``channel_status`` are common for the batch — one channel of one
        tenant), and the outcome of the sending is finalised for EACH row of the batch. Hence the
        idempotency: success → all rows ``sent`` (a second digest will not pick them up);
        a temporary error → all return to the queue and will go out as one
        batch on the next cycle — there is no half-sent state.
        """
        lead = jobs[0]
        adapter = self._adapters.get(lead.channel)
        if adapter is None:  # should not happen — poll filters the channels
            result = DeliveryResult(
                ok=False, error_text=f"нет адаптера: {lead.channel}"
            )
        elif not lead.target or (lead.channel_status not in (None, "active")):
            result = DeliveryResult(ok=False, error_text="канал неактивен или нет цели")
            for job in jobs:
                await self._finalize(job, result, block_channel=False)
            return result
        else:
            try:
                message = adapter.format_digest(jobs)
                result = await adapter.send(lead, message)
            except NotImplementedError as exc:
                result = DeliveryResult(
                    ok=False, error_text=f"канал не реализован: {exc}"
                )
            except (
                Exception
            ) as exc:  # noqa: BLE001 — a failure does not break the other batches
                logger.exception(
                    "delivery: digest send failure profile=%s rows=%d",
                    lead.profile_id,
                    len(jobs),
                )
                result = DeliveryResult(ok=False, error_text=str(exc), retryable=True)

        for job in jobs:
            await self._finalize(job, result)
        if result.ok:
            logger.info(
                "delivery: digest sent profile=%s channel=%s tenders=%d",
                lead.profile_id,
                lead.channel,
                len(jobs),
            )
        return result

    async def _finalize(
        self,
        job: DeliveryJob,
        result: DeliveryResult,
        *,
        block_channel: bool = True,
    ) -> None:
        """Writes the attempt and moves the outbox status according to the result."""
        async with self._session_factory() as session:
            await session.execute(
                _INSERT_ATTEMPT_SQL,
                {
                    "outbox_id": job.outbox_id,
                    "code": result.response_code,
                    "err": result.error_text,
                },
            )
            if result.ok:
                await session.execute(_SET_SENT_SQL, {"id": job.outbox_id})
            elif result.blocked:
                await session.execute(_SET_FAILED_SQL, {"id": job.outbox_id})
                if block_channel and job.user_channel_id is not None:
                    await session.execute(
                        _BLOCK_CHANNEL_SQL, {"id": job.user_channel_id}
                    )
            elif result.retryable:
                await session.execute(_REVERT_READY_SQL, {"id": job.outbox_id})
            else:
                await session.execute(_SET_FAILED_SQL, {"id": job.outbox_id})
            await session.commit()

        if result.ok:
            logger.info("delivery: sent outbox=%s", job.outbox_id)
        else:
            logger.warning(
                "delivery: not sent outbox=%s code=%s blocked=%s retry=%s",
                job.outbox_id,
                result.response_code,
                result.blocked,
                result.retryable,
            )

    async def _fail_no_target(self, outbox_id: int) -> None:
        """Marks a claimed row without a loadable target as failed."""
        async with self._session_factory() as session:
            await session.execute(
                _INSERT_ATTEMPT_SQL,
                {
                    "outbox_id": outbox_id,
                    "code": None,
                    "err": "нет активной привязки канала (user_channels)",
                },
            )
            await session.execute(_SET_FAILED_SQL, {"id": outbox_id})
            await session.commit()
        logger.warning(
            "delivery: outbox=%s without a delivery target → failed", outbox_id
        )

    # -- anti-spam: the daily limit per profile -------------------------------- #

    async def _load_profile_tenders(
        self, jobs: list[DeliveryJob]
    ) -> dict[str, set[str]]:
        """The tenders sent for the current UTC day, per the profiles of the batch.

        Returns ``{profile_id: {tender_id, ...}}``. An empty dict (without a trip to the
        DB) if the limit is off (``daily_cap <= 0``) or there are no jobs with a
        ``profile_id`` in the batch (only the rows with a set profile fall under the limit).
        """
        if self._daily_cap <= 0:
            return {}
        pids = sorted(
            {
                key
                for key in (_profile_key(job.profile_id) for job in jobs)
                if key is not None
            }
        )
        if not pids:
            return {}
        async with self._session_factory() as session:
            result = await session.execute(_PROFILE_DAILY_TENDERS_SQL, {"pids": pids})
            sent: dict[str, set[str]] = {}
            for row in result:
                sent.setdefault(str(row.profile_id), set()).add(str(row.tender_id))
        return sent

    async def _suppress_capped(self, job: DeliveryJob) -> None:
        """Suppresses a row beyond the daily limit: status ``suppressed`` + attempt.

        ``suppressed`` is terminal (the row will not be re-sent). On the FIRST
        suppression of the profile for the day it sends a one-off warning summary to the
        channel (see :meth:`_send_cap_notice`).
        """
        async with self._session_factory() as session:
            await session.execute(_SET_SUPPRESSED_SQL, {"id": job.outbox_id})
            await session.execute(
                _INSERT_ATTEMPT_SQL,
                {"outbox_id": job.outbox_id, "code": None, "err": "daily_cap"},
            )
            exists = await session.execute(
                _SUPPRESS_EXISTS_SQL,
                {"pid": job.profile_id, "id": job.outbox_id, "channel": job.channel},
            )
            already = bool(exists.scalar())
            await session.commit()
        logger.info(
            "delivery: profile=%s limit=%d reached → outbox=%s suppressed",
            job.profile_id,
            self._daily_cap,
            job.outbox_id,
        )
        if not already:
            await self._send_cap_notice(job)

    async def _send_cap_notice(self, job: DeliveryJob) -> None:
        """A one-off (per day, per profile and channel) summary of the reached limit.

        The text is localised (sr/ru/en; the language of the channel ``job.locale``) and leads to the site —
        where all matches are — and to the template settings. Best-effort: an error or
        ``NotImplementedError`` of ``send_text`` is logged as a warning and does NOT
        roll back the suppression of the row.
        """
        adapter = self._adapters.get(job.channel)
        if adapter is None or not job.target:
            return
        t = email_t if job.channel == "email" else common_t
        base = cabinet_base_url()
        profile_name = job.profile_name or t(job.locale, "no_profile_name")
        if job.channel == "telegram":
            # parse_mode=HTML: “<” / “&” in the template name otherwise break the markup →
            # a Bad Request “can't parse entities” and the summary is silently lost.
            # E-mail escapes by itself (html_body); its text is plain — we do not touch it.
            profile_name = html.escape(profile_name)
        notice = t(job.locale, "cap_notice").format(
            cap=self._daily_cap,
            name=profile_name,
            feed_url=f"{base}/app/feed?profile_id={job.profile_id}",
            settings_url=f"{base}/profiles/{job.profile_id}/edit",
        )
        try:
            await adapter.send_text(
                job.target,
                notice,
                locale=getattr(job, "locale", None),
                subject=t(job.locale, "cap_notice_subject"),
            )
        except NotImplementedError:
            logger.warning(
                "delivery: channel=%s does not support send_text — the limit summary was not sent",
                job.channel,
            )
        except (
            Exception
        ):  # noqa: BLE001 — a failure of the summary does not roll back the suppress
            logger.warning(
                "delivery: limit summary send failure profile=%s", job.profile_id
            )

    async def _sleep(self, delay: float) -> None:
        """An interruptible sleep: reacts to stop() while waiting."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the delivery worker (phase 1: the container via Compose)."""
    from src.bots.telegram import build_bot
    from src.log_secrets import install_secret_log_filter
    from src.workers.adapters import default_adapters

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()
    # Anti-spam: the pause between sends and the daily card limit per profile
    # are read from env (defaults — as in the constructor: 1.0 sec and 30).
    send_delay = float(os.getenv("DELIVERY_SEND_DELAY_SEC", "1.0"))
    daily_cap = daily_cap_from_env()
    bot = build_bot()
    worker = DeliveryWorker(
        adapters=default_adapters(bot),
        send_delay=send_delay,
        daily_cap=daily_cap,
    )
    await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
