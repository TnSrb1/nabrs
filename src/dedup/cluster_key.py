"""compute_cluster_key — a deterministic key of the cross-platform deduplication.

Groups the duplicates of one tender, which came from different platforms, into one
candidate cluster. The confirmation of the cluster (cosine similarity of the descriptions ≥ 0.92)
and the assignment of ``canonical_tender_id`` — a separate step, not here (ADR-022).

The formula and the HARD order of the components — ``docs/DB_SCHEMA.md §7``
(ADR-033 addendum: ``country_code`` as the FIRST component — it eliminates
cross-country false clustering of identical tax_id-format strings).
The result is a SHA-1 hex (40 characters, the type of the column
``tenders_normalized.cluster_key`` — CHAR(40), DB_SCHEMA §2.10/§2.11).

The SHA-1 here is a deterministic grouping key, NOT cryptographic protection
(``usedforsecurity=False`` removes bandit B324; collision resistance is not
required — the key only groups the candidates, the confirmation goes through the cosine).

A pure function: no network, DB or side effects; tested by snapshots. At the MVP
there is one source (jnportal) → there is no real grouping, but the contract and the tests
are ready for ≥2 sources.

See ADR-022 (the two-level model of the tender + canonical), ADR-033
(multi-country foundations), ``docs/DB_SCHEMA.md §7`` (the formula).
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from src.parsers.base import NormalizedTender

__all__ = ["compute_cluster_key"]

# A hard order: country | tax | system_code | code | amount | deadline
# (DB_SCHEMA §7). The separator "|" is mandatory — it avoids collisions of the concatenation
# of neighbouring fields (for example "10"+"000" vs "1"+"0000").
_SEPARATOR = "|"


def _normalize_country(country_code: str | None) -> str:
    """upper + strip. None/empty → ``""``."""
    if not country_code:
        return ""
    return country_code.strip().upper()


def _normalize_tax_id(customer_tax_id: str | None) -> str:
    """Digits only (for RS PIB = 9 digits); other characters are dropped. None → ``""``."""
    if not customer_tax_id:
        return ""
    return "".join(ch for ch in customer_tax_id if ch.isdigit())


def _primary_classification(tender: NormalizedTender) -> tuple[str, str]:
    """The first element of ``classifications`` → (system_code, code), strip.

    An empty list → ``("", "")``. ``code`` is NOT truncated (CPV is already cut to
    8 digits in the parser).
    """
    if not tender.classifications:
        return "", ""
    primary = tender.classifications[0]
    system_code = (primary.get("system_code") or "").strip()
    code = (primary.get("code") or "").strip()
    return system_code, code


def _normalize_amount(amount_eur: float | None) -> str:
    """``round(amount_eur, -3)`` → whole thousands, a string without a fractional part.

    None → ``""``. ``round()`` in Python is banker’s rounding
    (half-to-even); the behavior is deterministic and pinned by a test.
    """
    if amount_eur is None:
        return ""
    return str(int(round(amount_eur, -3)))


def _normalize_deadline(deadline_at: datetime | None) -> str:
    """Truncate to a date (``date()``), ISO ``YYYY-MM-DD``. None → ``""``."""
    if deadline_at is None:
        return ""
    return deadline_at.date().isoformat()


def compute_cluster_key(tender: NormalizedTender) -> str | None:
    """Compute the cluster_key of the tender or return None on incomplete data.

    Returns the 40-character SHA-1 hex. Returns ``None`` if ALL of
    (tax_id, primary_classification.code, amount_eur, deadline_at) are empty —
    there is nothing to cluster by, a key from a single ``country_code`` (and/or
    ``system_code``) would give false mega-clusters. At least ONE non-empty
    of these four is enough.
    """
    country = _normalize_country(tender.country_code)
    tax = _normalize_tax_id(tender.customer_tax_id)
    system_code, code = _primary_classification(tender)
    amount = _normalize_amount(tender.amount_eur)
    deadline = _normalize_deadline(tender.deadline_at)

    # country_code and system_code alone are NOT considered sufficient.
    if not (tax or code or amount or deadline):
        return None

    raw_key = _SEPARATOR.join([country, tax, system_code, code, amount, deadline])
    return hashlib.sha1(raw_key.encode("utf-8"), usedforsecurity=False).hexdigest()
