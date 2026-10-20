"""Unit tests of the pure helpers of the card formatting v6 (src.workers.adapters.base).

No network/DB. We check all the branches: format_amount_pair (3 branches + estimated), priority_stars (5 thresholds), contract_type_label, procedure_type_label, regions_label, format_published, format_amount, cpv_label, plan_period_label.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.workers.adapters.base import (
    contract_type_label,
    cpv_label,
    format_amount,
    format_amount_pair,
    format_deadline,
    format_published,
    plan_period_label,
    priority_stars,
    procedure_type_label,
    regions_label,
)


@pytest.mark.parametrize(
    "amount,expected",
    [
        (None, ""),
        (0, ""),
        (-5, ""),
        (49_999, "⭐"),
        (50_000, "⭐⭐"),
        (249_999, "⭐⭐"),
        (250_000, "⭐⭐⭐"),
        (999_999, "⭐⭐⭐"),
        (1_000_000, "⭐⭐⭐⭐"),
        (5_000_000, "⭐⭐⭐⭐"),
        ("not-a-number", ""),
    ],
)
def test_priority_stars_thresholds(amount, expected):
    assert priority_stars(amount) == expected


def test_format_amount_pair_both_currencies():
    assert (
        format_amount_pair(12_345_678, "RSD", 105_200, None)
        == "12 345 678 RSD (~105 200 EUR)"
    )


def test_format_amount_pair_eur_only():
    assert format_amount_pair(None, None, 105_200, None) == "105 200 EUR"


def test_format_amount_pair_original_only():
    assert format_amount_pair(12_345_678, "RSD", None, None) == "12 345 678 RSD"


def test_format_amount_pair_none_when_no_amounts():
    assert format_amount_pair(None, None, None, None) is None


def test_format_amount_pair_estimated_marks_tilde_and_note():
    out = format_amount_pair(12_345_678, "rsd", 105_200, True)
    assert out == "~12 345 678 RSD (~105 200 EUR) (оценочно)"


def test_format_amount_pair_original_without_currency_is_dropped():
    # without a currency the original is not formatted; only EUR remains
    assert format_amount_pair(12_345_678, None, 105_200, None) == "105 200 EUR"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("works", "Работы"),
        ("Services", "Услуги"),
        ("GOODS", "Поставка"),
        ("usluge", "Услуги"),
        ("неизвестный", "неизвестный"),
        (None, None),
        ("   ", None),
    ],
)
def test_contract_type_label(value, expected):
    assert contract_type_label(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("open", "Открытая процедура"),
        ("small_value", "Закупка малой стоимости"),
        ("framework", "Рамочное соглашение"),
        ("weird_code", "weird_code"),
        (None, None),
        ("", None),
    ],
)
def test_procedure_type_label(value, expected):
    assert procedure_type_label(value) == expected


# --- the chrome labels by the recipient’s language (job.locale; None → ru, as before) ----- #


@pytest.mark.parametrize(
    "locale,contract,procedure",
    [
        ("sr", "Radovi", "Otvoreni postupak"),
        ("en", "Works", "Open procedure"),
        ("ru", "Работы", "Открытая процедура"),
        (None, "Работы", "Открытая процедура"),
        ("de", "Работы", "Открытая процедура"),  # an unknown code → ru explicitly
    ],
)
def test_labels_follow_locale(locale, contract, procedure):
    assert contract_type_label("works", locale) == contract
    assert procedure_type_label("open", locale) == procedure


def test_unknown_code_is_returned_as_is_in_any_locale():
    """We do not invent a label for an unknown code in any language."""
    for locale in ("sr", "en", None):
        assert contract_type_label("weird", locale) == "weird"
        assert procedure_type_label("art27_exempt", locale) == "art27_exempt"


@pytest.mark.parametrize(
    "locale,expected",
    [
        ("sr", "~12 345 678 RSD (~105 200 EUR) (procenjeno)"),
        ("en", "~12 345 678 RSD (~105 200 EUR) (estimated)"),
        (None, "~12 345 678 RSD (~105 200 EUR) (оценочно)"),
    ],
)
def test_format_amount_pair_estimated_note_follows_locale(locale, expected):
    """Numbers and currency are data (identical), “estimated” is chrome (by language)."""
    assert format_amount_pair(12_345_678, "rsd", 105_200, True, locale) == expected


@pytest.mark.parametrize(
    "regions,expected",
    [
        (["Београд", "Ниш"], "Београд, Ниш"),
        (["Београд"], "Београд"),
        ([], None),
        (None, None),
        (["", "  "], None),
        (["A", "", "B"], "A, B"),
    ],
)
def test_regions_label(regions, expected):
    assert regions_label(regions) == expected


def test_format_published_date_only():
    dt = datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)
    assert format_published(dt) == "20.05.2026"
    assert format_published(None) is None


def test_format_deadline_with_time():
    # format_deadline converts UTC → Belgrade (the boundary of the presentation): June
    # (CEST +2), 14:30Z → 16:30 local. Mutation: remove to_local in base.py →
    # «14:30» comes back and the test turns red.
    dt = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
    assert format_deadline(dt) == "01.06.2026 16:30"
    assert format_deadline(None) is None


def test_format_deadline_winter_offset_differs_from_summer():
    """Winter (CET +1): 14:30Z → 15:30 — the shift is NOT the same 2 h as in summer (DST)."""
    dt = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)
    assert format_deadline(dt) == "01.01.2026 15:30"


def test_format_amount_eur_single():
    assert format_amount(1_234_567.0) == "1 234 567 EUR"
    assert format_amount(None) is None


# --------------------------------------------------------------------------- #
#  cpv_label — the CPV record from classifications (Chat 44)
# --------------------------------------------------------------------------- #
_CPV = {"scheme": "CPV", "code": "45000000", "label": "Грађевински радови"}
_PLAN = {"scheme": "PLAN", "quarter": "2. квартал", "year": 2026}


def test_cpv_label_code_and_label_from_list():
    assert cpv_label([_CPV, _PLAN]) == "45000000 — Грађевински радови"


def test_cpv_label_accepts_single_dict():
    assert cpv_label(_CPV) == "45000000 — Грађевински радови"


def test_cpv_label_code_only():
    assert cpv_label([{"scheme": "CPV", "code": "45000000"}]) == "45000000"


def test_cpv_label_label_only():
    assert cpv_label([{"scheme": "CPV", "label": "Радови"}]) == "Радови"


def test_cpv_label_none_and_no_cpv_record():
    assert cpv_label(None) is None
    assert cpv_label([]) is None
    assert cpv_label([_PLAN]) is None  # only PLAN, no CPV
    assert cpv_label("not-a-classification") is None


def test_cpv_label_scheme_case_insensitive():
    assert cpv_label([{"scheme": "cpv", "code": "45000000"}]) == "45000000"


# --------------------------------------------------------------------------- #
#  plan_period_label — the PLAN record from classifications (Chat 45)
# --------------------------------------------------------------------------- #
def test_plan_period_label_quarter_and_year_from_list():
    assert plan_period_label([_CPV, _PLAN]) == "2. квартал 2026"


def test_plan_period_label_accepts_single_dict():
    assert plan_period_label(_PLAN) == "2. квартал 2026"


def test_plan_period_label_quarter_only():
    rec = {"scheme": "PLAN", "quarter": "2. квартал", "year": None}
    assert plan_period_label([rec]) == "2. квартал"


def test_plan_period_label_year_only():
    rec = {"scheme": "PLAN", "quarter": None, "year": 2026}
    assert plan_period_label([rec]) == "2026"


def test_plan_period_label_none_and_no_plan_record():
    assert plan_period_label(None) is None
    assert plan_period_label([]) is None
    assert plan_period_label([_CPV]) is None  # only CPV, no PLAN
    rec = {"scheme": "PLAN", "quarter": None, "year": None}
    assert plan_period_label([rec]) is None  # both are empty


def test_plan_period_label_scheme_case_insensitive():
    rec = {"scheme": "plan", "quarter": None, "year": 2026}
    assert plan_period_label([rec]) == "2026"


# PIN (DocumentTypeId 3/18) i sistem kvalifikacije (10) — ožidanie literalom iz
# of the catalog pt_pin / pt_qualification; mutation (remove "pin" from _PROCEDURE_TYPE_KEYS)
# must turn exactly this place red.
@pytest.mark.parametrize(
    "locale,label",
    [
        ("sr", "Prethodno informativno obaveštenje"),
        ("ru", "Предварительное информационное уведомление (PIN)"),
        ("en", "Prior information notice"),
    ],
)
def test_pin_label_in_every_locale(locale, label):
    assert procedure_type_label("pin", locale) == label
