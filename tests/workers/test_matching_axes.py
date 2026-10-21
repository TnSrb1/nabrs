"""The axes of the matching profile↔tender: the price, the region, the categories, the keywords.

It complements ``test_matching.py`` / ``test_matching_engine_extra.py``. Everything without a DB, on fixtures.

Two groups of tests — by the nature of the axes themselves:

1. **The mirror of the predicates of stage A** (the price / the region / the categories / the status live in the SQL and are not checked behaviorally without PG). We check what can be checked and what really breaks: ``STAGE_A_SQL`` (the routing: which profiles will get the tender) and ``PREVIEW_WHERE_A`` (the preview in the cabinet: how many tenders are promised to the profile) — two selections of one meaning in one module. A divergence of the boundaries = the preview lies to the subscriber about what he will receive (the invariant of the project rules “a predicate duplicating the selection of another module — mirror it”). We compare not the whole text (it is different: profile-centric against tender-centric), but the extracted **defaults and constants** — what diverges when one side is edited.
2. **The pure functions of stage B** — the branches of the normalization of the categories and the morphology (``keyword_dict``), not covered by the existing tests, plus the degradation of the dictionary of word forms when ``keyword_dict.json`` is unavailable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import src.workers.keyword_expand as keyword_expand
from src.workers.matching import (
    PREVIEW_WHERE_A,
    STAGE_A_SQL,
    keywords_match,
    match_reason,
)

_ROUTING_SQL = str(STAGE_A_SQL)

# --------------------------------------------------------------------------- #
#  The axis “price” — the bounds of the range
# --------------------------------------------------------------------------- #

# `x BETWEEN COALESCE(<min>, owner) AND COALESCE(<max>, partner)` → (owner, partner).
_BETWEEN_DEFAULTS_RX = (
    r"BETWEEN\s+COALESCE\(.+?,\s*([\w.+-]+)\)\s+AND\s+COALESCE\(.+?,\s*([\w.+-]+)\)"
)


def _amount_bounds(sql: str) -> list[tuple[str, str]]:
    return re.findall(_BETWEEN_DEFAULTS_RX, sql, re.S)


def test_amount_range_defaults_mirror_between_routing_and_preview():
    """An unset bound of the price is expanded into the same thing from both sides.

    If the defaults diverge (for example, the top 1e18 against 1e9) — the preview would count for the subscriber one thing, the routing would send another, and the error is silent.
    """
    routing = _amount_bounds(_ROUTING_SQL)
    preview = _amount_bounds(PREVIEW_WHERE_A)

    assert routing, "the BETWEEN predicate of the price was not found in STAGE_A_SQL"
    assert (
        preview
    ), "the BETWEEN predicate of the price was not found in PREVIEW_WHERE_A"
    assert routing == preview


def test_amount_range_defaults_are_open_ended():
    """The defaults of the bounds — “zero below, unlimited above”, and not a cut-off."""
    assert _amount_bounds(_ROUTING_SQL) == [("0", "1e18")]


def test_tender_without_amount_passes_only_via_include_no_amount_on_both_sides():
    """A tender without an amount: both sides decide its fate in the same way.

    The skip — only if the profile explicitly asked for tenders without an amount (``include_no_amount``) or set no bound at all.
    """
    for sql in (_ROUTING_SQL, PREVIEW_WHERE_A):
        assert "include_no_amount" in sql
        amount_null_branch = re.search(
            r"include_no_amount.+?IS NULL AND.+?IS NULL", sql, re.S
        )
        assert (
            amount_null_branch
        ), "the branch “no bounds → a tender without an amount is OK” is lost"


# --------------------------------------------------------------------------- #
#  The axes “region” and “categories” — an empty filter means “not set”
# --------------------------------------------------------------------------- #


# The branches “the filter is not set” are named one by one, and not by a regex over any `= 0`:
# in both queries there are several such branches (procedure_types, languages), and a common
# pattern would silently catch a neighbouring one — the test would pass with a broken region.
_REGION_NO_FILTER = {
    _ROUTING_SQL: "cardinality(COALESCE(p.regions, '{}')) = 0",
    PREVIEW_WHERE_A: "cardinality(COALESCE(CAST(:regions AS text[]), '{}')) = 0",
}
_REGION_OVERLAP = {
    _ROUTING_SQL: "COALESCE(p.regions, '{}') && CAST(:tender_regions AS text[])",
    PREVIEW_WHERE_A: "COALESCE(tn.regions, '{}') && CAST(:regions AS text[])",
}
_CLS_INCLUDE_NO_FILTER = {
    _ROUTING_SQL: (
        "jsonb_array_length(COALESCE(p.classifications->'include', '[]'::jsonb)) = 0"
    ),
    PREVIEW_WHERE_A: (
        "jsonb_array_length(COALESCE(CAST(:cls_include AS jsonb), '[]'::jsonb)) = 0"
    ),
}


def test_empty_region_filter_means_no_filter_on_both_sides():
    """An empty list of regions — “any region”, and not “none”.

    Both sides must treat the emptiness in the same way, otherwise a profile without regions catches either everything or nothing — depending on where one looks.
    """
    for sql, branch in _REGION_NO_FILTER.items():
        assert (
            branch in sql
        ), "the branch “regions are not set → there is no filter” is lost"
    for sql, overlap in _REGION_OVERLAP.items():
        assert overlap in sql, "the overlap match of the regions is lost"


def test_empty_classification_include_means_no_filter_on_both_sides():
    """An empty include-list of categories — “any category”."""
    for sql, branch in _CLS_INCLUDE_NO_FILTER.items():
        assert (
            branch in sql
        ), "the branch “categories are not set → there is no filter” is lost"


def test_classification_wildcard_is_translated_to_like_on_both_sides():
    """`*` in the code of a category — a prefix match via a LIKE replacement with `%`."""
    for sql in (_ROUTING_SQL, PREVIEW_WHERE_A):
        assert sql.count("replace(inc->>'code', '*', '%')") == 1
        assert sql.count("replace(exc->>'code', '*', '%')") == 1


# --------------------------------------------------------------------------- #
#  The axis “status/deadline” — a completed tender is not sent
# --------------------------------------------------------------------------- #

_NOT_IN_RX = r"NOT IN \(([^)]*)\)"


def _completed_statuses(sql: str) -> list[frozenset[str]]:
    return [
        frozenset(s.strip().strip("'") for s in group.split(","))
        for group in re.findall(_NOT_IN_RX, sql)
    ]


def test_completed_statuses_mirror_between_routing_and_preview():
    """The list of “dead” statuses is the same in the routing and in the preview."""
    assert _completed_statuses(_ROUTING_SQL) == _completed_statuses(PREVIEW_WHERE_A)


def test_completed_statuses_are_closed_awarded_cancelled():
    """The pin of the composition: if a status drops out, the closed tenders will go out as new."""
    assert _completed_statuses(_ROUTING_SQL) == [
        frozenset({"closed", "awarded", "cancelled"})
    ]


def test_min_days_until_deadline_defaults_to_zero_on_both_sides():
    """min_days not set → 0 (we cut nothing), the same on both sides."""
    rx = r"COALESCE\((?:p\.min_days_until_deadline|CAST\(:min_days AS int\)),\s*(\d+)\)"
    assert re.findall(rx, _ROUTING_SQL) == ["0"]
    assert re.findall(rx, PREVIEW_WHERE_A) == ["0"]


# --------------------------------------------------------------------------- #
#  The categories of the tender — the normalization of the input into match_reason
# --------------------------------------------------------------------------- #

_PROFILE_CLS = {"include": [{"system_code": "CPV", "code": "45*"}]}


def test_match_reason_accepts_single_classification_as_dict():
    """The classification of the tender came as one dict (not a list) — the match is found."""
    reason = match_reason(
        None,
        None,
        "радови",
        tender_classifications={"system_code": "CPV", "code": "45200000"},
        profile_classifications=_PROFILE_CLS,
    )
    assert reason.matched_cpv == ["45*"]


def test_match_reason_ignores_non_dict_classification_scalar():
    """A garbage scalar instead of the classifications — an empty reason, without a fall."""
    reason = match_reason(
        None,
        None,
        "радови",
        tender_classifications="45200000",
        profile_classifications=_PROFILE_CLS,
    )
    assert reason.matched_cpv == []


def test_match_reason_skips_non_dict_entries_inside_list():
    """A list with mixed content — only the dict records are taken."""
    reason = match_reason(
        None,
        None,
        "радови",
        tender_classifications=["45200000", {"system_code": "CPV", "code": "45200000"}],
        profile_classifications=_PROFILE_CLS,
    )
    assert reason.matched_cpv == ["45*"]


# --------------------------------------------------------------------------- #
#  The keywords: morphology (keyword_dict) — the uncovered branches
# --------------------------------------------------------------------------- #


def test_dict_enabled_empty_include_matches_everything():
    """An empty profile (no include) with a dictionary — there is no filter, the match is always there."""
    assert keywords_match([], None, "било шта", keyword_dict_enabled=True) is True
    assert keywords_match(None, None, "било шта", keyword_dict_enabled=True) is True


def test_dict_enabled_exclude_that_is_absent_does_not_block():
    """A stop word none of whose forms appeared does not cut the match."""
    assert (
        keywords_match(
            [{"all_of": ["рачунар"]}],
            ["строительство"],
            "набавка рачунара за школу",
            keyword_dict_enabled=True,
        )
        is True
    )


def test_dict_enabled_exclude_blocks_via_serbian_form():
    """The stop word is given in Russian, and in the text there is the Serbian form: we cut."""
    assert (
        keywords_match(
            [{"all_of": ["рачунар"]}],
            ["строительство"],
            "набавка рачунара за грађевинарство",
            keyword_dict_enabled=True,
        )
        is False
    )


def test_dict_enabled_skips_empty_exclude_entries():
    """The empty/None records in the exclude do not turn into “exclude everything”."""
    assert (
        keywords_match(
            [{"all_of": ["рачунар"]}],
            ["", None],  # type: ignore[list-item]
            "набавка рачунара",
            keyword_dict_enabled=True,
        )
        is True
    )


# --------------------------------------------------------------------------- #
#  The degradation of the dictionary of word forms
# --------------------------------------------------------------------------- #


def _reset_dict_cache(monkeypatch, path: Path) -> None:
    """Resets the module cache of the dictionary and substitutes the path.

    monkeypatch will return both ``_KW_DICT`` and ``_DICT_PATH`` after the test — we do not spoil the real dictionary for the other tests.
    """
    monkeypatch.setattr(keyword_expand, "_KW_DICT", None)
    monkeypatch.setattr(keyword_expand, "_DICT_PATH", path)
    keyword_expand.expand_term_forms.cache_clear()


@pytest.fixture(autouse=True)
def _expand_cache_isolated():
    """The cache of ``expand_term`` must not carry the dictionary of the test into the neighbouring tests."""
    yield
    keyword_expand.expand_term_forms.cache_clear()


def test_expand_term_degrades_to_literal_when_dict_file_missing(monkeypatch, tmp_path):
    """There is no dictionary file → the term stays itself, the matching does not fall."""
    _reset_dict_cache(monkeypatch, tmp_path / "нет-такого.json")

    assert keyword_expand.expand_term("компьютер") == ["компьютер"]


def test_expand_term_degrades_to_literal_when_dict_file_is_broken(
    monkeypatch, tmp_path
):
    """The dictionary is broken (not JSON) → the same safe fallback, without an exception."""
    broken = tmp_path / "keyword_dict.json"
    broken.write_text("{ это не json", encoding="utf-8")
    _reset_dict_cache(monkeypatch, broken)

    assert keyword_expand.expand_term("компьютер") == ["компьютер"]


def test_matching_survives_missing_dict_but_loses_morphology(monkeypatch, tmp_path):
    """The dictionary is lost — the literal match works, the morphological one stops.

    The pin of the degradation: a profile with a dictionary does not start catching everything and does not fall; it just loses the Serbian word forms.
    """
    _reset_dict_cache(monkeypatch, tmp_path / "нет-такого.json")

    assert (
        keywords_match(
            [{"all_of": ["компьютер"]}],
            None,
            "набавка компьютеров",
            keyword_dict_enabled=True,
        )
        is True
    )
    assert (
        keywords_match(
            [{"all_of": ["компьютер"]}],
            None,
            "набавка рачунара",
            keyword_dict_enabled=True,
        )
        is False
    )


def test_meta_keys_are_not_loaded_as_terms(monkeypatch, tmp_path):
    """The `_meta` keys of the dictionary do not become a term (otherwise `_` junk in the forms)."""
    d = tmp_path / "keyword_dict.json"
    d.write_text(
        json.dumps({"_meta": {"sr": ["мусор"]}, "рачунар": {"sr": ["компјутер"]}}),
        encoding="utf-8",
    )
    _reset_dict_cache(monkeypatch, d)

    assert keyword_expand.expand_term("_meta") == ["_meta"]
    assert keyword_expand.expand_term("рачунар") == ["рачунар", "компјутер"]
