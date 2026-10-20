"""One-off backfill of the deadline timezone: wall-clock Serbian time → true UTC.

PROBLEM. Before the ingest fix, the wall-clock deadline from the eNabavke HTML card
(«06.08.2026 11:00:00» = 11:00 Belgrade time) was written to ``deadline_at`` as 11:00
UTC — the real moment drifted 1–2 h (CEST/CET) into the past. Every comparison with
``now()`` (feed, routing, reminders) fired before the real deadline.

WHAT IT DOES. It reinterprets the already written wall-clock time as local Serbian time
and converts it to the true UTC:

    deadline_at := (deadline_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Belgrade'

``AT TIME ZONE 'UTC'`` strips from the ``timestamptz`` the wall-clock UTC hours (11:00);
the second ``AT TIME ZONE 'Europe/Belgrade'`` treats them as local and returns the true
``timestamptz`` (09:00Z in summer / 10:00Z in winter). DST is handled by Postgres itself — no hard-coded “2 hours”. Only ``deadline_at`` is updated (and
``opening_at``, if not empty). ``last_changed_at``/``last_seen_at``/``published_at``
are NOT touched: the workers' checkpoints depend on them, an extra UPDATE would wake
routing-replay (a real incident: a mass UPDATE → 765 extra messages).

TWO PROTECTIVE LAYERS (a double shift = data corruption — the main risk):

1. *The correctness boundary* ``--before-ts`` (MANDATORY). We shift only rows
   written by the OLD ingest: ``first_seen_at < before_ts`` (or ``IS NULL`` — ancient rows without a mark). Rows that arrived AFTER the fix was deployed already hold the correct UTC — they must not be touched. ``before_ts`` = the moment the corrected ingest was deployed to production (passed by the owner).

2. *Idempotency* — the service marker table ``deadline_tz_backfill``: the id of
   each shifted row is written IN THE SAME transaction as the shift itself. A repeated run
   (incl. after a failure midway — committed batches are marked, uncommitted ones rolled back) skips the marked rows via ``NOT EXISTS``.
   So a double shift is impossible even with repeated/interrupted runs.

By default it is a dry-run: it prints how many rows will fall under the edit, and 5 examples of
“was → will become”. Writing only with ``--apply``. The owner runs the script AFTER a backup.

Run:
    python -m scripts.backfill_deadline_tz --before-ts 2026-07-22T12:00:00Z
    python -m scripts.backfill_deadline_tz --before-ts 2026-07-22T12:00:00Z --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult

from src.db.session import session_scope

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 5000

# Reinterpretation of the wall-clock time as true UTC (DST — Postgres itself).
# Moved into a string so that the SELECT preview and the UPDATE compute the SAME thing and so that
# the guard in the tests sees the expression whole (the same trick as in the RLS-force test).
_REINTERPRET = "(deadline_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Belgrade'"
_REINTERPRET_OPENING = "(opening_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Belgrade'"

# Service marker table of idempotency. Not tenant-scoped (tenders_normalized is
# the shared pool of tenders, without RLS), therefore no policy is needed.
_CREATE_MARKER = text("""
    CREATE TABLE IF NOT EXISTS deadline_tz_backfill (
        tender_normalized_id uuid PRIMARY KEY,
        shifted_at timestamptz NOT NULL DEFAULT now()
    )
""")

# Candidates: there is something to shift (deadline/opening not empty), the row is from the OLD
# ingest (first_seen_at < boundary or NULL) and NOT yet shifted (absent from the marker).
# Cursor by id (not OFFSET): in dry-run the rows do not disappear from the selection, OFFSET would
# loop over one batch forever. The preview computes the new value with the same expression.
_SELECT_COLS = f"""
    SELECT
        tn.id AS id,
        tn.deadline_at AS old_deadline,
        {_REINTERPRET} AS new_deadline,
        tn.opening_at AS old_opening
    FROM tenders_normalized AS tn
    WHERE (tn.deadline_at IS NOT NULL OR tn.opening_at IS NOT NULL)
      AND (tn.first_seen_at IS NULL OR tn.first_seen_at < CAST(:before_ts AS timestamptz))
      AND NOT EXISTS (
          SELECT 1 FROM deadline_tz_backfill b
          WHERE b.tender_normalized_id = tn.id
      )
