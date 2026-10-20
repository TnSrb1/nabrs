"""Unit: QuantizedNumeric quantises amount_* to 2 decimal places on write (cosmetics).

Without a network/DB — only the ``process_bind_param`` of the TypeDecorator is checked: the dirty float “tail” of a binary division (38461.5400…0873) → Decimal('38461.54').
"""

from decimal import Decimal

from src.db.models.tender import QuantizedNumeric


def test_quantizes_dirty_float() -> None:
    t = QuantizedNumeric()
    dirty = round(4500000 / 117, 2)  # float with a binary tail
    assert t.process_bind_param(dirty, None) == Decimal("38461.54")


def test_exactly_two_places() -> None:
    r = QuantizedNumeric().process_bind_param(0.0, None)
    assert r == Decimal("0.00")
    assert r.as_tuple().exponent == -2


def test_none_passthrough() -> None:
    assert QuantizedNumeric().process_bind_param(None, None) is None


def test_half_up() -> None:
    t = QuantizedNumeric()
    assert t.process_bind_param(38461.535, None) == Decimal("38461.54")
    assert t.process_bind_param(38461.545, None) == Decimal("38461.55")


def test_accepts_decimal() -> None:
    assert QuantizedNumeric().process_bind_param(
        Decimal("999999.999"), None
    ) == Decimal("1000000.00")
