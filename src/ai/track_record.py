"""Behavioral memory of the company — a compact track record from ``tender_user_status``.

The idea of mem0 (accumulated context about the client) WITHOUT mem0/a vector DB/a second
LLM provider: only our own data INSIDE RLS. From the tenant’s status funnel
(``tender_user_status``: submitted/won/lost) we count how many bids were submitted /
won / lost, and which CPV categories the company wins most often
(a join on ``tenders_normalized`` for the classifier of the won tenders).

RLS scope: ``tender_user_status`` — tenant-scoped (a direct ``tenant_id`` column,
FORCE RLS). The query MUST run under the same tenant GUC as the profile loading in the
calling service (bot: ``resolve_tenant_id`` sets ``app.current_tenant_id``;
web: ``repo.set_tenant`` FIRST). Additionally — an explicit ``tenant_id =
CAST(:tenant_id AS uuid)`` (defense-in-depth to RLS, as in the web profile loading).
``tenders_normalized`` is global (NOT tenant-scoped), the LEFT JOIN is safe.

PII: the track record is an AGGREGATE (counters + CPV codes), without addresses/phones/names.
On a query error we degrade to empty, and the log gets only the CLASS of the error
(:func:`build_track_record_context`), without ``exc_info`` with data.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from src.ai.profile_context import _extract_cpv_codes

logger = logging.getLogger(__name__)

# How many top CPVs among the won tenders to show in the context (compactness).
_TOP_CPV = 3

# Only the “productive” funnel stages feed the track record. Explicit tenant_id —
# mirrors the defense-in-depth of the web profile loading; the RLS GUC remains primary.
_LOAD_TRACK_RECORD_SQL = text("""
    SELECT tus.status AS status, tn.classifications AS classifications
    FROM tender_user_status tus
    LEFT JOIN tenders_normalized tn ON tn.id = tus.tender_id
    WHERE tus.tenant_id = CAST(:tenant_id AS uuid)
      AND tus.status IN ('submitted', 'won', 'lost')
    """)


@dataclass(frozen=True)
class TrackRecord:
    """Aggregate of the tenant’s funnel: counters + top CPVs among the won ones."""

    submitted: int = 0
    won: int = 0
    lost: int = 0
    top_cpv: list[str] = field(
        default_factory=list
    )  # frequent CPVs among won, descending.

    @property
    def is_empty(self) -> bool:
        """No single productive mark → the block is not added to the context."""
        return self.submitted == 0 and self.won == 0 and self.lost == 0


async def load_track_record(session: Any, tenant_id: str) -> TrackRecord:
    """Builds the :class:`TrackRecord` of the tenant from ``tender_user_status`` (under the RLS GUC).

    ‼️ Call ONLY after ``app.current_tenant_id`` is set (bot:
    ``resolve_tenant_id``; web: ``repo.set_tenant``) — otherwise RLS returns 0 rows
    fail-closed. There is no cross-tenant read: GUC + the explicit ``tenant_id`` predicate.
    """
    rows = (
        await session.execute(_LOAD_TRACK_RECORD_SQL, {"tenant_id": str(tenant_id)})
    ).all()

    submitted = won = lost = 0
    won_cpv: Counter[str] = Counter()
    for row in rows:
        status = row.status
        if status == "submitted":
            submitted += 1
        elif status == "lost":
            lost += 1
        elif status == "won":
            won += 1
            for entry in _extract_cpv_codes(row.classifications):
                won_cpv[entry] += 1

    top_cpv = [entry for entry, _ in won_cpv.most_common(_TOP_CPV)]
    return TrackRecord(submitted=submitted, won=won, lost=lost, top_cpv=top_cpv)


def format_track_record(track_record: TrackRecord) -> str | None:
    """Compact track-record string for the profile context; empty → ``None``.

    E.g.: "Company track record: submitted 5, won 2, lost 1. Most often wins:
    CPV 45000000 (Construction), CPV 71000000 (Design)".
    """
    if track_record.is_empty:
        return None
    line = (
        f"Трек-рекорд компании: подано {track_record.submitted}, "
        f"выиграно {track_record.won}, проиграно {track_record.lost}."
    )
    if track_record.top_cpv:
        cpv_str = ", ".join(f"CPV {c}" for c in track_record.top_cpv)
        line = f"{line} Чаще выигрывает: {cpv_str}."
    return line


async def build_track_record_context(session: Any, tenant_id: str) -> str | None:
    """load + format with degradation to empty: any failure → ``None``, not a crash of the AI feature.

    The track record is an auxiliary memory; its failure must NOT break bid/checklist/
    draft. Into the log — only the CLASS of the error (without ``exc_info``: the track-record strings carry
    the tender payload, there is no PII there, but we do not write tracebacks with data either).
    """
    try:
        record = await load_track_record(session, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "track_record: skipped from the context (%s)", type(exc).__name__
        )
        return None
    return format_track_record(record)
