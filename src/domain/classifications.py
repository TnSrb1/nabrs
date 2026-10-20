"""A single dictionary of classification systems: tender side ↔ profile side.

Invariant (the same class as text folding in :mod:`src.domain.text_norm`):
**the comparison “tender classification ↔ profile filter” goes by ONE canonical
form of the system code.** The two sides write it differently and always have:

- the TENDER side (ingest, a source adapter (not published)) —
  ``{"scheme": "CPV", "code": …, "label": …}`` (+ a record ``{"scheme": "PLAN"}``
  for the planning period, it has no code). An early version of ingest wrote
  ``{"system_code": "eu_cpv", …}`` — such rows remained in the database;
- the PROFILE side (the cabinet form, library templates, vertical packages) —
  ``{"system_code": "eu_cpv", "code": …}``.

The matching predicate compared ``tc->>'system_code'`` with ``inc->>'system_code'``:
tenders have no ``system_code`` key at all → NULL → **any profile with a CPV
filter matched NO tender** (measured on 04.08 in production: 7428 classification records
live under the key ``scheme``, 92 under ``system_code``; all 16
library templates gave 0 matches with a non-empty CPV, regardless of
keywords and the word-form dictionary).

Therefore the canonicalisation lives HERE and is reused by both sides:
the Python path (routing stage A is parametrised by the tender's values) —
:func:`canonical_system` / :func:`primary_classification`; the SQL path (preview and
profile prefilter) — :func:`sql_canonical_system` and the ready expressions
:func:`sql_tender_system` / :func:`sql_profile_system`.

The module is a leaf: stdlib only, so that both matching and the API can import it.
"""

from __future__ import annotations

from typing import Any, Sequence

#: The canonical code of the CPV system — as written by the profile side (the cabinet form
#: gives ``eu_cpv:<code>``, the 0016 seed templates and vertical packages — the same).
CPV_SYSTEM = "eu_cpv"

#: Synonyms of the system code → canon. Keys are compared in lower case.
#: ``cpv`` — what ingest writes in the ``scheme`` field.
SYSTEM_ALIASES: dict[str, str] = {"cpv": CPV_SYSTEM}


def canonical_system(value: Any) -> str | None:
    """The classification system code → canon (``CPV``/``cpv``/``eu_cpv`` → ``eu_cpv``).

    Empty/not a string → ``None`` (“system not specified”), so that the calling code can
    tell absence from a mismatch.
    """
    if not isinstance(value, str):
        return None
    folded = value.strip().lower()
    if not folded:
        return None
    return SYSTEM_ALIASES.get(folded, folded)


def entry_system(entry: Any) -> str | None:
    """The canonical system code of ONE tender classification record.

    Reads both ingest contracts: first ``system_code`` (old rows), then
    ``scheme`` (the current adapter). Neither — ``None``.
    """
    if not isinstance(entry, dict):
        return None
    return canonical_system(entry.get("system_code")) or canonical_system(
        entry.get("scheme")
    )


def entry_code(entry: Any) -> str | None:
    """The classification code of a record (as a string, without spaces) or ``None``."""
    if not isinstance(entry, dict):
        return None
    code = entry.get("code")
    if code is None:
        return None
    code = str(code).strip()
    return code or None


def primary_classification(
    classifications: Sequence[Any] | None,
) -> tuple[str | None, str | None]:
    """The tender’s primary classification → ``(system, code)`` in the canon.

    ‼️ “Primary” = the first record **with a code**, not simply the first: a jnportal
    tender has two records — CPV and the planning period ``{"scheme": "PLAN"}`` without
    a code. Right now CPV comes first, but the order is a property of the parser, not a contract;
    take literally the first — and if the order changes stage A will silently stop
    passing profiles with a CPV filter (the same defect this module fixes).
    Records without a code are skipped; none at all — ``(None, None)``.
    """
    for entry in classifications or []:
        code = entry_code(entry)
        if code:
            return entry_system(entry), code
    return None, None


def cpv_prefix(code: Any) -> str:
    """A CPV code of a division/group → a prefix filter pattern (``30000000`` → ``30*``).

    CPV is hierarchical: ``30`` is a division, ``301`` a group, ``3019`` a class,
    ``30190000`` is the code of the “office machinery” division, under which notices
    carry DESCENDANTS (``30192000``, ``30197000`` …). The matching predicate compares
    codes exactly (a pattern expands only with an explicit ``*``), so the filter "division
    30" would not match any tender whose code is more specific. Measured
    on 04.08 in production: for 4 of the 16 library templates the CPV filter gave 0 matches,
    with a prefix — from 2 to 41 in 30 days.

    ‼️ At least TWO characters: for the division ``30000000`` naive stripping of zeros would give
    ``3`` — the prefix of all divisions 30–39 (measured: 464 tenders instead of 47).
    A code with an explicit ``*``, non-numeric, or without trailing zeros is returned as is.
    """
    if not isinstance(code, str):
        return code
    raw = code.strip()
    if not raw or "*" in raw or not raw.isdigit():
        return raw
    trimmed = raw.rstrip("0")
    if len(trimmed) < 2:
        trimmed = raw[:2]
    return raw if trimmed == raw else trimmed + "*"


def cpv_division(code: Any) -> str | None:
    """A CPV code → a division (the first 2 digits), for the link ``/tenderi/cpv/{division}``.

    Mirrors ``left(code, 2)`` from ``src.api.repo`` (the SQL side of the same split)
    — does not reuse :func:`cpv_prefix` (that returns a filter pattern with
    ``*``, not a bare division). A code shorter than 2 digits or non-numeric — ``None``.
    """
    if not isinstance(code, str):
        return None
    raw = code.strip()
    if len(raw) < 2 or not raw[:2].isdigit():
        return None
    return raw[:2]


def as_prefix_filters(entries: Any) -> list[dict]:
    """A list of profile filters ``[{system_code, code}]`` with prefix codes."""
    out: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        out.append({**entry, "code": cpv_prefix(entry.get("code"))})
    return out


# --------------------------------------------------------------------------- #
#  The SQL path (the same rules as above — for the matching/preview predicates)
# --------------------------------------------------------------------------- #


def sql_canonical_system(expr: str) -> str:
    """An SQL expression canonicalising the system code (a mirror of :func:`canonical_system`).

    ``regexp_replace('^cpv$')``, not ``replace``: a substring replacement would turn
    the already canonical ``eu_cpv`` into ``eu_eu_cpv``.
    """
    return "regexp_replace(lower(btrim(COALESCE(" + expr + ", ''))), '^cpv$', 'eu_cpv')"


def sql_tender_system(alias: str) -> str:
    """The canonical system code of a TENDER classification record (a jsonb alias)."""
    return sql_canonical_system(
        f"COALESCE({alias}->>'system_code', {alias}->>'scheme')"
    )


def sql_profile_system(alias: str) -> str:
    """The canonical system code of a PROFILE filter record (a jsonb alias)."""
    return sql_canonical_system(f"{alias}->>'system_code'")
