"""Unit tests of the SSOT predicate of the freeze (FIX-SG-01) — src/domain/access.py.

Pure functions without a DB/network: None / the past / the future / the boundary now, the inversion of is_frozen and the form of the SQL fragment are checked.

The second invariant of the same axis — the EXTENSION DOES NOT REDUCE the access (extend_until): “N days” are counted from the larger of the dates (the effective term / now), otherwise an extension for a term less than the remainder silently cuts the already granted access.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.access import (
    access_active,
    access_active_sql,
    extend_until,
    extend_until_sql,
    is_frozen,
)

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
PAST = NOW - timedelta(days=1)
FUTURE = NOW + timedelta(days=1)


@pytest.mark.parametrize(
    ("access_until", "expected_active"),
    [
        (None, True),  # a permanent access
        (FUTURE, True),  # the term has not expired yet
        (PAST, False),  # deadline passed
        (NOW, False),  # the boundary: == now is treated as expired
    ],
)
def test_access_active(access_until, expected_active):
    assert access_active(access_until, NOW) is expected_active


@pytest.mark.parametrize(
    ("access_until", "expected_frozen"),
    [
        (None, False),
        (FUTURE, False),
        (PAST, True),
        (NOW, True),
    ],
)
def test_is_frozen(access_until, expected_frozen):
    assert is_frozen(access_until, NOW) is expected_frozen


def test_is_frozen_is_inverse_of_active():
    for access_until in (None, PAST, NOW, FUTURE):
        assert is_frozen(access_until, NOW) is (not access_active(access_until, NOW))


@pytest.mark.parametrize(
    ("access_until", "days", "expected"),
    [
        # The effective access: the base is itself, and NOT now (otherwise the term would shrink).
        (NOW + timedelta(days=11), 3, NOW + timedelta(days=14)),
        (NOW + timedelta(days=30), 30, NOW + timedelta(days=60)),
        # Expired / not granted: the base is now.
        (PAST, 3, NOW + timedelta(days=3)),
        (None, 3, NOW + timedelta(days=3)),
        # The boundary «== now» is treated as expired (consistent with access_active).
        (NOW, 7, NOW + timedelta(days=7)),
    ],
)
def test_extend_until_base_is_the_later_point(access_until, days, expected):
    assert extend_until(access_until, days, NOW) == expected


def test_extend_until_never_shrinks_live_access():
    """The incident of 04.08.2026: access until +11d, an extension for 3 days → it was −11 days."""
    before = NOW + timedelta(days=11)
    after = extend_until(before, 3, NOW)
    assert after > before, "the extension must INCREASE the term"
    assert after == NOW + timedelta(days=14)
    assert after != NOW + timedelta(days=3), "it would be a setting, not an extension"


def test_extend_until_sql_mirrors_python_rule():
    """The SQL mirror for the points where the current term is unknown in Python."""
    frag = extend_until_sql("tenants", ":days")
    assert frag == (
        "GREATEST(COALESCE(tenants.access_until, now()), now()) "
        "+ (CAST(:days AS int) * INTERVAL '1 day')"
    )
    # asyncpg in text(): the number of days comes only via CAST(:p AS type).
    assert "CAST(:days AS int)" in frag


def test_access_active_sql_fragment():
    assert (
        access_active_sql("t") == "(t.access_until IS NULL OR t.access_until > now())"
    )
    # an arbitrary alias
    assert (
        access_active_sql("tenants")
        == "(tenants.access_until IS NULL OR tenants.access_until > now())"
    )
