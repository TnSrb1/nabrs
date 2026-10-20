"""Tests of compute_cluster_key (T6). Pure, without a network/DB.

The style — as ``tests/parsers/test_jnportal_normalize.py``: the factory
``_tender()`` assembles a ``NormalizedTender`` with a minimum of fields. It checks the determinism, the format (40 hex), the order of the components (country first,
ADR-033 addendum), the canonicalisation of amount/deadline/tax and the rule of incomplete data.

The formula — ``docs/DB_SCHEMA.md §7``.
"""

from __future__ import annotations

from datetime import datetime

from src.dedup.cluster_key import compute_cluster_key
from src.parsers.base import NormalizedTender

_HEX = set("0123456789abcdef")


def _tender(**kwargs: object) -> NormalizedTender:
    """NormalizedTender with the mandatory source_key/source_uid + an override of the fields."""
    base: dict[str, object] = {"source_key": "jnportal", "source_uid": "1"}
    base.update(kwargs)
    return NormalizedTender(**base)  # type: ignore[arg-type]


def _full(**overrides: object) -> NormalizedTender:
    """A tender with all the components of the key filled (for the control of equality)."""
    base: dict[str, object] = {
        "country_code": "RS",
        "customer_tax_id": "100000000",
        "classifications": [{"system_code": "eu_cpv", "code": "32200000"}],
        "amount_eur": 10000.0,
        "deadline_at": datetime(2026, 5, 20, 12, 0, 0),
    }
    base.update(overrides)
    return _tender(**base)


# --------------------------------------------------------------------------- #
#  Determinism and format
# --------------------------------------------------------------------------- #
def test_deterministic_same_input_same_key():
    t = _full()
    assert compute_cluster_key(t) == compute_cluster_key(t)


def test_key_is_40_char_hex():
    key = compute_cluster_key(_full())
    assert key is not None
    assert len(key) == 40
    assert set(key) <= _HEX


# --------------------------------------------------------------------------- #
#  country_code FIRST component (ADR-033 addendum, anti cross-country)
# --------------------------------------------------------------------------- #
def test_country_code_changes_key():
    rs = compute_cluster_key(_full(country_code="RS"))
    uz = compute_cluster_key(_full(country_code="UZ"))
    assert rs is not None and uz is not None
    assert rs != uz


def test_country_code_upper_strip_canonical():
    # "rs" and " RS " are canonicalised to "RS" → the same key
    assert compute_cluster_key(_full(country_code="rs")) == compute_cluster_key(
        _full(country_code=" RS ")
    )


# --------------------------------------------------------------------------- #
#  Rounding of amount_eur to 1000 (banker’s round, half-to-even)
# --------------------------------------------------------------------------- #
def test_amount_rounding_to_nearest_thousand():
    # 10499 and 9501 → both round(-3)=10000 → an identical key
    assert compute_cluster_key(_full(amount_eur=10499.0)) == compute_cluster_key(
        _full(amount_eur=9501.0)
    )


def test_amount_bankers_rounding_half_to_even():
    # round(10500, -3) == 10000 (half-to-even: 10 is even) — we pin it by the fact
    assert compute_cluster_key(_full(amount_eur=10500.0)) == compute_cluster_key(
        _full(amount_eur=10000.0)
    )


def test_amount_different_buckets_differ():
    # 10000 vs 12000 — different thousand buckets → different keys
    assert compute_cluster_key(_full(amount_eur=10000.0)) != compute_cluster_key(
        _full(amount_eur=12000.0)
    )


# --------------------------------------------------------------------------- #
#  Truncation of the deadline to the day
# --------------------------------------------------------------------------- #
def test_deadline_truncated_to_day():
    # a different time of the same day → an identical key
    morning = _full(deadline_at=datetime(2026, 5, 20, 9, 0, 0))
    evening = _full(deadline_at=datetime(2026, 5, 20, 18, 30, 45))
    assert compute_cluster_key(morning) == compute_cluster_key(evening)


def test_deadline_adjacent_days_differ():
    d20 = _full(deadline_at=datetime(2026, 5, 20, 12, 0, 0))
    d21 = _full(deadline_at=datetime(2026, 5, 21, 12, 0, 0))
    assert compute_cluster_key(d20) != compute_cluster_key(d21)


# --------------------------------------------------------------------------- #
#  Normalisation of tax_id (digits only)
# --------------------------------------------------------------------------- #
def test_tax_id_normalization_strips_non_digits():
    spaced = _full(customer_tax_id="RS 100 000 000")
    plain = _full(customer_tax_id="100000000")
    assert compute_cluster_key(spaced) == compute_cluster_key(plain)


# --------------------------------------------------------------------------- #
#  primary = the FIRST element of classifications
# --------------------------------------------------------------------------- #
def test_primary_classification_is_first_element():
    a = _full(
        classifications=[
            {"system_code": "eu_cpv", "code": "32200000"},
            {"system_code": "eu_cpv", "code": "30200000"},
        ]
    )
    b = _full(
        classifications=[
            {"system_code": "eu_cpv", "code": "30200000"},
            {"system_code": "eu_cpv", "code": "32200000"},
        ]
    )
    # a different first element → different keys (the order matters)
    assert compute_cluster_key(a) != compute_cluster_key(b)


# --------------------------------------------------------------------------- #
#  The rule of incomplete data
# --------------------------------------------------------------------------- #
def test_only_country_code_returns_none():
    # country_code='RS', everything else empty → there is nothing to cluster by
    t = _tender(country_code="RS")
    assert compute_cluster_key(t) is None


def test_country_plus_system_code_only_returns_none():
    # country + system_code, but code/tax/amount/deadline are empty → None
    # (system_code alone is not sufficient)
    t = _tender(
        country_code="RS",
        classifications=[{"system_code": "eu_cpv", "code": ""}],
    )
    assert compute_cluster_key(t) is None


def test_single_non_empty_field_returns_hash():
    # only deadline_at is non-empty → the key is built, 40 hex
    t = _tender(deadline_at=datetime(2026, 5, 20, 12, 0, 0))
    key = compute_cluster_key(t)
    assert key is not None
    assert len(key) == 40
    assert set(key) <= _HEX
