"""Strengthening of the unit tests of the matching engine profile↔tender (stage B, §6.2).

It complements ``test_matching.py`` / ``test_card_profile_label.py`` — it covers the edge cases of the pure functions :func:`keywords_match` and :func:`match_reason`, which were not checked directly before:

- the normalization of a non-list ``keywords_include`` (dict/str/None) → “no filter” (the match is always);
- an empty group ``all_of`` is NOT counted as matched (otherwise a profile without words would catch everything);
- ``None``/empty strings in ``keywords_exclude`` are safely skipped;
- the exclude has priority over the include and in :func:`match_reason` (an empty reason);
- the CPV in :func:`match_reason` — an exact code, a wildcard pattern, a break after the first match and the absence of a match when ``profile_classifications`` is not a dict.

Everything is pure, without a network/DB (the style of ``tests/dedup/test_cluster_key.py``).
"""

from __future__ import annotations

from src.workers.matching import keywords_match, match_reason

# --------------------------------------------------------------------------- #
#  keywords_match — the normalization of the include to a list of groups
# --------------------------------------------------------------------------- #


def test_include_dict_treated_as_no_filter():
    # keywords_include is not a list (dict) → _include_groups gives [] → there is no filter
    assert keywords_match({"all_of": ["видео"]}, None, "ничего похожего") is True


def test_include_string_treated_as_no_filter():
    # a string include — also not a list → “there is no filter”, the match is always
    assert keywords_match("видео", None, "ничего похожего") is True


def test_include_none_treated_as_no_filter():
    assert keywords_match(None, None, "любой текст") is True


def test_empty_all_of_group_is_not_a_match():
    # a group with an empty all_of is NOT matched — otherwise a profile without words would catch everything
    assert keywords_match([{"all_of": []}], None, "любой текст") is False


def test_group_without_all_of_key_is_not_a_match():
    # a group without the key all_of → terms is empty → not a match
    assert keywords_match([{}], None, "любой текст") is False


def test_none_group_element_does_not_crash():
    # None among the groups must not drop the function — it simply does not match
    assert keywords_match([None], None, "любой текст") is False


# --------------------------------------------------------------------------- #
#  keywords_match — the edge cases of the exclude
# --------------------------------------------------------------------------- #


def test_exclude_none_and_empty_entries_skipped():
    # None and "" in the exclude are skipped, they do not cut the tender
    assert keywords_match([], [None, ""], "abc def") is True


def test_exclude_case_insensitive():
    # the exclude is case-insensitive (kw.lower() in hay), it mirrors the include
    assert keywords_match([], ["СТОП"], "тендер стоп-слово") is False


def test_exclude_wins_over_matching_include_multiple_groups():
    # even if one of the include groups matched — any exclude word excludes
    assert (
        keywords_match(
            [{"all_of": ["видео"]}, {"all_of": ["звук"]}],
            ["ремонт"],
            "видео ремонт опреме",
        )
        is False
    )


# --------------------------------------------------------------------------- #
#  match_reason — the priority of the exclude and the CPV edge cases
# --------------------------------------------------------------------------- #


def test_match_reason_exclude_returns_empty_even_if_include_matches():
    # the exclude fired → the reason is empty (the tender must not have matched)
    reason = match_reason([{"all_of": ["видео"]}], ["стоп"], "видео стоп")
    assert reason.matched_keywords == []
    assert reason.matched_cpv == []


def test_match_reason_cpv_exact_code_match():
    # a pattern without a wildcard equal to the code of the tender → fnmatch matches
    profile_cls = {"include": [{"system_code": "eu_cpv", "code": "32200000"}]}
    tender_cls = [{"system_code": "eu_cpv", "code": "32200000"}]
    reason = match_reason([], None, "", tender_cls, profile_cls)
    assert reason.matched_cpv == ["32200000"]


def test_match_reason_cpv_no_match_different_prefix():
    # the pattern 45* does not match the code 32... → an empty CPV
    profile_cls = {"include": [{"system_code": "eu_cpv", "code": "45*"}]}
    tender_cls = [{"system_code": "eu_cpv", "code": "32200000"}]
    reason = match_reason([], None, "", tender_cls, profile_cls)
    assert reason.matched_cpv == []


def test_match_reason_cpv_pattern_added_once_per_pattern():
    # two codes of the tender fit one pattern → the pattern is not duplicated (break)
    profile_cls = {"include": [{"system_code": "eu_cpv", "code": "32*"}]}
    tender_cls = [
        {"system_code": "eu_cpv", "code": "32200000"},
        {"system_code": "eu_cpv", "code": "32100000"},
    ]
    reason = match_reason([], None, "", tender_cls, profile_cls)
    assert reason.matched_cpv == ["32*"]


def test_match_reason_cpv_only_matching_pattern_listed():
    # of two include patterns only the one that gave the match gets into the reason
    profile_cls = {
        "include": [
            {"system_code": "eu_cpv", "code": "45*"},
            {"system_code": "eu_cpv", "code": "32*"},
        ]
    }
    tender_cls = [{"system_code": "eu_cpv", "code": "32200000"}]
    reason = match_reason([], None, "", tender_cls, profile_cls)
    assert reason.matched_cpv == ["32*"]


def test_match_reason_cpv_empty_when_profile_classifications_is_list():
    # profile_classifications is not a dict (list) → inc_list stays [] → no CPV
    tender_cls = [{"system_code": "eu_cpv", "code": "32200000"}]
    reason = match_reason([], None, "", tender_cls, [{"code": "32*"}])
    assert reason.matched_cpv == []


def test_match_reason_cpv_skips_blank_profile_pattern():
    # an empty pattern in the include is skipped, it does not give a false match
    profile_cls = {"include": [{"system_code": "eu_cpv", "code": ""}]}
    tender_cls = [{"system_code": "eu_cpv", "code": "32200000"}]
    reason = match_reason([], None, "", tender_cls, profile_cls)
    assert reason.matched_cpv == []


def test_match_reason_keywords_first_group_wins_of_two():
    # with two matched groups the FIRST one goes into the reason (the order matters)
    reason = match_reason(
        [{"all_of": ["видео"]}, {"all_of": ["опреме"]}],
        None,
        "набавка видео опреме",
    )
    assert reason.matched_keywords == ["видео"]
