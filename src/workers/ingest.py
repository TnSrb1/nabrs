"""Ingest Worker — the entry of the pipeline: ``fetch_batch → tenders_raw → tenders_normalized``.

ARCHITECTURE §9 (the ingest/persist worker), §10 (SourceAdapter), §13.1 (routing
reads from ``tenders_normalized`` by trigger). DB_SCHEMA §2.8 (sources), §2.9
(tenders_raw), §2.10 (tenders_normalized), §5.1 (notify_tender_change).
Closes the gap C-01 (CHAT21_TECH_AUDIT §2/§4): without a writer to
``tenders_normalized`` the trigger ``notify_tender_change`` (0001) is silent and the whole
pipeline (routing→ai→delivery) starves.

The pipeline per source:

0. **which sources** — the enabled rows of the table ``sources`` ∩ the registry of
   adapters (SRC-0a, R1); re-read EVERY pass, enabling/disabling without a restart. An adapter without a row gets a DISABLED row (auto-seed).
1. **since** — ``sources.last_run_at``; on the first pass (NULL) —
   ``enabled_at - adapter.initial_lookback`` (SRC-0a, R4: the archive is not pulled).
   ``since=None`` the worker never passes; an enabled row without
   ``enabled_at`` is not loaded (fail-closed). We call
   ``adapter.fetch_batch(since)`` (an async generator).
2. **raw** — for each ``RawTender`` we count ``raw_hash`` = SHA-256 of the canonicalised ``payload`` and upsert ``tenders_raw`` by
   UNIQUE(source_key, source_uid). If the record exists and ``raw_hash`` has not changed — **skip entirely** (we touch neither raw nor normalized → the trigger is not fired needlessly). This covers the main mass of “seen the same again”.
3. **normalized** — for a new/changed payload we call ``adapter.normalize``
   and upsert ``tenders_normalized`` by UNIQUE(source_key, source_uid):

   - **INSERT** (no record): ``first_seen_at = last_seen_at = last_changed_at
     = now`` → the trigger sends ``change_kind=INSERT`` → routing.
   - **content UPDATE** (the content fingerprint changed): we update the fields +
     ``last_seen_at = last_changed_at = now`` → the trigger ``change_kind=UPDATE``.
   - **lightweight touch** (the payload changed, but the normalized content is the same):
     we update only ``last_seen_at = now``, without the content and without ``last_changed_at``.

DECISION ON THE TRIGGER (0001): the trigger ``tenders_normalized_notify`` is attached
``AFTER INSERT OR UPDATE ... FOR EACH ROW`` **without a WHEN condition** — it fires on *any* UPDATE. Therefore the lightweight touch of ``last_seen_at`` inevitably produces one extra ``NOTIFY change_kind=UPDATE``; it cannot be removed without an edit of the trigger itself (a migration — out of T1's scope). **The lightweight touch is accepted**: (a) the case is rare — the main flow of identical payloads is cut off at the raw level by ``raw_hash`` before normalization; (b) routing is idempotent (the outbox ``ON CONFLICT DO NOTHING`` on UNIQUE(profile,tender,
channel), §6.4) — the extra UPDATE-notify will re-run the matching, but will not create duplicates. The contract of routing (``id`` + the checkpoint by ``first_seen_at``) does not change.

cluster_key / description_embedding are NOT filled by the adapter or by ingest (NULL).
``canonical_tender_id`` (+ ``canonical_confidence``/``canonical_score``, 0047)
is written by :mod:`src.dedup.linker` after the INSERT/UPDATE of a row of the country RS — the mark of the TED↔jnportal duplicate (the owner's decision of 01.10.2026), see :meth:`IngestWorker._link_cross_source`.

Idempotency and resilience (the review focus):

- one bad row/tender does not break the pass (a log + ``rollback`` + we continue);
- network errors of ``fetch_batch`` — a log + the next source (``sources`` is marked
  ``last_status='error'``, ``error_count`` is incremented);
- a successful pass moves ``sources.last_run_at = now``, resets
  ``error_count`` and sets ``last_status='ok'``.

The dependencies are injected (the session factory, the source key, the adapter factory,
the clock) — for unit tests with a mocked DB and a fake adapter, as routing/ai.
It goes to the DB over the owner connection; ``tenders_raw``/``tenders_normalized`` are
public data without RLS (DB_SCHEMA §4), tenant isolation is not applied here.

Observability (REV-05): a broken pass of a source (``result.failed``) and a
successful full cycle are signalled through the injected ``WorkerHealth`` (heartbeat
in Healthchecks + Telegram alert). By default ``WorkerHealth.from_env()`` is
env-driven and a no-op without configuration; the alerting NEVER breaks the worker.
‼️ OWNER DECISION 20.09.2026: the anti-flapping of the repeated alert “the source broke”
(``SourceAlertGate``, added 12.09.2026) is REMOVED — “it only hides the problem” (verbatim). There is NO suppression of repeats and there will be none: a persisting problem rings on EVERY pass, however many there are (tmp/TASK_flow_observability.md).
‼️ On 22.09.2026 a DISTINCTION was added (not suppression): the Telegram alert goes out from the
SECOND broken pass IN A ROW (``_FAIL_STREAK_ALERT_THRESHOLD``, the length of the streak = ``sources.error_count``). A single failure, cured by the next pass, stays visible in three places — ``heartbeat_fail`` (always goes out), the WARNING in the log, the daily counters ``passes_failed``/``fail_streak_max`` in the summary of :mod:`src.workers.flow_report`. Only a SINGLE failure is silent, and only in Telegram; nothing is silent by TIME.
Observability (REV-07, tmp/TASK_flow_observability.md): besides the failure/the silence of the inflow — the rate/failures of the source, the completeness of the card enrichment (sum/deadline/description), the silent loss of documents (429 on ``GetDocument.ashx``) and the request budget per card. The counter — :class:`src.workers.flow_observability.
RequestCounterHandler`, attached to the ``httpx`` logger for the duration of the pass (it parses the already formatted log line; the secrets in it are already masked by
``SecretMaskingFilter`` — a second network client is not set up). The threshold alerts — immediately and WITHOUT suppression of repeats (the same owner directive). The daily summary — a separate worker :mod:`src.workers.flow_report` (the same technique as ``ai_spend_report``); it accumulates the figures through Redis (:class:`FlowMetricsStore`, the same client; a new one is not set up).
Observability (REV-06): a clean pass (without ``result.failed``) by itself does not mean “the data is fresh” — the portal may return emptiness without an exception. ``_check_silence()`` after each clean pass measures the ACTUAL freshness of ``tenders_normalized`` and sends a Telegram alert when the silence is longer than ``src.workers.health.silence_threshold_hours()`` (the anti-flapping — :class:`SilenceAlertGate`, the same Redis as ``PendingEnrichStore``).

Not done (separate steps/sprints): dedup/cluster_key (T6), embeddings,
``fetch_documents`` (ADR-005 — metadata only), wiring into the Compose service (T2).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from collections.abc import AsyncIterator
from typing import Any, Callable, Protocol, runtime_checkable

from sqlalchemy import text

from src.db.session import session_scope
from src.dedup.linker import link_cross_source
from src.domain.actuality import no_deadline_is_fresh
from src.domain.repoll import REPOLL_MAX_PER_PASS, select_due
from src.domain.source_silence import is_silent, silence_hours
from src.domain.tender_changes import (
    CORRECTION_KEY,
    CORRECTIONS_SEEN_KEY,
    build_change,
)
from src.domain.tz import deadline_before_published
from src.log_secrets import install_secret_log_filter
from src.parsers.base import (
    NormalizedTender,
    RawTender,
    SourceAdapter,
    get_source,
    registered_sources,
)
from src.parsers.registry import load_all_adapters
from src.workers.news_autopost import autopost_enabled
from src.workers.news_autopost import run_forever as news_run_forever
from src.workers.source_reconcile import SourceReconciler, due_today
from src.workers.alsu_collector import alsu_enabled, collect_asset_sales
from src.workers.plans_collector import collect_plans, plans_enabled
from src.workers.flow_observability import (
    CompletenessCounts,
    FlowMetricsStore,
    PassRequestStats,
    build_completeness_alerts,
    build_docs_lost_alert,
    build_rate_pass_log,
    check_tender_budget,
    count_requests,
    docs_lost_count,
)
from src.workers.health import (
    SourceFreshness,
    WorkerHealth,
    build_silence_alert_text,
    silence_threshold_hours,
)

logger = logging.getLogger(__name__)

# The defensive minimum of the scheduler interval (sec): even if the source has it set too aggressively,
# we do not hammer the site more often than once a minute.
_MIN_POLL_INTERVAL = 60
# ALSU ≈22 notices/month: a pass (~4 listings of 5 s + the cards of the new ones) every 6 h — with a margin.
ALSU_PASS_INTERVAL_SEC = 6 * 3600

# The degradation of the interval on a persistent failure of the source (VG-03 03.2): after
# _DEGRADE_ERROR_THRESHOLD IN A ROW of broken passes (sources.error_count,
# reset to 0 on success — see _SOURCE_MARK_OK_SQL) we thin out the polling,
# so as not to hammer a down portal. Transient failures are already absorbed by the retry
# of the adapter’s HTTP layer (INGEST_HTTP_MAX_RETRIES) — only the case reaches here
# when the retries are exhausted and the pass has still failed several times in a row.
_DEGRADE_ERROR_THRESHOLD = 3

# ‼️ THE THRESHOLD OF THE TELEGRAM ALERT ABOUT A SOURCE FAILURE — A NUMBER FROM A FACT (22.09.2026).
#
# What was observed. For the whole life of the ingest container (the deploy of 20.09 17:50 UTC
# → 22.09 06:30 UTC, 72 passes of 30 minutes) a broken pass was EXACTLY ONE:
#   2026-09-22T02:25:28Z ERROR ingest: fetch_batch failure of source jnportal
#   → httpx.ConnectTimeout
# The next pass at 02:55 went normally (the portal 200 OK, “requests=3 429=0
# (0.0%) peak=2 req/s”), the inflow was not affected: max(first_seen_at) = 2026-09-21
# 23:49:54 UTC — a normal night batch. At the same time the alert went to the owner in Telegram and
# was read as “the errors are creeping in again”. A streak of failures in a row: 1 of 1.
#
# Why exactly 2, and not 3 nor “by eye”:
#   * a broken pass is ALREADY exhausted retries of the HTTP layer
#     (INGEST_HTTP_MAX_RETRIES inside `_get`). Two broken passes in a row =
#     retries exhausted TWICE with the polling interval between (DB fact: sources.
#     polling_interval_sec = 1800) — this stops being “the network blinked”;
#   * the price of the threshold is known and bounded: ONE polling interval of delay,
#     that is, in a real outage the alert still comes in ~30 minutes;
#   * a threshold of 3 would cost an hour of silence in a real outage. Thinning out the
#     polling (_DEGRADE_ERROR_THRESHOLD = 3) is about the load on a down portal —
#     it has a different error price, therefore the number is different;
#   * on the observed window a threshold of 2 would remove 1 false alert out of 1 and NOT A SINGLE
#     of a real outage (there were no persistent failures in the window).
# ‼️ This is the threshold of the TELEGRAM channel, not suppression: a single failure stays
# a WARNING in the log, heartbeat_fail always goes out, and the failure gets into the daily
# summary flow_report (passes_failed / fail_streak_max). The silent stop
# of the inflow is caught by a separate guard (#522, _check_silence) — we do not duplicate it here.
_FAIL_STREAK_ALERT_THRESHOLD = 2
_DEGRADE_MAX_MULTIPLIER = 8
_DEGRADE_MAX_INTERVAL_SEC = 6 * 3600  # absolute ceiling — 6 hours


def _degrade_interval(base_interval: int, error_count: int) -> int:
    """Thins out the polling interval of the source on a persistent failure (VG-03).

    ``error_count`` below the threshold — the interval is not touched (transient failures are already retried at the HTTP request level; only the exhausted attempts reach here).
    From the threshold — we stretch it exponentially; the multiplier and the resulting interval are bounded from above (do not stretch infinitely).
    """
    if error_count < _DEGRADE_ERROR_THRESHOLD:
        return base_interval
    extra = error_count - _DEGRADE_ERROR_THRESHOLD + 1
    multiplier = min(2**extra, _DEGRADE_MAX_MULTIPLIER)
    return min(base_interval * multiplier, _DEGRADE_MAX_INTERVAL_SEC)


# --------------------------------------------------------------------------- #
#  The state of the pass: what was not fetched because of 429 (the 429 loop, tmp/TASK_ingest_state.md)
# --------------------------------------------------------------------------- #
# A size fuse for the set: the set must not grow infinitely (dead
# sources/a persistent ban must not accumulate the unfetched forever).
_PENDING_MAX = 500
# TTL 7 days — forgotten junk (the source was fixed, but the set remained) expires by itself.
_PENDING_TTL_SEC = 7 * 24 * 3600


class PendingEnrichStore:
    """The unfetched ``source_uid`` of one source because of 429 — Redis, not the DB.

    A flat scheme (CLAUDE.md — “no nested structures”): a SET of the rows
    ``ingest:<source_key>:pending_enrich`` + a timestamp
    ``ingest:<source_key>:pending_updated``. The approach is the same as in
    :class:`src.parsers.guards.CountStore`: Redis was chosen because it is ALREADY in the contour (``REDIS_URL``, ``src/workers/routing.py``) — a new migration is not needed (RED). ``redis=None`` (not configured) or a client failure → all methods no-op / an empty result + a WARNING on failure: the state speeds up the pass, and is not the condition of its working.
    """

    def __init__(
        self,
        redis: Any | None,
        *,
        max_size: int = _PENDING_MAX,
        ttl: int = _PENDING_TTL_SEC,
    ) -> None:
        self._redis = redis
        self._max_size = max_size
        self._ttl = ttl

    @staticmethod
    def _uids_key(source_key: str) -> str:
        return f"ingest:{source_key}:pending_enrich"

    @staticmethod
    def _updated_key(source_key: str) -> str:
        return f"ingest:{source_key}:pending_updated"

    async def get(self, source_key: str) -> list[str]:
        if self._redis is None:
            return []
        try:
            raw = await self._redis.smembers(self._uids_key(source_key))
        except Exception:  # noqa: BLE001 — the state is optional
            logger.warning(
                "ingest[%s]: Redis unavailable for reading pending_enrich", source_key
            )
            return []
        return sorted(v.decode() if isinstance(v, bytes) else v for v in raw)

    async def add(self, source_key: str, uids: set[str]) -> None:
        """Adds ``uids`` to the set — if it is already at the limit, it does NOT add
        (WARNING) instead of digging into a partial addition: the rule is simple and predictable (“the set must not grow infinitely”)."""
        if self._redis is None or not uids:
            return
        uids_key = self._uids_key(source_key)
        try:
            current = await self._redis.scard(uids_key)
        except Exception:  # noqa: BLE001
            logger.warning(
                "ingest[%s]: Redis unavailable for writing pending_enrich", source_key
            )
            return
        if current >= self._max_size:
            logger.warning(
                "ingest[%s]: pending_enrich is full (%d >= %d) — new uids "
                "were not added",
                source_key,
                current,
                self._max_size,
            )
            return
        try:
            await self._redis.sadd(uids_key, *uids)
            await self._redis.expire(uids_key, self._ttl)
            await self._redis.set(
                self._updated_key(source_key),
                datetime.now(timezone.utc).isoformat(),
                ex=self._ttl,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "ingest[%s]: Redis unavailable for writing pending_enrich", source_key
            )

    async def discard(self, source_key: str, uid: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.srem(self._uids_key(source_key), uid)
        except Exception:  # noqa: BLE001
            logger.warning(
                "ingest[%s]: Redis unavailable for removing from pending_enrich",
                source_key,
            )


# --------------------------------------------------------------------------- #
#  The anti-flapping of the alert about the silence of the inflow (REV-06, tmp/TASK_ingest_silence_alert.md)
# --------------------------------------------------------------------------- #
_SILENCE_ALERT_REDIS_KEY = "ingest:silence_alert_sent_at"
# No more than once a day (owner 30.09 evening, item 4): one alert per episode of silence,
# a repeat — no more often than once in 24h, otherwise it goes out on EVERY pass while the silence
# lasts. (Before SRC-0d it was 6h.)
_SILENCE_ALERT_COOLDOWN_SEC = 24 * 3600


async def _admin_email_from_env(subject: str, body: str) -> int:
    """An e-mail to the operators from ``CABINET_ADMIN_EMAILS`` (best-effort, SRC-0d).

    The transport — as for the mail workers (``email_sender_from_env``: SMTP → Postmark; without configuration ``None`` → no-op, not Mock: “sent” into the void is worse than an honest silence). Any error — a WARNING (only the class of the error, without the address: PII #347); the worker does not fall. Returns the number sent.
    """
    try:
        from types import SimpleNamespace

        from src.api.admin_notify import notify_admins_email
        from src.workers.adapters import email_sender_from_env

        sender = email_sender_from_env()
        if sender is None:
            return 0
        settings = SimpleNamespace(
            platform_admin_emails=os.getenv("CABINET_ADMIN_EMAILS", "")
        )
        return await notify_admins_email(
            email_sender=sender,
            settings=settings,  # type: ignore[arg-type]
            subject=subject,
            text_body=body,
        )
    except Exception as exc:  # noqa: BLE001 — the alerting does not break the worker
        logger.warning(
            "ingest: the e-mail alert about the silence was not sent (%s)", type(exc).__name__
        )
        return 0


class SilenceAlertGate:
    """Not more often than once per ``_SILENCE_ALERT_COOLDOWN_SEC`` — the same Redis as :class:`PendingEnrichStore` (``REDIS_URL``, do not create a new client).

    ``redis=None`` or a client failure → ``should_send`` is always True: the state speeds up/smooths the alerting, and is not the condition of its work (the same technique as ``PendingEnrichStore`` — without Redis the silence will alert on each pass instead of once per 6h, which is worse as spam, but NOT worse than a full failure of the monitoring itself).
    """

    def __init__(
        self, redis: Any | None, *, cooldown_sec: int = _SILENCE_ALERT_COOLDOWN_SEC
    ) -> None:
        self._redis = redis
        self._cooldown_sec = cooldown_sec

    @staticmethod
    def _key(source_key: str | None) -> str:
        # None — the previous key (one enabled source): the anti-flapping of jnportal
        # is not reset after the rollout. Several sources — a key for each.
        if source_key is None:
            return _SILENCE_ALERT_REDIS_KEY
        return f"{_SILENCE_ALERT_REDIS_KEY}:{source_key}"

    async def should_send(
        self, *, now: datetime, source_key: str | None = None
    ) -> bool:
        if self._redis is None:
            return True
        try:
            raw = await self._redis.get(self._key(source_key))
        except Exception:  # noqa: BLE001
            logger.warning("ingest: Redis unavailable for reading silence_alert_sent_at")
            return True
        if raw is None:
            return True
        raw = raw.decode() if isinstance(raw, bytes) else raw
        try:
            sent_at = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (ValueError, TypeError):
            return True
        return (now - sent_at).total_seconds() >= self._cooldown_sec

    async def mark_sent(self, *, now: datetime, source_key: str | None = None) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(
                self._key(source_key),
                str(now.timestamp()),
                ex=self._cooldown_sec * 2,
            )
        except Exception:  # noqa: BLE001
            logger.warning("ingest: Redis unavailable for writing silence_alert_sent_at")


# --- sources --------------------------------------------------------------- #
_SOURCE_RUNTIME_SQL = text("""
    SELECT polling_interval_sec, last_run_at, enabled, error_count,
           enabled_at, baseline_at
    FROM sources
    WHERE source_key = :source_key
    """)
# A quiet first pass (SRC-0a, R4): the first successful pass sets
# baseline_at — the tenders seen before it, STAGE_A_SQL does not send to anybody.
# COALESCE: subsequent passes do not move the mark.
_SOURCE_MARK_OK_SQL = text("""
    UPDATE sources
    SET last_run_at = :now, last_status = 'ok', error_count = 0,
        baseline_at = COALESCE(baseline_at, :now)
    WHERE source_key = :source_key
    """)
# A repeated poll of the open tenders (the repoll-open task, 07.10.2026). The candidates —
# by definition of the task open: the deadline not earlier than “today−1”, OR the deadline is absent and
# the tender is fresh (src/domain/actuality.py). The period and the order — in
# src/domain/repoll.py, here only the selection. ``tenders_raw.fetched_at`` — the mark of
# the last check: ``tenders_raw`` has no trigger, so the mark does not wake
# the matching and requires no migration.
_REPOLL_CANDIDATES_SQL = text("""
    SELECT tn.id AS tender_id, tn.source_uid, tn.deadline_at, tn.published_at,
           tn.first_seen_at, tn.status, tr.fetched_at AS checked_at
    FROM tenders_normalized tn
    JOIN tenders_raw tr
      ON tr.source_key = tn.source_key AND tr.source_uid = tn.source_uid
    WHERE tn.source_key = :source_key
      AND (tn.deadline_at >= CAST(:now AS timestamptz) - interval '1 day'
           OR (tn.deadline_at IS NULL
               AND COALESCE(tn.published_at, tn.first_seen_at)
                   >= CAST(:now AS timestamptz) - interval '30 days'))
    """)
# ‼️ An UPDATE of a row of tenders_normalized fires the trigger tenders_normalized_notify
# (AFTER UPDATE without WHEN, 0001) → NOTIFY → routing → matching. Therefore we write ONLY
# the really changed deadline/status. The repeated delivery is cut by the outbox
# UNIQUE(profile, tender, channel) + ON CONFLICT DO NOTHING (routing §6.4).
_REPOLL_UPDATE_NORM_SQL = text("""
    UPDATE tenders_normalized
    SET deadline_at = :deadline_at,
        status = :status,
        last_seen_at = :now,
        last_changed_at = :now
    WHERE source_key = :source_key AND source_uid = :source_uid
    """)
_REPOLL_TOUCH_RAW_SQL = text("""
    UPDATE tenders_raw
    SET fetched_at = :now
    WHERE source_key = :source_key AND source_uid = :source_uid
    """)
# Recheck of documents of delivered open tenders (AI analysis, step 3).
# Candidate: an open source tender DELIVERED to at least one subscriber
# (delivery_outbox.status='sent'), last rechecked long ago (or never).
# The oldest check goes first: if the budget is smaller than the queue, every one gets its turn.
_DOCS_RECHECK_CANDIDATES_SQL = text("""
    SELECT tn.id AS tender_id, tn.source_uid, tn.documents
    FROM tenders_normalized tn
    LEFT JOIN tender_docs_check dc ON dc.tender_id = tn.id
    WHERE tn.source_key = :source_key
      AND tn.deadline_at >= CAST(:now AS timestamptz)
      AND (dc.checked_at IS NULL
           OR dc.checked_at < CAST(:now AS timestamptz) - interval '1 day')
      AND EXISTS (SELECT 1 FROM delivery_outbox o
                  WHERE o.tender_id = tn.id AND o.status = 'sent')
    ORDER BY dc.checked_at NULLS FIRST, tn.id
    LIMIT :limit
    """)
_DOCS_CHECK_MARK_SQL = text("""
    INSERT INTO tender_docs_check (tender_id, checked_at)
    VALUES (:tender_id, :now)
    ON CONFLICT (tender_id) DO UPDATE SET checked_at = EXCLUDED.checked_at
    """)
# The source registry (SRC-0a, R1): what to load — the table decides, not the import.
_ENABLED_SOURCES_SQL = text("""
    SELECT source_key
    FROM sources
    WHERE enabled = true
    ORDER BY source_key
    """)
# Auto-seed: a registered adapter without a row gets it DISABLED.
# ON CONFLICT DO NOTHING — an existing row (and its enabled flag) is not touched.
_SOURCE_AUTOSEED_SQL = text("""
    INSERT INTO sources
        (source_key, display_name, base_url, country_codes,
         polling_interval_sec, enabled)
    VALUES
        (CAST(:source_key AS text), CAST(:display_name AS text),
         CAST(:base_url AS text), CAST(:country_codes AS char(2)[]),
         CAST(:polling_interval_sec AS integer), false)
    ON CONFLICT (source_key) DO NOTHING
    """)
# RETURNING error_count — this IS the length of the streak of failures IN A ROW: the counter
# grows on each broken pass and is reset by the first successful one
# (_SOURCE_MARK_OK_SQL). A separate table/migration for “how many times in a row”
# is not needed — the state is already there.
_SOURCE_MARK_ERROR_SQL = text("""
    UPDATE sources
    SET last_status = 'error',
        error_count = COALESCE(error_count, 0) + 1
    WHERE source_key = :source_key
    RETURNING error_count, polling_interval_sec
    """)

# The freshness of the inflow (REV-06, tmp/TASK_ingest_silence_alert.md): tenders_normalized
# — a public table without RLS (see the module docstring/get_ingest_health), bypass
# is not needed. The same pattern as admin_service._INGEST_TENDERS_SQL.
# Freshness — PER EACH enabled source (SRC-0a): a global MAX
# would let a live second source mask the death of the first.
_INGEST_FRESHNESS_SQL = text("""
    SELECT
        s.source_key,
        s.display_name,
        s.enabled_at,
        (SELECT MAX(first_seen_at) FROM tenders_normalized tn
          WHERE tn.source_key = s.source_key)                              AS last_at,
        (SELECT COUNT(*) FROM tenders_normalized tn
          WHERE tn.source_key = s.source_key
            AND tn.first_seen_at >= NOW() - INTERVAL '24 hours')           AS cnt_24h
    FROM sources s
    WHERE s.enabled = true
    ORDER BY s.source_key
    """)
_SOURCES_FRESHNESS_SQL = text("""
    SELECT source_key, last_run_at, error_count
    FROM sources
    ORDER BY source_key
    """)

# --- tenders_raw ----------------------------------------------------------- #
_RAW_LOOKUP_SQL = text("""
    SELECT id, raw_hash
    FROM tenders_raw
    WHERE source_key = :source_key AND source_uid = :source_uid
    """)
_RAW_PAYLOAD_LOOKUP_SQL = text("""
    SELECT raw_payload
    FROM tenders_raw
    WHERE source_key = :source_key AND source_uid = :source_uid
    """)
# The reconciliation of the pending_enrich set with the FACT (see _settle_pending_by_db): which of
# the passed uids already lie in tenders_raw WITH enrichment. ``jsonb_exists(...)``,
# and not the operator ``?`` — the latter in text() is read as a placeholder in the
# qmark style and breaks the compilation. An array as a parameter — a CAST by the asyncpg rule.
_RAW_ENRICHED_UIDS_SQL = text("""
    SELECT source_uid
    FROM tenders_raw
    WHERE source_key = :source_key
      AND source_uid = ANY(CAST(:uids AS text[]))
      AND jsonb_exists(raw_payload, '_enrich')
    """)
_RAW_INSERT_SQL = text("""
    INSERT INTO tenders_raw
        (country_code, source_key, source_uid, raw_payload, raw_hash, fetched_at)
    VALUES
        (:country_code, :source_key, :source_uid,
         CAST(:raw_payload AS jsonb), :raw_hash, :fetched_at)
    RETURNING id
    """)
_RAW_UPDATE_SQL = text("""
    UPDATE tenders_raw
    SET raw_payload = CAST(:raw_payload AS jsonb),
        raw_hash = :raw_hash,
        fetched_at = :fetched_at
    WHERE id = :id
    """)

# --- tenders_normalized ---------------------------------------------------- #
# The fields for the content fingerprint (they determine INSERT / content UPDATE / touch).
_CONTENT_FIELDS = (
    "source_url",
    "title",
    "title_normalized",
    "description",
    "description_normalized",
    "customer_name",
    "customer_tax_id",
    "customer_region_code",
    "amount_original",
    "currency_original",
    "amount_eur",
    "is_amount_estimated",
    "published_at",
    "deadline_at",
    "opening_at",
    "classifications",
    "procedure_type",
    "contract_type",
    "language",
    "regions",
    "status",
    "documents",
)
_NORM_LOOKUP_SQL = text(f"""
    SELECT id, {", ".join(_CONTENT_FIELDS)}
    FROM tenders_normalized
    WHERE source_key = :source_key AND source_uid = :source_uid
    """)
_NORM_INSERT_SQL = text("""
    INSERT INTO tenders_normalized (
        country_code, tenders_raw_id, source_key, source_uid, source_url,
        title, title_normalized, description, description_normalized,
        customer_name, customer_tax_id, customer_region_code,
        amount_original, currency_original, amount_eur, is_amount_estimated,
        published_at, deadline_at, opening_at,
        classifications, procedure_type, contract_type, language,
        regions, status, documents,
        first_seen_at, last_seen_at, last_changed_at
    ) VALUES (
        :country_code, :tenders_raw_id, :source_key, :source_uid, :source_url,
        :title, :title_normalized, :description, :description_normalized,
        :customer_name, :customer_tax_id, :customer_region_code,
        :amount_original, :currency_original, :amount_eur, :is_amount_estimated,
        :published_at, :deadline_at, :opening_at,
        CAST(:classifications AS jsonb), :procedure_type, :contract_type,
        :language, :regions, :status, CAST(:documents AS jsonb),
        :now, :now, :now
    )
    RETURNING id
    """)
# The content changed → overwrite the fields + last_seen/last_changed = now (notify UPDATE).
_NORM_UPDATE_CONTENT_SQL = text("""
    UPDATE tenders_normalized
    SET tenders_raw_id = :tenders_raw_id,
        source_url = :source_url,
        title = :title,
        title_normalized = :title_normalized,
        description = :description,
        description_normalized = :description_normalized,
        customer_name = :customer_name,
        customer_tax_id = :customer_tax_id,
        customer_region_code = :customer_region_code,
        amount_original = :amount_original,
        currency_original = :currency_original,
        amount_eur = :amount_eur,
        is_amount_estimated = :is_amount_estimated,
        published_at = :published_at,
        deadline_at = :deadline_at,
        opening_at = :opening_at,
        classifications = CAST(:classifications AS jsonb),
        procedure_type = :procedure_type,
        contract_type = :contract_type,
        language = :language,
        regions = :regions,
        status = :status,
        documents = CAST(:documents AS jsonb),
        last_seen_at = :now,
        last_changed_at = :now
    WHERE id = :id
    """)
# The duplicate link — under a SAVEPOINT (see IngestWorker._link_cross_source).
_LINK_SAVEPOINT_SQL = text("SAVEPOINT dedup_link")
_LINK_ROLLBACK_SQL = text("ROLLBACK TO SAVEPOINT dedup_link")
_LINK_RELEASE_SQL = text("RELEASE SAVEPOINT dedup_link")
# Tender change journal (0058): under SAVEPOINT — a failure to write the diff (the table is still
# absent in prod until the migration is applied) must not drop the tender save.
_CHANGE_SAVEPOINT_SQL = text("SAVEPOINT tender_change")
_CHANGE_ROLLBACK_SQL = text("ROLLBACK TO SAVEPOINT tender_change")
_CHANGE_RELEASE_SQL = text("RELEASE SAVEPOINT tender_change")
_CHANGE_INSERT_SQL = text("""
    INSERT INTO tender_changes (tender_id, kind, diff)
    VALUES (:tender_id, :kind, CAST(:diff AS jsonb))
    """)
# Lightweight touch: only last_seen_at (see the TRIGGER DECISION in the docstring).
_NORM_TOUCH_SQL = text("""
    UPDATE tenders_normalized
    SET last_seen_at = :now
    WHERE id = :id
    """)


def _format_fail_reason(exc: BaseException) -> str:
    """The short reason of a broken pass — for the alert/heartbeat (REV-05).

    ``httpx.HTTPStatusError`` → ``"HTTP <code>"`` (e.g. ``"HTTP 429"``);
    otherwise — the name of the exception class. ‼️ Do not bring PII/secrets here: the request URL
    carries query parameters, and the AccessToken goes in a header — both stay in the log
    (``logger.exception``); only the type and the status code get into the text of the alert/heartbeat (incident 08.09.2026: three hours of diagnostics went on the fact that the alert did not name the cause of the failure).
    """
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


@dataclass(frozen=True)
class SourceResult:
    """The result of a pass for one source (for logs/tests)."""

    source_key: str
    inserted: int = 0
    updated: int = 0
    touched: int = 0
    skipped: int = 0
    errors: int = 0
    # Skipped BY POLICY “only the actual” (SRC-0a, R4) — not a failure.
    skipped_not_actual: int = 0
    failed: bool = False  # the pass broke entirely (e.g. the network in fetch_batch)
    # The short reason of a broken pass for the alert/heartbeat (see
    # _format_fail_reason) — NOT the URL and NOT the headers (PII/secrets), only the type
    # exception and, for httpx.HTTPStatusError, the response code (e.g. "HTTP 429").
    fail_reason: str | None = None
    # Observability (REV-07): the rate/failures of the requests, the completeness of the enrichment,
    # inserted/updated cards, the request budget per card — counted ACROSS THE
    # TIME of the pass (incl. a partial one, if it broke in the middle).
    request_stats: PassRequestStats = field(default_factory=PassRequestStats)
    completeness: CompletenessCounts = field(default_factory=CompletenessCounts)
    budget_over_count: int = 0
    # The most expensive card of the day AMONG ALL (not only among those that exceeded
    # the budget) — otherwise “max 0” lies when budget_over_count == 0 (owner).
    budget_max: int = 0


# --------------------------------------------------------------------------- #
#  Canonicalisation / fingerprints (pure functions)
# --------------------------------------------------------------------------- #
def _canonical_json(value: Any) -> str:
    """Deterministic JSON serialization (a stable key order)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def compute_raw_hash(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonicalised payload — detection of a change of the raw tender."""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _num(value: Any) -> float | None:
    """Numeric/Decimal/str/float → float|None (a single form for the fingerprint)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> str | None:
    """datetime → ISO string (a single form for the fingerprint)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# Money: ingest writes ``tenders_normalized`` with RAW SQL through ``text()``,
# binding ``:amount_*`` directly — the ORM type ``QuantizedNumeric`` (#121,
# src/db/models/tender.py) does NOT take part in the raw-SQL path, therefore the float “tail”
# of a division (e.g. 98030.33999999999650754…) would go into the DB and into the bot.
# We quantise in Python BEFORE the write with our own pure function (the ORM type is not
# pulled into ingest). The contract matches ``QuantizedNumeric.process_bind_param``.
_MONEY_QUANT = Decimal("0.01")


def _quantize_money(value: Any) -> Decimal | None:
    """A money value → ``Decimal`` with exactly 2 digits (``ROUND_HALF_UP``).

    ``None`` → ``None``; accepts float/int/Decimal/str. The same contract as
    ``QuantizedNumeric.process_bind_param`` (#121), but without a dependency on the ORM.
    """
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


# Keys of the document-list record that are NOT part of the fingerprint: ``extracted``/
# ``chars`` — the result of download/extraction, not the file content. A network failure
# of a single file flips them ok↔error, and without this exception every such failure would
# cause an extra content UPDATE (last_changed_at, NOTIFY, re-matching, AI cache reset).
# Blacklist, not whitelist: sources have different document schemas (jnportal —
# name/sha256/…, bih — title/url/kind, ukck — name/url/type/size), and a whitelist
# dropped their keys entirely (a change of url was invisible to the fingerprint).
_DOC_FINGERPRINT_VOLATILE_KEYS = frozenset({"extracted", "chars"})


def _fingerprint_fields(fields: dict[str, Any]) -> str:
    """The canonical fingerprint of the set of content fields (for comparing versions)."""
    canon = {
        **{k: fields.get(k) for k in _CONTENT_FIELDS},
        "documents": [
            {k: v for k, v in d.items() if k not in _DOC_FINGERPRINT_VOLATILE_KEYS}
            for d in (fields.get("documents") or [])
        ],
        "amount_original": _num(fields.get("amount_original")),
        "amount_eur": _num(fields.get("amount_eur")),
        "published_at": _dt(fields.get("published_at")),
        "deadline_at": _dt(fields.get("deadline_at")),
        "opening_at": _dt(fields.get("opening_at")),
    }
    return hashlib.sha256(_canonical_json(canon).encode("utf-8")).hexdigest()


def _normalized_to_fields(n: NormalizedTender) -> dict[str, Any]:
    """The content fields of ``NormalizedTender`` (for the fingerprint and the write).

    The money fields are quantised to 2 digits HERE (at the input to fields), not only
    in the SQL parameters: otherwise ``_fingerprint_fields`` (via ``_num``→float) and
    the written value would diverge, and an old “tailed” row against a new
    quantised one would count as different content → an extra UPDATE+notify on EACH
    pick-up. We quantise once — and both the write and the fingerprint see one value.
    """
    return {
        "source_url": n.source_url,
        "title": n.title,
        "title_normalized": n.title_normalized,
        "description": n.description,
        "description_normalized": n.description_normalized,
        "customer_name": n.customer_name,
        "customer_tax_id": n.customer_tax_id,
        "customer_region_code": n.customer_region_code,
        "amount_original": _quantize_money(n.amount_original),
        "currency_original": n.currency_original,
        "amount_eur": _quantize_money(n.amount_eur),
        "is_amount_estimated": n.is_amount_estimated,
        "published_at": n.published_at,
        "deadline_at": n.deadline_at,
        "opening_at": n.opening_at,
        "classifications": n.classifications,
        "procedure_type": n.procedure_type,
        "contract_type": n.contract_type,
        "language": n.language,
        "regions": n.regions,
        "status": n.status,
        "documents": n.documents,
    }


DOCS_RECHECK_MAX_PER_PASS = 20

#: Statuses at which the tender is no longer actual (a mirror of the activity filter
#: ``STAGE_A_SQL``/``PREVIEW_WHERE_A`` in :mod:`src.workers.matching`).
_TERMINAL_STATUSES = frozenset({"closed", "awarded", "cancelled"})


@runtime_checkable
class _RepollAdapter(Protocol):
    """An adapter with a repoll of cards (jnportal — ``repoll_cards``).

    ``isinstance`` instead of ``hasattr``: mypy narrows the type and sees the method, not
    a ``SourceAdapter`` without the attribute (the base class does not declare it).
    """

    def repoll_cards(
        self, tender_ids: list[str]
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]: ...


@runtime_checkable
class _DocsRecheckAdapter(Protocol):
    """Adapter with document recheck (jnportal has ``recheck_documents``)."""

    def recheck_documents(
        self, targets: list[tuple[str, Any]]
    ) -> AsyncIterator[tuple[str, Any, dict[str, Any]]]: ...


def repoll_decision(row: Any, card: dict[str, Any]) -> tuple[Any, Any, bool]:
    """What to write by the results of the poll: (deadline, status, whether the deadline/status changed).

    A single decision for the repeated poll and the one-off run of the overdues
    (``scripts/repoll_backlog.py``): an empty value from the card does not overwrite the
    stored one, and a deadline earlier than the publication is a broken deadline of the portal (Q-29), not a new value.
    """
    card_deadline = card.get("deadline_at")
    # The same guard as in the main pass (Q-29, adapter): a deadline earlier than the
    # publication — a broken deadline of the portal, not a new value. Without it the
    # deadline zeroed on the first pass would come back as a “change” and
    # would throw the tender out of the feed.
    if deadline_before_published(card_deadline, row["published_at"]):
        card_deadline = None
    deadline = card_deadline or row["deadline_at"]
    status = card.get("status") or row["status"]
    changed = deadline != row["deadline_at"] or status != row["status"]
    return deadline, status, changed


def repoll_enabled() -> bool:
    """``INGEST_JNPORTAL_REPOLL`` → whether the repeated poll of the open ones is enabled (DEFAULT OFF).

    The flag is env-only: it is not in ``deploy/`` yet (RED, owner), and in prod it does not
    act until the owner adds it to the ``environment:`` of the ingest service.
    """
    raw = (os.getenv("INGEST_JNPORTAL_REPOLL") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def docs_recheck_enabled() -> bool:
    """``INGEST_DOCS_RECHECK`` → recheck of documents of delivered tenders (DEFAULT OFF).

    The owner enables it in the ``environment:`` of the ingest service (``deploy/``, RED): until the flag
    is set, nothing changes in prod.
    """
    raw = (os.getenv("INGEST_DOCS_RECHECK") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def docs_recheck_max_per_pass() -> int:
    """``INGEST_DOCS_RECHECK_MAX_PER_PASS`` → ceiling of tenders per pass (default 20).

    Request budget = 2 per tender when documents are unchanged (card + list) and
    up to 2 + ``_MAX_FILES`` (12) when they changed; all of them go through the rate gate (≤1 rps).
    """
    raw = (os.getenv("INGEST_DOCS_RECHECK_MAX_PER_PASS") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DOCS_RECHECK_MAX_PER_PASS
    return value if value > 0 else DOCS_RECHECK_MAX_PER_PASS


def repoll_max_per_pass() -> int:
    """``INGEST_JNPORTAL_REPOLL_MAX_PER_PASS`` → the ceiling of cards per pass (default 40)."""
    raw = (os.getenv("INGEST_JNPORTAL_REPOLL_MAX_PER_PASS") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return REPOLL_MAX_PER_PASS
    return value if value > 0 else REPOLL_MAX_PER_PASS


def _is_actual(n: NormalizedTender, now: datetime) -> bool:
    """A tender is actual: the status is not finished AND the deadline is in the future.

    No deadline → actual for 30 days from ``published_at`` (owner 06.10.2026,
    :mod:`src.domain.actuality`); no publication date either → the count from “now”
    (``first_seen_at`` of a new tender = the moment of insertion), i.e. actual.

    ‼️ A naive ``deadline_at`` is treated as UTC (the contract of ``src/domain/tz.py``:
    stored as UTC). Without this the aware/naive comparison would raise ``TypeError``, the tender
    would go into “broken”, and the cursor would move on — a loss of a record by a FAILURE.
    """
    if n.status in _TERMINAL_STATUSES:
        return False
    deadline = n.deadline_at
    if deadline is None:
        return no_deadline_is_fresh(n.published_at, now, now)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline > now


def _row_to_fields(row: Any) -> dict[str, Any]:
    """The content fields from a ``tenders_normalized`` row (for the fingerprint)."""
    return {k: getattr(row, k, None) for k in _CONTENT_FIELDS}


# --------------------------------------------------------------------------- #
#  Worker
# --------------------------------------------------------------------------- #
class IngestWorker:
    """Pulls tenders from the source adapters and writes into the two-tier storage.

    The dependencies are injected: the session factory, the restriction by the source key,
    the adapter factory (by default — the ``@register_source`` registry), the clock,
    ``WorkerHealth`` (heartbeat/alert; by default from ENV, a no-op without configuration).
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] = session_scope,
        source_key: str | None = None,
        adapter_factory: Callable[[str], SourceAdapter] | None = None,
        clock: Callable[[], datetime] | None = None,
        health: WorkerHealth | None = None,
        redis: Any | None = None,
        admin_emailer: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        # The e-mail alert to the administrators about the silence of a source (SRC-0d): by default
        # from ENV (CABINET_ADMIN_EMAILS + SMTP/Postmark), a no-op without configuration.
        self._admin_emailer = admin_emailer or _admin_email_from_env
        # None → all registered sources; otherwise one specific one.
        self._only_source_key = source_key
        self._adapter_factory = adapter_factory or (lambda key: get_source(key)())
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Observability (REV-05): by default from ENV, a no-op without configuration.
        self._health = health if health is not None else WorkerHealth.from_env()
        # The state of the pass — what was not fetched because of 429 (STATE, tmp/TASK_ingest_state.md).
        # redis=None → PendingEnrichStore no-op (see its docstring), ingest
        # works as before — the state speeds up the pass, and is not the condition of its work.
        self._pending = PendingEnrichStore(redis)
        self._reconciler: SourceReconciler | None = None
        # The anti-flapping of the alert about the silence of the inflow (REV-06) — the same Redis client.
        self._silence_gate = SilenceAlertGate(redis)
        # The daily accumulator of the flow observability (REV-07) — the same Redis.
        self._flow_metrics = FlowMetricsStore(redis)
        self._pass_had_failure = False
        self._alsu_last_pass: datetime | None = None
        # The enabled rows without an adapter, about which an alert has already gone out (once per
        # process): a persistent desync is visible in the log of each pass.
        self._missing_adapter_alerted: set[str] = set()
        self._stopped = asyncio.Event()

    # -- public API ----------------------------------------------------- #

    def stop(self) -> None:
        """Asks the worker to finish after the current pass."""
        self._stopped.set()

    async def source_keys(self) -> list[str]:
        """The keys for the pass: ``sources.enabled = true`` ∩ the registry of adapters.

        The TABLE decides (SRC-0a, R1); the registry only says whether there is code.
        Re-read every pass — enabling/disabling without a restart.
        An enabled row without an adapter → a WARNING every pass + an alert once.
        ``source_key`` in the constructor (a manual/test run) — exactly it.
        """
        if self._only_source_key is not None:
            return [self._only_source_key]
        async with self._session_factory() as session:
            rows = (await session.execute(_ENABLED_SOURCES_SQL)).all()
        registry = registered_sources()
        keys: list[str] = []
        for row in rows:
            key = str(row.source_key)
            if key in registry:
                keys.append(key)
                continue
            logger.warning(
                "ingest: source %s is enabled in sources, but there is no adapter in the "
                "registry (src/parsers/registry.py) — skipping",
                key,
            )
            if key not in self._missing_adapter_alerted:
                self._missing_adapter_alerted.add(key)
                await self._health.alert(
                    f"⚠️ TenderSRB ingest: источник «{key}» включён в sources, "
                    "но адаптера нет в реестре — не грузится."
                )
        return keys

    async def seed_registered_sources(self) -> None:
        """Auto-seed: a ``sources`` row for an adapter without one — DISABLED.

        The attributes — from the adapter class. Existing rows are not touched
        (``ON CONFLICT DO NOTHING``); enabling — only by the owner’s decision
        (the contract — :mod:`src.domain.sources`). A failure does not break the scheduler.
        """
        try:
            async with self._session_factory() as session:
                for key, cls in sorted(registered_sources().items()):
                    await session.execute(
                        _SOURCE_AUTOSEED_SQL,
                        {
                            "source_key": key,
                            "display_name": getattr(cls, "source_name", None),
                            "base_url": getattr(cls, "base_url", None),
                            "country_codes": list(
                                getattr(cls, "country_codes", None) or []
                            ),
                            "polling_interval_sec": getattr(
                                cls, "default_polling_interval", None
                            ),
                        },
                    )
                await session.commit()
        except Exception:  # noqa: BLE001 — the seed is not a condition of ingest working
            logger.warning("ingest: the auto-seed of sources rows failed", exc_info=True)

    async def run(self) -> None:
        """The endless scheduler: a pass over the sources → sleep by the interval."""
        await self.seed_registered_sources()
        logger.info(
            "ingest: scheduler started, registry=%s",
            sorted(registered_sources()),
        )
        while not self._stopped.is_set():
            interval = await self.run_once()
            await self._sleep(interval)

    async def run_once(self) -> int:
        """One pass over all sources. Returns the sleep interval (sec)."""
        self._pass_had_failure = False
        intervals: list[int] = []
        try:
            keys = await self.source_keys()
        except Exception:  # noqa: BLE001 — no registry = a broken pass
            logger.exception("ingest: could not read the sources registry")
            self._pass_had_failure = True
            await self._health.heartbeat_fail("sources: registry read failed")
            keys = []
        for source_key in keys:
            interval = await self.ingest_source(source_key)
            intervals.append(interval)
        await self._maybe_collect_asset_sales()
        # a pass without broken sources → heartbeat (Healthchecks watches the deadline)
        if not self._pass_had_failure:
            await self._health.heartbeat_ok()
            # REV-06: a pass WITHOUT exceptions does not mean “the data is fresh” — the portal
            # may have returned emptiness without an error (the incident of 07-09.09.2026). A broken
            # pass already alerts above (ingest_source) — here only the “silent success”.
            await self._check_silence()
            await self._reconcile_sources()
        # the scheduler sleeps for the most frequent source, but not more often than the defensive minimum
        return (
            max(_MIN_POLL_INTERVAL, min(intervals)) if intervals else _MIN_POLL_INTERVAL
        )

    async def _reconcile_sources(self) -> None:
        """The daily reconciliation with the source (M2): after 01:30 UTC, once a day.

        The sources — the enabled ones whose adapter can ``count_published_on``. A failure
        of the reconciliation must not break the scheduler (observation is not a condition of acceptance).
        """
        now = self._clock()
        if not due_today(now):
            return
        try:
            keys = await self.source_keys()
            sources: list[tuple[str, str | None, SourceAdapter]] = [
                (k, None, self._adapter_factory(k)) for k in keys
            ]
            if self._reconciler is None:  # lives between passes: holds the backoff of failures
                self._reconciler = SourceReconciler(
                    session_factory=self._session_factory,
                    alert=self._health.alert,
                    email=self._admin_emailer,
                )
            await self._reconciler.reconcile_enabled(sources, now=now)
        except Exception as exc:  # noqa: BLE001 — monitoring does not break the worker
            logger.warning(
                "ingest: the reconciliation with the sources failed (%s)", type(exc).__name__
            )

    async def _check_silence(self) -> None:
        """Checks the ACTUAL freshness of ``tenders_normalized`` after a clean
        pass and sends a Telegram alert on a prolonged silence (REV-06,
        tmp/TASK_ingest_silence_alert.md). The threshold/text — in ``src.workers.health``,
        the anti-flapping — :class:`SilenceAlertGate`. An error of the request to the DB here must
        not break the scheduler — the alerting is not a condition of its work.

        SRC-0a: the freshness and the threshold — for EACH enabled source (the threshold —
        the attribute of the adapter ``silence_threshold_hours``, otherwise the common one). A source without
        a single tender is measured from ``enabled_at`` (a just-enabled one does not
        ring at once). While ONE source is enabled, the text and the key of the anti-flapping are
        the previous ones (the jnportal behavior does not change).
        """
        now = self._clock()
        try:
            async with self._session_factory() as session:
                fresh_rows = (await session.execute(_INGEST_FRESHNESS_SQL)).all()
                source_rows = (await session.execute(_SOURCES_FRESHNESS_SQL)).all()
        except Exception:  # noqa: BLE001 — monitoring does not break the worker
            logger.warning(
                "ingest: could not check the freshness of the inflow (silence alert)",
                exc_info=True,
            )
            return

        sources = [
            SourceFreshness(
                source_key=row.source_key,
                last_run_at=row.last_run_at,
                error_count=row.error_count or 0,
            )
            for row in source_rows
        ]
        registry = registered_sources()
        single = len(fresh_rows) == 1
        for row in fresh_rows:
            key = str(row.source_key)
            last_at = row.last_at
            hours_silent = silence_hours(now, last_at, row.enabled_at)
            if hours_silent is None:
                hours_silent = float("inf")
            adapter_cls = registry.get(key)
            threshold = getattr(adapter_cls, "silence_threshold_hours", None)
            if threshold is None:
                threshold = silence_threshold_hours()
            if not is_silent(hours_silent, threshold):
                continue
            gate_key = None if single else key
            if not await self._silence_gate.should_send(now=now, source_key=gate_key):
                continue
            text_ = build_silence_alert_text(
                hours_silent=hours_silent,
                last_tender_at=last_at,
                count_24h=int(row.cnt_24h or 0),
                sources=sources,
                threshold_hours=threshold,
                source_key=None if single else key,
                source_label=(getattr(row, "display_name", None) or None),
            )
            await self._health.alert(text_)
            label = getattr(row, "display_name", None) or key
            try:
                await self._admin_emailer(
                    f"TenderSRB: проверить источник «{label}»", text_
                )
            except Exception as exc:  # noqa: BLE001 — a mail failure does not break the worker
                logger.warning(
                    "ingest: the e-mail alert about the silence was not sent (%s)", type(exc).__name__
                )
            await self._silence_gate.mark_sent(now=now, source_key=gate_key)

    async def ingest_source(self, source_key: str) -> int:
        """A full pass over one source. Returns its polling interval."""
        try:
            adapter = self._adapter_factory(source_key)
        except Exception:  # noqa: BLE001 — no adapter → we skip the source
            logger.exception("ingest: could not create the adapter %s", source_key)
            return _MIN_POLL_INTERVAL

        async with self._session_factory() as session:
            since, interval, enabled = await self._load_runtime(session, adapter)

        # fail-closed: we load ONLY the explicitly enabled (`is not True`, NOT `is False`).
        # From SRC-0a source_keys() already goes through the table (enabled = true ∩ registry),
        # but ingest_source is also called directly (source_key in the constructor): the row
        # is absent → enabled=None → we do not load, rather than “load everything” (the RLS principle).
        if enabled is not True:
            if enabled is None:
                logger.warning(
                    "ingest: source %s is not in the sources table — skipping "
                    "(fail-closed). Enabling a source: a row in sources with "
                    "enabled=true",
                    source_key,
                )
            else:
                logger.info(
                    "ingest: source %s is disabled (enabled=false), skipping", source_key
                )
            return interval

        # R4 (SRC-0a): the “whole archive” (since=None) is NEVER loaded. An enabled
        # row without enabled_at (enabled bypassing the contract
        # src.domain.sources.enable_source) — we do not load, rather than “load everything”.
        if since is None:
            logger.warning(
                "ingest: source %s is enabled, but sources.enabled_at is empty — "
                "skipping (fail-closed). Enable via "
                "src.domain.sources.enable_source",
                source_key,
            )
            return interval

        result = await self._ingest(adapter, source_key, since)
        # The repeated poll — only after a NON-broken pass: a broken (429) one —
        # the portal’s budget is already spent, the poll does not get into it. A failure of the poll is counted as
        # a broken pass (see _maybe_repoll): the same error_count and alert.
        repoll_reason = (
            None if result.failed else await self._maybe_repoll(adapter, source_key)
        )
        if not result.failed:
            await self._maybe_collect_plans(adapter, source_key)
        # Document recheck — after repoll, through the same gate and by the same
        # rule: a broken pass has already used the portal's budget.
        if not result.failed and repoll_reason is None:
            repoll_reason = await self._maybe_recheck_documents(adapter, source_key)
        pass_failed = result.failed or repoll_reason is not None
        fail_reason = result.fail_reason if result.failed else repoll_reason
        stage = "fetch_batch" if result.failed else "repoll"

        # The length of the streak of failures IN A ROW (step 3): 1 — the first failure, 2+ — a repeat.
        # ‼️ The fallback is the threshold, not 1: if the sources row was for some reason not updated,
        # the alert must go out. The anti-flapping must not hide the problem (owner).
        fail_streak = _FAIL_STREAK_ALERT_THRESHOLD
        async with self._session_factory() as session:
            if pass_failed:
                row = (
                    await session.execute(
                        _SOURCE_MARK_ERROR_SQL, {"source_key": source_key}
                    )
                ).first()
                if row is not None and row.error_count:
                    fail_streak = int(row.error_count)
                    # The interval of the next pass — by the ALREADY increased counter:
                    # otherwise a failure on this pass would not thin out the next one (one step later).
                    interval = _degrade_interval(
                        int(
                            row.polling_interval_sec or adapter.default_polling_interval
                        ),
                        fail_streak,
                    )
            else:
                await session.execute(
                    _SOURCE_MARK_OK_SQL,
                    {"source_key": source_key, "now": self._clock()},
                )
            await session.commit()

        # Observability (REV-05): a broken pass → heartbeat_fail + a TG alert
        # (outside the DB transaction — it is the network). Health is a no-op without configuration and NEVER
        # does not break the worker. A successful heartbeat is sent in run_once on a clean pass.
        if pass_failed:
            self._pass_had_failure = True
            # The reason (the exception type / "HTTP <code>") — so that the alert does not make
            # one spend an hour on diagnostics of “the portal / AccessToken / network” when the cause
            # is known at once (the incident of 08.09.2026, jnportal 429).
            reason_suffix = f": {fail_reason}" if fail_reason else ""
            await self._health.heartbeat_fail(
                f"{source_key}: {stage} failed{reason_suffix}"
            )
            # ‼️ When the cause is known (fail_reason), the advice “check the portal /
            # AccessToken / the network” misleads — it is about OTHER causes and
            # leads away from the real one (the incident of 08.09.2026: an hour of diagnostics on a
            # 200 portal and a valid token, while the known cause was HTTP 429).
            # The tail is relevant only when the cause is NOT determined.
            tail = (
                " Подробности — в логе контейнера ingest."
                if fail_reason
                else " Поток новых тендеров мог встать — проверь портал/AccessToken/сеть."
            )
            # ‼️ DISTINGUISH, DO NOT SUPPRESS (the owner’s 20.09.2026 decision stands: the anti-flapping
            # must not HIDE the problem). The difference between “hiding” and
            # “distinguishing” is in what exactly goes silent:
            #   * the old anti-flapping was silent BY TIME — a persistent problem
            #     stopped ringing, and that is what killed it;
            #   * here only a SINGLE failure, cured by the next
            #     pass, is silent. From the second in a row an alert goes out on EVERY pass,
            #     however many there are — a persistent problem rings always.
            # A single failure does not disappear: a WARNING in the log + the daily counter +
            # a line in the daily summary flow_report.
            # The threshold 2 — the rationale is in _FAIL_STREAK_ALERT_THRESHOLD.
            if fail_streak >= _FAIL_STREAK_ALERT_THRESHOLD:
                await self._health.alert(
                    f"⚠️ TenderSRB ingest: источник «{source_key}» сорвался "
                    f"{fail_streak}-й раз подряд ({stage})"
                    f"{reason_suffix}.{tail}"
                )
            else:
                logger.warning(
                    "ingest: %s — единичный срыв прохода%s. В Telegram не шлём: "
                    "следующий проход через %d сек обычно лечит. Повторится "
                    "подряд — уйдёт алерт; счётчик уже в суточной сводке",
                    source_key,
                    reason_suffix,
                    interval,
                )

        # Observability of the flow/enrichment (REV-07, tmp/TASK_flow_observability.md):
        # the rate/failures, the completeness, the silent loss of documents — are counted even on
        # a broken pass (the partial data before the failure is also indicative).
        # The threshold alerts — IMMEDIATELY and WITHOUT suppression of repeats (owner).
        logger.info(build_rate_pass_log(source_key, result.request_stats))
        for alert_text in build_completeness_alerts(source_key, result.completeness):
            await self._health.alert(alert_text)
        docs_lost = docs_lost_count(result.request_stats)
        docs_lost_alert = build_docs_lost_alert(source_key, docs_lost)
        if docs_lost_alert:
            await self._health.alert(docs_lost_alert)
        await self._flow_metrics.record_pass(
            self._clock().date().isoformat(),
            stats=result.request_stats,
            completeness=result.completeness,
            budget_over_count=result.budget_over_count,
            budget_max=result.budget_max,
            failed=pass_failed,
            fail_streak=fail_streak if pass_failed else 0,
        )

        logger.info(
            "ingest: %s — new=%d upd=%d touch=%d skip=%d err=%d "
            "skipped_not_actual=%d%s",
            source_key,
            result.inserted,
            result.updated,
            result.touched,
            result.skipped,
            result.errors,
            result.skipped_not_actual,
            " (FAILED)" if pass_failed else "",
        )
        return interval

    # -- internal -------------------------------------------------------- #

    async def _maybe_collect_asset_sales(self) -> None:
        """The collection of the ALSU notices (“property”) — only under the flag, no more often than once in ALSU_PASS_INTERVAL.

        The flag ``INGEST_ALSU_SALES`` (default OFF, :func:`alsu_enabled`). ALSU is NOT a source
        of tenders (no row in ``sources``, no adapter in the registry): a separate entity with its own
        cursor. A failure does NOT break the tender pass: a warning with the TYPE of the exception.
        """
        if not alsu_enabled():
            return
        now = self._clock()
        last = self._alsu_last_pass
        if last is not None and (now - last).total_seconds() < ALSU_PASS_INTERVAL_SEC:
            return
        self._alsu_last_pass = now
        try:
            await collect_asset_sales(self._session_factory)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — the collection of property does not break the tender pass
            logger.warning(
                "ingest[alsu]: the collection of notices failed — %s", _format_fail_reason(exc)
            )

    async def _maybe_recheck_documents(
        self, adapter: SourceAdapter, source_key: str
    ) -> str | None:
        """Recheck of documents of delivered tenders — with the flag, and for an adapter that has it.

        Returns as :meth:`_maybe_repoll`: ``None`` — it passed (or is disabled), otherwise
        the reason for the failure (exception type / HTTP code, without a URL with a token).
        """
        if not docs_recheck_enabled() or not isinstance(adapter, _DocsRecheckAdapter):
            return None
        try:
            checked, changed = await self._recheck_documents(adapter, source_key)
        except Exception as exc:  # noqa: BLE001 — the failure is visible as a broken pass
            reason = _format_fail_reason(exc)
            logger.warning(
                "ingest[%s]: document recheck failed — %s", source_key, reason
            )
            return reason
        logger.info(
            "ingest[%s]: document recheck — checked %d, changed %d",
            source_key,
            checked,
            changed,
        )
        return None

    async def _recheck_documents(
        self, adapter: _DocsRecheckAdapter, source_key: str
    ) -> tuple[int, int]:
        """Select candidates, recheck them, write the changed ones. (checked, changed).

        ‼️ The DB is not held open for the duration of the network calls: the selection is a short transaction,
        each write is its own.
        """
        now = self._clock()
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        _DOCS_RECHECK_CANDIDATES_SQL,
                        {
                            "source_key": source_key,
                            "now": now,
                            "limit": docs_recheck_max_per_pass(),
                        },
                    )
                )
                .mappings()
                .all()
            )
        if not rows:
            return 0, 0
        by_uid = {str(r["source_uid"]): dict(r) for r in rows}
        targets = [(uid, r["documents"]) for uid, r in by_uid.items()]
        checked = changed = 0
        async for uid, docs, card in adapter.recheck_documents(targets):
            checked += 1
            if await self._apply_documents_recheck(
                adapter, source_key, uid, by_uid[uid]["tender_id"], docs, now, card
            ):
                changed += 1
        return checked, changed

    async def _apply_documents_recheck(
        self,
        adapter: Any,
        source_key: str,
        uid: str,
        tender_id: Any,
        docs: Any,
        now: datetime,
        card: dict[str, Any] | None = None,
    ) -> bool:
        """Write the result of the recheck of one tender. Returns: was the list updated?

        ‼️ Deadline and status are NOT taken from the saved ``_enrich`` (it holds the deadline at the moment
        of intake: a repeated poll only moves ``tenders_normalized.deadline_at``, and does not touch raw).
        The rule is the same as for a repeated poll (:func:`repoll_decision`):
        the fresh value from the card, and if there is none — the current one from ``tenders_normalized``.
        A rollback or zeroing of the deadline by a recheck is excluded.

        The check marker ALWAYS moves (including on "no changes"): otherwise a tender without a
        list would occupy the first places of every pass. Changed documents go through the SAME ``_process_raw``
        as a regular intake: raw hash → content UPDATE
        → ``last_changed_at`` → NOTIFY → diff into ``tender_changes``.
        """
        updated = False
        async with self._session_factory() as session:
            if docs is not None and (docs.index or docs.text):
                stored = (
                    await session.execute(
                        _RAW_PAYLOAD_LOOKUP_SQL,
                        {"source_key": source_key, "source_uid": uid},
                    )
                ).first()
                current = (
                    await session.execute(
                        _NORM_LOOKUP_SQL,
                        {"source_key": source_key, "source_uid": uid},
                    )
                ).first()
                if stored is not None and current is not None:
                    deadline, status, _ = repoll_decision(
                        {
                            "deadline_at": current.deadline_at,
                            "published_at": current.published_at,
                            "status": current.status,
                        },
                        card or {},
                    )
                    payload = dict(stored.raw_payload or {})
                    enrich = dict(payload.get("_enrich") or {})
                    if docs.text:
                        enrich["documents_text"] = docs.text
                    else:
                        enrich.pop("documents_text", None)
                    enrich["documents_index"] = docs.index
                    payload["_enrich"] = enrich
                    outcome, _ = await self._process_raw(
                        session,
                        adapter,
                        RawTender(
                            source_key=source_key,
                            payload=payload,
                            country_code="RS",
                            fetched_at=now,
                        ),
                        keep={"deadline_at": deadline, "status": status},
                    )
                    updated = outcome == "updated"
            await session.execute(
                _DOCS_CHECK_MARK_SQL, {"tender_id": str(tender_id), "now": now}
            )
            await session.commit()
        return updated

    async def _maybe_collect_plans(
        self, adapter: SourceAdapter, source_key: str
    ) -> None:
        """The collection of the jnportal procurement plans — only under the flag and only for jnportal.

        The flag ``INGEST_JNPORTAL_PLANS`` (default OFF, :func:`plans_enabled`). A collection failure
        does NOT break the tender pass (another entity, its own cursor): a warning is written
        with the TYPE of the exception (the text may carry a URL with a token); the next pass will continue
        from the same cursor.
        """
        if not plans_enabled() or source_key != "jnportal":
            return
        try:
            await collect_plans(adapter, self._session_factory)
        except Exception as exc:  # noqa: BLE001 — the collection of plans does not break the tender pass
            logger.warning(
                "ingest[%s]: the collection of plans failed — %s",
                source_key,
                _format_fail_reason(exc),
            )

    async def _maybe_repoll(
        self, adapter: SourceAdapter, source_key: str
    ) -> str | None:
        """The repeated poll of the open tenders — only under the flag and only for the adapter that has it.

        Return: ``None`` — the poll passed (or is off); otherwise the reason of the failure
        (``"HTTP 429"`` / the exception name, see :func:`_format_fail_reason`).
        The failure is NOT swallowed: the caller counts it as a broken pass — the same
        streak of ``error_count``, the same interval degradation and the same alert as the
        main pass. Otherwise a 429 on the poll would silently spin every 5 minutes.

        The exception TYPE, not the text: the text may carry a URL with ``accessToken``
        (see ``src/log_secrets.py``).
        """
        if not repoll_enabled() or not isinstance(adapter, _RepollAdapter):
            return None
        try:
            checked, changed = await self._repoll_open(adapter, source_key)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — the failure of the poll is visible as a broken pass
            reason = _format_fail_reason(exc)
            logger.warning("ingest[%s]: repoll failed — %s", source_key, reason)
            return reason
        logger.info(
            "ingest[%s]: repoll — checked %d, deadline/status changed %d",
            source_key,
            checked,
            changed,
        )
        return None

    async def _repoll_open(
        self, adapter: _RepollAdapter, source_key: str
    ) -> tuple[int, int]:
        """Select the due tenders, poll the cards, write the changes. Return: (checked, changed).

        ‼️ The DB is not held open for the time of the network: the selection is one short
        transaction, each write is its own (see ``_repoll_apply``).
        """
        now = self._clock()
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        _REPOLL_CANDIDATES_SQL, {"source_key": source_key, "now": now}
                    )
                )
                .mappings()
                .all()
            )
        due = select_due([dict(r) for r in rows], now=now, limit=repoll_max_per_pass())
        if not due:
            return 0, 0
        by_uid = {str(r["source_uid"]): r for r in due}
        checked = changed = 0
        async for uid, card in adapter.repoll_cards(list(by_uid)):
            checked += 1
            if await self._repoll_apply(source_key, uid, by_uid[uid], card, now):
                changed += 1
        return checked, changed

    async def _repoll_apply(
        self,
        source_key: str,
        uid: str,
        row: Any,
        card: dict[str, Any],
        now: datetime,
    ) -> bool:
        """Write the result of the poll of one tender. Return: whether the deadline/status changed.

        An empty value from the card does not overwrite the stored one (a deadline/status “not
        received” ≠ “removed”). The check mark always moves — otherwise a tender without
        data in the card would take the first places in each pass.
        """
        deadline, status, changed = repoll_decision(row, card)
        async with self._session_factory() as session:
            if changed:
                await session.execute(
                    _REPOLL_UPDATE_NORM_SQL,
                    {
                        "source_key": source_key,
                        "source_uid": uid,
                        "deadline_at": deadline,
                        "status": status,
                        "now": now,
                    },
                )
                if deadline != row["deadline_at"] and row.get("tender_id"):
                    await self._record_change(
                        session,
                        row["tender_id"],
                        "deadline",
                        build_change(
                            {"deadline_at": row["deadline_at"]},
                            {"deadline_at": deadline},
                        ),
                    )
                logger.info(
                    "repoll[%s] %s: deadline %s → %s, status %s → %s",
                    source_key,
                    uid,
                    row["deadline_at"],
                    deadline,
                    row["status"],
                    status,
                )
            await session.execute(
                _REPOLL_TOUCH_RAW_SQL,
                {"source_key": source_key, "source_uid": uid, "now": now},
            )
            await session.commit()
        return changed

    async def _load_runtime(
        self, session: Any, adapter: SourceAdapter
    ) -> tuple[datetime | None, int, bool | None]:
        """The ``sources`` row: since, the polling interval (thinned out on a persistent
        failure — see :func:`_degrade_interval`), enabled.

        since = ``last_run_at``; on the first pass —
        ``enabled_at - adapter.initial_lookback`` (SRC-0a, R4). ``None`` — only if there is neither of the two: the caller does NOT
        run such a pass (fail-closed), rather than treating it as “the whole archive”.
        """
        row = (
            await session.execute(
                _SOURCE_RUNTIME_SQL, {"source_key": adapter.source_key}
            )
        ).first()
        if row is None:
            # the source is not in the DB — enabled=None, the caller will skip it (fail-closed)
            return None, adapter.default_polling_interval, None
        base_interval = int(
            row.polling_interval_sec or adapter.default_polling_interval
        )
        error_count = getattr(row, "error_count", None) or 0
        interval = _degrade_interval(base_interval, error_count)
        since = row.last_run_at
        enabled_at = getattr(row, "enabled_at", None)
        if since is None and enabled_at is not None:
            since = enabled_at - adapter.initial_lookback
        return since, interval, row.enabled

    async def _ingest(
        self, adapter: SourceAdapter, source_key: str, since: datetime | None
    ) -> SourceResult:
        """Runs ``fetch_batch`` and writes each tender. Does not throw outwards."""
        inserted = updated = touched = skipped = errors = 0
        not_actual = 0
        # Observability (REV-07): the counter of httpx requests FOR THE WHOLE pass (for the
        # rate/429/peak — Indicator 1), the completeness of the enrichment (Indicator 2,
        # only inserted/updated — touched/skipped carry no new content),
        # the request budget per card (Test C). The handler parses the ALREADY ready
        # log line — adapter.py/documents_fetch.py are not touched (the boundary
        # of the brief, parallel to claude/jnportal-throttle).
        completeness = CompletenessCounts()
        budget_over_count = 0
        budget_max = 0
        # The state of the pass (STATE §1/§2, tmp/TASK_ingest_state.md): the uids
        # unfetched because of 429 in the PREVIOUS passes — we hand them to the adapter FIRST
        # (priority_uids), we remove them from the set by the fact of successful handover (below),
        # a 429 again in this pass — we add them anew (on_rate_limited_skip).
        pending_before = await self._pending.get(source_key)
        pending_initial = len(pending_before)
        pending_remaining = set(pending_before)
        newly_rate_limited: set[str] = set()
        pending_settled = 0
        with count_requests() as request_counter:
            async with self._session_factory() as session:
                # ‼️ THE INVARIANT OF THE SET (production fact 22.09.2026). pending_enrich
                # contains exactly those uids for which WE HAVE NO enriched
                # data (`tenders_raw.raw_payload` without the key `_enrich`).
                # The set is not a journal of attempts: as soon as the fact “the data is there” is
                # established, the uid must leave, whoever put it there.
                # Until this reconciliation the uid was removed ONLY by the fact of handover (yield)
                # by the adapter, therefore seven uids, enriched as early as 11.09, hung in
                # set for 73 passes in a row (“was 7, fetched 0, remaining 7”) and
                # every 30 minutes cost an extra request to the portal, which we ourselves
                # are relieving. The portal does not return their list record at all, so
                # neither a yield nor a probe of unchangedness reaches them — they can be
                # removed only by reconciliation with our own DB.
                if pending_before:
                    settled = await self._settle_pending_by_db(
                        session, source_key, pending_before
                    )
                    if settled:
                        for uid in sorted(settled):
                            await self._pending.discard(source_key, uid)
                        pending_settled = len(settled)
                        pending_remaining -= settled
                        pending_before = [
                            uid for uid in pending_before if uid not in settled
                        ]

                # An optional channel “this tender is already known and its list part
                # has not changed?” — the adapter asks it BEFORE the enrichment (network
                # requests for the card/lots/plan), if it implements its own
                # fetch_batch with getattr(self, "is_unchanged_probe", None). The base
                # (SourceAdapter, src/parsers/base.py) and the other adapters do not read the attribute —
                # setting it is harmless to them. Read-only,
                # the probe writes nothing to the DB.
                async def _is_unchanged(
                    source_uid: str, list_record: dict[str, Any]
                ) -> bool:
                    # ‼️ For a uid that remained in pending AFTER the reconciliation with the DB,
                    # the answer is always “changed”. It got into the set exactly
                    # because there is no enrichment for it, and the reconciliation above
                    # confirmed this: “the list part is the same” is not an argument for it —
                    # the enrichment was not done for it at all, and a short circuit
                    # of the probe would forever deprive its card (and would keep the uid
                    # in the set until the TTL expires).
                    if source_uid in pending_remaining:
                        return False
                    return await self._list_part_unchanged(
                        session, source_key, source_uid, list_record
                    )

                async def _on_rate_limited_skip(source_uid: str) -> None:
                    newly_rate_limited.add(source_uid)

                # The attributes are not declared on SourceAdapter (the base is deliberately not
                # touched, see the docstring above) — mypy attr-defined is expected, we
                # ignore it locally. The other adapters do not read them — harmless.
                adapter.is_unchanged_probe = _is_unchanged  # type: ignore[attr-defined]
                adapter.on_rate_limited_skip = (  # type: ignore[attr-defined]
                    _on_rate_limited_skip
                )
                if pending_before:
                    adapter.priority_uids = pending_before  # type: ignore[attr-defined]
                # A manual iteration (instead of `async for`) — for the sake of Test C: between
                # two yields the adapter makes ALL the network requests for ONE
                # tender, therefore the delta of request_counter.total BEFORE/AFTER
                # __anext__() IS the cost of the card (without editing adapter.py).
                batch_iter = adapter.fetch_batch(since).__aiter__()
                try:
                    while True:
                        requests_before = request_counter.total
                        pass_scoped_before = request_counter.pass_scoped
                        documents_before = request_counter.documents
                        try:
                            raw = await batch_iter.__anext__()
                        except StopAsyncIteration:
                            break
                        # ‼️ The price of the CARD — without the requests of the pass and of the list page:
                        # they fall into this same measurement window (the delta
                        # between two yields), but have nothing to do with the card
                        # and inflated its price (fact 22.09.2026 — two extra
                        # warnings out of 151).
                        per_tender_requests = (
                            request_counter.total - requests_before
                        ) - (request_counter.pass_scoped - pass_scoped_before)
                        per_tender_documents = (
                            request_counter.documents - documents_before
                        )
                        source_uid = (adapter.build_source_uid(raw) or "").strip()
                        if source_uid in pending_remaining:
                            pending_remaining.discard(source_uid)
                        try:
                            outcome, normalized = await self._process_raw(
                                session, adapter, raw
                            )
                            await session.commit()
                        except Exception:  # noqa: BLE001 — one line does not break the pass
                            with contextlib.suppress(Exception):
                                await session.rollback()
                            errors += 1
                            logger.exception(
                                "ingest: skipping a broken tender in %s", source_key
                            )
                            continue
                        if outcome == "inserted":
                            inserted += 1
                        elif outcome == "updated":
                            updated += 1
                        elif outcome == "touched":
                            touched += 1
                        elif outcome == "skipped_not_actual":
                            not_actual += 1
                        else:
                            skipped += 1
                        if (
                            outcome in ("inserted", "updated")
                            and normalized is not None
                        ):
                            completeness.add(
                                amount_original=normalized.amount_original,
                                amount_eur=normalized.amount_eur,
                                deadline_at=normalized.deadline_at,
                                description=normalized.description,
                            )
                        if source_uid:
                            budget_msg = check_tender_budget(
                                source_uid, per_tender_requests, per_tender_documents
                            )
                            budget_max = max(budget_max, per_tender_requests)
                            if budget_msg:
                                logger.warning(budget_msg)
                                budget_over_count += 1
                except Exception as exc:  # noqa: BLE001 — network/parsing → broken
                    logger.exception(
                        "ingest: fetch_batch failure of source %s", source_key
                    )
                    await self._update_pending_state(
                        source_key,
                        pending_before,
                        pending_remaining,
                        newly_rate_limited,
                        initial=pending_initial,
                        settled=pending_settled,
                    )
                    return SourceResult(
                        source_key,
                        inserted,
                        updated,
                        touched,
                        skipped,
                        errors,
                        skipped_not_actual=not_actual,
                        failed=True,
                        fail_reason=_format_fail_reason(exc),
                        request_stats=PassRequestStats.from_handler(request_counter),
                        completeness=completeness,
                        budget_over_count=budget_over_count,
                        budget_max=budget_max,
                    )
                finally:
                    adapter.is_unchanged_probe = None  # type: ignore[attr-defined]
                    adapter.on_rate_limited_skip = None  # type: ignore[attr-defined]
                    adapter.priority_uids = None  # type: ignore[attr-defined]
        await self._update_pending_state(
            source_key,
            pending_before,
            pending_remaining,
            newly_rate_limited,
            initial=pending_initial,
            settled=pending_settled,
        )
        return SourceResult(
            source_key,
            inserted,
            updated,
            touched,
            skipped,
            errors,
            skipped_not_actual=not_actual,
            request_stats=PassRequestStats.from_handler(request_counter),
            completeness=completeness,
            budget_over_count=budget_over_count,
            budget_max=budget_max,
        )

    async def _settle_pending_by_db(
        self, session: Any, source_key: str, uids: list[str]
    ) -> set[str]:
        """Which of the ``uids`` are ALREADY enriched in our DB (the key ``_enrich``).

        This is the check of the set’s invariant by a FACT: the set describes “there is no
        data”, not “an attempt was made”. Read-only. A failure of the request does not break the pass
        — then no uid is removed (the behavior as before the reconciliation), but the WARNING
        stays in the log: a silent degradation of the reconciliation hides the same loop
        for which it was written.
        """
        try:
            rows = (
                await session.execute(
                    _RAW_ENRICHED_UIDS_SQL,
                    {"source_key": source_key, "uids": list(uids)},
                )
            ).all()
        except Exception:  # noqa: BLE001 — the state reconciliation does not break the pass
            with contextlib.suppress(Exception):
                await session.rollback()
            logger.warning(
                "ingest[%s]: the reconciliation of pending_enrich with the DB failed — the set "
                "stays as it is",
                source_key,
                exc_info=True,
            )
            return set()
        return {str(row.source_uid) for row in rows}

    async def _update_pending_state(
        self,
        source_key: str,
        pending_before: list[str],
        pending_remaining: set[str],
        newly_rate_limited: set[str],
        *,
        initial: int,
        settled: int,
    ) -> None:
        """Step 3 (STATE): remove the fetched ones, add the new ones, ONE INFO line.

        Called once at the end of the pass (success OR a broken pass) —
        the successfully handed over (yield) tenders remain fetched, regardless of
        whether the pass fell later on another tender.

        ``initial``/``settled`` — the size of the set BEFORE the reconciliation with the DB and the number of uids removed by it (see ``_settle_pending_by_db``): ``pending_before`` comes here already without them, and the log needs both numbers, otherwise “was” would silently decrease and the reconciliation would become invisible.
        """
        for uid in set(pending_before) - pending_remaining:
            await self._pending.discard(source_key, uid)
        if newly_rate_limited:
            await self._pending.add(source_key, newly_rate_limited)
        if not initial and not newly_rate_limited:
            return
        final_pending = await self._pending.get(source_key)
        logger.info(
            "ingest: %s pending_enrich — was %d, removed by the DB fact %d, "
            "fetched %d, remaining %d",
            source_key,
            initial,
            settled,
            len(set(pending_before) - pending_remaining),
            len(final_pending),
        )

    async def _list_part_unchanged(
        self,
        session: Any,
        source_key: str,
        source_uid: str,
        list_record: dict[str, Any],
    ) -> bool:
        """``source_uid`` is already known and its list part has not changed?

        “list part” — the stored ``tenders_raw.raw_payload`` WITHOUT the key
        ``_enrich`` (it is added by the adapter ONLY AFTER the enrichment, and in the new
        list record it is not there yet). The comparison — by the same ``compute_raw_hash`` as
        the main dedup in ``_process_raw`` (the same canonicalisation contract).
        An unknown source_uid → False (nothing to compare with). Read-only —
        it writes nothing to the DB.
        """
        row = (
            await session.execute(
                _RAW_PAYLOAD_LOOKUP_SQL,
                {"source_key": source_key, "source_uid": source_uid},
            )
        ).first()
        if row is None:
            return False
        stored = dict(row.raw_payload or {})
        stored.pop("_enrich", None)
        seen_corrections = stored.pop(CORRECTIONS_SEEN_KEY, None) or []
        correction = list_record.get(CORRECTION_KEY)
        if correction is not None:
            # Fix: "not changed" = its Id has already been accepted (otherwise it, living in the window
            # of the poll, would re-download the card on EVERY pass).
            return correction.get("notice_id") in seen_corrections
        return compute_raw_hash(stored) == compute_raw_hash(list_record)

    async def _process_raw(
        self,
        session: Any,
        adapter: SourceAdapter,
        raw: RawTender,
        keep: dict[str, Any] | None = None,
    ) -> tuple[str, NormalizedTender | None]:
        """Upsert of one tender into raw+normalized. Returns ``(outcome, normalized)``.

        Outcomes: ``“inserted”`` / ``“updated”`` / ``“touched”`` / ``“skipped”`` /
        ``“skipped_not_actual”``. ``normalized`` — ``None`` for both
        ``skipped*``, otherwise the object for measuring the completeness of the enrichment (REV-07, the caller).

        ‼️ ``“skipped_not_actual”`` — a skip BY POLICY (SRC-0a, R4: a new source loads only the actual), and NOT by a failure. The invariant of
        ``.claude/rules/matching-delivery.md`` “a skip is safe only if the cursor will not move” is about a write that we WANTED to save and could not (it must be fetched again); here the write is not needed by the owner’s decision, and the moved cursor loses nothing. The policy applies only to a tender NEW to us (there is no ``tenders_raw`` row): an already known tender that became closed is updated as before. Nothing is written — neither raw nor normalized: the next pass will see the tender again and will skip it again.
        """
        source_key = adapter.source_key
        source_uid = (adapter.build_source_uid(raw) or "").strip()
        if not source_uid:
            # without a stable UID it cannot be deduplicated — we skip (not an error)
            logger.warning("ingest: empty source_uid in %s — skipping", source_key)
            return "skipped", None

        payload = raw.payload or {}
        fetched_at = raw.fetched_at or self._clock()

        raw_row = (
            await session.execute(
                _RAW_LOOKUP_SQL, {"source_key": source_key, "source_uid": source_uid}
            )
        ).first()

        # Correction (F14, DocumentTypeId 9) is NOT a new tender, but a signal that "the card
        # and the documents changed". Its list record (its own Id/PublishDate/type) must NOT
        # overwrite the saved record of the notice — otherwise
        # published_at would shift, and the next pass with the original record would roll it back.
        # Known tender: we keep the saved list part, we take a fresh
        # ``_enrich``. Unknown (we did not see the notice — outside the window, before
        # the source started): the card by TenderId still returns the current
        # state in full, so we accept it as a regular new tender.
        correction = payload.get(CORRECTION_KEY)
        note: str | None = None
        if correction is not None:
            payload = {k: v for k, v in payload.items() if k != CORRECTION_KEY}
            raw = dataclasses.replace(raw, payload=payload)
            note = (correction or {}).get("note")
            notice_id = (correction or {}).get("notice_id")
            if raw_row is None:
                payload[CORRECTIONS_SEEN_KEY] = [notice_id]
                raw = dataclasses.replace(raw, payload=payload)
            else:
                stored = (
                    await session.execute(
                        _RAW_PAYLOAD_LOOKUP_SQL,
                        {"source_key": source_key, "source_uid": source_uid},
                    )
                ).first()
                base = dict(stored.raw_payload or {}) if stored else {}
                if not payload.get("_enrich"):
                    # enrichment failed — nothing to update with, we leave the saved one untouched
                    return "skipped", None
                base["_enrich"] = payload["_enrich"]
                base[CORRECTIONS_SEEN_KEY] = sorted(
                    {*(base.get(CORRECTIONS_SEEN_KEY) or []), notice_id}, key=str
                )
                payload = base
                raw = dataclasses.replace(raw, payload=payload)
        raw_hash = compute_raw_hash(payload)

        # the payload has not changed since last time → we touch nothing (notify is silent)
        if raw_row is not None and raw_row.raw_hash == raw_hash:
            return "skipped", None

        # normalize is a pure function (no network/DB), so it can be called BEFORE
        # the raw write: an exception rolls back the pass over the tender, the same as before.
        normalized = adapter.normalize(raw)
        if keep:
            # document recheck: deadline/status are decided outside (see
            # _apply_documents_recheck), the saved raw does not know them
            normalized = dataclasses.replace(normalized, **keep)
        if (
            raw_row is None
            and getattr(adapter, "insert_only_actual", True)
            and not _is_actual(normalized, self._clock())
        ):
            return "skipped_not_actual", None

        raw_params = {
            "country_code": raw.country_code,
            "source_key": source_key,
            "source_uid": source_uid,
            "raw_payload": _canonical_json(payload),
            "raw_hash": raw_hash,
            "fetched_at": fetched_at,
        }
        if raw_row is None:
            inserted = (await session.execute(_RAW_INSERT_SQL, raw_params)).first()
            tenders_raw_id = str(inserted.id)
        else:
            await session.execute(_RAW_UPDATE_SQL, {**raw_params, "id": raw_row.id})
            tenders_raw_id = str(raw_row.id)

        outcome = await self._write_normalized(
            session, normalized, source_key, source_uid, tenders_raw_id, note=note
        )
        if outcome in ("inserted", "updated"):
            await self._link_cross_source(session, adapter, normalized, payload)
        return outcome, normalized

    async def _link_cross_source(
        self,
        session: Any,
        adapter: SourceAdapter,
        normalized: NormalizedTender,
        payload: dict[str, Any],
    ) -> None:
        """The duplicate mark TED↔jnportal (:mod:`src.dedup.linker`) — in THE SAME
        transaction as the row write: routing will see the link from the first NOTIFY.

        Under a SAVEPOINT: a failure of the link rolls back only it, the tender is saved
        (without a link it is merely not marked; a lost row with a moved
        cursor would never come back — matching-delivery.md).
        """
        await session.execute(_LINK_SAVEPOINT_SQL)
        try:
            await link_cross_source(
                session,
                normalized,
                own_name_variants=adapter.customer_name_variants(payload),
                name_variants=self._name_variants,
            )
        except Exception as exc:  # noqa: BLE001 — the mark must not break the tender
            await session.execute(_LINK_ROLLBACK_SQL)
            logger.warning(
                "dedup: link %s/%s skipped (%s)",
                normalized.source_key,
                normalized.source_uid,
                type(exc).__name__,
            )
            return
        await session.execute(_LINK_RELEASE_SQL)

    def _name_variants(self, source_key: str, payload: dict[str, Any]) -> list[str]:
        """The customer names of the candidate row of another source (its adapter)."""
        try:
            return list(
                self._adapter_factory(source_key).customer_name_variants(payload)
            )
        except Exception:  # noqa: BLE001 — no adapter → customer_name is enough
            return []

    async def _record_change(
        self, session: Any, tender_id: Any, kind: str, diff: dict[str, Any] | None
    ) -> bool:
        """Write the diff into ``tender_changes`` (an empty diff is not written).

        Under SAVEPOINT: the journal is secondary, the tender matters more. A write failure is logged
        by the error class (without text: it may contain tender data).
        """
        if not diff:
            return False
        await session.execute(_CHANGE_SAVEPOINT_SQL)
        try:
            await session.execute(
                _CHANGE_INSERT_SQL,
                {
                    "tender_id": str(tender_id),
                    "kind": kind,
                    "diff": _canonical_json(diff),
                },
            )
        except Exception as exc:  # noqa: BLE001 — the journal must not drop the tender
            await session.execute(_CHANGE_ROLLBACK_SQL)
            logger.warning("changes: diff write skipped (%s)", type(exc).__name__)
            return False
        await session.execute(_CHANGE_RELEASE_SQL)
        return True

    async def _write_normalized(
        self,
        session: Any,
        normalized: NormalizedTender,
        source_key: str,
        source_uid: str,
        tenders_raw_id: str,
        note: str | None = None,
    ) -> str:
        """Upsert ``tenders_normalized``: INSERT / content-UPDATE / touch."""
        now = self._clock()
        fields = _normalized_to_fields(normalized)
        new_fp = _fingerprint_fields(fields)

        norm_row = (
            await session.execute(
                _NORM_LOOKUP_SQL,
                {"source_key": source_key, "source_uid": source_uid},
            )
        ).first()

        # The JSONB fields are serialized as a string (CAST ... AS jsonb in the SQL).
        json_fields = {
            "classifications": _canonical_json(fields["classifications"]),
            "documents": _canonical_json(fields["documents"]),
        }
        scalar_fields = {k: fields[k] for k in fields if k not in json_fields}

        if norm_row is None:
            await session.execute(
                _NORM_INSERT_SQL,
                {
                    **scalar_fields,
                    **json_fields,
                    "country_code": normalized.country_code,
                    "tenders_raw_id": tenders_raw_id,
                    "source_key": source_key,
                    "source_uid": source_uid,
                    "now": now,
                },
            )
            return "inserted"

        old_fp = _fingerprint_fields(_row_to_fields(norm_row))
        if old_fp == new_fp:
            # the content is the same, the payload changed service-wise → only last_seen_at.
            # One extra notify(UPDATE) is acceptable — see the TRIGGER DECISION.
            await session.execute(_NORM_TOUCH_SQL, {"id": norm_row.id, "now": now})
            return "touched"

        await session.execute(
            _NORM_UPDATE_CONTENT_SQL,
            {
                **scalar_fields,
                **json_fields,
                "tenders_raw_id": tenders_raw_id,
                "id": norm_row.id,
                "now": now,
            },
        )
        await self._record_change(
            session,
            norm_row.id,
            "content",
            build_change(_row_to_fields(norm_row), fields, note=note),
        )
        return "updated"

    async def _sleep(self, delay: float) -> None:
        """An interruptible sleep: reacts to stop() while waiting."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the ingest worker (phase 1: the container via Compose, T2)."""
    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()
    # the import of the adapters registers them in the registry (@register_source)
    # The adapter registry — an explicit list (SRC-0a); whether to load a source is decided
    # by the table sources (source_keys), not by the import.
    load_all_adapters()

    # Redis for PendingEnrichStore (STATE) — the same REDIS_URL as routing.py
    # (§ ARCHITECTURE, already passed into the ingest container). Unavailable/not set →
    # redis=None → PendingEnrichStore no-op, ingest works as before.
    redis_client = None
    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0")
        )
    except Exception:  # noqa: BLE001 — the state is optional for ingest working
        logger.warning("ingest: Redis unavailable at start-up — pending_enrich is off")

    # Automatic publication of news /novosti to @nabrs_news (owner 16.10): behind a flag, default OFF.
    news_task = None
    if autopost_enabled():
        news_task = asyncio.create_task(news_run_forever())

    worker = IngestWorker(redis=redis_client)
    try:
        await worker.run()
    finally:
        if news_task is not None:
            news_task.cancel()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
