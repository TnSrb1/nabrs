"""The cases in the matching: the conservative Serbian truncation of the endings (template_wizard, step 1).

The fact from production (90 days, title+description, 01.10.2026): the Serb writes a word whole and loses most of the tenders — the search is a substring one:

    gradjevina → 0     gradjevin → 172
    skola      → 60    skol      → 333
    hrana      → 46    hran      → 158

The expectations — literals from these titles, not through the same function as the code.
"""

from __future__ import annotations

import pytest

from src.domain.text_norm import stem_sr
from src.workers.matching import keywords_match, match_reason


def _inc(*words: str) -> list[dict]:
    return [{"all_of": list(words)}]


# --------------------------------------------------------------------------- #
#  stem_sr — a table of literals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "word, stem",
    [
        ("gradjevina", "gradjevin"),
        ("građevina", "gradjevin"),  # the fold đ→dj before the truncation
        ("škola", "skol"),
        ("школа", "skol"),  # Cyrillic → the same stem
        ("hrana", "hran"),
        ("skolama", "skol"),  # a long ending first
        ("radovima", "radov"),
        ("obezbedjenje", "obezbedjenj"),
        ("goriva", "goriv"),
        ("lekovi", "lekov"),
        ("namestaj", "namestaj"),  # there is no ending from the list
        ("voda", "voda"),  # < 5 characters — we do not touch
        ("auto", "auto"),
        ("radom", "radom"),  # the stem «rad» < 4 — we do not truncate
        ("pošta", "posta"),  # «post» — the prefix of postupak/postojeci (the measurement 280/12)
        ("stana", "stana"),
        ("", ""),
    ],
)
def test_stem_sr_table(word, stem):
    assert stem_sr(word) == stem


# --------------------------------------------------------------------------- #
#  Stage B: include catches the cases
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "keyword, title",
    [
        ("gradjevina", "izvodjenje gradjevinskih radova na objektu"),
        ("građevina", "radovi na rekonstrukciji gradjevine"),
        ("skola", "rekonstrukcija osnovne skole vuk karadzic"),
        ("škola", "nabavka opreme za skolske kabinete"),
        ("hrana", "nabavka hrane za decu u vrticu"),
        ("hrana", "usluge pripreme hrane"),
    ],
)
def test_include_matches_other_case(keyword, title):
    assert keywords_match(_inc(keyword), [], title) is True


def test_short_word_not_stemmed():
    # «voda» (4) is not truncated to «vod» — otherwise it would catch «vodovod», «vodic»…
    assert keywords_match(_inc("voda"), [], "nabavka vode za pice") is False


def test_ambiguous_stem_not_used():
    # A real title from the measurement: «posta» by the stem «post» caught a foreign word.
    title = "zamena opreme postojecih fekalnih crpnih stanica u kuli"
    assert keywords_match(_inc("posta"), [], title) is False


def test_phrase_matches_cased_words_in_order():
    assert (
        keywords_match(
            _inc("kancelarijski materijal"),
            [],
            "nabavka kancelarijskog materijala za potrebe opstine",
        )
        is True
    )


def test_phrase_keeps_word_order():
    assert (
        keywords_match(
            _inc("kancelarijski materijal"), [], "materijal kancelarijski i ostalo"
        )
        is False
    )


def test_and_group_each_word_by_stem():
    assert keywords_match(_inc("skola", "hrana"), [], "hrane za skole") is True
    assert keywords_match(_inc("skola", "hrana"), [], "hrane za vrtice") is False


def test_exclude_stays_literal():
    # The stop word is NOT extended: a false exclusion is invisible to the client (the tender simply
    # does not come), therefore the exclude catches exactly the entered form.
    assert keywords_match([], ["skola"], "rekonstrukcija skole") is True
    assert keywords_match([], ["skola"], "skola u centru") is False


def test_keyword_dict_path_also_stems():
    assert (
        keywords_match(
            _inc("gradjevina"), [], "gradjevinski radovi", keyword_dict_enabled=True
        )
        is True
    )


def test_match_reason_uses_same_rule():
    reason = match_reason(_inc("skola"), [], "rekonstrukcija skole")
    assert reason.matched_keywords == ["skola"]


# --------------------------------------------------------------------------- #
#  The left boundary of the word: the stem does not catch the middle of a foreign word
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "term,hay",
    [
        ("vrata", "povrat sredstava"),
        ("servera", "preserver sistem"),
        ("obuka", "obucavanje zaposlenih"),
        ("radovi", "radijator grejanje"),
    ],
)
def test_stem_requires_word_start(term, hay):
    assert keywords_match(_inc(term), [], hay) is False


@pytest.mark.parametrize(
    "term,hay",
    [
        ("vrata", "nabavka vrat za zgradu"),
        ("radovi", "gradjevinski radova na putu"),
    ],
)
def test_stem_still_matches_word_start(term, hay):
    assert keywords_match(_inc(term), [], hay) is True


@pytest.mark.parametrize(
    "term,hay",
    [
        ("hrana", "namirnice za ishranu bolesnika"),
        ("hrana", "nabavka za ishranu ucenika"),
        ("snabdevanje", "vodosnabdevanja opstine"),
    ],
)
def test_stem_inner_words_match_inside_word(term, hay):
    """The stems from STEM_INNER are searched inside the word too (the literals — from production 01.10.2026)."""
    assert keywords_match(_inc(term), [], hay) is True


@pytest.mark.parametrize(
    "term,hay",
    [
        ("vrata", "povratnik na teritoriju"),
        ("skola", "elektricna energija za potrebe predskolske ustanove"),
        ("lekovi", "usluga sece rastinja ispod dalekovoda"),
    ],
)
def test_stem_inner_set_is_explicit_not_general(term, hay):
    assert keywords_match(_inc(term), [], hay) is False


@pytest.mark.parametrize(
    "term,hay",
    [
        ("medicinski", "telemedicinskog sistema"),
        ("montaža", "elektromontaznih radova"),
        ("sanacije", "resanacija objekta"),
        ("zgrada", "izgradnja skole"),
    ],
)
def test_stem_inner_d1_extension(term, hay):
    """The owner’s decision 01.10: medicinsk/montaz/sanacij/zgrad — inside the word."""
    assert keywords_match(_inc(term), [], hay) is True
