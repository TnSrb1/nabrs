"""Template words find English-language tenders (World Bank, EBRD): dictionary en-forms
and the word boundary. Expectations are literals; the Serbian path does not change."""

from __future__ import annotations

from src.domain.text_norm import fold
from src.workers.adapters.base import match_reason_of
from src.workers.keyword_expand import expand_term_en, term_forms
from src.workers.matching import (
    is_english_tender,
    keywords_match,
    match_reason,
)

ROAD = "Construction of the regional road Section 3"
SOFT = "Supply of Software licenses for the ministry"
COMP = "Purchase of computer input devices and output units"


def _m(words, text, *, dict_on=True, english=True, exclude=None):
    return keywords_match(
        [{"all_of": words}], exclude, fold(text), dict_on, english=english
    )


def test_ru_word_finds_english_tender():
    assert _m(["строительство"], ROAD)
    assert not _m(["строительство"], ROAD, english=False)


def test_sr_stem_finds_english_tender_via_reverse_path():
    assert _m(["građevin"], ROAD)
    assert "construction" in expand_term_en("građevin")


def test_softver_finds_software_licenses():
    assert _m(["softver"], SOFT)
    assert _m(["licenc"], SOFT)


def test_put_does_not_match_inside_english_words():
    assert not _m(["put"], COMP)
    assert not _m(["put"], COMP, dict_on=False)


def test_word_start_boundary_is_load_bearing():
    # Serbian mode (substring) on the same text catches 'put' in 'input' — the boundary is needed.
    assert _m(["put"], COMP, dict_on=False, english=False)


def test_exclude_uses_english_forms():
    assert not _m(["строительство"], ROAD, exclude=["дорога"])


def test_serbian_tender_unchanged():
    """Serbian mode (english=False): expectations as literals on real titles."""
    road = "Izgradnja i rekonstrukcija puta Beograd – Novi Sad"
    it = "Nabavka računarske opreme i softvera"
    school = "Радови на санацији зграде основне школе"
    clean = "Održavanje javnih površina i čišćenje"
    assert _m(["put"], road, english=False)
    assert not _m(["softver"], road, english=False)
    assert _m(["softver"], it, english=False)
    assert _m(["računar"], it, english=False)
    assert _m(["zgrad"], school, english=False)
    assert not _m(["zgrad"], clean, english=False)
    assert _m(["čišćenj"], clean, english=False)
    assert _m(["održavanj"], clean, english=False)
    # substring inside a word — the Serbian mode FINDS it (in English — it does not).
    assert _m(["put"], COMP, english=False)
    assert not _m(["put"], COMP, english=True)


def test_match_reason_returns_template_word_on_english_match():
    r = match_reason(
        [{"all_of": ["строительство"]}],
        None,
        fold(ROAD),
        keyword_dict_enabled=True,
        english=True,
    )
    assert r.matched_keywords == ["строительство"]
    r = match_reason(
        [{"all_of": ["put"]}], None, fold(COMP), keyword_dict_enabled=True, english=True
    )
    assert r.matched_keywords == []


def test_is_english_tender():
    assert is_english_tender("en", None)
    assert is_english_tender(None, "worldbank")
    assert is_english_tender(None, "ebrd")
    assert not is_english_tender("sr", "jnportal")
    assert not is_english_tender(None, None)


def test_term_forms_exposes_en():
    out = term_forms(["строительство", "xyzzy"])
    assert "construction" in out[0]["forms_en"]
    assert out[1]["forms_en"] == []


def test_every_article_has_en():
    from src.workers.keyword_expand import _load_dict

    assert all(v.get("en") for v in _load_dict().values())


def test_match_reason_of_uses_source_key():
    from tests.workers.test_why_matched_keyword_dict import _job

    wb = _job(
        source_key="worldbank", tender_haystack=fold(ROAD), profile_keyword_dict=True
    )
    assert match_reason_of(wb).matched_keywords == ["строительство"]
    # 'put' in English text from worldbank — not a match (word boundary).
    put = _job(
        source_key="worldbank",
        tender_haystack=fold(COMP),
        profile_keywords_include=[{"all_of": ["put"]}],
        profile_keyword_dict=True,
    )
    assert match_reason_of(put).matched_keywords == []


def test_stage_b_filter_applies_english_mode_by_source():
    from src.workers.matching import Candidate, TenderView, stage_b_filter

    cand = Candidate(
        profile_id="p",
        tenant_id="t",
        keywords_include=[{"all_of": ["put"]}],
        keywords_exclude=None,
        ai_analysis_level=None,
        delivery_channels=None,
        keyword_dict=True,
    )

    def view(source_key, language):
        return TenderView(
            id="1",
            country_code="RS",
            source_key=source_key,
            language=language,
            title_normalized=fold(COMP),
            description_normalized="",
        )

    assert stage_b_filter([cand], view("worldbank", "en")) == []
    assert stage_b_filter([cand], view("jnportal", "sr")) == [cand]


