"""Data of the “Sources” showcase (``GET /izvori``).

Only the ENABLED rows of ``sources``: the showcase does not promise a source we do not have (owner’s decision 02.10.2026). For each one — what the client gets in the card (the adapter’s declaration :mod:`src.parsers.capabilities`), with which sources duplicates are marked (pairs of :mod:`src.dedup.linker`, only the enabled partners), how often we check, and from which moment we collect.

“We collect from” — ``LEAST(enabled_at, the first tender seen)``: for jnportal ``enabled_at`` = the moment of migration 0045 (30.09.2026), while the tenders from the source go from 03.06.2026 — the bare ``enabled_at`` would understate the term by four months.

The order of the cards (owner’s decision 16.10.2026): the primary source (:data:`PRIMARY_SOURCE_KEY`) first regardless of the number of tenders; then the Serbian block (country ``RS``), then the other countries; inside a country — by the sum of the tenders of its sources (DESC); inside the block — by the number of the source’s tenders (DESC); on a tie — ``source_key``. The country of a source — ``sources.country_codes``; a multi-country source (TED Balkan BA/ME/MK, EEAS RS/BA/ME) — in the Serbian block if RS is among its countries, otherwise in the country with the largest number of its tenders (``tenders_normalized.country_code``; on a tie — alphabetically by the code). The number — by one ``GROUP BY`` over ``tenders_normalized``, not N queries.

A per-process cache with a short TTL (as :mod:`sources_view`): the page is public, the table changes by the admin’s button.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text

from src.parsers.capabilities import (
    CapabilityRow,
    SourceCapabilities,
    capabilities_by_key,
    matrix_rows,
)

logger = logging.getLogger(__name__)

_TTL_SECONDS = 60.0
_cache: tuple[float, tuple["SourceCard", ...]] | None = None

#: The primary source of the showcase — first, regardless of the number of tenders.
PRIMARY_SOURCE_KEY = "jnportal"
_SERBIA = "RS"

_ENABLED_SOURCES_PAGE_SQL = text("""
    SELECT s.source_key, s.display_name, s.country_codes, s.base_url,
           s.polling_interval_sec,
           LEAST(
             s.enabled_at,
             (SELECT min(tn.first_seen_at) FROM tenders_normalized tn
              WHERE tn.source_key = s.source_key)
           ) AS collecting_since
    FROM sources s
    WHERE s.enabled = true
    ORDER BY s.source_key
    """)

_TENDER_COUNTS_SQL = text("""
    SELECT tn.source_key, tn.country_code, count(*) AS n
    FROM tenders_normalized tn
    GROUP BY tn.source_key, tn.country_code
    """)


@dataclass(frozen=True)
class SourceCard:
    key: str
    name: str
    country_codes: tuple[str, ...]
    url: str | None
    polling_interval_sec: int | None
    collecting_since: datetime | None
    caps: SourceCapabilities | None
    dedup_with: tuple[str, ...] = field(default_factory=tuple)

    @property
    def rows(self) -> list[CapabilityRow]:
        return matrix_rows(self.caps) if self.caps is not None else []

    @property
    def interval(self) -> tuple[int, str] | None:
        """: The check interval ``(n, unit)``: ``h`` for whole hours, otherwise ``min``."""
        sec = self.polling_interval_sec
        if not sec or sec <= 0:
            return None
        if sec % 3600 == 0:
            return sec // 3600, "h"
        return max(1, round(sec / 60)), "min"


def clear_cache() -> None:
    """Resets the cache (tests)."""
    global _cache
    _cache = None


def _partners(key: str) -> list[str]:
    try:
        from src.dedup.linker import partner_sources

        return partner_sources(key)
    except (
        Exception
    ) as exc:  # noqa: BLE001 — the page lives without the duplicate marks
        logger.warning(
            "izvori: the dedup pairs are unavailable (%s)", type(exc).__name__
        )
        return []


def _home_country(codes: tuple[str, ...], per_country: dict[str, int]) -> str:
    """The block’s country: RS if it is among the countries; otherwise the country with the largest number of tenders."""
    if _SERBIA in codes:
        return _SERBIA
    if not codes:
        return ""
    return min(codes, key=lambda c: (-per_country.get(c, 0), c))


def _order(
    cards: list[SourceCard], totals: dict[str, int], home: dict[str, str]
) -> list[SourceCard]:
    """The showcase order: see the module docstring."""
    country_total: dict[str, int] = {}
    for card in cards:
        country_total[home[card.key]] = (
            country_total.get(home[card.key], 0) + totals[card.key]
        )

    def key(card: SourceCard) -> tuple[Any, ...]:
        country = home[card.key]
        return (
            card.key != PRIMARY_SOURCE_KEY,
            country != _SERBIA,
            -country_total[country],
            country or "ZZ",
            -totals[card.key],
            card.key,
        )

    return sorted(cards, key=key)


async def get_source_cards(session: Any) -> tuple[SourceCard, ...]:
    """The cards of the enabled platforms in the showcase order (see the module docstring)."""
    global _cache
    now = time.monotonic()
    if _cache is not None and _cache[0] > now:
        return _cache[1]
    rows = (await session.execute(_ENABLED_SOURCES_PAGE_SQL)).all()
    per_country: dict[str, dict[str, int]] = {}
    for t in (await session.execute(_TENDER_COUNTS_SQL)).all():
        cc = str(t.country_code or "").strip().upper()
        per_country.setdefault(str(t.source_key), {})[cc] = int(t.n)
    caps = capabilities_by_key()
    names = {
        str(r.source_key): (str(r.display_name or "").strip() or str(r.source_key))
        for r in rows
    }
    cards = []
    totals: dict[str, int] = {}
    home: dict[str, str] = {}
    for r in rows:
        key = str(r.source_key)
        c = caps.get(key)
        codes = tuple(
            str(x).strip().upper() for x in (r.country_codes or ()) if str(x).strip()
        )
        counts = per_country.get(key, {})
        totals[key] = sum(counts.values())
        home[key] = _home_country(codes, counts)
        cards.append(
            SourceCard(
                key=key,
                name=names[key],
                country_codes=codes,
                # The site for a human — from the declaration; the base_url of the row for TED / BiH is the API.
                url=(c.public_url if c is not None else None) or r.base_url,
                polling_interval_sec=r.polling_interval_sec,
                collecting_since=r.collecting_since,
                caps=c,
                # A dedup partner that is currently switched off — no marks.
                dedup_with=tuple(names[p] for p in _partners(key) if p in names),
            )
        )
    result = tuple(_order(cards, totals, home))
    _cache = (now + _TTL_SECONDS, result)
    return result
