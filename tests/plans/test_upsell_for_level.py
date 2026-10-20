"""The upsell by level: which tier to offer when the client has hit the limit.

The owner’s requirement of 15.08.2026: a refusal by the limit must offer “a more expensive tier, with a larger limit of AI requests”. Here it is pinned WHAT exactly counts as such a tier — and that the offer cannot diverge from the gating: the thresholds are taken from ``src.ai.usage._TIER_LIMITS``, the price and the name — from the catalog of the showcase.
"""

from __future__ import annotations

from src.ai.usage import _TIER_LIMITS
from src.plans import catalog


def test_lite_gets_pro_for_bid():
    """Free/Lite: there is no verdict at all (0) → the nearest plan where it exists."""
    plan = catalog.upsell_for_level("bid", "lite")
    assert plan is not None and plan.key == "pro"
    assert catalog.level_limit_for_plan(plan, "bid") == _TIER_LIMITS["pro"]["bid"]


def test_pro_gets_business_for_auto_analysis():
    """Pro hit the auto-analysis (400) → Business (1500), not “the same Pro”."""
    plan = catalog.upsell_for_level("standard", "pro")
    assert plan is not None and plan.key == "business"
    assert catalog.level_limit_for_plan(plan, "standard") == 1500


def test_business_has_nothing_to_sell():
    """The top of the ladder: enterprise is not offered for self-service → None.

    ``None`` means “there is nothing to sell”, NOT “stay silent”: in this case the template shows the fact of the exhaustion and the date of the limit’s renewal.
    """
    assert catalog.upsell_for_level("standard", "business") is None


def test_level_absent_on_higher_plan_is_not_an_upsell():
    """A plan where the level is absent (0) is not counted as an upsell — it would be a downgrade."""
    limits = {
        "lite": {"deep": 0},
        "pro": {"deep": 0},  # the level is absent on the “more expensive” one too
        "business": {"deep": 5},
        "enterprise": {"deep": None},
    }
    plan = catalog.upsell_for_level("deep", "lite", tier_limits=limits)
    assert plan is not None and plan.key == "business"


def test_unlimited_current_has_no_upsell():
    """Already unlimited at the level → there is nothing to offer (∞ is not “less” than ∞)."""
    limits = {
        "lite": {"bid": None},
        "pro": {"bid": None},
        "business": {"bid": None},
        "enterprise": {"bid": None},
    }
    assert catalog.upsell_for_level("bid", "lite", tier_limits=limits) is None


def test_trial_is_read_as_its_own_tier():
    """The plan is resolved via the catalog: an unknown value = free, not a failure."""
    assert catalog.upsell_for_level("bid", None) is not None
    assert catalog.upsell_for_level("bid", "нет-такого-тарифа") is not None
