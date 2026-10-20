"""``exp`` (the expiry horizon) from the query: only a whitelist, without ``int()`` on garbage.

``str.isdigit()`` lets through ``'²'`` (superscript two) and other Unicode “digits”,
which ``int()`` does not take → ValueError → 500 on ``/app/analitika?subject=awards&exp=²``.
The expectations are literals: the admitted horizons are 3/6/12, everything else — 0.
"""

from __future__ import annotations

import pytest

from src.analytics import report_builder as rb


@pytest.mark.parametrize("raw, expected", [("3", 3), ("6", 6), ("12", 12)])
def test_exp_allowed_horizons(raw, expected):
    spec = rb.parse_spec({"subject": "awards", "exp": raw})
    assert spec.expiring_months == expected


@pytest.mark.parametrize("raw", ["²", "³", "¹²", "٣", "7", "0", "-1", "3x", ""])
def test_exp_garbage_falls_back_to_zero(raw):
    spec = rb.parse_spec({"subject": "awards", "exp": raw})
    assert spec.expiring_months == 0
