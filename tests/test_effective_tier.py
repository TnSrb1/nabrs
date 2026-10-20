"""Unit tests of the SSOT resolver of the effective tier (R1: trial = the Pro rights).

The tier axis (``effective_tier``) and the freeze axis (``is_frozen``) are SEPARATE: the resolver does not know about ``access_until``; a frozen trial is cut off BEFORE the tier at the gate sites (see test_trial_effective_tier*).
"""

from __future__ import annotations

import pytest

from src.domain.tiers import effective_tier, normalize_tier


@pytest.mark.parametrize("raw_tier", ["free", None, "", "lite", "pro", "business"])
def test_trial_status_always_pro(raw_tier):
    """status='trial' → 'pro' regardless of the raw tier (free/NULL/any)."""
    assert effective_tier("trial", raw_tier) == "pro"


@pytest.mark.parametrize("status", ["active", "expired", None, ""])
def test_free_or_null_without_trial_stays_lite(status):
    """free / NULL WITHOUT the trial status → lite (the gate as before, not Pro)."""
    assert effective_tier(status, "free") == "lite"
    assert effective_tier(status, None) == "lite"


@pytest.mark.parametrize("tier", ["pro", "business", "enterprise", "starter", "lite"])
def test_known_tiers_unchanged_for_non_trial(tier):
    """The known plans of the non-trial are returned as they are (the behavior is not changed)."""
    assert effective_tier("active", tier) == tier


def test_trial_case_insensitive():
    assert effective_tier("TRIAL", "free") == "pro"
    assert effective_tier(" Trial ", None) == "pro"


def test_reexport_from_api_layer_is_same_object():
    """src.api.tiers re-exports the domain resolver (backward compatibility)."""
    from src.api import tiers as api_tiers

    assert api_tiers.effective_tier is effective_tier
    assert api_tiers.normalize_tier is normalize_tier
