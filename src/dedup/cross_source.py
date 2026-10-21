"""The scoring of a pair “the same procurement on two sources” (TED ↔ jnportal).

Owner’s decision 01.10.2026: we include Serbia (RS) in TED, and we MARK the duplicates from jnportal
(the first month — only the mark, the delivery is not suppressed). The sources have no
common number, jnportal does not give the contracting authority’s MB/PIB (0 of 10,300 over
90 days) — therefore the pair is recognized by the coincidence of the features, and not by a key.

The candidate
-------------
A pair becomes a candidate only if the tenders have DIFFERENT sources, ONE AND THE SAME
submission deadline to the minute (UTC) and at least one common CPV code
in full (8 digits). The measurement of 01.10.2026 (19 TED RS notices since 01.06, the jnportal candidates
±3 days, ``docs/reports/TESTING_ted_rs_dupes.md``):

* minute + CPV8 → 6 candidates: 4 correct, 2 false;
* minute + a common prefix of the CPV (the hierarchy: ``45200000`` is an ancestor of ``45233120``)
  adds 4 more pairs, of which 1 correct (without the amount and the toponym — it would still
  gather only the base and remain “different”) and 3 false. The prefix adds noise
  and no marks, therefore CPV8 in full.

The TED seconds ``10:00:59`` (“to the end of the minute”) are reduced to the minute — therefore
the comparison is to the minute, and not to the second.

The scores and the thresholds — below as constants; see :func:`score_pair`.

A pure module: no network and DB. The binding in the DB — :mod:`src.dedup.linker`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.domain.text_norm import fold

OPSTINA_NAME_TO_OKRUG: dict[str, str] = (
    {}
)  # snapshot: the municipality reference of the connector is not published

__all__ = [
    "DedupSide",
    "PairScore",
    "SCORE_AMOUNT",
    "SCORE_BASE",
    "SCORE_TOPONYM",
    "THRESHOLD_DUP",
    "THRESHOLD_PROBABLE",
    "VERDICT_DUP",
    "VERDICT_PROBABLE",
    "DEDUP_MODE_ENV",
    "cpv8_codes",
    "deadline_minute",
    "is_candidate",
    "score_pair",
    "suppress_duplicates_enabled",
    "toponyms",
]

# --------------------------------------------------------------------------- #
#  The scores and the thresholds — the owner’s decision 01.10.2026
# --------------------------------------------------------------------------- #
#: A candidate (the deadline to the minute + CPV8) by itself — 1 point: in the measurement the false
#: candidates were there (2 of 6), therefore the candidacy alone is not enough even for “probable”.
SCORE_BASE = 1
#: The amount matched EXACTLY (to the cent, in one currency) — the strongest sign: in the
#: measurement 2 correct / 0 false. Base + amount = 4 = the “duplicate” threshold.
SCORE_AMOUNT = 3
#: The common toponym of the contracting authority (municipality/city from the name) — a middle sign: TED
#: often does not give the amount (Šabac, Vršac), but the city is in the name. Base + toponym = 3
#: = “probably the same”; base + toponym + amount = 6.
SCORE_TOPONYM = 2
#: ≥ — a duplicate (the suppression of the delivery is possible, but only behind a flag and it is off).
THRESHOLD_DUP = 4
#: ≥ (and < THRESHOLD_DUP) — “probably the same”: only a mark, never suppress.
THRESHOLD_PROBABLE = 2

VERDICT_DUP = "dup"
VERDICT_PROBABLE = "probable"

# --------------------------------------------------------------------------- #
#  The mode: only the mark (the owner’s decision — the first month) or the suppression
# --------------------------------------------------------------------------- #
#: of the ENV of the routing. ``mark`` (the default) — a duplicate is only marked, the delivery
#: is not suppressed; ``suppress`` — the second delivery of a DUPLICATE (≥ THRESHOLD_DUP) to the same
#: profile is not set. “Probable” is not suppressed in any mode.
DEDUP_MODE_ENV = "CROSS_SOURCE_DEDUP_MODE"
DEDUP_MODE_MARK = "mark"
DEDUP_MODE_SUPPRESS = "suppress"


def suppress_duplicates_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only with an explicit ``suppress``; empty/other — the mark (the safe default)."""
    value = (os.environ if env is None else env).get(DEDUP_MODE_ENV, "")
    return value.strip().lower() == DEDUP_MODE_SUPPRESS


