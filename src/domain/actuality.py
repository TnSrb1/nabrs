"""A tender without a submission deadline is current for :data:`NO_DEADLINE_ACTUAL_DAYS` days.

Owner decision 06.10.2026: no ``deadline_at`` → the tender is considered current for 30 days
from ``COALESCE(published_at, first_seen_at)``, for ALL sources and at ALL
points where “current” was decided by the ``deadline_at IS NULL`` branch. Before the decision
such a branch meant “current forever”: in the cabinet feed 6400 of 6529 “current” pairs
had no deadline, of them 6351 were older than 30 days (production, 06.10.2026).

Where it applies (keep the list complete — the predicate is found by grepping
``no_deadline_fresh``):
  * the cabinet feed / dashboard teaser / the “N arrived” pulse —
    ``src/api/cabinet/feed_service.py::_CURRENT_ONLY_PREDICATE``;
  * matching: stage A (``STAGE_A_SQL``, tender bind parameters) and the form preview
    (``PREVIEW_WHERE_A``) — ``src/workers/matching.py``;
  * ingest “insert only the current” — ``src/workers/ingest.py::_is_actual``.

Where it does NOT apply and why: the public portal/sitemap (``src/api/repo.py``)
considers only ``deadline_at > now()`` open — a tender without a deadline is not
“open” there at all, there is no ``IS NULL`` branch; indexing (:mod:`src.domain.indexing`)
has a different meaning (freshness for a search engine, 90 days).

Two forms of one predicate — an SQL fragment and Python — live here side by side and
are pinned by a common boundary test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: How many days a tender without a submission deadline is considered current (owner 06.10.2026).
NO_DEADLINE_ACTUAL_DAYS = 30


def sql_no_deadline_fresh(*, deadline: str, published: str, first_seen: str) -> str:
    """SQL: "no deadline AND the tender is younger than :data:`NO_DEADLINE_ACTUAL_DAYS` days".

    The arguments are SQL expressions, not column names: in the feed they are the columns
    ``tn.*``, in matching stage A — bind parameters (``CAST(:deadline AS
    timestamptz)``). The boundary is inclusive (``>=``), like :func:`no_deadline_is_fresh`.
    """
    return (
        f"({deadline} IS NULL AND COALESCE({published}, {first_seen}) "
        f">= now() - interval '{NO_DEADLINE_ACTUAL_DAYS} days')"
    )


def sql_no_deadline_fresh_tn(alias: str = "tn") -> str:
    """:func:`sql_no_deadline_fresh` for a ``tenders_normalized`` row under ``alias``."""
    return sql_no_deadline_fresh(
        deadline=f"{alias}.deadline_at",
        published=f"{alias}.published_at",
        first_seen=f"{alias}.first_seen_at",
    )


def no_deadline_is_fresh(
    published_at: datetime | None, first_seen_at: datetime | None, now: datetime
) -> bool:
    """The Python mirror of the SQL fragment for a tender WITHOUT a deadline (boundary ``>=``).

    Both dates unknown → not current: freshness is not proven (in SQL
    ``COALESCE(NULL, NULL) >= …`` is not true either). naive → UTC
    (the contract of ``src/domain/tz.py``).
    """
    ref = published_at if published_at is not None else first_seen_at
    if ref is None:
        return False
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref >= now - timedelta(days=NO_DEADLINE_ACTUAL_DAYS)
