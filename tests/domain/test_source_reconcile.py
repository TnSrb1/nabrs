"""The predicate of the divergence of the reconciliation M2 and its mirror in the SQL of the admin. The expectations — literals."""

from __future__ import annotations

from datetime import date

import pytest

from src.api.admin import sources_admin
from src.domain.source_reconcile import (
    build_reconcile_alert_text,
    is_mismatch,
    tolerance,
)


@pytest.mark.parametrize(
    ("source", "ours", "expected"),
    [
        (176, 175, False),  # the fact of 14.10: −1 — within tolerance
        (5, 7, False),  # a small number: tolerance 2 units
        (5, 8, True),
        (218, 218, False),
        (200, 196, False),  # 2% of 200 = 4 → the boundary included
        (200, 195, True),
        (1000, 979, True),  # 2% = 20
        (1000, 980, False),
        (100, 104, True),  # an excess on our side is also a divergence
        (0, 0, False),
        (0, 3, True),
    ],
)
def test_is_mismatch(source, ours, expected):
    assert is_mismatch(source, ours) is expected


def test_tolerance_floor_and_relative():
    assert tolerance(10) == 2.0
    assert tolerance(500) == 10.0


def test_sql_mirror_uses_same_tolerance():
    sql = " ".join(str(sources_admin._RECONCILE_SQL).split())
    assert "ABS(source_count - our_count) > GREATEST(2, 0.02 * source_count)" in sql


def test_alert_text_has_numbers_and_day():
    text_ = build_reconcile_alert_text(
        source_key="jnportal",
        source_label="Portal javnih nabavki",
        day=date(2026, 10, 14),
        source_count=176,
        our_count=150,
    )
    for needle in ("14.10.2026", "176", "150", "-26", "jnportal"):
        assert needle in text_
