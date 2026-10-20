"""Strengthening of the tests of compute_cluster_key (T6) — the reliability of the deduplication key.

It complements ``test_cluster_key.py``: the separator as the anti-collision of gluing of adjacent
components, the significance of the position of a component (country FIRST — ADR-033),
the sufficiency of one non-empty field, the difference of amount=0 from amount=None and the filtering of
a tax_id of non-digits only.

The formula — ``docs/DB_SCHEMA.md §7``. Pure, without a network/DB.
"""

from __future__ import annotations

from datetime import datetime

from src.dedup.cluster_key import compute_cluster_key
from src.parsers.base import NormalizedTender

_HEX = set("0123456789abcdef")


def _tender(**kwargs: object) -> NormalizedTender:
    base: dict[str, object] = {"source_key": "jnportal", "source_uid": "1"}
    base.update(kwargs)
    return NormalizedTender(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
#  The separator "|" — the anti-collision of the gluing of neighbouring components
# --------------------------------------------------------------------------- #
def test_separator_prevents_component_shift_collision():
    # The same set of digits in DIFFERENT components (tax vs code) gives different
    # keys — without the separator "123456789||" and "|123456789|" would be glued into one.
    tax_only = compute_cluster_key(_tender(customer_tax_id="123456789"))
    code_only = compute_cluster_key(
        _tender(classifications=[{"system_code": "", "code": "123456789"}])
    )
    assert tax_only is not None and code_only is not None
    assert tax_only != code_only


def test_system_code_position_matters_when_code_present():
    # system_code — a separate component: its change with an equal code changes the key
    a = compute_cluster_key(
        _tender(classifications=[{"system_code": "eu_cpv", "code": "32200000"}])
    )
    b = compute_cluster_key(
        _tender(classifications=[{"system_code": "rs_cpv", "code": "32200000"}])
    )
    assert a is not None and b is not None
    assert a != b


# --------------------------------------------------------------------------- #
#  The sufficiency of ONE non-empty field (the rule of incomplete data)
# --------------------------------------------------------------------------- #
def test_tax_id_only_builds_key():
    key = compute_cluster_key(_tender(customer_tax_id="100000000"))
    assert key is not None
    assert len(key) == 40 and set(key) <= _HEX


def test_amount_only_builds_key():
    key = compute_cluster_key(_tender(amount_eur=50000.0))
    assert key is not None
    assert len(key) == 40 and set(key) <= _HEX


def test_classification_code_only_builds_key():
    key = compute_cluster_key(
        _tender(classifications=[{"system_code": "eu_cpv", "code": "32200000"}])
    )
    assert key is not None
    assert len(key) == 40 and set(key) <= _HEX


# --------------------------------------------------------------------------- #
#  amount: 0 ≠ None (0 is a valid thousand bucket, not “no data”)
# --------------------------------------------------------------------------- #
def test_amount_zero_builds_key_unlike_none():
    # amount_eur=0.0 → "0" (not empty) → the key is built, although the other fields are empty
    key = compute_cluster_key(_tender(amount_eur=0.0))
    assert key is not None


def test_amount_zero_differs_from_country_only_none():
    # 0.0 gives a key, and a bare country_code — None: a falsy amount value must not
    # silently be equated with “no data”
    assert compute_cluster_key(_tender(amount_eur=0.0)) is not None
    assert compute_cluster_key(_tender(country_code="RS")) is None


# --------------------------------------------------------------------------- #
#  tax_id of non-digits only → empty → not counted as a filled field
# --------------------------------------------------------------------------- #
def test_tax_id_all_non_digits_treated_as_empty():
    # "ABC-DEF" → after the filtering of non-digits "" → the single field is empty → None
    assert (
        compute_cluster_key(_tender(country_code="RS", customer_tax_id="ABC")) is None
    )


def test_tax_id_mixed_keeps_only_digits_for_key():
    # a mixed tax → the same key as “digits only” from it
    mixed = compute_cluster_key(_tender(customer_tax_id="RS-100/000/000"))
    plain = compute_cluster_key(_tender(customer_tax_id="100000000"))
    assert mixed is not None and mixed == plain


# --------------------------------------------------------------------------- #
#  The stability of the key for one input over time
# --------------------------------------------------------------------------- #
def test_key_stable_across_repeated_calls():
    t = _tender(
        country_code="RS",
        customer_tax_id="100000000",
        classifications=[{"system_code": "eu_cpv", "code": "32200000"}],
        amount_eur=10000.0,
        deadline_at=datetime(2026, 5, 20, 12, 0, 0),
    )
    keys = {compute_cluster_key(t) for _ in range(5)}
    assert len(keys) == 1
