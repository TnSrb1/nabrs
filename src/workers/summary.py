"""Summary Worker — the daily summary of the subscriber's funnel (the port of v6 daily_summary).

A separate polling worker: once per calendar day of the tenant it sends the subscriber a
short summary of their funnel ``tender_user_status`` (migration 0008) — the status counters
(✅ Participating / 👀 Researching / 📨 Submitted / 🏆 Won) plus the
top-5 nearest deadlines among the tenders that they marked as ``active`` or
``watching``.

The summary goes through THE SAME :class:`~src.workers.adapters.telegram.TelegramAdapter` (the bot in
polling), but it is a short HTML message, NOT the rich card v6 — through
:meth:`DeliveryAdapter.send_text` (plain/HTML without buttons).

The sending window. The summary goes out **once per calendar day of the tenant** in its
zone ``COALESCE(tenants.tz, 'Europe/Belgrade')`` and not earlier than
:data:`SUMMARY_HOUR` (by default 8 — the morning by the tenant's locale). A candidate for
the sending: the current local time of the tenant ``>= SUMMARY_HOUR`` AND there is no record in ``summary_log`` for today's local date yet.

Reliability — claim-then-send (as the outbox pattern of delivery / reminders):

1. **Idempotency / anti-race.** Before sending a “claim” is made: an insert
   into the log ``INSERT ... ON CONFLICT (tenant_id, summary_date) DO NOTHING
   RETURNING id`` in a separate short transaction. The message is sent ONLY if
   ``RETURNING`` returned a row. The UNIQUE index of the log (migration 0010) guarantees
   “one summary per day per tenant”: on a double run the second instance will not
   get ``RETURNING`` and will not send.
2. **A channel failure does NOT roll the log back.** The order: first the claim (a short
   transaction, commit), then the send. If the send failed — we do NOT roll the log back,
   we log the error. The assumption (the port of v6): better to skip one summary on
   a channel failure than to loop the spam.
3. **We do NOT send an empty summary.** If all the funnel counters are 0 AND there are no
   nearest deadlines — the claim is not made and the message is not sent (so as not to spam with an empty one).

Like delivery/reminders, the worker goes to the DB over a **privileged** connection —
WITHOUT ``SET app.current_tenant_id``: the aggregates are counted cross-tenant (a server
operation), RLS on ``tender_user_status``/``summary_log`` is enabled, but the owner of the
tables bypasses it (ARCHITECTURE §8, DB_SCHEMA §4).

The dependencies are injected (the adapter registry, the session factory, the interval,
the provider of the “current time”) — for unit tests with a mocked DB and a fake
adapter, without a real PostgreSQL/network (ADR-013).

Not done (separate tasks): the deadline reminders (a separate worker
``reminders``), digest/quiet-hours, dedup. Telegram only (``enabled_channels``).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from datetime import date, datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import text

from src.db.session import session_scope
from src.domain.access import access_active_sql
from src.i18n_common import common_t
from src.workers.adapters.base import DeliveryAdapter, format_deadline

logger = logging.getLogger(__name__)

# The default time zone (if the tenant has no tz set). Serbia — the MVP.
DEFAULT_TZ = "Europe/Belgrade"

# The local hour of the tenant from which the summary may be sent (morning by tz).
SUMMARY_HOUR = 8

# The funnel statuses by which we take the nearest deadlines: participating / researching.
_WATCH_STATUSES = ("active", "watching")

# The candidates for the summary: the tenants with an active channel of the needed type. DISTINCT ON
# (tenant) — one addressee per tenant (on MVP the link user↔channel is 1:1; with
# several active channels the freshest by linked_at is taken). The worker goes
# privileged (without SET tenant) — a server cross-tenant selection. The access axis —
# the same predicate as matching (access_active_sql): an expired
# tenant does not get the summary; NULL = unlimited.
_TENANTS_SQL = text(f"""
    SELECT DISTINCT ON (ten.id)
           ten.id                        AS tenant_id,
           COALESCE(ten.tz, :default_tz) AS tz,
           uc.external_id                AS target,
           -- Язык сводки: язык КАНАЛА (0038), иначе язык кабинета. NULL в
           -- uc.locale = «наследовать users.locale», а не «язык неизвестен» —
           -- поэтому COALESCE (зеркало delivery._LOAD_JOBS_SQL).
           COALESCE(uc.locale, u.locale) AS locale
    FROM tenants ten
    JOIN users u          ON u.tenant_id = ten.id
    JOIN user_channels uc ON uc.user_id = u.id
                         AND uc.channel = :channel
                         AND uc.status = 'active'
    WHERE {access_active_sql("ten")}
    ORDER BY ten.id, uc.linked_at DESC NULLS LAST
    """)

# The funnel counters of the tenant (privileged, without SET tenant). count(*) FILTER
# returns 0 (not NULL) when there are no rows — a separate branch is not needed.
_COUNTS_SQL = text("""
    SELECT
        count(*) FILTER (WHERE status = 'active')    AS active,
        count(*) FILTER (WHERE status = 'watching')  AS watching,
        count(*) FILTER (WHERE status = 'submitted') AS submitted,
        count(*) FILTER (WHERE status = 'won')       AS won
    FROM tender_user_status
    WHERE tenant_id = :tenant_id
    """)

# The top-5 nearest deadlines of the tenant’s funnel (active/watching, a future deadline).
_DEADLINES_SQL = text("""
    SELECT t.title        AS title,
           t.deadline_at  AS deadline_at,
           t.source_url   AS source_url,
           t.source_key   AS source_key
    FROM tender_user_status tus
    JOIN tenders_normalized t ON t.id = tus.tender_id
    WHERE tus.tenant_id = :tenant_id
      AND tus.status = ANY(:statuses)
      AND t.deadline_at IS NOT NULL
      AND t.deadline_at >= now()
    ORDER BY t.deadline_at ASC
    LIMIT 5
    """)

# Claim-then-send: an atomic insert into the log. RETURNING will return a row only
# if for this (tenant, summary_date) there has been no summary yet — then (and only then)
# we send. ON CONFLICT stops the repeat/race (the UNIQUE of migration 0010).
_CLAIM_SQL = text("""
    INSERT INTO summary_log (tenant_id, summary_date)
    VALUES (:tenant_id, :summary_date)
    ON CONFLICT (tenant_id, summary_date) DO NOTHING
    RETURNING id
    """)


def _resolve_tz(tz_name: str | None) -> Any:
    """The IANA zone name → :class:`ZoneInfo`; unknown/empty → UTC (we do not fall over)."""
    name = (tz_name or "").strip() or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — an unknown zone must not break the worker
        return timezone.utc


def build_summary_text(
    *,
    active: int,
    watching: int,
    submitted: int,
    won: int,
    deadlines: list[Any],
    locale: str | None = None,
) -> str:
    """Assembles the short HTML message of the summary (a pure function, no network/DB).

    The format (``parse_mode=HTML``, all dynamic text is escaped)::

        📊 Daily summary
        ✅ Participating: N   👀 Researching: N   📨 Submitted: N   🏆 Won: N

        ⏳ Nearest deadlines:
        • <b>{title}</b> — 🗓 {deadline}      (up to 5 rows)

    If there are no nearest deadlines — instead of the list the row
    “no upcoming deadlines”. The counters are integers, escaping is not needed;
    the ``title`` of each deadline is escaped. ``deadlines`` — a sequence of
    rows with the attributes ``title`` and ``deadline_at`` (as from ``_DEADLINES_SQL``).

    ``locale`` — the language of the CHROME of the summary (``COALESCE(uc.locale, u.locale)`` from
    :data:`_TENANTS_SQL`); ``None``/unknown code → ru (an explicit fallback
    ``common_t``, not the module DEFAULT of the neighbouring catalog). The tender titles are
    data; they are NOT translated.
    """
    counters = "   ".join(
        (
            f"✅ {common_t(locale, 'sum_active')}: {active}",
            f"👀 {common_t(locale, 'sum_watching')}: {watching}",
            f"📨 {common_t(locale, 'sum_submitted')}: {submitted}",
            f"🏆 {common_t(locale, 'sum_won')}: {won}",
        )
    )
    lines = [
        f"📊 {common_t(locale, 'sum_title')}",
        counters,
        "",
        f"⏳ {common_t(locale, 'sum_deadlines')}:",
    ]
    if deadlines:
        no_title = common_t(locale, "no_title")
        for row in deadlines:
            title_safe = html.escape(getattr(row, "title", None) or no_title)
            deadline = format_deadline(
                getattr(row, "deadline_at", None),
                source_key=getattr(row, "source_key", None),
                note=common_t(locale, "deadline_hour_unknown"),
            )
            deadline_part = html.escape(deadline) if deadline else "—"
            lines.append(f"• <b>{title_safe}</b> — 🗓 {deadline_part}")
    else:
        lines.append(common_t(locale, "sum_no_deadlines"))
    return "\n".join(lines)


class SummaryWorker:
    """The polling worker of the daily summary (one summary a day per tenant, idempotent)."""

    def __init__(
        self,
        *,
        adapters: dict[str, DeliveryAdapter],
        session_factory: Callable[[], Any] = session_scope,
        enabled_channels: tuple[str, ...] = ("telegram",),
        poll_interval: float = 3600.0,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapters = adapters
        self._session_factory = session_factory
        self._enabled_channels = list(enabled_channels)
        self._poll_interval = poll_interval
        self._now = now_factory or (lambda: datetime.now(timezone.utc))
        self._stopped = asyncio.Event()

    # -- public API ----------------------------------------------------- #

    def stop(self) -> None:
        """Asks the worker to finish after the current iteration."""
        self._stopped.set()

    async def run(self) -> None:
        """The main loop: poll_once every ``poll_interval`` until stopped."""
        logger.info(
            "summary: polling started (channels=%s, hour=%d, interval=%.0fs)",
            self._enabled_channels,
            SUMMARY_HOUR,
            self._poll_interval,
        )
        while not self._stopped.is_set():
            try:
                sent = await self.poll_once()
                if sent:
                    logger.info("summary: summaries sent per cycle: %d", sent)
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ):  # noqa: BLE001 — a cycle failure does not break the worker
                logger.exception("summary: polling cycle error")
            await self._sleep(self._poll_interval)

    async def poll_once(self) -> int:
        """One pass: tenants → window → aggregates → claim → send. Returns the number of sends."""
        sent = 0
        for channel in self._enabled_channels:
            adapter = self._adapters.get(channel)
            if (
                adapter is None
            ):  # the channel is in enabled, but there is no adapter — skipping
                continue
            for row in await self._load_tenants(channel):
                local_now = self._now().astimezone(_resolve_tz(row.tz))
                # window: before SUMMARY_HOUR by the tenant’s locale it is still early
                if local_now.hour < SUMMARY_HOUR:
                    continue
                summary_date = local_now.date()
                active, watching, submitted, won = await self._load_counts(
                    row.tenant_id
                )
                deadlines = await self._load_deadlines(row.tenant_id)
                # an empty summary is not sent (we do not make the claim) — so as not to spam
                if (
                    active == 0
                    and watching == 0
                    and submitted == 0
                    and won == 0
                    and not deadlines
                ):
                    continue
                # claim-then-send: we log BEFORE the sending (a separate transaction);
                # we send only if RETURNING returned a row (there was no summary yet).
                if not await self._claim(row.tenant_id, summary_date):
                    continue
                if await self._send_summary(
                    adapter, row, active, watching, submitted, won, deadlines
                ):
                    sent += 1
        return sent

    # -- internal -------------------------------------------------------- #

    async def _load_tenants(self, channel: str) -> list[Any]:
        """The tenants with an active channel of the needed type (one addressee per tenant)."""
        async with self._session_factory() as session:
            result = await session.execute(
                _TENANTS_SQL,
                {"channel": channel, "default_tz": DEFAULT_TZ},
            )
            return list(result)

    async def _load_counts(self, tenant_id: Any) -> tuple[int, int, int, int]:
        """The funnel counters of the tenant (active/watching/submitted/won)."""
        async with self._session_factory() as session:
            result = await session.execute(_COUNTS_SQL, {"tenant_id": tenant_id})
            rows = list(result)
        if not rows:
            return (0, 0, 0, 0)
        row = rows[0]
        return (
            int(row.active or 0),
            int(row.watching or 0),
            int(row.submitted or 0),
            int(row.won or 0),
        )

    async def _load_deadlines(self, tenant_id: Any) -> list[Any]:
        """The top-5 nearest deadlines of the tenant’s funnel (active/watching, future ones)."""
        async with self._session_factory() as session:
            result = await session.execute(
                _DEADLINES_SQL,
                {"tenant_id": tenant_id, "statuses": list(_WATCH_STATUSES)},
            )
            return list(result)

    async def _claim(self, tenant_id: Any, summary_date: date) -> bool:
        """The atomic claim in summary_log (a short transaction). True — ours, we send."""
        async with self._session_factory() as session:
            result = await session.execute(
                _CLAIM_SQL,
                {"tenant_id": tenant_id, "summary_date": summary_date},
            )
            await session.commit()
        return bool(list(result))

    async def _send_summary(
        self,
        adapter: DeliveryAdapter,
        row: Any,
        active: int,
        watching: int,
        submitted: int,
        won: int,
        deadlines: list[Any],
    ) -> bool:
        """Sends the summary through ``send_text``. The log is NOT rolled back on failure.

        The assumption (the port of v6): the log is already fixed in :meth:`_claim`. If the channel
        failed — we do NOT roll back (otherwise the next cycle would again make the claim and again
        try to send = a spam risk). Better to skip one summary.
        """
        message = build_summary_text(
            active=active,
            watching=watching,
            submitted=submitted,
            won=won,
            deadlines=deadlines,
            # getattr: the old test fakes without the column do not break (the same technique
            # as in delivery._load_jobs).
            locale=getattr(row, "locale", None),
        )
        try:
            result = await adapter.send_text(
                row.target, message, locale=getattr(row, "locale", None)
            )
        # a channel failure does not break the worker; the log is NOT touched (see the docstring)
        except Exception:  # noqa: BLE001
            logger.exception("summary: send failure tenant=%s", row.tenant_id)
            return False

        if not result.ok:
            logger.warning(
                "summary: not sent tenant=%s code=%s",
                row.tenant_id,
                result.response_code,
            )
            return False

        logger.info("summary: sent tenant=%s", row.tenant_id)
        return True

    async def _sleep(self, delay: float) -> None:
        """An interruptible sleep: reacts to stop() while waiting."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the summary worker (phase 1: the container via Compose)."""
    from src.bots.telegram import build_bot
    from src.log_secrets import install_secret_log_filter
    from src.workers.adapters import default_adapters

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()
    bot = build_bot()
    worker = SummaryWorker(adapters=default_adapters(bot))
    await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
