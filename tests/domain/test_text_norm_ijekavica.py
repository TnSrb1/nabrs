"""Ijekavica → ekavica in the single fold (BA/ME, 15.10.2026).

The expectations — literals from the real titles: BA — the fixtures of tests/parsers/
test_bih_enabavke.py (the live OData of e-Nabavke), RS — jnportal for 30 days
(``tenders_normalized.title_normalized``, the measurement in docs/reports/TESTING_*).
"""

from __future__ import annotations

import itertools
import unicodedata

import pytest

from src.domain.text_norm import CYR2LAT, fold, fold_ijekavica, normalize_text


@pytest.mark.parametrize(
    "ijekavian,ekavian",
    [
        # BA: real e-Nabavke titles
        (
            "Sistematski pregled djelatnika Ustanove",
            "sistematski pregled delatnika ustanove",
        ),
        ("Rezervni dijelovi za Joy drobilicu", "rezervni delovi za joy drobilicu"),
        (
            "Unaprjeđenje sportske infrastrukture",
            "unapredjenje sportske infrastrukture",
        ),
        ("Odsjeka za administraciju u Sjedištu", "odseka za administraciju u sedistu"),
        # examples from the S4/S5 reconnaissance
        ("cijevi", "cevi"),
        ("cjevovodi", "cevovodi"),
        ("mlijeko", "mleko"),
        ("rijeka", "reka"),
        ("dijete", "dete"),
        ("snijeg", "sneg"),
        ("snabdijevanje", "snabdevanje"),
        ("djeca", "deca"),
        ("mjesto", "mesto"),
        ("rješenje", "resenje"),
        ("gdje", "gde"),
        ("cijena", "cena"),
        ("cijeli", "celi"),
        # Cyrillic of the ijekavica is folded the same way as Latin
        ("мљијеко", "mleko"),
        ("дјеца", "deca"),
    ],
)
def test_ijekavian_folds_to_ekavian(ijekavian: str, ekavian: str) -> None:
    assert normalize_text(ijekavian) == ekavian


def _legacy_fold(value: str) -> str:
    """The fold BEFORE the fixes: cyrl→latn → lower → đ→dj → unaccent (without the ijekavica)."""
    out = "".join(CYR2LAT.get(ch, ch) for ch in value).lower().replace("đ", "dj")
    return "".join(
        c for c in unicodedata.normalize("NFKD", out) if not unicodedata.combining(c)
    )


@pytest.mark.parametrize(
    "serbian",
    [
        # Real RS words where «ije»/«je» is not the yat (a corpus measurement, 30 days)
        "ekskurzije", "dokumentacije", "energije", "hemikalije", "higijene",
        "pacijenata", "prijem", "dijeta", "decijeg", "najpovoljnijeg", "barijere",
        "izvođenje", "građevinski", "uređenje", "područje", "dečje", "oružje",
        "unapređenje", "obezbeđenje", "izgradnje", "zdravlje", "ponovljeni",
        "krpljenje", "objekta", "projekta", "adjektiv", "Kraljevo", "Valjevo",
        "Boljevac", "Đerdap", "Karađorđe", "Anđelić", "gospođe",
    ],
)  # fmt: skip
def test_serbian_words_keep_their_fold(serbian: str) -> None:
    """The rule must not change a Serbian word relative to the fold without it."""
    assert normalize_text(serbian) == _legacy_fold(serbian)


def test_serbian_and_ijekavian_titles_meet_in_one_form() -> None:
    """The key of a Serbian profile finds the ijekavica title of BA, and vice versa."""
    ba_title = fold("Rezervni dijelovi za bager Liebherr")
    assert fold("rezervni delovi") in ba_title
    # and vice versa: the ijekavica key finds the ekavica title of RS
    assert fold("mlijek") in fold("Nabavka mleka i mlečnih proizvoda")


def test_idempotent_on_synthetic_alphabet() -> None:
    """fold(fold(x)) == fold(x): the haystack is safe to fold again."""
    alphabet = "ijedcsnkvgmh "
    for n in range(1, 7):
        for combo in itertools.islice(
            itertools.product(alphabet, repeat=n), 0, None, 13
        ):
            word = "".join(combo)
            once = fold_ijekavica(word)
            assert fold_ijekavica(once) == once, word


def test_cyrillic_equals_latin() -> None:
    """The symmetry of the alphabets: the ijekavica in Cyrillic == in Latin."""
    assert fold("Цијеви") == fold("cijevi") == "cevi"
    assert fold("Мјесто") == fold("mjesto") == "mesto"


def test_plain_fold_skips_ijekavica_for_sql_paths():
    """``ijekavica=False`` — paired with the SQL fold of the columns; the input and the column are equal."""
    assert fold("Bijelo Polje") == "belo polje"
    assert fold("Bijelo Polje", ijekavica=False) == "bijelo polje"
    assert normalize_text("cijevi", ijekavica=False) == "cijevi"


def test_portal_filters_keep_plain_tokens_for_customer_sql():
    from src.api.public.portal_filters import parse_filters

    f = parse_filters({"q": "Bijelo Polje", "customer": "Bijelo Polje"})
    assert f.q_tokens == ("belo", "polje")
    assert f.q_tokens_plain == ("bijelo", "polje")
    assert f.customer_folded == "bijelo polje"


def test_prefix_key_ending_in_ije_is_not_folded_documented_limit():
    """A known limit: the key «cije» is not folded, the haystack «cijevi»→«cevi»."""
    assert fold("cije") == "cije"
    assert fold("cijevi") == "cevi"
    assert fold("cije") not in fold("cijevi")
