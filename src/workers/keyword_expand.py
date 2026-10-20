"""Expansion of ru/en keywords → Serbian word forms in Cyrillic (+ en-forms for English-language sources).

The keyword_dict.json dictionary is loaded once (module cache).
expand_terms — a pure function; called before matching stage B.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from src.domain.text_norm import fold

_DICT_PATH = Path(__file__).parent.parent / "api" / "static" / "keyword_dict.json"
_KW_DICT: dict[str, dict] | None = None


def _load_dict() -> dict[str, dict]:
    global _KW_DICT
    if _KW_DICT is None:
        try:
            raw = json.loads(_DICT_PATH.read_text(encoding="utf-8"))
            _KW_DICT = {k: v for k, v in raw.items() if not k.startswith("_")}
        except (FileNotFoundError, json.JSONDecodeError):
            _KW_DICT = {}
    return _KW_DICT


@lru_cache(maxsize=4096)
def expand_term_forms(term: str) -> tuple[str, ...]:
    """One term → a tuple of forms (the original + the Serbian ones). The hot path of the matching.

    The dictionary is static, so the result is cached by the term: the matching and the report
    call it on EACH row (measurement 02.10: 741k calls per run).
    The tuple is immutable — the cache is safe to hand out.
    """
    d = _load_dict()
    entry = d.get(term.lower())
    if not entry:
        return (term,)
    sr_forms = [f for f in entry.get("sr", []) if f != term]
    return (term, *sr_forms)


#: Shorter — too generic a stem for the reverse path (``pu`` ⊂ half the dictionary).
_REVERSE_MIN_STEM = 3


@lru_cache(maxsize=1)
def _sr_en_index() -> tuple[tuple[str, bool, tuple[str, ...]], ...]:
    """``(fold(Serbian form), this is the MAIN form of the entry, en-forms of the entry)`` — once."""
    rows: list[tuple[str, bool, tuple[str, ...]]] = []
    for entry in _load_dict().values():
        en = tuple(entry.get("en") or ())
        if not en:
            continue
        for i, form in enumerate(entry.get("sr", [])):
            folded = fold(form)
            if folded:
                rows.append((folded, i == 0, en))
    return tuple(rows)


@lru_cache(maxsize=4096)
def expand_term_en(term: str) -> tuple[str, ...]:
    """One term → English forms (for English-language tenders). Without the original.

    Direct path: term — a dictionary key (ru OR en) → the ``en`` of the entry. Reverse path:
    term — the Serbian STEM of the template (``građevin``, ``softver``, ``put``). Entries are taken
    whose MAIN (first) sr-form after ``fold`` starts with the stem;
    if there are none — entries where ANY sr-form starts with the stem (union, order
    by dictionary). A narrow rule prevents ``rekonstrukcij`` (its main form sits under "repair") from pulling
    ``maintenance``/``repair`` from entries where it is only a secondary form. Neither → ``()``. The same flag
    ``keyword_dict`` as :func:`expand_term_forms`; the caller applies it ONLY to an English-language
    tender (Serbian texts are not changed).
    """
    entry = _load_dict().get(term.lower())
    if entry:
        return tuple(entry.get("en") or ())
    stem = fold(term).strip()
    if len(stem) < _REVERSE_MIN_STEM:
        return ()
    primary: dict[str, None] = {}
    anyform: dict[str, None] = {}
    for folded, is_main, en in _sr_en_index():
        if folded.startswith(stem):
            anyform.update(dict.fromkeys(en))
            if is_main:
                primary.update(dict.fromkeys(en))
    return tuple(primary or anyform)


def expand_term(term: str) -> list[str]:
    """One term → a list of forms (the original + the Serbian ones). See :func:`expand_term_forms`."""
    return list(expand_term_forms(term))


def term_forms(terms: list[str]) -> list[dict]:
    """For display in the preview: each ENTERED word → its Serbian forms.

    Mirrors the matching (:func:`expand_term`, used by
    :func:`src.workers.matching.keywords_match`): ``forms`` — exactly those forms by
    which the matching really searches when ``keyword_dict`` is on. The original
    remains in ``term``; ``forms`` — only the Serbian forms (without the original). An empty
    ``forms`` = the word is not in the dictionary (“we search as entered”). The forms are taken from the dictionary
    as they are — NOT recomputed (one predicate per meaning). The repeats of a word
    are collapsed (case-insensitive), keeping the first spelling.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for t in terms:
        key = (t or "").strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        out.append(
            {
                "term": key,
                "forms": expand_term(key)[1:],
                "forms_en": list(expand_term_en(key)),
            }
        )
    return out


def expand_terms(terms: list[str], enabled: bool) -> tuple[list[str], set[str]]:
    """Expands the list of words. Returns (flat_expanded_list, unfound_set).

    - enabled=False → (terms unchanged, an empty set)
    - The original term is always kept in the result.
    - unfound — the words not found in the dictionary (only at enabled=True).
    """
    if not enabled:
        return list(terms), set()
    result: list[str] = []
    unfound: set[str] = set()
    for t in terms:
        forms = expand_term(t)
        result.extend(forms)
        if len(forms) == 1:
            unfound.add(t)
    return result, unfound