_MONEY_QUANT = Decimal("0.01")
_CPV_RE = re.compile(r"^\d{8}$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# --------------------------------------------------------------------------- #
#  Toponyms
# --------------------------------------------------------------------------- #
# The case endings (folded Latin), the long ones first. One is removed.
_ENDINGS: tuple[str, ...] = (
    "ima",
    "ama",
    "om",
    "em",
    "og",
    "oj",
    "im",
    "ih",
    "u",
    "a",
    "e",
    "i",
    "o",
)
# Assimilation by voicing: Šabac → Šapca (b→p before c). We compare the keys without
# voicing on BOTH sides — symmetrically.
_DEVOICE = str.maketrans("bdgz", "ptks")
# English/Italian city names that are not in the Serbian register.
_TOPONYM_ALIASES: dict[str, str] = {
    "beograd": "beograd",
    "belgrade": "beograd",
    "belgrado": "beograd",
}


def _word_key(word: str) -> str:
    """The key of the toponym word: the case and the fleeting “a” removed, the voicing unified.

    ``sabac``/``sapca`` → ``sapc``; ``vrsac``/``vrsca`` → ``vrsc``;
    ``valjevo``/``valjeva`` → ``valjev``; ``novi``/``novog`` → ``nov``.
    """
    w = word
    if len(w) >= 4:
        for end in _ENDINGS:
            if w.endswith(end) and len(w) - len(end) >= 3:
                w = w[: -len(end)]
                break
    # The fleeting “a” (Šabac → Šapca, Kragujevac → Kragujevca): -ac → -c.
    if len(w) >= 4 and w.endswith("ac"):
        w = w[:-2] + "c"
    return w.translate(_DEVOICE)


def _phrase_keys(name: str) -> tuple[str, ...]:
    return tuple(_word_key(t) for t in _TOKEN_RE.findall(fold(name)))


def _build_toponym_index() -> dict[tuple[str, ...], str]:
    index: dict[tuple[str, ...], str] = {}
    # The municipality register of jnportal (189 names, folded Latin) — the same one by
    # which the ingest puts the district: we do not create our own list of the cities.
    for name in OPSTINA_NAME_TO_OKRUG:
        index[_phrase_keys(name)] = name
    for alias, canon in _TOPONYM_ALIASES.items():
        index[_phrase_keys(alias)] = canon
    return index


_TOPONYM_INDEX = _build_toponym_index()
_MAX_TOPONYM_WORDS = max(len(k) for k in _TOPONYM_INDEX)


def toponyms(names: Iterable[str | None]) -> frozenset[str]:
    """The municipalities/cities named in the names of the contracting authority (any alphabet and case).

    The match is by the sequence of words as a whole, not by a substring:
    “Ada” is not found inside “Adaptacija”.
    """
    found: set[str] = set()
    for name in names:
        if not name:
            continue
        keys = _phrase_keys(name)
        for i in range(len(keys)):
            for n in range(1, _MAX_TOPONYM_WORDS + 1):
                canon = _TOPONYM_INDEX.get(keys[i : i + n])
                if canon is not None:
                    found.add(canon)
    return frozenset(found)


# --------------------------------------------------------------------------- #
#  Features
# --------------------------------------------------------------------------- #
def deadline_minute(value: datetime | None) -> datetime | None:
    """The submission deadline, truncated to the minute in UTC; None → None."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def cpv8_codes(classifications: Any) -> frozenset[str]:
    """The full 8-digit CPV codes from ``classifications`` (other schemes — ignored)."""
    codes: set[str] = set()
    for item in classifications or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("scheme") or "").upper() != "CPV":
            continue
        code = str(item.get("code") or "").strip()
        if _CPV_RE.match(code):
            codes.add(code)
    return frozenset(codes)


def _money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(_MONEY_QUANT)
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True)
class DedupSide:
    """One side of the pair — only what the scoring is computed by."""

    source_key: str
    deadline_at: datetime | None
    cpv: frozenset[str]
    amount_original: Decimal | None = None
    currency_original: str | None = None
    amount_eur: Decimal | None = None
    customer_names: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        source_key: str,
        deadline_at: datetime | None,
        classifications: Any,
        amount_original: Any = None,
        currency_original: str | None = None,
        amount_eur: Any = None,
        customer_names: Sequence[str | None] = (),
    ) -> DedupSide:
        return cls(
            source_key=source_key,
            deadline_at=deadline_minute(deadline_at),
            cpv=cpv8_codes(classifications),
            amount_original=_money(amount_original),
            currency_original=(currency_original or "").strip().upper() or None,
            amount_eur=_money(amount_eur),
            customer_names=tuple(n for n in customer_names if n),
        )


def is_candidate(a: DedupSide, b: DedupSide) -> bool:
    """Different sources + the deadline to the minute + a common CPV8."""
    return (
        a.source_key != b.source_key
        and a.deadline_at is not None
        and a.deadline_at == b.deadline_at
        and bool(a.cpv & b.cpv)
    )


def _same_amount(a: DedupSide, b: DedupSide) -> bool:
    if (
        a.amount_original is not None
        and a.amount_original == b.amount_original
        and a.currency_original is not None
        and a.currency_original == b.currency_original
    ):
        return True
    return a.amount_eur is not None and a.amount_eur == b.amount_eur


@dataclass(frozen=True)
class PairScore:
    score: int
    verdict: str | None  # VERDICT_DUP / VERDICT_PROBABLE / None — different
    reasons: tuple[str, ...] = ()


def score_pair(a: DedupSide, b: DedupSide) -> PairScore:
    """The scores of the pair and the verdict by the owner’s thresholds (see the constants of the module).

    Not a candidate → 0 points, “different”. A candidate: base + the amount exactly + the common
    toponym of the contracting authority; ≥ :data:`THRESHOLD_DUP` — a duplicate, ≥
    :data:`THRESHOLD_PROBABLE` — “probably the same”, below — different.
    """
    if not is_candidate(a, b):
        return PairScore(0, None)
    score = SCORE_BASE
    reasons = ["deadline+cpv8"]
    if _same_amount(a, b):
        score += SCORE_AMOUNT
        reasons.append("amount")
    common = toponyms(a.customer_names) & toponyms(b.customer_names)
    if common:
        score += SCORE_TOPONYM
        reasons.append("toponym:" + ",".join(sorted(common)))
    if score >= THRESHOLD_DUP:
        verdict: str | None = VERDICT_DUP
    elif score >= THRESHOLD_PROBABLE:
        verdict = VERDICT_PROBABLE
    else:
        verdict = None
    return PairScore(score, verdict, tuple(reasons))
