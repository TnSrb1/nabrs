"""AI Worker — analysis of the specification from ``delivery_outbox`` (the pipeline ``ai_pending → ai_done``).

ARCHITECTURE §8 (outbox AI fail path), §13.3 (routing writes the outbox), §14
(AI pipeline: §14.2 L1, §14.3 L2, §14.5 limits, §14.7 failover), ADR-024/029.
DB_SCHEMA §2.14 (delivery_outbox), §2.16 (tender_ai_analyses,
UNIQUE(tender_id,profile_id,level)), §2.17 (ai_usage).

The pipeline (Sprint-18; L1 quick + L2 standard are implemented; L3 deep — on demand
via a button §14.4, auto-routing of deep is protected by the NotImplementedError circuit breaker):

1. **poll** — every ``poll_interval`` atomically takes a batch of outbox rows
   ``status='ai_pending'`` into the transit status ``analyzing`` through
   ``FOR UPDATE SKIP LOCKED`` (as in delivery — the status is a free TEXT without CHECK,
   no migration is needed; see 0001 `delivery_outbox.status`).
2. **analyze** — for each row: the only level admissible on the auto path is
   ``standard`` (L2, see ``_VALID_LEVELS``), the gate “freeze → status/plan →
   limit” (:func:`src.ai.usage.auto_limit_decision`, §14.5), the cache
   ``tender_ai_analyses`` by UNIQUE(tender_id, profile_id, level); on a miss it
   calls :mod:`src.ai`. Any refusal — a degradation into delivery WITHOUT analysis.
3. **finalize** — INSERT ``tender_ai_analyses`` (``model`` = the slug at the moment
   of the call, ADR-029), UPSERT ``ai_usage``, the outbox moves to ``ai_done`` +
   ``ai_analysis_id``.

The shared helpers (limit, cache, finalize) are in :mod:`src.ai.usage`; they are
reused here, not duplicated.

Parameterisation of the prompt (§14.3, ADR-033): ``{{country_name}}`` is taken from
``countries.name`` by ``tenants.country_code``; ``target_language`` — from
``profiles.target_language``. No hardcoded country.

Reliability and the review focus of the task:

1. **Idempotency / cache.** A level analysis is reused via the cache by
   UNIQUE(tender_id, profile_id, level): L1 — ``profile_id IS NULL`` (shared by
   all tenants on the tender), L2 — per profile. A cache hit does not call the model and does NOT increment
   ``ai_usage`` (no call — no spend). Taking a batch is atomic
   (``SKIP LOCKED`` + the transit ``analyzing``); hung ``analyzing`` rows (a crash before finalisation) are revived at start-up by :meth:`recover_stale` → ``ai_pending``.
2. **Gate of the paid call (§14.5).** Before the model call — three axes in a row:
   freeze (``access_until``, :func:`src.domain.access.is_frozen`) → the tenant's
   status and the plan limit (:func:`src.ai.usage.auto_limit_decision`: ``active``
   — the monthly limit of the level, ``trial`` — the demo quota
   :data:`~src.ai.usage.TRIAL_AUTO_DEMO_LIMIT` for the whole trial, other statuses —
   refusal). Soft 80% → a log warning and pass. Any refusal → **degradation**
   into ``pending``+``requires_ai=false`` (the tender is delivered without analysis), NOT
   ``ai_rejected``: see :meth:`AiWorker._degrade_to_plain`. The profile form is not
   the last line: a POST can be sent by hand, so the gate stands here.
3. **Failover (§14.7).** Retries 30s/2m/10m on 429/5xx — inside :mod:`src.ai`.
   After exhaustion (:class:`AiRetryableError`) the row returns to
   ``ai_pending`` (NOT ``ai_done``), logged in DLQ style; delivery will not take it
   (§8 AI fail path). This mirrors the retryable path of delivery
   (a retry on the next cycle; the infrastructure may recover).

DEVIATIONS (per the task — the decisions are recorded here):

- **Transit/terminal statuses** ``analyzing`` / ``ai_rejected`` — a free TEXT
  without CHECK (as delivery ``delivering``/``failed``), no migration.
- **A deterministic failure of the model's response** (``AiResponseError``: invalid
  JSON/schema) or another unexpected error → a **terminal** ``ai_rejected``,
  NOT a revert. The divergence from delivery (there unexpected → retryable revert)
  is deliberate: a model call costs money, and a deterministic failure on revert would give an endless paid cycle (against the spirit of ADR-014). Only the infrastructure
  429/5xx (``AiRetryableError``) return to ``ai_pending``.
- **Only ``standard`` lives in the auto path** (15.08.2026, owner decision): ``quick``
  yields service metadata that no template renders, ``deep`` — a circuit breaker
  (``profiles.ai_analysis_level='deep'`` is not expected in delivery_outbox). Both degrade into delivery without analysis BEFORE the limit and the cache.
  The on-demand L3 “by button” is handled by
  :mod:`src.bots.deep_analysis_service` (§14.4).
- **A race on UNIQUE(tender_id,profile_id,level)** (several channels of one
  profile taken in parallel): on an ``IntegrityError`` on the INSERT we re-read
  the cache and finalise ``ai_done`` with the found id (we do not lose the work).
  For L1 (``profile_id IS NULL``) UNIQUE does not deduplicate NULLs — we protect ourselves
  with the cache lookup before the call; a rare L1 duplicate is acceptable (TODO).

The dependencies are injected (the AI client, the session factory, the batch size, the interval,
the limits) — for unit tests with a mocked DB and a fake AI client.

‼️ **The role contour — verified by action on 15.08.2026, not inferred from the docstring**
(in the project the docstring about the role had already been wrong for nine weeks, CLAUDE.md). In the container
``deploy-ai-1`` ``DATABASE_URL`` points to the role ``tendersrb``, which has ``rolsuper=t`` and ``rolbypassrls=t`` — RLS does not touch it at all, and ``set_tenant`` is not needed here (a run in the container: ``current_user='tendersrb'``,
``SELECT count(*) FROM ai_usage`` returned rows with an empty ``app.current_tenant_id``). The wording “the owner of the RLS tables bypasses it” would be WRONG: ``ai_usage`` is under **FORCE** RLS, and FORCE applies to the owner too —
it is the BYPASSRLS attribute of the role that works. Cross-tenant AI accounting is a server operation (DB_SCHEMA §4). The web has a DIFFERENT contour (``tendersrb_app``, NOBYPASSRLS):
the same ``src.ai.usage`` called from the cabinet must call ``repo.set_tenant`` as its first action.

Not done (separate sprints): PDF download / embeddings; digest;
fallback to OpenAI/Gemini (§14.7); wiring into the Compose service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text

from src.ai import AiClient, AiRetryableError, AnalysisRequest
from src.ai.usage import (
    _TIER_LIMITS,
    auto_limit_decision,
    cache_lookup,
    finalize_with_insert,
    record_error_spend,
)
from src.db.session import session_scope
from src.domain.access import is_frozen

logger = logging.getLogger(__name__)

# The ready-to-enter status and the transit status of taking (DB_SCHEMA §2.14).
_READY_STATUS = "ai_pending"
_CLAIMED_STATUS = "analyzing"
_REJECTED_STATUS = "ai_rejected"  # terminal (delivery will not take it)

# The only level the product admits on the AUTO path (owner decision of
# 15.08.2026, ``src.domain.tiers.AUTO_ANALYSIS_LEVELS``). Everything else from
# profiles.ai_analysis_level — 'none' (must not get into ai_pending,
# routing §6.4), 'quick' (the output is service metadata that no
# single template renders) and 'deep' (forbidden by the circuit breaker §14.4, lives only by
# the button) — degrades into delivery WITHOUT analysis: the tender is not lost.
_VALID_LEVELS = ("standard",)

# Atomic taking of the ai_pending batch into the transit analyzing (SKIP LOCKED — anti-race).
_CLAIM_SQL = text("""
    UPDATE delivery_outbox
    SET status = :claimed
    WHERE id IN (
        SELECT id FROM delivery_outbox
        WHERE status = :ready
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT :batch
    )
    RETURNING id
    """)

# Revival of hung analyzing rows (a crash before finalisation) — at the worker’s start-up.
_RECOVER_SQL = text(
    "UPDATE delivery_outbox SET status = :ready WHERE status = :claimed"
)

# Loading of the jobs: outbox + the profile (level/language/context/questions) + the tenant
# (tier for the limit, country_code → countries.name for {{country_name}}) + the tender.
_LOAD_JOBS_SQL = text("""
    SELECT o.id           AS outbox_id,
           o.tender_id    AS tender_id,
           o.profile_id   AS profile_id,
           p.tenant_id    AS tenant_id,
           p.ai_analysis_level AS ai_level,
           p.target_language   AS target_language,
           p.ai_extra_context  AS extra_context,
           p.ai_focus_questions AS focus_questions,
           t.tier         AS tier,
           t.status       AS status,
           t.access_until AS access_until,
           c.name         AS country_name,
           tn.title       AS title,
           tn.description AS description
    FROM delivery_outbox o
    JOIN profiles p ON p.id = o.profile_id
    JOIN tenants t  ON t.id = p.tenant_id
    LEFT JOIN countries c ON c.code = t.country_code
    JOIN tenders_normalized tn ON tn.id = o.tender_id
    WHERE o.id = ANY(:ids)
    """)

_SET_AI_DONE_SQL = text(
    "UPDATE delivery_outbox SET status='ai_done', ai_analysis_id=:aid WHERE id=:id"
)
_SET_REJECTED_SQL = text(
    "UPDATE delivery_outbox SET status='ai_rejected', failed_at=NOW() WHERE id=:id"
)
_REVERT_PENDING_SQL = text(
    "UPDATE delivery_outbox SET status='ai_pending' WHERE id=:id"
)
# ‼️ Degradation instead of loss: the AI did not come together — we deliver the card WITHOUT analysis.
# `requires_ai=false` is mandatory, otherwise delivery will take the row into the AI path again.
_DEGRADE_PLAIN_SQL = text(
    "UPDATE delivery_outbox SET status='pending', requires_ai=false WHERE id=:id"
)


@dataclass(frozen=True)
class AiJob:
    """The ``delivery_outbox`` row + the profile/tenant/tender fields for the analysis."""

    outbox_id: int
    tender_id: str
    profile_id: str | None
    tenant_id: str | None
    ai_level: str | None
    target_language: str | None
    extra_context: str | None
    focus_questions: Any
    tier: str | None
    # The plan axis (``status``) and the freeze axis (``access_until``) are SEPARATE and
    # both are needed here: the auto-analysis is a paid call without a human action,
    # so the gate “freeze → status/plan → limit” stands in the worker, not
    # only in the form (the form can be forged with a POST).
    status: str | None
    access_until: Any
    country_name: str | None
    title: str | None
    description: str | None


def _focus_list(value: Any) -> list[str]:
    """Normalises ``ai_focus_questions`` (JSONB) to a list of strings."""
    if isinstance(value, list):
        return [str(q) for q in value]
    return []


class AiWorker:
    """The polling AI analysis worker: ``delivery_outbox.ai_pending → ai_done``."""

    def __init__(
        self,
        *,
        ai_client: AiClient,
        session_factory: Callable[[], Any] = session_scope,
        batch_size: int = 10,
        poll_interval: float = 2.0,
        tier_limits: dict[str, dict[str, int | None]] | None = None,
    ) -> None:
        self._ai = ai_client
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._limits = tier_limits or _TIER_LIMITS
        self._stopped = asyncio.Event()

    # -- public API ----------------------------------------------------- #

    def stop(self) -> None:
        """Asks the worker to finish after the current iteration."""
        self._stopped.set()

    async def run(self) -> None:
        """The main cycle: revival of hung rows → polling until stopped."""
        await self.recover_stale()
        logger.info("ai: polling started")
        while not self._stopped.is_set():
            try:
                processed = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a cycle failure does not break the worker
                logger.exception("ai: polling cycle error")
                processed = 0
            if processed < self._batch_size:
                await self._sleep(self._poll_interval)

    async def recover_stale(self) -> int:
        """Returns the hung ``analyzing`` rows to ``ai_pending`` (anti-loss at start-up)."""
        async with self._session_factory() as session:
            result = await session.execute(
                _RECOVER_SQL, {"ready": _READY_STATUS, "claimed": _CLAIMED_STATUS}
            )
            await session.commit()
        count = getattr(result, "rowcount", 0) or 0
        if count:
            logger.info("ai: revived hung rows: %d", count)
        return count

    async def poll_once(self) -> int:
        """One cycle: taking a batch → loading jobs → analysis. Returns the batch size."""
        async with self._session_factory() as session:
            claimed = await self._claim_batch(session)
            await session.commit()
            if not claimed:
                return 0
            jobs = await self._load_jobs(session, claimed)

        loaded_ids = {job.outbox_id for job in jobs}
        for missing in [cid for cid in claimed if cid not in loaded_ids]:
            # claimed without a profile/tender (the routing invariant is broken) — terminal.
            await self._reject(missing, "нет профиля/тендера для job")

        for job in jobs:
            await self._process_one(job)
        return len(claimed)

    # -- internal: taking and loading -------------------------------------- #

    async def _claim_batch(self, session: Any) -> list[int]:
        result = await session.execute(
            _CLAIM_SQL,
            {
                "claimed": _CLAIMED_STATUS,
                "ready": _READY_STATUS,
                "batch": self._batch_size,
            },
        )
        return [row.id for row in result]

    async def _load_jobs(self, session: Any, ids: list[int]) -> list[AiJob]:
        result = await session.execute(_LOAD_JOBS_SQL, {"ids": ids})
        jobs: list[AiJob] = []
        for row in result:
            jobs.append(
                AiJob(
                    outbox_id=row.outbox_id,
                    tender_id=str(row.tender_id),
                    profile_id=(
                        str(row.profile_id) if row.profile_id is not None else None
                    ),
                    tenant_id=(
                        str(row.tenant_id) if row.tenant_id is not None else None
                    ),
                    ai_level=row.ai_level,
                    target_language=row.target_language,
                    extra_context=row.extra_context,
                    focus_questions=row.focus_questions,
                    tier=row.tier,
                    status=row.status,
                    access_until=row.access_until,
                    country_name=row.country_name,
                    title=row.title,
                    description=row.description,
                )
            )
        return jobs

    # -- internal: processing of one row -------------------------------- #

    async def _process_one(self, job: AiJob) -> None:
        """Determines the level, checks the limit/cache, calls the model, finalises."""
        level = (job.ai_level or "").strip()
        if level not in _VALID_LEVELS:
            # A mismatch “requires_ai=true with a level not for AI” is our error,
            # and not a reason to deprive the subscriber of the tender: we give the card without analysis.
            await self._degrade_to_plain(job.outbox_id, f"уровень не для AI: {level!r}")
            return

        # L2 (standard) — analysis per profile (in the cache keyed by profile_id).
        profile_id = job.profile_id

        # 1) freeze (the access_until axis) — FIRST, before the plan and before the limit
        # (src/domain/access.py; the same predicate as in matching STAGE_A_SQL).
        if is_frozen(job.access_until, datetime.now(timezone.utc)):
            await self._degrade_to_plain(
                job.outbox_id, f"доступ заморожен (access_until={job.access_until})"
            )
            return

        # 2) status/plan/limit (§14.5) — before any model call. The auto-analysis
        # is paid by subscription, not by a press, so the gate takes the status into account too:
        # active → the monthly limit of the plan, trial → the demo quota, otherwise — none.
        async with self._session_factory() as session:
            decision = await auto_limit_decision(
                session,
                tenant_id=job.tenant_id,
                status=job.status,
                tier=job.tier,
                level=level,
                tier_limits=self._limits,
            )
        if decision != "ok":
            # We do not lose the tender: the level is unavailable or the limit/demo is exhausted —
            # we give the card without analysis (see _degrade_to_plain). The client sees
            # the reason in the cabinet: the same spend snapshot is read by usage_snapshot.
            await self._degrade_to_plain(
                job.outbox_id,
                f"авто-разбор {level} недоступен "
                f"(status={job.status!r} tier={job.tier!r}): {decision}",
            )
            return

        # 2) cache: we reuse the existing analysis (without a call and without a spend).
        async with self._session_factory() as session:
            cached_id = await cache_lookup(session, job.tender_id, profile_id, level)
        if cached_id is not None:
            await self._finalize_done(job.outbox_id, cached_id)
            logger.info(
                "ai: cache hit outbox=%s level=%s analysis=%s",
                job.outbox_id,
                level,
                cached_id,
            )
            return

        # (the L3 deep circuit breaker is no longer needed as a separate branch: deep does not
        # pass _VALID_LEVELS and degrades BEFORE the limit, the cache and the model call.)

        # 4) the model call (outside the transaction — the network may take seconds).
        request = AnalysisRequest(
            level=level,
            title=job.title,
            description=job.description,
            country_name=job.country_name,
            target_language=job.target_language,
            extra_context=job.extra_context,
            focus_questions=_focus_list(job.focus_questions),
        )
        try:
            result = await self._ai.analyze(request)
        except AiRetryableError as exc:
            # 429/5xx exhausted → the row stays ai_pending (§8 AI fail path).
            await self._revert_pending(job.outbox_id, f"DLQ retryable: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 — a deterministic failure → terminal
            logger.exception("ai: terminal analysis failure outbox=%s", job.outbox_id)
            async with self._session_factory() as session:
                await record_error_spend(
                    session, tenant_id=job.tenant_id, level=level, exc=exc
                )
            await self._reject(job.outbox_id, f"сбой анализа: {exc}")
            return

        # 5) finalisation: INSERT analysis + UPSERT usage + outbox ai_done.
        await self._finalize_with_insert(job, level, profile_id, result)

    # -- internal: finalisation ------------------------------------------- #

    async def _finalize_with_insert(
        self, job: AiJob, level: str, profile_id: str | None, result: Any
    ) -> None:
        """INSERT of the analysis + UPSERT usage + outbox ai_done (one transaction)."""
        async with self._session_factory() as session:
            analysis_id = await finalize_with_insert(
                session,
                tender_id=job.tender_id,
                profile_id=profile_id,
                tenant_id=job.tenant_id,
                level=level,
                result=result,
            )
            if analysis_id is None:
                await self._revert_pending(
                    job.outbox_id, "конфликт UNIQUE без находки кеша"
                )
                return
            await session.execute(
                _SET_AI_DONE_SQL, {"aid": analysis_id, "id": job.outbox_id}
            )
            await session.commit()
        logger.info(
            "ai: ai_done outbox=%s level=%s analysis=%s",
            job.outbox_id,
            level,
            analysis_id,
        )

    async def _finalize_done(self, outbox_id: int, analysis_id: str) -> None:
        """Cache hit: we reuse the analysis and move the outbox to ai_done."""
        async with self._session_factory() as session:
            await session.execute(
                _SET_AI_DONE_SQL, {"aid": analysis_id, "id": outbox_id}
            )
            await session.commit()

    async def _reject(self, outbox_id: int, reason: str) -> None:
        """Terminalises the row (``ai_rejected``): delivery will not take it."""
        async with self._session_factory() as session:
            await session.execute(_SET_REJECTED_SQL, {"id": outbox_id})
            await session.commit()
        logger.warning("ai: rejected outbox=%s — %s", outbox_id, reason)

    async def _degrade_to_plain(self, outbox_id: int, reason: str) -> None:
        """Hands the tender over WITHOUT the AI analysis instead of losing it altogether.

        ‼️ A production measurement 15.08.2026: for the profiles with AI enabled **100%** of the deliveries
        were ``ai_rejected`` — 2018 records in total, not a single ``sent``.
        The client who enabled the AI analysis stopped receiving tenders AT ALL:
        delivery by design takes only ``pending``/``ai_done``, and the unavailability of the level terminalised the row. Switching on a paid feature zeroed the core product — this is worse than the absence of the feature.

        In such a situation the tender is not to blame and remains valid: we give it
        as an ordinary card. There is no paid call here and there was none — the decision
        is taken BEFORE the model is contacted.
        """
        async with self._session_factory() as session:
            await session.execute(_DEGRADE_PLAIN_SQL, {"id": outbox_id})
            await session.commit()
        logger.warning("ai: without analysis outbox=%s — %s", outbox_id, reason)

    async def _revert_pending(self, outbox_id: int, reason: str) -> None:
        """Return of the row to ``ai_pending`` (a retry on the next cycle, §8)."""
        async with self._session_factory() as session:
            await session.execute(_REVERT_PENDING_SQL, {"id": outbox_id})
            await session.commit()
        logger.warning("ai: return to ai_pending outbox=%s — %s", outbox_id, reason)

    async def _sleep(self, delay: float) -> None:
        """An interruptible sleep: reacts to stop() while waiting."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the AI worker (phase 1: the container via Compose)."""
    from src.log_secrets import install_secret_log_filter

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()
    worker = AiWorker(ai_client=AiClient())
    await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
