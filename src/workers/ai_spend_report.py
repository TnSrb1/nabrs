"""AI Spend Report — the daily summary of Anthropic spend + an alert to the operator.

WHY. Per-tenant limits of levels exist (``src/ai/usage.py::_TIER_LIMITS``, #468),
but there is no ceiling for the WHOLE account in the code and there will be none: a hard ceiling is set
in the Anthropic Console (the owner’s zone). Before opening public access at least an observer
is needed — how much was spent for the day and the month, and a shout to the operator when
the threshold is exceeded. This worker blocks nothing and cancels not a single model call: it only
LOOKS and REPORTS.

‼️ HOW THE DAILY SPEND IS COUNTED. ``ai_usage`` is a monthly aggregate
(PK ``(tenant_id, period, level)``, accumulation via ON CONFLICT DO UPDATE), and the
row has NO timestamp — the “spend for the day” cannot be computed from it by any
query. Therefore:

1. we sum ``ai_usage`` for the CURRENT ``period`` — this is the cumulative total of the month;
2. we write a snapshot to ``ai_spend_daily`` for today (UTC);
3. the daily spend = today’s total − yesterday’s snapshot OF THE SAME ``period``.

‼️ The change of month: on the first day yesterday’s snapshot belongs to the PAST period,
so it is taken strictly with a filter by ``period`` — if not found, the daily
spend equals today’s total. Without this filter the first day of every month would
give a negative delta (yesterday’s monthly total is bigger than today’s from zero) — a silent lie in the report exactly on the day the count starts over.

IDEMPOTENCY. The day’s snapshot is ``ON CONFLICT (day) DO UPDATE``: a repeated run
for the same day OVERWRITES the row (fresh figures) and does not multiply them. The alert is
claim-then-send through ``ai_spend_alert_log`` (PK ``(day, kind)``, a model:
``subscription_reminders``): the claim in a separate short transaction with a commit,
then the send. ‼️ A channel failure does NOT roll the log back — otherwise the next cycle
(an hour later) would make the claim again and the operator would get the same alert again.

RELIABILITY CONTRACT (as in :mod:`src.workers.health`). The observation NEVER breaks
what it observes: no configuration, the DB is unavailable, Telegram is silent,
a timeout — a silent no-op + a WARNING in the log. The thresholds are not set → the alert is off, but the
summary is written anyway (the snapshot is needed for tomorrow’s delta, regardless of
whether the shout is on).

PII. Only the tenant’s id and the sums go into the message and the log — no e-mails, no addresses,
no names. ``logger.exception`` is deliberately not used: the traceback prints the
arguments of the exception in full (see ``SMTPRecipientsRefused`` in the project rules), so we log
the CLASS of the error.

The worker goes to the DB PRIVILEGED, without ``SET app.current_tenant_id``:
a cross-tenant server operation (the owner bypasses RLS, ARCHITECTURE §8) — like
``subscription_reminders``. Both tables of 0040 are platform tables, without tenant_id.

ENV (both thresholds are optional; not set → the alert is off):
  AI_SPEND_DAILY_ALERT_USD  — the daily spend threshold, $. Empty → no alert.
  AI_SPEND_MONTH_ALERT_USD  — the current-month spend threshold, $. Empty → none.
  ADMIN_BOT_TOKEN, TG_ALERTS_CHAT_ID — the alert channel (the admin bot, via
      ``WorkerHealth``). ‼️ The user ``TG_BOT_TOKEN``/``ADMIN_CHAT_ID`` is NOT read here: diagnostics go only through the admin bot,
      there is deliberately no fallback between them.

The dependencies are injected (session, alert sender, “current time”, interval,
thresholds) — the unit tests run without a real PostgreSQL and without the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from sqlalchemy import text

from src.db.session import session_scope

logger = logging.getLogger(__name__)

# The polling loop. The summary is daily, but the snapshot is idempotent, so “once an hour”
# simply keeps the day’s row fresh (and survives a container restart at any
# moment of the day). The alert goes out once per kind — it guards the log.
DEFAULT_POLL_INTERVAL = 3600.0

# How many tenants to show in the breakdown of the message (by descending spend).
TOP_TENANTS = 5

_ALERT_DAILY = "daily"
_ALERT_MONTH = "month"

# The cumulative total of the CURRENT period. COALESCE — a period without a single call
# must give 0, not NULL: a snapshot “spent zero today” is as valuable as any other
# (tomorrow’s delta is computed from it).
_TOTALS_SQL = text("""
    SELECT COALESCE(SUM(u.cost_usd_total), 0) AS cost_usd_total,
           COALESCE(SUM(u.tokens_total), 0)   AS tokens_total,
           COALESCE(SUM(u.count), 0)          AS calls,
           COUNT(DISTINCT u.tenant_id)        AS tenants_count
    FROM ai_usage u
    WHERE u.period = CAST(:period AS text)
    """)

# The breakdown by tenants — only for the text of the message to the operator, NOT stored
# (see the docstring of migration 0040: storing it would require tenant_id + FORCE RLS).
_TOP_TENANTS_SQL = text("""
    SELECT u.tenant_id                        AS tenant_id,
           COALESCE(SUM(u.cost_usd_total), 0) AS cost_usd_total
    FROM ai_usage u
    WHERE u.period = CAST(:period AS text)
    GROUP BY u.tenant_id
    ORDER BY 2 DESC
    LIMIT CAST(:limit AS integer)
    """)

# Yesterday’s snapshot of the SAME period. The filter by period is not decoration: at the change
# of the month the row for yesterday exists, but belongs to the past account.
_PREV_SNAPSHOT_SQL = text("""
    SELECT cost_usd_total, tokens_total, calls
    FROM ai_spend_daily
    WHERE day = CAST(:day AS date)
      AND period = CAST(:period AS text)
    """)

_UPSERT_SNAPSHOT_SQL = text("""
    INSERT INTO ai_spend_daily
           (day, period, cost_usd_total, tokens_total, calls, tenants_count,
            captured_at)
    VALUES (CAST(:day AS date), CAST(:period AS text),
            CAST(:cost AS numeric), CAST(:tokens AS bigint),
            CAST(:calls AS integer), CAST(:tenants AS integer), now())
    ON CONFLICT (day) DO UPDATE
       SET period         = EXCLUDED.period,
           cost_usd_total = EXCLUDED.cost_usd_total,
           tokens_total   = EXCLUDED.tokens_total,
           calls          = EXCLUDED.calls,
           tenants_count  = EXCLUDED.tenants_count,
           captured_at    = now()
    """)

# The alert claim: RETURNING gives the row only on the FIRST claim of the day.
_CLAIM_ALERT_SQL = text("""
    INSERT INTO ai_spend_alert_log (day, kind)
    VALUES (CAST(:day AS date), CAST(:kind AS text))
    ON CONFLICT (day, kind) DO NOTHING
    RETURNING day
    """)


def _env_decimal(name: str) -> Decimal | None:
    """ENV → Decimal or None. Empty/garbage → None + WARNING (the threshold is off).

    Garbage in the threshold must not break the worker: observation does not break the observed.
    But silently swallowing it is not allowed either — otherwise a typo in ``secrets.env`` would turn the alert off forever and unnoticed.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        logger.warning(
            "ai_spend_report: %s is set but is not a number — the threshold is off",
            name,
        )
        return None


