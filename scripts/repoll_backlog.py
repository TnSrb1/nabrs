"""One-off re-poll of jnportal tenders with an EXPIRED deadline (the owner's decision 08.10.2026).

WHY. The regular re-poll (``src/domain/repoll.py``) takes deadlines ≥ today−1.
A tender whose deadline passed before repoll was enabled (08.10 00:46 UTC) could have been
extended by the portal, while for us it is “closed” and reaches neither the feed nor matching.

WHAT IT DOES. It takes ``status='active' AND deadline_at < now()-1 day`` (the freshest
expired ones first: ``ORDER BY deadline_at DESC, source_uid``), polls the cards
via ``adapter.repoll_cards`` (the same rate gate ``_get``, not a new client),
the decision “what to write” — ``repoll_decision`` (it contains the protection “deadline earlier than publication”),
the write — ``IngestWorker._repoll_apply`` (only really changed rows;
``tenders_raw.fetched_at`` always moves).

MODES. By default ``--dry-run``: nothing is written to the DB (neither a row nor a mark),
it counts how many cards were extended into the future / changed status / unchanged /
errors, and (``--estimate``, on by default) estimates how many messages
would go to the subscribers from the extended ones. ``--apply`` writes.

‼️ DELIVERY. An UPDATE of ``tenders_normalized`` wakes the trigger → routing → matching:
an expired active tender does not match, an EXTENDED one matches and goes to subscribers as
new, if it has not been delivered yet (a repeat is suppressed by the UNIQUE of ``delivery_outbox``).
Therefore ``--apply`` without ``--yes-delivery`` does not start: first a dry-run and an estimate.

IDEMPOTENCY. ``--since T`` skips the tenders checked since the moment T
(``tenders_raw.fetched_at >= T``). The first run of ``--apply`` prints its own
``--since`` — the next batches and the retry after an interruption are run with the same one.

PACE. One thread, no parallelism. The pace is set by the adapter's gate
(``INGEST_JNPORTAL_RPS``), on top of it a floor ``--min-interval`` between cards
(default 1.0 s: the measurement of 08.10 — 45 HTTP in ~44 s). ``--max-errors`` errors in a row
(429 and the like) — a stop: we do not hammer a closed door.

RUN (owner, on production; a short finite probe — under ``timeout``):
    docker exec deploy-ingest-1 timeout 600 python -m scripts.repoll_backlog --limit 100
    docker exec deploy-ingest-1 timeout 3600 python -m scripts.repoll_backlog \\
        --apply --yes-delivery --limit 500 --since 2026-10-08T07:00:00Z
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

logger = logging.getLogger("repoll_backlog")

_SOURCE_KEY = "jnportal"
_DEFAULT_LIMIT = 100
_DEFAULT_MIN_INTERVAL = 1.0
_DEFAULT_MAX_ERRORS = 3
#: An estimate above the threshold of messages — the owner decides (see TASK).
DELIVERY_THRESHOLD = 200
_TERMINAL = frozenset({"closed", "awarded", "cancelled"})

# ``checked_at`` — the mark of the last check; ``:since IS NULL`` — without skipping.
# The order is deterministic (``deadline_at DESC, source_uid``).
_BACKLOG_SQL = text("""
    SELECT tn.id, tn.source_uid, tn.deadline_at, tn.published_at,
           tn.first_seen_at, tn.status, tr.fetched_at AS checked_at
    FROM tenders_normalized tn
    JOIN tenders_raw tr
      ON tr.source_key = tn.source_key AND tr.source_uid = tn.source_uid
    WHERE tn.source_key = :source_key
      AND tn.status = 'active'
      AND tn.deadline_at < CAST(:now AS timestamptz) - interval '1 day'
      AND (CAST(:since AS timestamptz) IS NULL
           OR tr.fetched_at IS NULL
           OR tr.fetched_at < CAST(:since AS timestamptz))
    ORDER BY tn.deadline_at DESC, tn.source_uid
    LIMIT :limit
    """)
_TENDER_SQL = text("""
    SELECT id, country_code, source_key, regions, classifications,
           procedure_type, contract_type, customer_name,
           language, amount_eur, deadline_at, published_at, status,
           title_normalized, description_normalized, first_seen_at,
           canonical_tender_id, canonical_confidence
    FROM tenders_normalized
    WHERE id = :tender_id
    """)
_OUTBOX_PROFILES_SQL = text("""
    SELECT DISTINCT CAST(profile_id AS text) AS profile_id
    FROM delivery_outbox
    WHERE tender_id = :tender_id
    """)


@dataclass
class BacklogReport:
    """The run result (aggregates and uids; without PII)."""

    apply: bool
    selected: int = 0
    checked: int = 0
    extended_future: int = 0  # new deadline in the future
    extended_past: int = 0  # deadline changed, but still in the past
    status_changed: int = 0
    unchanged: int = 0
    errors: int = 0
    aborted: bool = False
    written: int = 0  # rows of tenders_normalized really updated (only --apply)
    # delivery estimate (only the dry-run with --estimate)
    est_tenders: int = 0
    est_profiles: int = 0
    est_messages_max: int = 0
    seconds: float = 0.0
    extended_uids: list[str] = field(default_factory=list)

    def classes(self) -> Counter:
        return Counter(
            extended_future=self.extended_future,
            extended_past=self.extended_past,
            status_changed=self.status_changed,
            unchanged=self.unchanged,
            errors=self.errors,
        )


def classify(row: Any, card: dict[str, Any], now: datetime) -> dict[str, bool]:
    """Result classes of a single card (by ``repoll_decision``, not by separate logic)."""
    from src.workers.ingest import repoll_decision

    deadline, status, changed = repoll_decision(row, card)
    deadline_changed = deadline != row["deadline_at"]
    return {
        "changed": changed,
        "extended_future": deadline_changed and deadline > now,
        "extended_past": deadline_changed and deadline <= now,
        "status_changed": status != row["status"],
    }


async def _estimate_one(
    session_factory: Callable[[], Any], row: Any, card: dict[str, Any], now: datetime
) -> tuple[int, int]:
    """How many profiles/messages an extended tender would get (READ ONLY, no writing).

    The same stages as routing: ``stage_a_candidates`` (the production SQL) + ``stage_b_filter``,
    the tender — as after the UPDATE (the new deadline/status). The pairs already in the outbox
    (profile, tender) are subtracted — their repeat will be suppressed by UNIQUE. Messages — an upper
    estimate: the number of the profile’s channels, the liveness of the channel is not checked.
    """
    from src.workers import matching
    from src.workers.ingest import repoll_decision

    deadline, status, _ = repoll_decision(row, card)
    async with session_factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout = '30s'"))
        try:
            r = (await session.execute(_TENDER_SQL, {"tender_id": row["id"]})).first()
            if r is None:
                return 0, 0
            view = matching.TenderView(
                id=str(r.id),
                country_code=r.country_code,
                source_key=r.source_key,
                regions=list(r.regions or []),
                classifications=list(r.classifications or []),
                procedure_type=r.procedure_type,
                contract_type=r.contract_type,
                customer_name=r.customer_name,
                language=r.language,
                amount_eur=float(r.amount_eur) if r.amount_eur is not None else None,
                deadline_at=r.deadline_at,
                published_at=r.published_at,
                status=r.status,
                title_normalized=r.title_normalized,
                description_normalized=r.description_normalized,
                first_seen_at=r.first_seen_at,
                canonical_tender_id=(
                    str(r.canonical_tender_id) if r.canonical_tender_id else None
                ),
                canonical_confidence=r.canonical_confidence,
            )
            view = replace(view, deadline_at=deadline, status=status)
            candidates = await matching.stage_a_candidates(session, view)
            matched = matching.stage_b_filter(candidates, view)
            seen = {
                x.profile_id
                for x in (
                    await session.execute(
                        _OUTBOX_PROFILES_SQL, {"tender_id": row["id"]}
                    )
                )
            }
        finally:
            await session.rollback()
    fresh = [c for c in matched if c.profile_id not in seen]
    messages = sum(len(c.delivery_channels or []) or 1 for c in fresh)
    return len(fresh), messages


async def run_backlog(
    adapter: Any,
    session_factory: Callable[[], Any],
    *,
    apply: bool = False,
    limit: int = _DEFAULT_LIMIT,
    since: datetime | None = None,
    source_key: str = _SOURCE_KEY,
    min_interval: float = _DEFAULT_MIN_INTERVAL,
    max_errors: int = _DEFAULT_MAX_ERRORS,
    estimate: bool = True,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> BacklogReport:
    """One pass: selection → per card (sequentially) → classification → write."""
    from src.workers.ingest import IngestWorker

    report = BacklogReport(apply=apply)
    started = time.monotonic()
    start_now = clock()
    async with session_factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        rows = [
            dict(r)
            for r in (
                await session.execute(
                    _BACKLOG_SQL,
                    {
                        "source_key": source_key,
                        "now": start_now,
                        "since": since,
                        "limit": limit,
                    },
                )
            )
            .mappings()
            .all()
        ]
        await session.rollback()
    report.selected = len(rows)
    worker = IngestWorker(session_factory=session_factory, source_key=source_key)
    consecutive = 0
    last = 0.0
    for row in rows:
        uid = str(row["source_uid"])
        wait = min_interval - (time.monotonic() - last)
        if last and wait > 0:
            await sleep(wait)
        last = time.monotonic()
        try:
            card = None
            # Per card: an error in one does not break the rest, and the pace gate
            # lives in the adapter and is shared across all calls.
            stream = adapter.repoll_cards([uid])
            try:
                async for _uid, card in stream:
                    break
            finally:
                await stream.aclose()
            if card is None:
                raise RuntimeError("an empty response from repoll_cards")
        except Exception as exc:  # noqa: BLE001 — an error counter, not a crash
            report.errors += 1
            consecutive += 1
            # No PII/URL with a token in the log is needed — only the exception type.
            logger.warning("repoll_backlog %s: error %s", uid, type(exc).__name__)
            if consecutive >= max_errors:
                report.aborted = True
                logger.error("repoll_backlog: %d errors in a row — stop", consecutive)
                break
            continue
        consecutive = 0
        report.checked += 1
        now = clock()
        cls = classify(row, card, now)
        if cls["extended_future"]:
            report.extended_future += 1
            report.extended_uids.append(uid)
        elif cls["extended_past"]:
            report.extended_past += 1
        if cls["status_changed"]:
            report.status_changed += 1
        if not cls["changed"]:
            report.unchanged += 1
        if apply:
            if await worker._repoll_apply(source_key, uid, row, card, now):
                report.written += 1
        elif estimate and cls["extended_future"]:
            profiles, messages = await _estimate_one(session_factory, row, card, now)
            report.est_tenders += 1 if profiles else 0
            report.est_profiles += profiles
            report.est_messages_max += messages
    report.seconds = time.monotonic() - started
    return report


def format_report(r: BacklogReport, *, backlog_total: int = 12171) -> str:
    n = r.checked or 1
    lines = [
        f"режим: {'APPLY (запись)' if r.apply else 'DRY-RUN (в БД не пишется ничего)'}",
        f"выбрано: {r.selected}, проверено: {r.checked}, время: {r.seconds:.1f} с",
        f"продлено в будущее: {r.extended_future} ({100 * r.extended_future / n:.1f}%)",
        f"срок изменён, но всё ещё прошёл: {r.extended_past}",
        f"смена статуса: {r.status_changed}",
        f"без изменений: {r.unchanged}",
        f"ошибок (429/сеть/пусто): {r.errors}{' — ПРОХОД ПРЕРВАН' if r.aborted else ''}",
    ]
    if r.apply:
        lines.append(f"записано строк tenders_normalized: {r.written}")
    else:
        lines.append(
            "доставка (оценка, верхняя): продлённых с совпадением "
            f"{r.est_tenders}, профилей {r.est_profiles}, сообщений ≤ {r.est_messages_max}"
        )
        if r.checked:
            total = r.est_messages_max * backlog_total / r.checked
            flag = (
                f" — БОЛЬШЕ {DELIVERY_THRESHOLD}: решает owner"
                if total > DELIVERY_THRESHOLD
                else ""
            )
            lines.append(
                f"экстраполяция на {backlog_total} просрочек: ≈ {total:.0f} сообщений{flag}"
            )
    return "\n".join(lines)


def _parse_since(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _amain(args: argparse.Namespace) -> None:
    from src.db.session import session_scope
    from src.parsers.base import get_source
    from src.parsers.registry import load_all_adapters

    load_all_adapters()
    apply = bool(args.apply)
    since = _parse_since(args.since) if args.since else None
    if apply and not args.yes_delivery:
        raise SystemExit(
            "--apply wakes routing→matching (the extended ones will go to subscribers): "
            "first a dry-run and an estimate, then --apply --yes-delivery"
        )
    if apply and since is None:
        since = datetime.now(timezone.utc)
        print(
            f"--since not set: the start moment {since.isoformat()} was taken; "
            "use the same one for the next batches"
        )
    report = await run_backlog(
        get_source(_SOURCE_KEY)(),
        session_scope,
        apply=apply,
        limit=args.limit,
        since=since,
        min_interval=args.min_interval,
        max_errors=args.max_errors,
        estimate=not args.no_estimate,
    )
    print(format_report(report))
    if report.extended_uids:
        print("extended uids (first 20):", ", ".join(report.extended_uids[:20]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="write to the DB")
    parser.add_argument("--dry-run", action="store_true", help="by default")
    parser.add_argument(
        "--yes-delivery",
        action="store_true",
        help="confirmation: the delivery risk has been estimated (required for --apply)",
    )
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--since", help="skip the ones checked since this moment (ISO)")
    parser.add_argument("--min-interval", type=float, default=_DEFAULT_MIN_INTERVAL)
    parser.add_argument("--max-errors", type=int, default=_DEFAULT_MAX_ERRORS)
    parser.add_argument(
        "--no-estimate", action="store_true", help="do not count the matches with profiles"
    )
    args = parser.parse_args()
    if args.apply and args.dry_run:
        raise SystemExit("--apply and --dry-run are mutually exclusive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
