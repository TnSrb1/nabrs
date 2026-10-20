"""In-process TTL cache of the ``/tenderi`` aggregates (CPV sections, regions).

The same pattern as :mod:`src.api.cabinet.library_counts`: a full scan of ``tenders_normalized`` for the cut by section / region (``repo.cpv_division_stats`` / ``repo.region_stats``) costs ~100 ms on 55K rows (measurement 26.09.2026 on production) — acceptable ONCE AN HOUR, not on every render of the hub / section. The data is not personal and not tenant-scoped (``tenders_normalized`` is a global table), so the cache is one per process, without a cut by user.
"""

from __future__ import annotations

import time

from src.api import repo
from src.domain.sources import list_enabled_sources

#: Ingest fills the database no more than once a day — an hour is a compromise between freshness
#: of the figures on the showcase and the cost of a full recomputation (see TASK geo_landings, step 2).
CACHE_TTL_SECONDS = 3600

_cpv_cache: tuple[float, dict[str, dict]] | None = None
_cpv_dir_cache: tuple[float, dict] | None = None
_region_cache: tuple[float, dict[str, dict]] | None = None
_landing_cache: tuple[float, dict] | None = None
_sources_cache: tuple[float, dict[str, str]] | None = None

#: The list of enabled sources for the portal filter is changed by the admin, not by
#: ingest — a shorter TTL than that of the aggregates.
SOURCES_TTL_SECONDS = 300


def clear_cache() -> None:
    """Resets all caches (tests; not called in production)."""
    global _cpv_cache, _cpv_dir_cache, _region_cache, _landing_cache, _sources_cache
    _cpv_cache = None
    _cpv_dir_cache = None
    _region_cache = None
    _landing_cache = None
    _sources_cache = None


async def get_cpv_stats(session) -> dict[str, dict]:
    global _cpv_cache
    now = time.monotonic()
    if _cpv_cache is not None and _cpv_cache[0] > now:
        return _cpv_cache[1]
    stats = await repo.cpv_division_stats(session)
    _cpv_cache = (now + CACHE_TTL_SECONDS, stats)
    return stats


async def get_cpv_directory(session) -> dict:
    """The CPV groups and the counters by code: ``{"groups": {NNN: stats}, "codes": {code8: n}}``.

    Two full scans of ``tenders_normalized`` — once per :data:`CACHE_TTL_SECONDS`. A shared cache for the group pages, ``/cpv-kodovi`` and the sitemap.
    """
    global _cpv_dir_cache
    now = time.monotonic()
    if _cpv_dir_cache is not None and _cpv_dir_cache[0] > now:
        return _cpv_dir_cache[1]
    data = {
        "groups": await repo.cpv_group_stats(session),
        "codes": await repo.cpv_code_counts(session),
    }
    _cpv_dir_cache = (now + CACHE_TTL_SECONDS, data)
    return data


async def get_region_stats(session) -> dict[str, dict]:
    global _region_cache
    now = time.monotonic()
    if _region_cache is not None and _region_cache[0] > now:
        return _region_cache[1]
    stats = await repo.region_stats(session)
    _region_cache = (now + CACHE_TTL_SECONDS, stats)
    return stats


async def get_landing_facts(session) -> dict:
    """The facts of the home page: open now / when updated / a card example / coverage.

    ``sources_count`` and ``countries_count`` — the enabled sources and the union of their ``country_codes`` (the same selection as the “Izvori” tab).

    ``open_total`` — the sum of ``open`` over the CPV sections from :func:`get_cpv_stats`, i.e. EXACTLY the counters that the ``/tenderi`` hub shows (a tender is counted once — by its first CPV section; a tender without a CPV is not visible in the hub, and here too: on 30.09.2026 this is 2 out of 1698 open ones). The same TTL as the hub.
    """
    global _landing_cache
    now = time.monotonic()
    if _landing_cache is not None and _landing_cache[0] > now:
        return _landing_cache[1]
    cpv = await get_cpv_stats(session)
    enabled = await list_enabled_sources(session)
    facts = {
        "open_total": sum(int(s["open"]) for s in cpv.values()),
        "updated_at": await repo.latest_tender_change_at(session),
        "example": await repo.latest_open_example(session),
        # Coverage for the facts row: the enabled sources and the union of their countries.
        "sources_count": len(enabled),
        "countries_count": len({c for s in enabled for c in s.country_codes}),
    }
    _landing_cache = (now + CACHE_TTL_SECONDS, facts)
    return facts


async def get_source_labels(session) -> dict[str, str]:
    """The enabled sources ``source_key → label`` (the portal’s “Source” filter)."""
    global _sources_cache
    now = time.monotonic()
    if _sources_cache is not None and _sources_cache[0] > now:
        return _sources_cache[1]
    labels = {src.source_key: src.label for src in await list_enabled_sources(session)}
    _sources_cache = (now + SOURCES_TTL_SECONDS, labels)
    return labels
