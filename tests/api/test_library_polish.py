"""Library showcase: sr transliteration, package words, package↔template duplicates."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api.cabinet import template_library_service as svc
from src.api.cabinet import vertical_packages as vp
from src.domain.text_norm import cyr_to_lat, fold


@pytest.mark.parametrize(
    "src, expected",
    [
        ("Ђурђевак", "Đurđevak"),
        ("Љиљана", "Ljiljana"),
        ("Џеп", "Džep"),
        ("ЉУБАВ", "LJUBAV"),
        ("Рачунарска опрема", "Računarska oprema"),
        ("Чишћење, шума, жена", "Čišćenje, šuma, žena"),
        ("Already latin 123", "Already latin 123"),
        (None, ""),
    ],
)
def test_cyr_to_lat_keeps_diacritics(src, expected):
    assert cyr_to_lat(src) == expected


def test_fold_unchanged_by_display_translit():
    """Search folding still folds the diacritics (the ingest contract)."""
    assert fold("Чишћење") == "ciscenje"
    assert fold("Đurđevak") == "djurdjevak"


_STEMS = {"gradjevin", "izgradnj", "medicinsk", "oprem", "snabdevanje"}


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
@pytest.mark.parametrize("pkg", vp.PACKAGES, ids=lambda p: p.slug)
def test_every_package_has_readable_display_words(pkg, lang):
    """ALL 7 packages: display_words exist in three languages, these are not search stems."""
    assert len(vp.PACKAGES) == 7
    shown = next(p for p in vp.display_packages(lang) if p["slug"] == pkg.slug)[
        "keywords"
    ]
    assert shown == list(pkg.display_words[lang])
    assert shown
    assert not set(map(fold, shown)) & _STEMS
    if lang == "sr":
        assert not any(ord(c) > 0x24F for w in shown for c in w), shown  # Latin


def test_display_words_do_not_change_search_keywords():
    """Matching untouched: the search stems stay in keywords_include/prefill."""
    pre = vp.to_profile_prefill(vp.get_package("medoprema"), "sr")
    assert {"all_of": ["medicinsk", "oprem"]} in pre["keywords_include"]


def test_medoprema_tags_literal():
    sr = next(p for p in vp.display_packages("sr") if p["slug"] == "medoprema")
    assert sr["keywords"][0] == "medicinska oprema"


def _tpl(name, sr=None, en=None):
    i18n = {}
    if sr:
        i18n["sr"] = {"name": sr}
    if en:
        i18n["en"] = {"name": en}
    return SimpleNamespace(name=name, i18n=i18n or None)


def test_duplicate_detection_pairs():
    assert svc.duplicates_of_packages(
        _tpl("Медицинское оборудование", sr="Медицинска опрема"), vp.PACKAGES
    )
    # A ru-only template (without i18n) is also caught by its Russian name.
    assert svc.duplicates_of_packages(_tpl("Медицинское оборудование"), vp.PACKAGES)
    assert not svc.duplicates_of_packages(
        _tpl("Лекарства и фармацевтика", sr="Лекови и фармација"), vp.PACKAGES
    )
    assert not svc.duplicates_of_packages(
        _tpl("Компьютеры и оргтехника", sr="Рачунарска опрема"), vp.PACKAGES
    )


def test_seed_templates_duplicates_are_exactly_known_pairs():
    """The predicate over ALL 16 seeded templates: exactly the known pairs are hidden."""
    root = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    spec = importlib.util.spec_from_file_location(
        "m36", root / "0036_template_library_i18n.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    hidden = sorted(
        v["sr"]["name"]
        for v in m._TRANSLATIONS.values()
        if svc.duplicates_of_packages(
            _tpl("x", sr=v["sr"]["name"], en=v["en"]["name"]), vp.PACKAGES
        )
    )
    assert hidden == ["Медицинска опрема", "Чишћење и одржавање хигијене"]
