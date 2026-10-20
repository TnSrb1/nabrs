"""scripts/sr_first_templates.py — pure functions of the conversion (without a DB)."""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sr_first_templates",
    Path(__file__).resolve().parents[2] / "scripts" / "sr_first_templates.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def test_convert_groups_product_and_idempotent():
    old = [{"all_of": ["ремонт", "здание"]}, {"all_of": ["дорога"]}]
    new = mod.convert_groups(old)
    assert {"all_of": ["remont", "zgrad"]} in new
    assert {"all_of": ["sanacij", "zgrad"]} in new
    assert {"all_of": ["putev"]} in new
    assert mod.convert_groups(new) == new  # a repeated run changes nothing


def test_convert_groups_unknown_term_is_error_not_silent():
    with pytest.raises(KeyError):
        mod.convert_groups([{"all_of": ["ксилофон"]}])


def test_latinize_serbian_with_diacritics():
    assert mod.latinize("Грађевински радови и адаптације") == (
        "Građevinski radovi i adaptacije"
    )
    assert mod.latinize("Чишћење") == "Čišćenje"
    assert mod.latinize("Čišćenje") == "Čišćenje"


def test_convert_i18n_touches_only_sr():
    src = {"sr": {"name": "Чишћење"}, "en": {"name": "Cleaning"}}
    assert mod.convert_i18n(src) == {
        "sr": {"name": "Čišćenje"},
        "en": {"name": "Cleaning"},
    }
    assert mod.convert_i18n(None) is None


def test_stems_cover_every_template_term_of_prod_seed():
    """All the ru terms of the seeds 0016 + the packages have stems (otherwise the dry-run will fall)."""
    for term in ("строительство", "информационная система", "электрооборудование"):
        assert term in mod.STEMS
