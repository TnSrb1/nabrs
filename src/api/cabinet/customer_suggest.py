"""Suggestions of contracting authorities for the profile filters “follow / exclude” (0046).

The list — from ``tenders_normalized`` for the last year, only the profile’s countries
(owner’s decision). Authorities are grouped by the SAME key that the matching compares
(:func:`src.workers.matching.sql_customer_key`): “EMS AD” and “ems  ad” — one
suggestion row, and the chosen name is guaranteed to match the tenders.

Search by the entered text — with the single folding :func:`src.domain.text_norm.fold`
(Cyrillic↔Latin, case, diacritics): a Serb types “elektroprivreda”
in Latin, while the portal writes the name in Cyrillic. The folding here is only for the SEARCH
in the suggestion; the identity of the authority in the profile and the matching is the key from SQL.

The window — ~2–5 thousand names per country, therefore the list is read in one query and
is kept in the process memory for ``_TTL`` seconds, and the filter by string — in Python.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.text_norm import fold
from src.workers.matching import sql_customer_key

#: How many suggestions we return per request.
SUGGEST_LIMIT = 10
#: We do not search for a shorter query: one letter matches half of the list.
MIN_QUERY_LEN = 2
_TTL = 600.0

# min(customer_name) — a deterministic representative of the group (without ORDER BY
# inside the group it would be any one). ORDER BY is mandatory: the order of the suggestions
# with an equal number of tenders must not jump between requests.
_NAMES_SQL = text(f"""
    SELECT min(customer_name) AS name, count(*) AS n
    FROM tenders_normalized
    WHERE country_code = CAST(:cc AS char(2))
      AND first_seen_at > now() - interval '365 days'
      AND NULLIF(btrim(customer_name), '') IS NOT NULL
    GROUP BY {sql_customer_key("customer_name")}
    ORDER BY n DESC, name
    """)


@dataclass(frozen=True)
class CustomerHit:
    name: str
    tenders: int


_cache: dict[str, tuple[float, list[tuple[CustomerHit, str]]]] = {}


async def _names(db: AsyncSession, country_code: str) -> list[tuple[CustomerHit, str]]:
    now = time.monotonic()
    cached = _cache.get(country_code)
    if cached is not None and now - cached[0] < _TTL:
        return cached[1]
    rows = (await db.execute(_NAMES_SQL, {"cc": country_code})).fetchall()
    names = [(CustomerHit(name=r.name, tenders=int(r.n)), fold(r.name)) for r in rows]
    _cache[country_code] = (now, names)
    return names


async def suggest(
    db: AsyncSession, country_code: str, query: str | None
) -> list[CustomerHit]:
    """The country’s authorities whose folded name contains the folded ``query``.

    Order — by the number of tenders for the year (frequent authorities first).
    """
    needle = fold(" ".join((query or "").split()))
    if len(needle) < MIN_QUERY_LEN:
        return []
    hits: list[CustomerHit] = []
    for hit, folded in await _names(db, country_code):
        if needle in folded:
            hits.append(hit)
            if len(hits) >= SUGGEST_LIMIT:
                break
    return hits