"""

_SELECT_FIRST_BATCH = text(
    _SELECT_COLS + "  ORDER BY tn.id\n  LIMIT CAST(:batch_size AS integer)"
)
_SELECT_BATCH = text(
    _SELECT_COLS
    + "  AND tn.id > CAST(:after_id AS uuid)\n"
    + "  ORDER BY tn.id\n  LIMIT CAST(:batch_size AS integer)"
)

# `NOT EXISTS(marker)` is repeated in the UPDATE deliberately: if a row has somehow
# already been shifted — we do not touch it a second time (a double shift is fail-closed).
# opening_at is shifted only when non-empty (otherwise it stays NULL).
_UPDATE_BATCH = text(f"""
    UPDATE tenders_normalized AS tn
    SET deadline_at = CASE WHEN tn.deadline_at IS NOT NULL
                           THEN {_REINTERPRET} ELSE tn.deadline_at END,
        opening_at = CASE WHEN tn.opening_at IS NOT NULL
                          THEN {_REINTERPRET_OPENING} ELSE tn.opening_at END
    WHERE tn.id = ANY(CAST(:ids AS uuid[]))
      AND NOT EXISTS (
          SELECT 1 FROM deadline_tz_backfill b
          WHERE b.tender_normalized_id = tn.id
      )
""")

# The marker is in the same transaction as the UPDATE: a batch is either shifted AND marked,
# or rolled back entirely. ON CONFLICT — a safety net against a race.
_MARK_BATCH = text("""
    INSERT INTO deadline_tz_backfill (tender_normalized_id)
    SELECT unnest(CAST(:ids AS uuid[]))
    ON CONFLICT (tender_normalized_id) DO NOTHING
""")


async def run(*, apply: bool, batch_size: int, before_ts: datetime) -> None:
    scanned = 0
    shifted = 0
    batches = 0
    samples: list[str] = []
    after_id: str | None = None

    if apply:
        async with session_scope() as session:
            await session.execute(_CREATE_MARKER)
            await session.commit()

    while True:
        async with session_scope() as session:
            params: dict[str, Any] = {
                "batch_size": batch_size,
                "before_ts": before_ts,
            }
            if after_id is None:
                result = await session.execute(_SELECT_FIRST_BATCH, params)
            else:
                result = await session.execute(
                    _SELECT_BATCH, {**params, "after_id": after_id}
                )
            rows = result.fetchall()

            if not rows:
                break

            batches += 1
            scanned += len(rows)
            after_id = str(rows[-1].id)

            for row in rows:
                if len(samples) < 5 and row.old_deadline is not None:
                    samples.append(f"{row.old_deadline}  →  {row.new_deadline}")

            if apply:
                ids = [str(row.id) for row in rows]
                update = await session.execute(_UPDATE_BATCH, {"ids": ids})
                shifted += cast("CursorResult[Any]", update).rowcount or 0
                await session.execute(_MARK_BATCH, {"ids": ids})
                await session.commit()

        logger.info("batch=%d  scanned=%d  shifted=%d", batches, scanned, shifted)

    logger.info(
        "\nRESULT: to edit=%d  shifted=%d%s",
        scanned,
        shifted,
        "  [DRY-RUN — БД не тронута]" if not apply else "",
    )
    if samples:
        logger.info("Examples (was UTC → will become the true UTC):")
        for s in samples:
            logger.info("  %s", s)
    if not apply:
        logger.info(
            "To apply: python -m scripts.backfill_deadline_tz "
            "--before-ts %s --apply",
            before_ts,
        )


def _parse_before_ts(value: str) -> datetime:
    """The ISO boundary string is parsed into an aware ``datetime`` (UTC) — for the asyncpg bind.

    ‼️ We return a datetime OBJECT, not a string: the parameter goes into
    ``CAST(:before_ts AS timestamptz)``, and asyncpg requires a ``datetime`` for timestamptz and does NOT convert a string (a real failure on production: DataError
    'expected a datetime.date or datetime.datetime instance, got str').

    We require a deliberate mark (the moment the ingest fix was deployed), not “now”: rows AFTER the deploy are already in the true UTC, they must not be shifted. A naive string is treated as UTC, an aware one is converted to UTC (Postgres will then compare tz-safe).
    """
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill of the deadline timezone (wall-clock Serbian → true UTC)"
    )
    parser.add_argument(
        "--before-ts",
        required=True,
        help="Correctness boundary (ISO): shift only rows with "
        "first_seen_at < this moment (= the moment the ingest fix was deployed to production)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply the changes to the DB (default — dry-run)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"Batch size (default {_DEFAULT_BATCH_SIZE})",
    )
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size должен быть ≥ 1")
    try:
        before_ts = _parse_before_ts(args.before_ts)
    except ValueError:
        parser.error("--before-ts: не ISO-дата (пример: 2026-07-22T12:00:00Z)")
    asyncio.run(run(apply=args.apply, batch_size=args.batch_size, before_ts=before_ts))


if __name__ == "__main__":
    main()