# --- routing ⇔ delivery parity (PR #801 rework) -------------------------------

#: (language, source_key, text) — real title forms of ted_balkan/worldbank.
PARITY_TENDERS = [
    ("en", "ted_balkan", "Construction of a regional road section"),
    ("en", "ted_balkan", "Supply of software licences for the ministry"),
    ("en", "ted_balkan", "Maintenance and repair of public buildings"),
    ("en", "ted_balkan", "Purchase of computer input devices and output units"),
    ("en", "worldbank", "Consultant for road safety capacity building"),
    ("en", "worldbank", "Cleaning services for the office premises"),
    ("sr", "jnportal", "Izgradnja puta Beograd - Novi Sad"),
]
PARITY_TERMS = [["строительство"], ["softver"], ["put"], ["održavanj"], ["чистка"]]


def test_delivery_reason_matches_routing_predicate():
    """Delivery (match_reason_of by DeliveryJob.language) ⇔ routing (keywords_match)."""
    from tests.workers.test_why_matched_keyword_dict import _job

    checked = 0
    for lang, src, text in PARITY_TENDERS:
        english = is_english_tender(lang, src)
        for words in PARITY_TERMS:
            routed = keywords_match(
                [{"all_of": words}], None, fold(text), True, english=english
            )
            job = _job(
                source_key=src,
                language=lang,
                tender_haystack=fold(text),
                profile_keywords_include=[{"all_of": words}],
                profile_keyword_dict=True,
            )
            assert bool(match_reason_of(job).matched_keywords) == routed, (
                words,
                text,
            )
            checked += routed
    assert checked >= 6  # the set is not degenerate: there are both matches and non-matches


def test_delivery_uses_job_language_not_only_source():
    from tests.workers.test_why_matched_keyword_dict import _job

    job = _job(
        source_key="ted_balkan",
        language="en",
        tender_haystack=fold(ROAD),
        profile_keyword_dict=True,
    )
    assert match_reason_of(job).matched_keywords == ["строительство"]


def test_reverse_path_prefers_main_form_of_article():
    # "održavanj" — the MAIN form of the entry about maintenance: without repair/cleaning neighbouring entries.
    assert expand_term_en("održavanj") == ("maintenance",)
    assert "maintenance" not in expand_term_en("građevin")
    assert expand_term_en("građevin") == (
        "construction",
        "civil works",
        "rehabilitation of",
    )
    # no entry with such a MAIN form → the previous rule (any form)
    assert expand_term_en("rekonstrukcij")


def test_highlight_en_forms_respect_word_boundary():
    from src.api.cabinet.preview_service import highlight_parts

    assert highlight_parts("Broad consultation", [], ["road"]) == [
        ("Broad consultation", False)
    ]
    assert highlight_parts("Regional road works", [], ["road"]) == [
        ("Regional ", False),
        ("road", True),
        (" works", False),
    ]


def test_short_en_form_needs_word_boundary_on_both_sides():
    # "car" ≤4 letters: "cardiology"/"cargo" — another word, not a match.
    assert not _m(["car"], "Cardiology services for the hospital")
    assert not _m(["car"], "Cargo handling terminal upgrade")
    assert not _m(["bus"], "Technical Assistance to Ministry of Business Development")


def test_short_en_form_still_matches_whole_word_and_plural():
    assert _m(["car"], "Purchase of cars for the agency")
    assert _m(["car"], "Car rental services")
    assert _m(["bus"], "Procurement of buses for the city")


def test_gips_reconstruction_does_not_match_maintenance_tenders():
    # "rekonstrukcij" → en entries for "remont" (repair): without maintenance (that is operation, not repair).
    assert "maintenance" not in expand_term_en("rekonstrukcij")
    assert not _m(["rekonstrukcij"], "Maintenance of automatic stop ramps")
    assert _m(["rekonstrukcij"], "Rehabilitation of the regional road")


def test_guard_word_does_not_match_generic_protection():
    # "ohrana" (obezbeđenje/čuvanje) — not "protection" (that is zaštita, measurement 17.10).
    assert "protection" not in expand_term_en("охрана")
    assert not _m(
        ["охрана"], "Critical infrastructure backup system and ransomware protection"
    )
    assert _m(["охрана"], "Physical security services for the premises")


def test_equipment_exclusion_catches_hardware_title():
    # "Remote services" excludes "equipment": "Hardware …" — the same equipment.
    hsm = "Support, Maintenance and Licences for Hardware Security Modules (HSMs)"
    assert _m(["лицензия"], hsm)
    assert not _m(["лицензия"], hsm, exclude=["оборудование"])
