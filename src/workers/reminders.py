"""Reminder Worker — reminders about an approaching deadline (the port of v6 reminders_loop).

A separate polling worker: it sends the subscriber a short reminder about the approaching
deadline for bids on the tenders that they marked in the funnel
(``tender_user_status``, migration 0008) as ``active`` (✅ Participating) or
``watching`` (👀 Researching). The thresholds — 5/3/1/0 days before ``deadline_at``. Each threshold
for each (tenant, tender) is sent **exactly once** — idempotency through
the log of the sent reminders ``reminder_log`` (migration 0009).

The reminder goes through THE SAME :class:`~src.workers.adapters.telegram.TelegramAdapter`
(the bot in polling), but it is a SEPARATE short message, NOT the rich card v6 —
through :meth:`DeliveryAdapter.send_text` (plain text without buttons).

Reliability — claim-then-send (as the outbox pattern of delivery):

1. **Idempotency / anti-race.** Before sending a “claim” is made: an insert
   into the log ``INSERT ... ON CONFLICT (tenant_id, tender_id, threshold_days) DO
   NOTHING RETURNING id`` in a separate short transaction. The message is sent
   ONLY if ``RETURNING`` returned a row. The UNIQUE index of the log guarantees:
   on a double run the second instance will not get ``RETURNING`` and will not send.
2. **A channel failure does NOT roll the log back.** The order: first the claim (a short
   transaction, commit), then the send. If the send failed — we do NOT roll the log back,
   we log the error. The assumption (the port of v6): better to skip one reminder on
   a channel failure than to loop the spam (a rollback of the log → a repeated claim → a repeated
   sending on each cycle).
3. **We do not remind about the expired.** The candidates are filtered by ``deadline_at >= now()``
   on the SQL side; additionally “days to the deadline” outside the thresholds are cut in
   Python.

Like delivery/routing, the worker goes to the DB over a **privileged** connection —
WITHOUT ``SET app.current_tenant_id``: the cross-tenant selection (a server operation),
RLS on ``tender_user_status``/``reminder_log`` is enabled, but the owner of the tables bypasses it
(ARCHITECTURE §8, DB_SCHEMA §4).

The dependencies are injected (the adapter registry, the session factory, the interval, the thresholds,
the provider of the “current time”) — for unit tests with a mocked DB and a fake
adapter, without a real PostgreSQL/network (ADR-013).

Not done (separate tasks): the daily summary (NEXT №2), digest/quiet-hours,
dedup. Telegram only (``enabled_channels``).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import text

from src.db.session import session_scope
from src.domain.access import access_active_sql
from src.i18n_common import common_t, days_phrase
from src.workers.adapters.base import DeliveryAdapter, format_deadline

logger = logging.getLogger(__name__)

# The default time zone (if the tenant has no tz set). Serbia — the MVP.
DEFAULT_TZ = "Europe/Belgrade"

# The funnel statuses for which reminders are sent (the v6 port): participating / researching.
_WATCH_STATUSES = ("active", "watching")

# The candidates for a reminder: the user funnel active/watching × a tender with a
# future deadline × the tenant’s active channel. DISTINCT ON (tenant, tender) —
# one addressee per pair (on MVP the link user↔channel is 1:1; with several
# active channels the freshest by linked_at is taken). The worker goes
# privileged (without SET tenant) — a server cross-tenant selection.
# The access axis — the same predicate as matching (access_active_sql): after the
# expiry of access_until the reminders stop; NULL = unlimited.
_CANDIDATES_SQL = text(f"""
    SELECT DISTINCT ON (tus.tenant_id, tus.tender_id)
           tus.tenant_id                 AS tenant_id,
           tus.tender_id                 AS tender_id,
           tus.status                    AS user_status,
           t.title                       AS title,
           t.source_url                  AS source_url,
           t.deadline_at                 AS deadline_at,
           t.source_key                  AS source_key,
           NULLIF(s.display_name, '')    AS source_label,
           COALESCE(ten.tz, :default_tz) AS tz,
           uc.id                         AS user_channel_id,
           uc.external_id                AS target,
           -- Язык напоминания: язык КАНАЛА (0038), иначе язык кабинета. NULL в
           -- uc.locale = «наследовать users.locale», а не «язык неизвестен» —
           -- поэтому COALESCE (зеркало delivery._LOAD_JOBS_SQL).
           COALESCE(uc.locale, u.locale) AS locale
    FROM tender_user_status tus
    JOIN tenders_normalized t ON t.id = tus.tender_id
    LEFT JOIN sources s       ON s.source_key = t.source_key
    JOIN tenants ten          ON ten.id = tus.tenant_id
    JOIN users u              ON u.tenant_id = tus.tenant_id
    JOIN user_channels uc     ON uc.user_id = u.id
                             AND uc.channel = :channel
                             AND uc.status = 'active'
    WHERE tus.status = ANY(:statuses)
      AND t.deadline_at IS NOT NULL
      AND t.deadline_at >= now()
      AND {access_active_sql("ten")}
    ORDER BY tus.tenant_id, tus.tender_id, uc.linked_at DESC NULLS LAST
    """)

# Claim-then-send: an atomic insert into the log. RETURNING will return a row only
# if this (tenant, tender, threshold) has not existed yet — then (and only then)
# we send. ON CONFLICT stops the repeat/race (the UNIQUE of migration 0009).
_CLAIM_SQL = text("""
    INSERT INTO reminder_log (tenant_id, tender_id, threshold_days)
    VALUES (:tenant_id, :tender_id, :threshold)
    ON CONFLICT (tenant_id, tender_id, threshold_days) DO NOTHING
    RETURNING id
    """)


def _resolve_tz(tz_name: str | None) -> Any:
    """The IANA zone name → :class:`ZoneInfo`; unknown/empty → UTC (we do not fall over)."""
    name = (tz_name or "").strip() or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — an unknown zone must not break the worker
        return timezone.utc


def days_until_deadline(
    deadline_at: datetime, *, now: datetime, tz_name: str | None = None
) -> int:
    """The whole number of CALENDAR days to the deadline in the zone ``tz_name`` (floor by dates).

    The difference of calendar dates is counted (not hours): a deadline today (the same date)
    → ``0``, tomorrow → ``1``, in 5 days by dates → ``5``. The past → negative
    (it does not reach the worker — it is cut by the SQL ``deadline_at >= now()``). Both
    arguments must be tz-aware.
    """
    tz = _resolve_tz(tz_name)
    deadline_date = deadline_at.astimezone(tz).date()
    now_date = now.astimezone(tz).date()
    return (deadline_date - now_date).days


def build_reminder_text(
    threshold_days: int,
    *,
    title: str | None,
    deadline_at: datetime | None,
    source_url: str | None,
    locale: str | None = None,
    source_label: str | None = None,
    source_key: str | None = None,
) -> str:
    """Assembles the short HTML message of the reminder (a pure function, no network/DB).

    The format (``parse_mode=HTML``, all dynamic text is escaped)::

        ⏰ Reminder: to the deadline <N> d.   (or “the deadline is TODAY” at N=0)
        <b>{title}</b>
        🗓 Submissions due {deadline}
        🏛 Source: {source_label}
        🔗 {source_url}

    The deadline/source/link rows are added only if a value is present.
    ``source_label`` — ``sources.display_name`` (as in the delivery alerts); no
    name → no row (we do not show the technical key to the client).

    ``locale`` — the language of the reminder CHROME (``COALESCE(uc.locale, u.locale)`` from
    :data:`_CANDIDATES_SQL`); ``None``/unknown code → ru (an explicit fallback
    ``common_t``). The tender’s title and the link are data, NOT translated. The unit
    “d.” is declined by language (:func:`~src.i18n_email.days_phrase`): in Serbian
    “1 dan” and “5 dana” are different forms; in Russian the abbreviation is non-declinable.
    """
    title_safe = html.escape(title or common_t(locale, "no_title"))
    if threshold_days <= 0:
        head = f"⏰ {common_t(locale, 'rem_today')}"
    else:
        head = "⏰ " + common_t(locale, "rem_in_days").format(
            days=days_phrase(threshold_days, locale)
        )

    lines = [head, f"<b>{title_safe}</b>"]
    deadline = format_deadline(
        deadline_at,
        source_key=source_key,
        note=common_t(locale, "deadline_hour_unknown"),
    )
    if deadline:
        lines.append(f"🗓 {common_t(locale, 'lbl_deadline')} {html.escape(deadline)}")
    if source_label:
        lines.append(
            f"🏛 {common_t(locale, 'lbl_platform')}: {html.escape(source_label)}"
        )
    if source_url:
        lines.append(f"🔗 {html.escape(source_url)}")
    return "\n".join(lines)


class ReminderWorker:
    """The polling worker of the deadline reminders (thresholds 5/3/1/0, idempotent)."""

    def __init__(
        self,
        *,
        adapters: dict[str, DeliveryAdapter],
        session_factory: Callable[[], Any] = session_scope,
        enabled_channels: tuple[str, ...] = ("telegram",),
        poll_interval: float = 3600.0,
        thresholds: tuple[int, ...] = (5, 3, 1, 0),
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapters = adapters
        self._session_factory = session_factory
        self._enabled_channels = list(enabled_channels)
        self._poll_interval = poll_interval
        self._thresholds = tuple(thresholds)
        self._thresholds_set = set(self._thresholds)
        self._now = now_factory or (lambda: datetime.now(timezone.utc))
        self._stopped = asyncio.Event()

    # -- public API ----------------------------------------------------- #

    def stop(self) -> None:
        """Asks the worker to finish after the current iteration."""
        self._stopped.set()

    async def run(self) -> None:
        """The main loop: poll_once every ``poll_interval`` until stopped."""
        logger.info(
            "reminders: polling started (channels=%s, thresholds=%s, interval=%.0fs)",
            self._enabled_channels,
            self._thresholds,
            self._poll_interval,
        )
        while not self._stopped.is_set():
            try:
                sent = await self.poll_once()
                if sent:
                    logger.info("reminders: reminders sent per cycle: %d", sent)
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ):  # noqa: BLE001 — a cycle failure does not break the worker
                logger.exception("reminders: polling cycle error")
            await self._sleep(self._poll_interval)

    async def poll_once(self) -> int:
        """One pass: candidates → threshold → claim → send. Returns the number of sends."""
        sent = 0
        for channel in self._enabled_channels:
            adapter = self._adapters.get(channel)
            if (
                adapter is None
            ):  # the channel is in enabled, but there is no adapter — skipping
                continue
            for row in await self._load_candidates(channel):
                threshold = self._threshold_for(row.deadline_at, row.tz)
                if threshold is None:
                    continue
                # claim-then-send: we log BEFORE the sending (a separate transaction);
                # we send only if RETURNING returned a row (this threshold did not exist yet).
                if not await self._claim(row.tenant_id, row.tender_id, threshold):
                    continue
                if await self._send_reminder(adapter, row, threshold):
                    sent += 1
        return sent

    # -- internal -------------------------------------------------------- #

    def _threshold_for(self, deadline_at: datetime, tz_name: str | None) -> int | None:
        """The threshold (5/3/1/0) if the days to the deadline match one of them; otherwise None."""
        days = days_until_deadline(deadline_at, now=self._now(), tz_name=tz_name)
        return days if days in self._thresholds_set else None

    async def _load_candidates(self, channel: str) -> list[Any]:
        """The candidates on the channel: the funnel active/watching × a future deadline × an active channel."""
        async with self._session_factory() as session:
            result = await session.execute(
                _CANDIDATES_SQL,
                {
                    "channel": channel,
                    "statuses": list(_WATCH_STATUSES),
                    "default_tz": DEFAULT_TZ,
                },
            )
            return list(result)

    async def _claim(self, tenant_id: Any, tender_id: Any, threshold: int) -> bool:
        """The atomic claim in reminder_log (a short transaction). True — ours, we send."""
        async with self._session_factory() as session:
            result = await session.execute(
                _CLAIM_SQL,
                {
                    "tenant_id": tenant_id,
                    "tender_id": tender_id,
                    "threshold": threshold,
                },
            )
            await session.commit()
        return bool(list(result))

    async def _send_reminder(
        self, adapter: DeliveryAdapter, row: Any, threshold: int
    ) -> bool:
        """Sends the reminder through ``send_text``. The log is NOT rolled back on failure.

        Assumption (the v6 port): the log is already fixed in :meth:`_claim`. If the channel
        failed — we do NOT roll back (otherwise the next cycle would again make the claim and again
        try to send = a spam risk). Better to skip one reminder.
        """
        message = build_reminder_text(
            threshold,
            title=row.title,
            deadline_at=row.deadline_at,
            source_url=row.source_url,
            # getattr: the old test fakes without the column do not break.
            locale=getattr(row, "locale", None),
            source_label=getattr(row, "source_label", None),
            source_key=getattr(row, "source_key", None),
        )
        try:
            result = await adapter.send_text(
                row.target, message, locale=getattr(row, "locale", None)
            )
        # a channel failure does not break the worker; the log is NOT touched (see the docstring)
        except Exception:  # noqa: BLE001
            logger.exception(
                "reminders: send failure tenant=%s tender=%s threshold=%s",
                row.tenant_id,
                row.tender_id,
                threshold,
            )
            return False

        if not result.ok:
            logger.warning(
                "reminders: not sent tenant=%s tender=%s threshold=%s code=%s",
                row.tenant_id,
                row.tender_id,
                threshold,
                result.response_code,
            )
            return False

        logger.info(
            "reminders: sent tenant=%s tender=%s threshold=%s",
            row.tenant_id,
            row.tender_id,
            threshold,
        )
        return True

    async def _sleep(self, delay: float) -> None:
        """An interruptible sleep: reacts to stop() while waiting."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the reminder worker (phase 1: the container via Compose)."""
    from src.bots.telegram import build_bot
    from src.log_secrets import install_secret_log_filter
    from src.workers.adapters import default_adapters

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()
    bot = build_bot()
    worker = ReminderWorker(adapters=default_adapters(bot))
    await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
