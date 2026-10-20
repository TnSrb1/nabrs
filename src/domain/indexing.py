"""Which tender cards we give to search engines (sitemap + ``robots``).

Two rules with different cut-off points (one date, two windows):

* **The card, ``noindex``** (:func:`is_indexable`, :data:`INDEX_CLOSED_MAX_AGE_DAYS`
  = 90 days): a closed card stays in search longer, it is useful as
  a result with a decision. We do NOT change ``noindex`` on the card.
* **Sitemap** (:func:`is_sitemap_indexable`, :data:`SITEMAP_CLOSED_MAX_AGE_DAYS`
  = 30 days): into the sitemap we pull only open and recently closed ones, so that the crawler
  does not walk the tail of closed ones. A measurement of 14.10.2026 (owner): ``sitemap-tenders-0.xml``
  = 12 367 cards, of them open (``deadline_at > now()``) 2 058 (17%),
  closed 10 024 (81%), without a deadline 285; closed ≤30 days — 3 739.

The reference point of both rules is the submission deadline, and if there is none — the publication date::

    indexable(t)        := COALESCE(t.deadline_at, t.published_at) > now() - 90 days
    in_sitemap(t)       := COALESCE(t.deadline_at, t.published_at) > now() - 30 days

A tender without both dates (on 30.09.2026 there were 35 891 of 55 415 — a historical
backfill) does not prove freshness and is not indexed under either rule. An open
tender (``deadline_at > now()``) falls into both automatically.

Each rule lives in two forms — Python (decides the card / tests) and
an SQL fragment (the sitemap selection) — side by side here, and each pair is pinned by a boundary
test so that they do not drift apart.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: How many days after the deadline a closed card stays without ``noindex``.
INDEX_CLOSED_MAX_AGE_DAYS = 90

#: How many days after the deadline a closed tender gets into the sitemap.
SITEMAP_CLOSED_MAX_AGE_DAYS = 30

#: A CPV group (``/tenderi/cpv/NNN``) is indexed and enters the sitemap only if
#: it has ``>=`` this many tenders since ``REGULAR_SINCE`` (below that the page is served,
#: but ``noindex,follow`` and outside the sitemap: a thin page = index bloat).
#: An owner measurement of 14.10.2026: groups above the threshold 154 (with ≥30 — 95).
INDEX_CPV_GROUP_MIN_TENDERS = 10

#: The SQL mirror of :func:`is_indexable` for ``tenders_normalized`` (the card, 90 days).
INDEXABLE_SQL = (
    "COALESCE(deadline_at, published_at) > "
    f"now() - interval '{INDEX_CLOSED_MAX_AGE_DAYS} days'"
)

#: The SQL mirror of :func:`is_sitemap_indexable` (sitemap, 30 days).
SITEMAP_SQL = (
    "COALESCE(deadline_at, published_at) > "
    f"now() - interval '{SITEMAP_CLOSED_MAX_AGE_DAYS} days'"
)


def is_indexable(
    deadline_at: datetime | None, published_at: datetime | None, now: datetime
) -> bool:
    """Whether we give the card to a search engine (the boundary is strict, like the SQL ``>``)."""
    ref = deadline_at if deadline_at is not None else published_at
    if ref is None:
        return False
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref > now - timedelta(days=INDEX_CLOSED_MAX_AGE_DAYS)


def is_sitemap_indexable(
    deadline_at: datetime | None, published_at: datetime | None, now: datetime
) -> bool:
    """Whether the card gets into the sitemap (the boundary is strict, like the SQL ``>``)."""
    ref = deadline_at if deadline_at is not None else published_at
    if ref is None:
        return False
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref > now - timedelta(days=SITEMAP_CLOSED_MAX_AGE_DAYS)
