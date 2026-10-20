"""Live figure “≈ N tenders in 30 days” for the showcase cards on /library.

Counted by :func:`preview_service.preview_counts` — THE SAME predicate that the profile form preview shows (see :mod:`src.api.cabinet.template_prefill`). Here only the cache and protection of the showcase from a counter failure.

**Why the cache is shared by all.** The figure does not depend on the tenant: the query reads ``tenders_normalized`` (a global table, not under RLS) with a country filter and a 30-day window, and the card set is public templates and packages. There is no personal data in the key or value, so one process cache serves all subscribers.

**Why a cache at all.** Measured on production data (04.08, 30-day window, 3364 notices): the naive per-card ``preview_count`` loop = 19.7 s per page view; one batch scan of the window = 0.95 s. 0.95 s on EVERY showcase view is still expensive, and the data changes once a day (ingest), so the values live for :data:`CACHE_TTL_SECONDS` and are recomputed lazily.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.cabinet import preview_service

logger = logging.getLogger(__name__)

#: How long a computed showcase lives. Ingest fills the database once a day, so
#: 15 minutes is a compromise between freshness and the cost of recomputation.
CACHE_TTL_SECONDS = 900

# key -> (expires at, counts). The key is the payloads themselves: an edit of a template in
# the admin changes the key, and the figure is recomputed rather than showing the stale one.
_cache: dict[str, tuple[float, list[int]]] = {}


def _cache_key(payloads: Sequence[dict[str, Any]]) -> str:
    return json.dumps(payloads, sort_keys=True, default=str, ensure_ascii=False)


def clear_cache() -> None:
    """Resets the cache (tests and manual runs; not called in production)."""
    _cache.clear()


async def counts_for(
    db: AsyncSession, payloads: Sequence[dict[str, Any]]
) -> list[int] | None:
    """Card counts in the order of ``payloads``; ``None`` — could not count.

    ``None`` (not zeros) — so that the showcase tells “no matches” apart from “the figure is not shown now”: lying with a zero is worse than showing nothing, and the page must not fail at all because of a counter.
    """
    if not payloads:
        return []
    key = _cache_key(payloads)
    cached = _cache.get(key)
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        counts = await preview_service.preview_counts(db, payloads)
    except Exception as exc:  # the showcase matters more than the counter
        logger.warning("library_counts failed: %s", type(exc).__name__)
        return cached[1] if cached is not None else None
    # The key is the card set: an edit of a template in the admin creates a NEW key, and the
    # old one stays as garbage. We clean up stale entries on write (there are only a few entries,
    # a scan is cheaper than any eviction policy).
    for stale in [k for k, (expires, _) in _cache.items() if expires <= now]:
        _cache.pop(stale, None)
    _cache[key] = (now + CACHE_TTL_SECONDS, counts)
    return counts
