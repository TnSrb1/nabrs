"""Binding a tender to the cross-source cluster at ingest (owner decision 01.10.2026).

Called from :meth:`src.workers.ingest.IngestWorker._process_raw` IN THE SAME
transaction as the INSERT/UPDATE of the row: the trigger NOTIFY goes out on commit, and
routing sees the tender already with ``canonical_tender_id``/``canonical_confidence``
(otherwise suppression behind the flag would be late by the first delivery).

Who searches for whom: a new/changed row of a country from :data:`LINK_COUNTRIES` searches for
a pair on a source from the allowed pair :data:`LINK_SOURCE_PAIRS` — a TED row
finds jnportal, and a jnportal row that arrived after TED finds TED. The deadline search window is exactly one minute of the bid deadline:
the candidate rule (:func:`src.dedup.cross_source.is_candidate`) requires a
match to the minute, a wider window would add nothing. The lookup goes through the
existing index ``tenders_normalized_deadline_at_idx``.

What is written (only on a verdict “duplicate”/“probable”, see ``cross_source``):

* a candidate without a cluster yet → a new ``canonical_tenders`` row
  (``primary_tender_id`` is the candidate, we have it earlier) and the candidate goes into it;
* a candidate already in a cluster → the tender joins its cluster;
* both rows get ``canonical_confidence``/``canonical_score`` of THIS pair;
  the confidence of an already linked row is not rewritten.

An already linked tender is not re-linked. Existing rows are not backfilled
(owner decision): the UPDATE wakes routing through the trigger.

Runs as the owner (ingest is the superuser ``tendersrb``); the tables are public, without RLS.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import text

from src.dedup.cross_source import (
    DedupSide,
    PairScore,
    cpv8_codes,
    deadline_minute,
    is_candidate,
    score_pair,
)
from src.parsers.base import NormalizedTender

logger = logging.getLogger(__name__)

__all__ = [
    "LINK_COUNTRIES",
    "LINK_SOURCE_PAIRS",
    "LinkResult",
    "link_cross_source",
    "partner_sources",
]

#: Countries where we have ≥2 sources with overlap: Serbia (jnportal + TED).
LINK_COUNTRIES: frozenset[str] = frozenset({"RS"})

#: Source pairs on which the scoring of :mod:`src.dedup.cross_source` is calibrated
#: (measured 01.10.2026 — TED ↔ jnportal). A country alone does NOT allow a pair:
#: the second national source of Serbia (``mod_nabavke``, wave 3) is another
#: source with other fields, and it cannot be linked to jnportal by the same scores,
#: until the scoring on this pair is measured. A new pair — only with a measurement.
LINK_SOURCE_PAIRS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"ted_balkan", "jnportal"}),
        frozenset({"ted_eu", "jnportal"}),
    }
)


def partner_sources(source_key: str) -> list[str]:
    """Sources with which the ``source_key`` row may be linked (sorted)."""
    return sorted(
        other
        for pair in LINK_SOURCE_PAIRS
        if source_key in pair
        for other in pair - {source_key}
    )


NameVariants = Callable[[str, dict[str, Any]], Sequence[str]]

_SELF_SQL = text("""
    SELECT id, canonical_tender_id
    FROM tenders_normalized
    WHERE source_key = :source_key AND source_uid = :source_uid
    """)
# Candidates: the same country, a source from an allowed pair (LINK_SOURCE_PAIRS),
# deadline in the same minute.
# ORDER BY — deterministic choice on equal scores: the one seen earlier.
_CANDIDATES_SQL = text("""
    SELECT id, source_key, deadline_at, classifications, amount_original,
           currency_original, amount_eur, customer_name, canonical_tender_id,
           tenders_raw_id
    FROM tenders_normalized
    WHERE country_code = :country_code
      AND source_key = ANY(:partners)
      AND deadline_at >= :dl_from AND deadline_at < :dl_to
    ORDER BY first_seen_at, id
    """)
_RAW_PAYLOAD_SQL = text("SELECT raw_payload FROM tenders_raw WHERE id = :id")
_NEW_CANONICAL_SQL = text("""
    INSERT INTO canonical_tenders (primary_tender_id)
    VALUES (:primary_tender_id)
    RETURNING id
    """)
_SET_LINK_SQL = text("""
    UPDATE tenders_normalized
    SET canonical_tender_id = :canonical_id,
        canonical_confidence = :confidence,
        canonical_score = :score
    WHERE id = :id
    """)


@dataclass(frozen=True)
class LinkResult:
    canonical_id: str
    partner_id: str
    score: PairScore


def _side(row: Any, names: Sequence[str | None]) -> DedupSide:
    return DedupSide.build(
        source_key=row.source_key,
        deadline_at=row.deadline_at,
        classifications=row.classifications,
        amount_original=row.amount_original,
        currency_original=row.currency_original,
        amount_eur=row.amount_eur,
        customer_names=names,
    )


async def _partner_names(
    session: Any, row: Any, name_variants: NameVariants | None
) -> list[str | None]:
    names: list[str | None] = [row.customer_name]
    if name_variants is None or row.tenders_raw_id is None:
        return names
    payload_row = (
        await session.execute(_RAW_PAYLOAD_SQL, {"id": row.tenders_raw_id})
    ).first()
    payload = payload_row.raw_payload if payload_row is not None else None
    if isinstance(payload, dict):
        names.extend(name_variants(row.source_key, payload))
    return names


async def link_cross_source(
    session: Any,
    normalized: NormalizedTender,
    *,
    own_name_variants: Sequence[str] = (),
    name_variants: NameVariants | None = None,
) -> LinkResult | None:
    """Finds a pair for the tender on another source and writes the link. None — not linked.

    ``own_name_variants`` — the customer names from THIS tender’s payload (all languages,
    :meth:`SourceAdapter.customer_name_variants`); ``name_variants(source_key,
    payload)`` — the same for candidate rows (their payload is read from
    ``tenders_raw`` only for those that passed candidacy).
    """
    if (normalized.country_code or "").upper() not in LINK_COUNTRIES:
        return None
    partners = partner_sources(normalized.source_key)
    if not partners:
        return None
    minute = deadline_minute(normalized.deadline_at)
    if minute is None or not cpv8_codes(normalized.classifications):
        return None

    me_row = (
        await session.execute(
            _SELF_SQL,
            {"source_key": normalized.source_key, "source_uid": normalized.source_uid},
        )
    ).first()
    if me_row is None or me_row.canonical_tender_id is not None:
        return None

    me = DedupSide.build(
        source_key=normalized.source_key,
        deadline_at=normalized.deadline_at,
        classifications=normalized.classifications,
        amount_original=normalized.amount_original,
        currency_original=normalized.currency_original,
        amount_eur=normalized.amount_eur,
        customer_names=[normalized.customer_name, *own_name_variants],
    )
    rows = (
        await session.execute(
            _CANDIDATES_SQL,
            {
                "country_code": normalized.country_code,
                "partners": partners,
                "dl_from": minute,
                "dl_to": minute + timedelta(minutes=1),
            },
        )
    ).all()

    best: tuple[PairScore, Any] | None = None
    for row in rows:
        if not is_candidate(me, _side(row, [row.customer_name])):
            continue
        names = await _partner_names(session, row, name_variants)
        scored = score_pair(me, _side(row, names))
        if scored.verdict is None:
            continue
        # Strictly greater: on equal scores the one seen earlier stays (ORDER BY).
        if best is None or scored.score > best[0].score:
            best = (scored, row)
    if best is None:
        return None

    scored, partner = best
    canonical_id = partner.canonical_tender_id
    if canonical_id is None:
        canonical_id = (
            await session.execute(_NEW_CANONICAL_SQL, {"primary_tender_id": partner.id})
        ).scalar_one()
        await session.execute(
            _SET_LINK_SQL,
            {
                "canonical_id": canonical_id,
                "confidence": scored.verdict,
                "score": scored.score,
                "id": partner.id,
            },
        )
    await session.execute(
        _SET_LINK_SQL,
        {
            "canonical_id": canonical_id,
            "confidence": scored.verdict,
            "score": scored.score,
            "id": me_row.id,
        },
    )
    logger.info(
        "dedup: %s/%s ↔ %s → %s (%s points: %s)",
        normalized.source_key,
        normalized.source_uid,
        partner.id,
        scored.verdict,
        scored.score,
        ";".join(scored.reasons),
    )
    return LinkResult(str(canonical_id), str(partner.id), scored)
