"""The scoring of the pair TED ↔ jnportal — literals from the real pairs of production (the measurement 01.10.2026).

The expectations are FACTS of the measurement (``docs/reports/TESTING_ted_rs_dupes.md``), not a retelling of the code: the TED/jnportal pairs are checked by eye by the links to both cards.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.dedup import cross_source as cs
from src.dedup.cross_source import (
    DedupSide,
    cpv8_codes,
    deadline_minute,
    is_candidate,
    score_pair,
    toponyms,
)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _cpv(*codes: str) -> list[dict[str, str]]:
    return [{"scheme": "CPV", "code": c} for c in codes]


def _ted(**kw) -> DedupSide:
    return DedupSide.build(source_key="ted_balkan", **kw)


def _jn(**kw) -> DedupSide:
    return DedupSide.build(source_key="jnportal", **kw)


# --- Real pairs (TED publication-number ↔ jnportal id) ------------------ #

# Šabac: TED 415425-2026 ↔ jnportal 374179. TED gives no sum.
SABAC_TED = _ted(
    deadline_at=_utc(2026, 8, 7, 9, 0),
    classifications=_cpv("71314200"),
    customer_names=("Gradska uprava Grada Šapca", "City of Šabac"),
)
SABAC_JN = _jn(
    deadline_at=_utc(2026, 8, 7, 9, 0),
    classifications=_cpv("71314200")
    + [{"year": 2026, "scheme": "PLAN", "quarter": "2. квартал"}],
    amount_original=1665000000.00,
    currency_original="RSD",
    customer_names=("Градска управа града Шапца",),
)

# Valjevo: TED 561246-2026 ↔ jnportal 386180 — the sum to the cent.
VALJEVO_TED = _ted(
    deadline_at=_utc(2026, 10, 5, 10, 0),
    classifications=_cpv("60112000"),
    amount_original=2974186256.53,
    currency_original="RSD",
    customer_names=("City of Valjevo",),
)
VALJEVO_JN = _jn(
    deadline_at=_utc(2026, 10, 5, 10, 0),
    classifications=_cpv("60112000"),
    amount_original=2974186256.53,
    currency_original="RSD",
    customer_names=("Градска управа града Ваљева",),
)
# A false candidate of the same minute and CPV: a school (transport of pupils), 1.5 mln RSD.
VALJEVO_SCHOOL_JN = _jn(
    deadline_at=_utc(2026, 10, 5, 10, 0),
    classifications=_cpv("60112000"),
    amount_original=1500000.00,
    currency_original="RSD",
    customer_names=('ОСНОВНА ШКОЛА "БРАНКО РАДИЧЕВИЋ"',),
)

# Ministry: TED 593914-2026 ↔ jnportal 389783 — the sum, there is no toponym.
CLINIC_TED = _ted(
    deadline_at=_utc(2026, 10, 13, 10, 0),
    classifications=_cpv("45215140"),
    amount_original=7637500000.0,
    currency_original="RSD",
    customer_names=("Vlada Republike Srbije, Ministry for Public Investment",),
)
CLINIC_JN = _jn(
    deadline_at=_utc(2026, 10, 13, 10, 0),
    classifications=_cpv("45215140"),
    amount_original=7637500000.00,
    currency_original="RSD",
    customer_names=("МИНИСТАРСТВО ЗА ЈАВНА УЛАГАЊА",),
)

# Vrsac: TED 457605-2026 ↔ jnportal 377533. TED gives no sum.
VRSAC_TED = _ted(
    deadline_at=_utc(2026, 8, 20, 8, 0),
    classifications=_cpv("60112000"),
    customer_names=("City Administration of the City of Vršac",),
)
VRSAC_JN = _jn(
    deadline_at=_utc(2026, 8, 20, 8, 0),
    classifications=_cpv("60112000"),
    amount_original=3440000000.00,
    currency_original="RSD",
    customer_names=("Градска управа града Вршца",),
)

# A false candidate: TED 448860-2026 (ministry, roads) ↔ jnportal 385658
# (health house Loznica, painting works) — the common CPV 45200000 and the minute.
ROADS_TED = _ted(
    deadline_at=_utc(2026, 8, 14, 10, 0),
    classifications=_cpv("45200000"),
    customer_names=("Ministry of Construction, Transport and Infrastructure",),
)
LOZNICA_JN = _jn(
    deadline_at=_utc(2026, 8, 14, 10, 0),
    classifications=_cpv("45200000"),
    customer_names=("ЗДРАВСТВЕНИ ЦЕНТАР ЛОЗНИЦА",),
)


def test_valjevo_amount_and_toponym_is_dup_6():
    s = score_pair(VALJEVO_TED, VALJEVO_JN)
    assert (s.score, s.verdict) == (6, "dup")


def test_clinic_amount_only_is_dup_4():
    s = score_pair(CLINIC_TED, CLINIC_JN)
    assert (s.score, s.verdict) == (4, "dup")


def test_sabac_toponym_only_is_probable_3():
    s = score_pair(SABAC_TED, SABAC_JN)
    assert (s.score, s.verdict) == (3, "probable")


def test_vrsac_fleeting_a_toponym_is_probable_3():
    s = score_pair(VRSAC_TED, VRSAC_JN)
    assert (s.score, s.verdict) == (3, "probable")


def test_false_candidates_score_base_only_and_are_different():
    for a, b in ((VALJEVO_TED, VALJEVO_SCHOOL_JN), (ROADS_TED, LOZNICA_JN)):
        s = score_pair(a, b)
        assert (s.score, s.verdict) == (1, None)


def test_score_is_symmetric():
    assert score_pair(VALJEVO_JN, VALJEVO_TED) == score_pair(VALJEVO_TED, VALJEVO_JN)


def test_thresholds_are_d1_decision():
    # Thresholds — the owner’s decision 01.10.2026; an edit = a new decision, not a refactoring.
    assert (cs.THRESHOLD_DUP, cs.THRESHOLD_PROBABLE) == (4, 2)
    assert (cs.SCORE_BASE, cs.SCORE_AMOUNT, cs.SCORE_TOPONYM) == (1, 3, 2)


# --- Candidates ---------------------------------------------------------- #


def test_ted_seconds_59_fold_to_same_minute():
    # TED writes “the end of the minute” (10:00:59) — jnportal 10:00:00.
    a = _ted(deadline_at=_utc(2026, 10, 2, 10, 0, 59), classifications=_cpv("45000000"))
    b = _jn(deadline_at=_utc(2026, 10, 2, 10, 0, 0), classifications=_cpv("45000000"))
    assert is_candidate(a, b)


def test_next_minute_is_not_candidate():
    a = _ted(deadline_at=_utc(2026, 10, 2, 10, 1), classifications=_cpv("45000000"))
    b = _jn(deadline_at=_utc(2026, 10, 2, 10, 0), classifications=_cpv("45000000"))
    assert not is_candidate(a, b)
    assert score_pair(a, b).verdict is None


def test_cpv_parent_prefix_is_not_candidate():
    # Measurement: the hierarchical prefix (45200000 ⊃ 45233120) added only noise.
    a = _ted(deadline_at=_utc(2026, 8, 14, 10), classifications=_cpv("45200000"))
    b = _jn(deadline_at=_utc(2026, 8, 14, 10), classifications=_cpv("45233120"))
    assert not is_candidate(a, b)


def test_same_source_is_never_candidate():
    assert not is_candidate(VALJEVO_JN, VALJEVO_SCHOOL_JN)


def test_no_deadline_is_never_candidate():
    a = _ted(deadline_at=None, classifications=_cpv("60112000"))
    b = _jn(deadline_at=None, classifications=_cpv("60112000"))
    assert not is_candidate(a, b)


def test_amount_needs_same_currency():
    a = _ted(
        deadline_at=_utc(2026, 9, 9, 13),
        classifications=_cpv("43312200"),
        amount_original=650000,
        currency_original="EUR",
    )
    b = _jn(
        deadline_at=_utc(2026, 9, 9, 13),
        classifications=_cpv("43312200"),
        amount_original=650000,
        currency_original="RSD",
    )
    assert score_pair(a, b).score == 1


# --- Signs ---------------------------------------------------------------- #


def test_toponyms_cases_scripts_and_aliases():
    assert toponyms(["Градска управа града Шапца"]) == {"sabac"}
    assert toponyms(["City of Šabac"]) == {"sabac"}
    assert toponyms(["Градска управа Града Новог Сада"]) == {"novi sad"}
    assert toponyms(["ICE Agenzia Belgrado"]) == {"beograd"}
    assert toponyms(["Општинска управа Кладово"]) == {"kladovo"}


def test_toponyms_whole_words_only():
    assert toponyms(["Adaptacija prostorija"]) == frozenset()
    assert toponyms(["МИНИСТАРСТВО ЗА ЈАВНА УЛАГАЊА"]) == frozenset()


def test_cpv8_codes_skips_other_schemes_and_short_codes():
    cls = _cpv("71314200", "4520") + [{"scheme": "PLAN", "code": "12345678"}]
    assert cpv8_codes(cls) == {"71314200"}


def test_deadline_minute_naive_is_utc():
    assert deadline_minute(datetime(2026, 1, 1, 9, 0, 30)) == _utc(2026, 1, 1, 9, 0)


# --- Mode (flag) ------------------------------------------------------------ #


def test_dedup_mode_defaults_to_mark_only():
    # The owner’s decision 01.10.2026: the first month — only a marking.
    assert cs.suppress_duplicates_enabled({}) is False
    assert cs.suppress_duplicates_enabled({"CROSS_SOURCE_DEDUP_MODE": "mark"}) is False
    assert cs.suppress_duplicates_enabled({"CROSS_SOURCE_DEDUP_MODE": "off"}) is False
    assert cs.suppress_duplicates_enabled({"CROSS_SOURCE_DEDUP_MODE": " Suppress "})


def test_dedup_mode_is_passed_to_routing_container():
    # A flag not passed through into the container is not a flag (CLAUDE.md, REFERRAL_ENABLED).
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml"
        ).read_text()
    )
    env = compose["services"]["routing"]["environment"]
    assert env["CROSS_SOURCE_DEDUP_MODE"] == "${CROSS_SOURCE_DEDUP_MODE:-mark}"
