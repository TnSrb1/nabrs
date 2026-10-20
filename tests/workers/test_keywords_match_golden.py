"""The equivalence of :func:`keywords_match` — a guard for the precomputation/cache (analitika-speed).

The expectations are LITERALS, taken from the code BEFORE the edit (origin/main, a99263d9): the cache of the fold of the terms and ``expand_term`` must not change a single answer. The cases cover Cyrillic↔Latin, the stems of words (STEM_INNER), the stop words, the dictionary ru/en→sr and the empty/None inclusions.
"""

import pytest

from src.workers.matching import keywords_match

# (keywords_include, keywords_exclude, haystack, keyword_dict_enabled, expected)
GOLDEN = [
    ([{"all_of": ["gradjevin"]}], [], "Izgradnja gradjevinskih objekata", False, True),
    ([{"all_of": ["gradjevina"]}], [], "Radovi na gradjevinskom objektu", False, True),
    (
        [{"all_of": ["laptop"]}],
        ["skola"],
        "Nabavka laptop računara za školu",
        False,
        True,
    ),
    ([{"all_of": ["laptop"]}], ["skola"], "Nabavka laptop računara", False, True),
    (
        [{"all_of": ["kancelarijski", "materijal"]}],
        [],
        "kancelarijskog materijala nabavka",
        False,
        True,
    ),
    (
        [{"all_of": ["kancelarijski", "materijal"]}],
        [],
        "materijal kancelarijski",
        False,
        True,
    ),
    ([{"all_of": ["hran"]}], [], "Usluge ishrane učenika", False, True),
    ([{"all_of": ["hran"]}], [], "Ni jedna rec", False, False),
    ([{"all_of": ["vrat"]}], [], "Popravka povrat novca", False, True),
    ([{"all_of": ["медицинск"]}], [], "Nabavka medicinskog materijala", False, True),
    ([{"all_of": ["медицинск"]}], [], "Nabavka medicinskog materijala", True, True),
    ([{"all_of": ["laptop"]}], [], "Nabavka računara", True, False),
    ([{"all_of": ["računar"]}], [], "Nabavka laptop uređaja", True, False),
    ([{"all_of": ["рачунар"]}], [], "Nabavka racunara", False, True),
    ([{"all_of": ["рачунар"]}], [], "Nabavka racunara", True, True),
    ([{"all_of": ["komp"]}], [], "Nabavka kompjutera", True, True),
    ([{"all_of": ["x", "y"]}], [], "x i y zajedno", False, True),
    ([{"all_of": ["x", "y"]}], [], "samo x", False, False),
    ([{"all_of": ["x", "y"]}, {"all_of": ["x"]}], [], "samo x", False, True),
    ([{"all_of": ["x", "y"]}, {"all_of": ["x"]}], [], "samo x ovde", False, True),
    ([], [], "bilo sta", False, True),
    (None, [], "bilo sta", False, True),
    (None, ["zabrana"], "nabavka zabrana", False, False),
    ([{"all_of": ["  "]}], [], "nista", False, False),
    ([{"all_of": ["sanacij"]}], [], "Resanacija zgrade", False, True),
    ([{"all_of": ["montaz"]}], [], "Demontaza opreme", False, True),
    ([{"all_of": ["đubr"]}], [], "Nabavka djubriva", False, True),
    ([{"all_of": ["đubr"]}], [], "Nabavka djubriva", True, True),
    ([{"all_of": ["stolic"]}], [], "Stolice za salu", True, True),
    ([{"all_of": ["stol"]}], [], "Stolice za salu", True, True),
    ([{"all_of": ["Lekov"]}], [], "lekovi i sredstva", False, True),
    ([{"all_of": ["Lekov"]}], [], "lekovi i sredstva", True, True),
    ([{"all_of": ["kancelarijski"]}], [], "kancelarijskog materijala", True, True),
    ([{"all_of": ["kancelarijski"]}], [], "materijal kancelarijski", True, True),
    ([{"all_of": ["gradjevina"]}], [], "gradjevinskih objekata", True, True),
    ([{"all_of": ["hran"]}], [], "ishrane ucenika", True, True),
    ([{"all_of": ["vrat"]}], [], "povrat novca", True, True),
    (
        [{"all_of": ["laptop", "kancelarijski"]}],
        [],
        "laptop kancelarijskog",
        True,
        True,
    ),
    (
        [{"all_of": ["laptop", "kancelarijski"]}],
        [],
        "kancelarijskog laptopa",
        True,
        True,
    ),
    ([{"all_of": ["laptop"]}], [], "laptopa za skolu", True, True),
    (
        [{"all_of": ["kancelarijski materijal"]}],
        [],
        "kancelarijskog materijala",
        True,
        True,
    ),
    (
        [{"all_of": ["kancelarijski materijal"]}],
        [],
        "kancelarijskog materijala",
        False,
        True,
    ),
    ([{"all_of": ["gradjevin"]}], ["objekat"], "gradjevinskih objekata", True, False),
    ([{"all_of": ["ishrana"]}], [], "Ishrana ucenika", True, True),
]


@pytest.mark.parametrize(("inc", "exc", "hay", "dict_on", "expected"), GOLDEN)
def test_keywords_match_unchanged(inc, exc, hay, dict_on, expected):
    assert keywords_match(inc, exc, hay, keyword_dict_enabled=dict_on) is expected


def test_keywords_match_repeated_calls_stable():
    """The cache must not depend on the order of the calls: the second run — the same answers."""
    first = [
        keywords_match(i, e, h, keyword_dict_enabled=d) for i, e, h, d, _ in GOLDEN
    ]
    second = [
        keywords_match(i, e, h, keyword_dict_enabled=d) for i, e, h, d, _ in GOLDEN
    ]
    assert first == second == [x for *_, x in GOLDEN]