def _as_decimal(value: Any) -> Decimal:
    """Numeric from the DB → Decimal (None → 0). SUM() returns Decimal, but not always."""
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _money(value: Decimal) -> str:
    """The $ sum for the operator’s message: 4 digits (the spend is measured in cents)."""
    return f"${value:.4f}"


def build_report_text(
    *,
    day: date,
    period: str,
    daily_cost: Decimal,
    month_cost: Decimal,
    daily_calls: int,
    month_calls: int,
    tenants_count: int,
    top_tenants: list[tuple[Any, Decimal]],
    reasons: list[str],
) -> str:
    """The text of the alert to the operator (admin bot; the i18n catalogs are not pulled in here).

    ‼️ No PII by design: the tenant’s id and the sum, nothing more.
    """
    lines = [
        f"💸 Расход AI — {day.isoformat()} (период {period})",
        f"За сутки: {_money(daily_cost)} · вызовов {daily_calls}",
        f"За месяц: {_money(month_cost)} · вызовов {month_calls} "
        f"· тенантов {tenants_count}",
    ]
    if reasons:
        lines.append("Превышены пороги: " + "; ".join(reasons))
    if top_tenants:
        lines.append("Топ по расходу за месяц:")
        lines.extend(
            f"• {tenant_id}: {_money(cost)}" for tenant_id, cost in top_tenants
        )
    return "\n".join(lines)


