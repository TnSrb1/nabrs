"""Single builder of the supplier-profile context string for AI prompts.

ONE pure function :func:`format_profile_context` (no DB) instead of four identical
copies of ``_format_profile_context`` in the bid/checklist/draft services and in the
web cabinet (``tender_ai_generate``). The string is substituted into the prompt via the
existing parameter ``AnalysisRequest.extra_context`` → the placeholder ``{profile_context}`` in
:mod:`src.ai.prompts` — ``prompts.py`` itself is NOT touched
(behavioral file, A/B is mandatory).

The CPV/keyword extraction logic mirrors the former copies byte for byte, so for the
existing fields (CPV, regions, keywords, ``ai_extra_context``) the output does NOT change —
there is no regression for the four callers (snapshot test
``tests/ai/test_profile_context.py``). New: the budget line (``min/max_amount_eur``),
``procedure_types`` and the optional track record (see :mod:`src.ai.track_record`),
which is inserted here as an already formatted string — the builder knows nothing about the DB.
"""

from __future__ import annotations

from typing import Any

_EMPTY = "(профиль не заполнен — оценивай только по тексту ТЗ)"


def _extract_cpv_codes(classifications: Any) -> list[str]:
    """CPV codes from the JSONB ``classifications`` (list/single dict ``{scheme,code,label}``)."""
    if not classifications:
        return []
    if isinstance(classifications, dict):
        classifications = [classifications]
    if not isinstance(classifications, list):
        return []
    codes: list[str] = []
    for item in classifications:
        if not isinstance(item, dict):
            continue
        scheme = item.get("scheme", "")
        if isinstance(scheme, str) and scheme.upper() == "CPV":
            code = item.get("code")
            label = item.get("label")
            entry = str(code).strip() if code else ""
            if label:
                entry = f"{entry} ({label})" if entry else str(label)
            if entry:
                codes.append(entry)
    return codes


def _extract_keywords(keywords_include: Any) -> list[str]:
    """Flat list of keywords from the JSONB ``keywords_include`` (dict/list)."""
    if not keywords_include:
        return []
    if isinstance(keywords_include, dict):
        raw = keywords_include.get("keywords") or []
        return [str(w) for w in raw if w]
    if isinstance(keywords_include, list):
        words: list[str] = []
        for item in keywords_include:
            if isinstance(item, dict):
                for group_words in item.values():
                    if isinstance(group_words, list):
                        words.extend(str(w) for w in group_words if w)
            elif isinstance(item, str):
                words.append(item)
        return words
    return []


def _fmt_eur(value: Any) -> str | None:
    """EUR amount → compact string (integer without a tail, otherwise 2 digits). ``None`` — if not a number."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}"


def format_profile_context(
    *,
    classifications: Any = None,
    regions: list[str] | None = None,
    keywords_include: Any = None,
    ai_extra_context: str | None = None,
    min_amount_eur: Any = None,
    max_amount_eur: Any = None,
    procedure_types: list[str] | None = None,
    track_record: str | None = None,
) -> str:
    """Formats the profile parameters into a string for the prompt substitution.

    The order of lines is STABLE: CPV → regions → keywords → budget →
    procedure types → ``ai_extra_context`` → track record. The new blocks (budget,
    procedures, track record) add lines ONLY when they are set, so for the
    former set of fields the output is identical to the old copies.

    ``track_record`` — an already ready string (or ``None``) from
    :func:`src.ai.track_record.format_track_record`; an empty track record arrives here
    as ``None`` and does not get into the context.

    Empty profile (no block at all) → fallback ``_EMPTY``.
    """
    parts: list[str] = []

    cpv_codes = _extract_cpv_codes(classifications)
    if cpv_codes:
        parts.append(f"CPV-коды профиля: {', '.join(cpv_codes)}")

    if regions:
        region_str = ", ".join(str(r).strip() for r in regions if r)
        if region_str:
            parts.append(f"Регионы: {region_str}")

    keywords = _extract_keywords(keywords_include)
    if keywords:
        parts.append(f"Ключевые слова: {', '.join(keywords)}")

    min_str = _fmt_eur(min_amount_eur)
    max_str = _fmt_eur(max_amount_eur)
    if min_str is not None and max_str is not None:
        parts.append(f"Бюджет профиля: от {min_str} до {max_str} EUR")
    elif min_str is not None:
        parts.append(f"Бюджет профиля: от {min_str} EUR")
    elif max_str is not None:
        parts.append(f"Бюджет профиля: до {max_str} EUR")

    if procedure_types:
        proc_str = ", ".join(str(p).strip() for p in procedure_types if p)
        if proc_str:
            parts.append(f"Типы процедур: {proc_str}")

    if ai_extra_context and ai_extra_context.strip():
        parts.append(f"Дополнительный контекст:\n{ai_extra_context.strip()}")

    if track_record and track_record.strip():
        parts.append(track_record.strip())

    if not parts:
        return _EMPTY
    return "\n".join(parts)
