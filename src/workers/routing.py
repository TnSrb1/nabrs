"""Routing Worker — a LISTEN/NOTIFY loop over the channel ``tender_changed``.

ARCHITECTURE §13.1 / DB_SCHEMA §5.1. The trigger ``notify_tender_change`` and the
trigger ``tenders_normalized_notify`` are created by migration 0001 — here only the
*consumer*: it holds a dedicated ``asyncpg`` connection (LISTEN requires a
separate connection, not one from the ORM pool), on each notification loads the tender and
runs the two-stage matching (:mod:`src.workers.matching`), writing the result
into ``delivery_outbox``.

Reliability (the review focus of the task):

1. **Idempotency.** The write into the outbox is ``ON CONFLICT DO NOTHING`` (§6.4),
   therefore a repeated processing of the tender (a replay, a duplicate NOTIFY) creates no
   duplicates. There is no separate ``routed`` flag in the schema — the idempotency is held
   by the UNIQUE index of the outbox.
2. **The cutoff by the moment of subscription** (SRC-0a) — inside stage A: the quiet first
   pass of the source and ``profiles.source_opt_in`` close at once the NOTIFY,
   UPDATE and replay. The country of the tender does not cut the profile (the gate is the source).
3. **Reconnect without losing tenders.** On a connection break the worker
   reconnects with a backoff and *replays* the missed tenders from the moment
   of the checkpoint (``first_seen_at > checkpoint``). The checkpoint is in Redis; it moves
   as the processing goes. The gap between the break and the reconnect is closed by the replay.

asyncpg calls the listener callback synchronously, therefore the notifications
are put into an ``asyncio.Queue`` and processed in the main loop (so the DB
is accessed from a normal async context, not from the driver's callback).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import text

from src.db.session import session_scope
from src.dedup.cross_source import suppress_duplicates_enabled
from src.workers import matching
from src.workers.matching import TenderView

logger = logging.getLogger(__name__)

CHANNEL = "tender_changed"
CHECKPOINT_KEY = "routing:checkpoint"

# backoff of the reconnect (sec): 1, 2, 5, 10, then hold 30
_RECONNECT_BACKOFF = (1, 2, 5, 10, 30)

# The tender fields needed by the matching (DB_SCHEMA §2.10).
_LOAD_TENDER_SQL = text("""
    SELECT id, country_code, source_key, regions, classifications,
           procedure_type, contract_type, customer_name,
           language, amount_eur, deadline_at, published_at, status,
           title_normalized, description_normalized, first_seen_at,
           canonical_tender_id, canonical_confidence
    FROM tenders_normalized
    WHERE id = :tender_id
    """)

# The replay of the missed tenders at a (re)connect — by the checkpoint.
# There is no `routed` flag in the schema; a repeated processing is safe (the outbox is idempotent).
_REPLAY_SQL = text("""
    SELECT id
    FROM tenders_normalized
    WHERE first_seen_at > :checkpoint
    ORDER BY first_seen_at ASC
    """)


def _asyncpg_dsn(url: str | None = None) -> str:
    """The DSN for ``asyncpg.connect`` from ``DATABASE_URL``.

    ``src.db.session`` uses the SQLAlchemy URL ``postgresql+asyncpg://``;
    the raw driver does not need the ``+asyncpg`` prefix.
    """
    raw = url or os.getenv("DATABASE_URL", "postgresql://localhost/tendersrb")
    return raw.replace("postgresql+asyncpg://", "postgresql://")


class RedisCheckpoint:
    """The processing checkpoint in Redis (ISO-8601 timestamp of ``first_seen_at``)."""

    def __init__(self, redis: Any, key: str = CHECKPOINT_KEY) -> None:
        self._redis = redis
        self._key = key

    async def get(self) -> datetime | None:
        raw = await self._redis.get(self._key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return datetime.fromisoformat(raw)

    async def set(self, value: datetime) -> None:
        await self._redis.set(self._key, value.isoformat())


class RoutingWorker:
    """Listens to ``tender_changed`` and routes the tenders into the outbox.

    The dependencies are injected (DSN, the Redis checkpoint, the session factory, the connect
    function of asyncpg) — for unit tests with a mocked DB.
    """

    def __init__(
        self,
        *,
        dsn: str | None = None,
        checkpoint: RedisCheckpoint | None = None,
        session_factory: Callable[[], Any] = session_scope,
        connect: Callable[[str], Awaitable[Any]] | None = None,
        suppress_duplicates: bool | None = None,
    ) -> None:
        self._dsn = _asyncpg_dsn(dsn)
        # The suppression of the second delivery of the TED↔jnportal duplicate — behind the flag
        # CROSS_SOURCE_DEDUP_MODE (default “mark” = off, the owner’s decision).
        self._suppress_duplicates = (
            suppress_duplicates_enabled()
            if suppress_duplicates is None
            else suppress_duplicates
        )
        self._checkpoint = checkpoint
        self._session_factory = session_factory
        self._connect = connect
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._stopped = asyncio.Event()

    # -- public API ----------------------------------------------------- #

    def stop(self) -> None:
        """Asks the worker to finish after the current iteration."""
        self._stopped.set()

    async def run(self) -> None:
        """The main loop: (re)connect → replay → LISTEN → processing of the queue."""
        if self._connect is None:  # a lazy import, so that tests do not require asyncpg
            import asyncpg

            self._connect = asyncpg.connect

        attempt = 0
        while not self._stopped.is_set():
            conn = None
            try:
                conn = await self._connect(self._dsn)
                attempt = 0
                await conn.add_listener(CHANNEL, self._on_notify)
                logger.info("routing: LISTEN %s set up", CHANNEL)
                await self._replay_since_checkpoint()
                await self._consume()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — any break → reconnect
                delay = _RECONNECT_BACKOFF[min(attempt, len(_RECONNECT_BACKOFF) - 1)]
                attempt += 1
                logger.exception("routing: connection break, reconnect in %ss", delay)
                await self._sleep(delay)
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.remove_listener(CHANNEL, self._on_notify)
                    with contextlib.suppress(Exception):
                        await conn.close()

    # -- internal -------------------------------------------------------- #

    def _on_notify(
        self, _connection: Any, _pid: int, _channel: str, payload: str
    ) -> None:
        """asyncpg-listener (synchronous): puts the payload into the queue."""
        self._queue.put_nowait(payload)

    async def _consume(self) -> None:
        """Processes the notifications from the queue until stopped."""
        while not self._stopped.is_set():
            try:
                payload = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self.route_tender(payload)
            except Exception:  # noqa: BLE001 — one bad record does not break the cycle
                logger.exception("routing: processing error payload=%s", payload)

    async def _replay_since_checkpoint(self) -> None:
        """Replays the tenders with ``first_seen_at > checkpoint`` (anti-loss)."""
        if self._checkpoint is None:
            return
        since = await self._checkpoint.get()
        if since is None:
            return
        async with self._session_factory() as session:
            result = await session.execute(_REPLAY_SQL, {"checkpoint": since})
            ids = [str(row.id) for row in result]
        if ids:
            logger.info(
                "routing: replay %d tenders from %s", len(ids), since.isoformat()
            )
        for tender_id in ids:
            with contextlib.suppress(Exception):
                await self.route_tender(json.dumps({"tender_id": tender_id}))

    async def route_tender(self, payload: str | dict[str, Any]) -> int:
        """Matches one tender and writes the outbox. Returns the number of matched profiles.

        ``payload`` — the JSON from ``pg_notify`` (DB_SCHEMA §5.1) or a dict (replay).
        ``change_kind`` maps to ``match_kind``: INSERT→``new``, UPDATE→
        ``update`` (the latter is deduplicated on the current UNIQUE index of the outbox —
        a full re-routing per §13.4 requires a 4-column index, outside the
        boundaries of Sprint-16, see the PR body).
        """
        data = payload if isinstance(payload, dict) else json.loads(payload)
        tender_id = data.get("tender_id")
        if not tender_id:
            return 0
        match_kind = "update" if data.get("change_kind") == "UPDATE" else "new"

        async with self._session_factory() as session:
            tender = await self._load_tender(session, str(tender_id))
            if tender is None:
                logger.warning("routing: tender %s not found", tender_id)
                return 0

            candidates = await matching.stage_a_candidates(session, tender)
            matched = matching.stage_b_filter(candidates, tender)
            # stage C — a stub (§6.3), an empty list in Sprint-16
            matched += await matching.stage_c_soft_match(session, tender)
            if self._suppress_duplicates:
                matched = await matching.suppress_cluster_duplicates(
                    session, tender, matched
                )

            # The cache of live channels — per pass: the unbinding of a channel is picked up
            # by the next tender, and the candidates of this one share one SELECT.
            live_cache = matching.LiveChannelsCache()
            for candidate in matched:
                await matching.write_outbox(
                    session,
                    candidate=candidate,
                    tender=tender,
                    match_kind=match_kind,
                    live_cache=live_cache,
                )
            await session.commit()

        await self._advance_checkpoint(tender)
        logger.info("routing: tender %s → %d profiles", tender_id, len(matched))
        return len(matched)

    async def _load_tender(self, session: Any, tender_id: str) -> TenderView | None:
        result = await session.execute(_LOAD_TENDER_SQL, {"tender_id": tender_id})
        row = result.first()
        if row is None:
            return None
        self._last_first_seen = row.first_seen_at  # for the checkpoint
        return TenderView(
            id=str(row.id),
            country_code=row.country_code,
            source_key=row.source_key,
            regions=list(row.regions or []),
            classifications=list(row.classifications or []),
            procedure_type=row.procedure_type,
            contract_type=row.contract_type,
            customer_name=row.customer_name,
            language=row.language,
            amount_eur=float(row.amount_eur) if row.amount_eur is not None else None,
            deadline_at=row.deadline_at,
            # getattr: the fake rows of the unit tests are set up without published_at.
            published_at=getattr(row, "published_at", None),
            status=row.status,
            title_normalized=row.title_normalized,
            description_normalized=row.description_normalized,
            first_seen_at=row.first_seen_at,
            # getattr: the fake rows of the unit tests are set up without the columns of 0047.
            canonical_tender_id=(
                str(row.canonical_tender_id)
                if getattr(row, "canonical_tender_id", None)
                else None
            ),
            canonical_confidence=getattr(row, "canonical_confidence", None),
        )

    async def _advance_checkpoint(self, tender: TenderView) -> None:
        """Moves the checkpoint to the ``first_seen_at`` of the processed tender."""
        if self._checkpoint is None:
            return
        ts = getattr(self, "_last_first_seen", None) or datetime.now(timezone.utc)
        with contextlib.suppress(Exception):
            await self._checkpoint.set(ts)

    async def _sleep(self, delay: float) -> None:
        """An interruptible sleep: reacts to stop() during the backoff."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the worker (phase 1: the start in the container via Compose)."""
    import redis.asyncio as aioredis

    from src.log_secrets import install_secret_log_filter

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()
    redis_client = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    worker = RoutingWorker(checkpoint=RedisCheckpoint(redis_client))
    await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
