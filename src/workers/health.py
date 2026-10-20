"""Worker observability: heartbeat (Healthchecks.io) + Telegram alert (REV-05/06).

PURPOSE. The eNabavke source is a public procurement portal. When the portal's markup changes or the portal becomes unavailable, the flow of new tenders stops **silently** (there is no exception outward: ingest catches the failure and marks the source `last_status=error`). This module is a cheap safeguard against silent degradation:

  * **heartbeat ping** to Healthchecks.io (or a compatible service) after each
    SUCCESSFUL ingest pass. The external service raises the alarm itself if the
    ping does not arrive in time (cron grace). This covers both "the container
    died" and "the pass did not reach its end".
  * **immediate Telegram alert** to the administrator (owner) when a source pass
    FAILED (`fetch_batch` raised: network / 5xx / expired AccessToken / broken
    markup).
  * **Telegram alert about silent inflow stalls** (REV-06, incident
    07-09.09.2026): a pass can finish WITHOUT a single exception (the portal
    returns nothing / changes its markup so the parser does not fail / throttles
    with 429 without an error). In that case heartbeat_ok() stays as quiet as it
    does on a healthy flow. After each clean pass, `src/workers/ingest.py` checks
    the ACTUAL freshness of `tenders_normalized` and calls
    :func:`build_silence_alert_text` + `WorkerHealth.alert()` if the silence
    exceeds :func:`silence_threshold_hours`. The anti-bounce (no more than once
    per 6 h) and the SQL measurement itself live in ingest.py (where the Redis
    client and session_factory are); this module is responsible only for the
    threshold and the message text.

RELIABILITY CONTRACT (CRITICAL). Alerting NEVER fails the worker and never changes
its behaviour: everything is env-driven, and any error, network timeout or missing
configuration is a silent no-op (a WARNING in the log). Monitoring must not be the
cause of a failure in what it monitors. If the ENV is not set, the module is fully
passive.

ENV (all optional; empty = the corresponding channel is off):
  INGEST_HEALTHCHECK_URL  — base URL of the Healthchecks check. Success → GET <url>;
      failure → GET <url>/fail. Empty → heartbeat off.
  ADMIN_BOT_TOKEN, TG_ALERTS_CHAT_ID — token/chat of a SEPARATE admin bot
      (`@tendersrb_bot`, owner's directive). Diagnostics and alerts NEVER go through
      the user-facing `TG_BOT_TOKEN`/`ADMIN_CHAT_ID` (the user bot, src/bots/
      config.py). These are different bots with different purposes, and there is
      deliberately NO fallback from one to the other. Both (token AND chat) set →
      alert on; otherwise (including only one set) → tg_enabled=False, silent no-op.
  WORKER_ALERT_TIMEOUT_SEC — HTTP timeout of the ping/alert, seconds (default 10).
  INGEST_SILENCE_ALERT_HOURS — silence threshold for inflow, hours (default 30, based on
      facts — see :func:`silence_threshold_hours`). Empty / not a number → default,
      WITHOUT editing docker-compose.

DEPLOYMENT (RED, env wiring — separately). For the alert to actually be sent, the
`ingest` service in docker-compose needs the env ADMIN_BOT_TOKEN, TG_ALERTS_CHAT_ID
and (optionally) INGEST_HEALTHCHECK_URL. Without them the module is passive and does
no harm in production.

Network primitives (:func:`_http_get`, :func:`_tg_send`) are module-level so that unit
tests can monkeypatch them without a real network (as the other workers inject their
dependencies). httpx is imported lazily inside the primitives — importing the module
does not touch the network.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 10.0
_TG_API = "https://api.telegram.org"

# The threshold of the inflow silence (REV-06, tmp/TASK_ingest_silence_alert.md). The default —
# from a FACT: the maximum gap between neighbouring tenders_normalized.first_seen_at over the
# last 30 days (run of 12.09.2026) = 1d 00:06:56 (~24.1h). A 30h threshold gives a
# margin of ~6h above the observed maximum (0 false alarms in 30 days;
# a 24h threshold would give 1). Env override — WITHOUT an edit of docker-compose (the default
# must work by itself, the owner’s boundary of the task).
_DEFAULT_SILENCE_HOURS = 30.0


def silence_threshold_hours() -> float:
    """The inflow silence threshold (hours) — ``INGEST_SILENCE_ALERT_HOURS`` or the default.

    Empty/not a number/<=0 → the default, without an exception (as the other ENV getters
    of the module).
    """
    raw = (os.getenv("INGEST_SILENCE_ALERT_HOURS") or "").strip()
    if not raw:
        return _DEFAULT_SILENCE_HOURS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_SILENCE_HOURS
    return value if value > 0 else _DEFAULT_SILENCE_HOURS


@dataclass(frozen=True)
class SourceFreshness:
    """The ``sources`` slice for one source — only for the alert text."""

    source_key: str
    last_run_at: datetime | None
    error_count: int


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value is not None else "никогда"


def build_silence_alert_text(
    *,
    hours_silent: float,
    last_tender_at: datetime | None,
    count_24h: int,
    sources: Sequence[SourceFreshness],
    threshold_hours: float | None = None,
    source_key: str | None = None,
    source_label: str | None = None,
) -> str:
    """The text of the TG alert about the silent stop of the inflow — with figures and the reason.

    An alert without figures is useless (claim #519) — the hours of silence, the time of the last tender,
    the inflow over 24h and, for each source, ``last_run_at``/``error_count`` are mandatory (diagnostics: a live source with an empty
    inflow vs a source that has stopped running altogether).

    SRC-0a: the freshness is measured for EACH enabled source (otherwise a second
    source would mask the death of the first). ``source_key`` — the name of the silent
    source in the header; ``None`` — the previous text (one source enabled,
    as jnportal now). ``threshold_hours`` — the threshold of this source
    (``None`` — the common :func:`silence_threshold_hours`).

    SRC-0d: ``source_label`` — the human-readable name of the source
    (``sources.display_name``); if present, the first line is “Check the
    source “<name>”” (the owner’s decision of 30.09 evening, item 4a) — the operator does not need to decode the key.
    """
    threshold = (
        threshold_hours if threshold_hours is not None else silence_threshold_hours()
    )
    subject = (
        "приток тендеров" if source_key is None else f"приток тендеров «{source_key}»"
    )
    lines = [] if not source_label else [f"🔎 Проверить источник «{source_label}»"]
    lines += [
        f"⚠️ TenderSRB ingest: {subject} молчит {hours_silent:.1f}ч "
        f"(порог {threshold:.0f}ч).",
        f"Последний тендер: {_fmt_dt(last_tender_at)}. За 24ч новых: {count_24h}.",
        "Источники:",
    ]
    for src in sources:
        lines.append(
            f"  {src.source_key}: last_run_at={_fmt_dt(src.last_run_at)} "
            f"error_count={src.error_count}"
        )
    return "\n".join(lines)


async def _http_get(url: str, timeout: float) -> None:
    """GET without a body (heartbeat ping). Taken out as a module-level function — monkeypatched in tests."""
    import httpx  # a local import: importing the module does not pull the network

    async with httpx.AsyncClient(timeout=timeout) as client:
        await client.get(url)


async def _tg_send(token: str, chat_id: int, text: str, timeout: float) -> None:
    """sendMessage in the Telegram Bot API. Taken out as a module-level function — monkeypatched in tests."""
    import httpx

    url = f"{_TG_API}/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=timeout) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})


def _env_int(name: str) -> int | None:
    """an ENV variable → int or None (empty/not a number → None, without an exception)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class WorkerHealth:
    """Heartbeat + alert. Constructed from ENV (:meth:`from_env`) or explicitly (tests).

    All methods are best-effort: network errors, timeouts and the absence of configuration
    are swallowed (a WARNING log); no exception leaves outwards.
    """

    healthcheck_url: str | None = None
    tg_bot_token: str | None = None
    admin_chat_id: int | None = None
    timeout: float = _DEFAULT_TIMEOUT
    label: str = "ingest"

    @classmethod
    def from_env(cls, *, label: str = "ingest") -> "WorkerHealth":
        """Assembles the configuration from ENV. Does not hit the network, raises no exceptions.

        The Telegram alert — ONLY through the admin bot `@tendersrb_bot` (the owner’s directive):
        `ADMIN_BOT_TOKEN` + `TG_ALERTS_CHAT_ID`. The user `TG_BOT_TOKEN`
        (@nabrs_bot) is deliberately NOT read here — the diagnostics do not fall back to it.
        """
        url = (os.getenv("INGEST_HEALTHCHECK_URL") or "").strip() or None
        token = (os.getenv("ADMIN_BOT_TOKEN") or "").strip() or None
        chat_id = _env_int("TG_ALERTS_CHAT_ID")
        timeout_raw = (os.getenv("WORKER_ALERT_TIMEOUT_SEC") or "").strip()
        try:
            timeout = float(timeout_raw) if timeout_raw else _DEFAULT_TIMEOUT
        except ValueError:
            timeout = _DEFAULT_TIMEOUT
        return cls(
            healthcheck_url=url,
            tg_bot_token=token,
            admin_chat_id=chat_id,
            timeout=timeout,
            label=label,
        )

    @property
    def tg_enabled(self) -> bool:
        """The Telegram alert is enabled only if BOTH the token AND the chat are set."""
        return bool(self.tg_bot_token) and bool(self.admin_chat_id)

    async def heartbeat_ok(self) -> None:
        """A ping “pass successful”. A no-op if the URL is not set; errors are swallowed."""
        if not self.healthcheck_url:
            return
        try:
            await _http_get(self.healthcheck_url, self.timeout)
        except Exception:  # noqa: BLE001 — the alerting does not break the worker
            logger.warning(
                "health[%s]: heartbeat_ok ping failed", self.label, exc_info=True
            )

    async def heartbeat_fail(self, reason: str | None = None) -> None:
        """A ping “pass broken” (<url>/fail). A no-op if the URL is not set."""
        if not self.healthcheck_url:
            return
        url = self.healthcheck_url.rstrip("/") + "/fail"
        try:
            await _http_get(url, self.timeout)
        except Exception:  # noqa: BLE001
            logger.warning(
                "health[%s]: heartbeat_fail ping failed (%s)",
                self.label,
                reason,
                exc_info=True,
            )

    async def alert(self, text: str) -> None:
        """A Telegram alert to the administrator. A no-op if the token/chat are not set."""
        if not self.tg_enabled:
            return
        try:
            await _tg_send(
                self.tg_bot_token,  # type: ignore[arg-type]
                self.admin_chat_id,  # type: ignore[arg-type]
                text,
                self.timeout,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "health[%s]: Telegram alert not sent", self.label, exc_info=True
            )
