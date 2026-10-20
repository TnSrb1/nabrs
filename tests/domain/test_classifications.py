"""The dictionary of the classification systems: the side of the tender ↔ the side of the profile.

A real defect (measurement on production 04.08): the ingest writes the classification of the tender as
``{"scheme": "CPV", "code": …}``, the profile form and the library templates — as
``{"system_code": "eu_cpv", "code": …}``, and the matching predicate compared
``system_code`` on both sides. In 7428 classification records there is no ``system_code``
key at all → any profile with a CPV filter matched none of the tenders (all 16 library templates gave 0).
"""

from __future__ import annotations

import pytest

from src.domain.classifications import (
    CPV_SYSTEM,
    as_prefix_filters,
    canonical_system,
    cpv_prefix,
    entry_system,
    primary_classification,
    sql_profile_system,
    sql_tender_system,
)

# --------------------------------------------------------------------------- #
# The canon of the system code
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["CPV", "cpv", " Cpv ", "eu_cpv", "EU_CPV"])
def test_both_vocabularies_fold_to_one_canon(value):
    assert canonical_system(value) == CPV_SYSTEM


@pytest.mark.parametrize("value", [None, "", "   ", 42, {}])
def test_missing_system_is_none_not_empty_string(value):
    """None ≠ “did not match”: the calling code must distinguish these cases."""
    assert canonical_system(value) is None


def test_unknown_system_kept_as_is_lowercased():
    # PLAN (the planned period) — not CPV, but not garbage either: it stays itself.
    assert canonical_system("PLAN") == "plan"


def test_entry_reads_both_ingest_contracts():
    assert entry_system({"scheme": "CPV", "code": "45000000"}) == CPV_SYSTEM
    assert entry_system({"system_code": "eu_cpv", "code": "45000000"}) == CPV_SYSTEM
    assert entry_system({"code": "45000000"}) is None


# --------------------------------------------------------------------------- #
# The primary classification of the tender
# --------------------------------------------------------------------------- #


def test_primary_classification_of_live_shaped_tender():
    """The record form exactly as in the jnportal adapter (real data)."""
    live = [
        {"code": "15000000", "label": "Храна, пиће…", "scheme": "CPV"},
        {"year": 2026, "scheme": "PLAN", "quarter": "2. квартал"},
    ]
    assert primary_classification(live) == (CPV_SYSTEM, "15000000")


def test_primary_classification_skips_entry_without_code():
    """‼️ “The first with a code”, not “the first”: a PLAN record carries no code.

    The order of the records is a property of the parser, not a contract. Take the literally first one —
    and on a change of order the profile with a CPV filter would silently stop matching.
    """
    reordered = [
        {"year": 2026, "scheme": "PLAN", "quarter": "3. квартал"},
        {"code": "48000000", "scheme": "CPV"},
    ]
    assert primary_classification(reordered) == (CPV_SYSTEM, "48000000")


@pytest.mark.parametrize("value", [None, [], [{"scheme": "PLAN", "year": 2026}]])
def test_primary_classification_without_code_is_empty(value):
    assert primary_classification(value) == (None, None)


# --------------------------------------------------------------------------- #
# The CPV prefixes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "code,expected",
    [
        ("45000000", "45*"),  # section
        ("45210000", "4521*"),  # class
        ("72200000", "722*"),  # group
        ("09310000", "0931*"),
        ("45233141", "45233141"),  # a concrete code — without substitution
        ("30*", "30*"),  # already a prefix
        ("", ""),
        ("не код", "не код"),
    ],
)
def test_cpv_prefix(code, expected):
    assert cpv_prefix(code) == expected


def test_cpv_prefix_keeps_two_digits_of_division():
    """‼️ 30000000 → 30*, and NOT 3*: «3*» is all the sections 30–39.

    On the real data the naive stripping of zeros gave 464 tenders instead of 47.
    """
    assert cpv_prefix("30000000") == "30*"


def test_as_prefix_filters_keeps_other_fields():
    out = as_prefix_filters([{"system_code": "eu_cpv", "code": "48000000"}])
    assert out == [{"system_code": "eu_cpv", "code": "48*"}]


# --------------------------------------------------------------------------- #
# SQL mirror of the canon
# --------------------------------------------------------------------------- #


def test_sql_tender_side_reads_both_keys():
    expr = sql_tender_system("tc")
    assert "tc->>'system_code'" in expr and "tc->>'scheme'" in expr


def test_sql_profile_side_does_not_read_scheme():
    # The profile has one key: `scheme` cannot come from there, and reading it would lie
    # about the contract of the form.
    expr = sql_profile_system("inc")
    assert "inc->>'system_code'" in expr and "scheme" not in expr


def test_sql_canon_does_not_double_prefix_eu_cpv():
    """replace('cpv','eu_cpv') would turn eu_cpv into eu_eu_cpv — therefore a regexp."""
    assert "regexp_replace" in sql_profile_system("inc")
    assert "'^cpv$', 'eu_cpv'" in sql_profile_system("inc")