class AiSpendReportWorker:
    """The daily snapshot of AI spend + an alert to the operator when thresholds are exceeded."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] = session_scope,
        alert_sender: Callable[[str], Awaitable[None]] | None = None,
        daily_threshold_usd: Decimal | None = None,
        month_threshold_usd: Decimal | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._alert = alert_sender
        self._daily_threshold = daily_threshold_usd
        self._month_threshold = month_threshold_usd
        self._poll_interval = poll_interval
        self._now = now_factory or (lambda: datetime.now(timezone.utc))
        self._stopped = asyncio.Event()

    @classmethod
    def from_env(cls, **over: Any) -> "AiSpendReportWorker":
        """Thresholds and the alert channel come from ENV. Nothing set → a snapshot without an alert."""
        from src.workers.health import WorkerHealth

        params: dict[str, Any] = {
            "daily_threshold_usd": _env_decimal("AI_SPEND_DAILY_ALERT_USD"),
            "month_threshold_usd": _env_decimal("AI_SPEND_MONTH_ALERT_USD"),
            "alert_sender": WorkerHealth.from_env(label="ai_spend").alert,
        }
        params.update(over)
        return cls(**params)

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        logger.info(
            "ai_spend_report: polling started (interval=%.0fs, daily threshold=%s, "
            "month threshold=%s)",
            self._poll_interval,
            self._daily_threshold if self._daily_threshold is not None else "—",
            self._month_threshold if self._month_threshold is not None else "—",
        )
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ) as exc:  # noqa: BLE001 — observation does not break the contour
                # Not logger.exception: the traceback drags the arguments of the exception
                # (incl. the request strings) into the log. The class of the error is enough.
                logger.error(
                    "ai_spend_report: the cycle did not complete (%s)",
                    type(exc).__name__,
                )
            await self._sleep(self._poll_interval)

    async def poll_once(self) -> dict[str, Any] | None:
        """One pass: the period total → the day’s snapshot → the delta → the alert by thresholds.

        Returns the summary of the pass (for tests and the log), or ``None`` if it could not be
        computed — a DB failure is quietly swallowed, the worker lives on.
        """
        now = self._now().astimezone(timezone.utc)
        day = now.date()
        period = now.strftime("%Y-%m")

        try:
            totals = await self._load_totals(period)
            prev = await self._load_prev_snapshot(day - timedelta(days=1), period)
            await self._save_snapshot(day, period, totals)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — observation does not break the contour
            logger.error(
                "ai_spend_report: the snapshot for %s is not saved (%s)",
                day.isoformat(),
                type(exc).__name__,
            )
            return None

        month_cost = totals["cost_usd_total"]
        month_calls = int(totals["calls"])
        # There is no yesterday’s snapshot of the SAME period (the first run or the first day
        # of the month) → for the day, exactly what has accumulated for the period was spent.
        prev_cost = prev["cost_usd_total"] if prev else Decimal(0)
        prev_calls = int(prev["calls"]) if prev else 0
        daily_cost = month_cost - prev_cost
        daily_calls = month_calls - prev_calls

        summary = {
            "day": day,
            "period": period,
            "daily_cost": daily_cost,
            "daily_calls": daily_calls,
            "month_cost": month_cost,
            "month_calls": month_calls,
            "tenants_count": int(totals["tenants_count"]),
            "alerts": [],
        }
        logger.info(
            "ai_spend_report: %s period=%s day=%s month=%s tenants=%d",
            day.isoformat(),
            period,
            _money(daily_cost),
            _money(month_cost),
            summary["tenants_count"],
        )

        # The claims are independent (the daily and the monthly thresholds are different kinds), but
        # the message is one: two e-mails with the same figures are not needed for the operator.
        # A kind already claimed today does not get into the text.
        claimed_reasons: list[str] = []
        for kind, reason in self._threshold_reasons(daily_cost, month_cost):
            if await self._claim_alert(day, kind):
                summary["alerts"].append(kind)
                claimed_reasons.append(reason)
        if claimed_reasons:
            await self._send_alert(day, period, summary, claimed_reasons)
        return summary

    def _threshold_reasons(
        self, daily_cost: Decimal, month_cost: Decimal
    ) -> list[tuple[str, str]]:
        """Which thresholds are exceeded. A threshold not set → this alert does not exist at all."""
        reasons: list[tuple[str, str]] = []
        if self._daily_threshold is not None and daily_cost > self._daily_threshold:
            reasons.append(
                (
                    _ALERT_DAILY,
                    f"за сутки {_money(daily_cost)} > {_money(self._daily_threshold)}",
                )
            )
        if self._month_threshold is not None and month_cost > self._month_threshold:
            reasons.append(
                (
                    _ALERT_MONTH,
                    f"за месяц {_money(month_cost)} > {_money(self._month_threshold)}",
                )
            )
        return reasons

    async def _send_alert(
        self, day: date, period: str, summary: dict[str, Any], reasons: list[str]
    ) -> None:
        """Sending the alert. ‼️ A channel failure does NOT roll the log back (anti-spam)."""
        if self._alert is None:
            return
        try:
            top = await self._load_top_tenants(period)
        except Exception as exc:  # noqa: BLE001 — the breakdown is not mandatory
            logger.warning(
                "ai_spend_report: the breakdown by tenants was not collected (%s)",
                type(exc).__name__,
            )
            top = []
        message = build_report_text(
            day=day,
            period=period,
            daily_cost=summary["daily_cost"],
            month_cost=summary["month_cost"],
            daily_calls=summary["daily_calls"],
            month_calls=summary["month_calls"],
            tenants_count=summary["tenants_count"],
            top_tenants=top,
            reasons=reasons,
        )
        try:
            await self._alert(message)
        except Exception as exc:  # noqa: BLE001 — the channel does not break the worker
            logger.warning("ai_spend_report: alert not sent (%s)", type(exc).__name__)

    # ------------------------------------------------------------------ #
    #  DB (privileged, without set_tenant — the platform tables of 0040)
    # ------------------------------------------------------------------ #

    async def _load_totals(self, period: str) -> dict[str, Any]:
        async with self._session_factory() as session:
            row = (await session.execute(_TOTALS_SQL, {"period": period})).first()
        if row is None:
            return {
                "cost_usd_total": Decimal(0),
                "tokens_total": 0,
                "calls": 0,
                "tenants_count": 0,
            }
        return {
            "cost_usd_total": _as_decimal(row.cost_usd_total),
            "tokens_total": int(row.tokens_total or 0),
            "calls": int(row.calls or 0),
            "tenants_count": int(row.tenants_count or 0),
        }

    async def _load_prev_snapshot(
        self, day: date, period: str
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    _PREV_SNAPSHOT_SQL, {"day": day, "period": period}
                )
            ).first()
        if row is None:
            return None
        return {
            "cost_usd_total": _as_decimal(row.cost_usd_total),
            "tokens_total": int(row.tokens_total or 0),
            "calls": int(row.calls or 0),
        }

    async def _save_snapshot(
        self, day: date, period: str, totals: dict[str, Any]
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                _UPSERT_SNAPSHOT_SQL,
                {
                    "day": day,
                    "period": period,
                    "cost": totals["cost_usd_total"],
                    "tokens": totals["tokens_total"],
                    "calls": totals["calls"],
                    "tenants": totals["tenants_count"],
                },
            )
            await session.commit()

    async def _load_top_tenants(self, period: str) -> list[tuple[Any, Decimal]]:
        async with self._session_factory() as session:
            result = await session.execute(
                _TOP_TENANTS_SQL, {"period": period, "limit": TOP_TENANTS}
            )
            rows = list(result)
        return [(row.tenant_id, _as_decimal(row.cost_usd_total)) for row in rows]

    async def _claim_alert(self, day: date, kind: str) -> bool:
        """Atomic claim into ai_spend_alert_log. True — the alert is ours, we send it.

        A DB failure on the claim = no alert: better not to shout than to shout twice
        (and certainly than to break the worker).
        """
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    _CLAIM_ALERT_SQL, {"day": day, "kind": kind}
                )
                claimed = bool(list(result))
                await session.commit()
            return claimed
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ai_spend_report: the claim of the alert %s failed (%s)",
                kind,
                type(exc).__name__,
            )
            return False

    async def _sleep(self, delay: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the AI spend summary worker (the container via Compose)."""
    from src.log_secrets import install_secret_log_filter

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()
    await AiSpendReportWorker.from_env().run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
