"""Unit tests of the SSOT catalog of the plans (VG-12).

We check: the mapping plan→tier, the prices, the limits are consistent with tiers.py/usage.py,
display_plans() does not include enterprise, tier_to_plan_key works correctly.
"""

from __future__ import annotations

from src.ai.usage import _TIER_LIMITS
from src.api.tiers import PROFILE_LIMITS, TIER_ORDER
from src.bots.awards_analytics_service import _is_pro_plus
from src.plans.catalog import (
    CATALOG,
    FEATURE_KEYS,
    SHOWCASE_FEATURE_KEYS,
    UPGRADE_REQUEST_PREFIX,
    checkout_plan_keys,
    comparison_matrix,
    display_plans,
    enterprise_plan,
    feature_matrix,
    get_plan,
    parse_upgrade_plan,
    showcase_feature_matrix,
    tier_to_plan_key,
    upgrade_delta,
    upgrade_targets,
)


def test_parse_upgrade_plan_extracts_paid_plan():
    assert parse_upgrade_plan(f"{UPGRADE_REQUEST_PREFIX}pro").key == "pro"
    assert parse_upgrade_plan(f"{UPGRADE_REQUEST_PREFIX}business").key == "business"


def test_parse_upgrade_plan_rejects_non_upgrade_and_bad_plans():
    # An ordinary test access / an empty comment — not an upgrade.
    assert parse_upgrade_plan(None) is None
    assert parse_upgrade_plan("") is None
    assert parse_upgrade_plan("нужен доступ") is None
    # free/enterprise are not sold in the checkout → they cannot be approved by them.
    assert parse_upgrade_plan(f"{UPGRADE_REQUEST_PREFIX}free") is None
    assert parse_upgrade_plan(f"{UPGRADE_REQUEST_PREFIX}enterprise") is None
    # A non-existent plan — garbage in the marker.
    assert parse_upgrade_plan(f"{UPGRADE_REQUEST_PREFIX}nonsense") is None


def test_checkout_plan_keys_are_paid_plans_only():
    """The paid checkout — only the plans with a positive price (pro, business).

    free (0) and enterprise (None, the contact CTA) are excluded — they do not need a price_id.
    """
    assert checkout_plan_keys() == ("pro", "business")


def test_catalog_has_four_plans():
    assert len(CATALOG) == 4
    keys = [p.key for p in CATALOG]
    assert keys == ["free", "pro", "business", "enterprise"]


def test_plan_tier_mapping():
    mapping = {p.key: p.tier for p in CATALOG}
    assert mapping["free"] == "lite"
    assert mapping["pro"] == "pro"
    assert mapping["business"] == "business"
    assert mapping["enterprise"] == "enterprise"


def test_all_paid_plans_have_positive_price():
    for plan in CATALOG:
        if plan.price_eur is not None:
            assert plan.price_eur >= 0, f"{plan.key} price should be non-negative"
    assert CATALOG[0].price_eur == 0  # Free
    assert CATALOG[1].price_eur == 29  # Pro
    assert CATALOG[2].price_eur == 79  # Business
    assert CATALOG[3].price_eur is None  # Enterprise on request


def test_profile_limits_match_tiers_py():
    """The profile limits in the catalog match PROFILE_LIMITS in tiers.py."""
    for plan in CATALOG:
        expected = PROFILE_LIMITS.get(plan.tier)
        assert plan.profiles == expected, (
            f"{plan.key}: catalog.profiles={plan.profiles} "
            f"but tiers.PROFILE_LIMITS[{plan.tier!r}]={expected}"
        )


def test_l3_limits_match_usage_py():
    """The L3 limits in the catalog match _TIER_LIMITS['deep'] in usage.py."""
    for plan in CATALOG:
        tier_row = _TIER_LIMITS.get(plan.tier, {})
        deep_limit = tier_row.get("deep")
        # usage.py uses 0 for “no access”; the catalog has 0 for Free too.
        expected = deep_limit if deep_limit != 0 else 0
        assert plan.l3_per_month == expected, (
            f"{plan.key}: catalog.l3_per_month={plan.l3_per_month} "
            f"but usage._TIER_LIMITS[{plan.tier!r}]['deep']={expected}"
        )


def test_display_plans_excludes_enterprise():
    plans = display_plans()
    assert len(plans) == 2
    keys = [p.key for p in plans]
    assert "enterprise" not in keys
    assert keys == ["pro", "business"]  # there is no public Free (the owner’s decision 30.09)
    assert get_plan("free") is not None  # but the internal Free stays in the catalog
    assert get_plan("free").showcase is False


def test_enterprise_plan_has_no_limits():
    ep = enterprise_plan()
    assert ep.key == "enterprise"
    assert ep.price_eur is None
    assert ep.profiles is None
    assert ep.l3_per_month is None


def test_get_plan_returns_correct_plan():
    p = get_plan("pro")
    assert p is not None
    assert p.tier == "pro"
    assert p.price_eur == 29


def test_get_plan_returns_none_for_unknown():
    assert get_plan("nonexistent") is None
    assert get_plan("starter") is None  # starter is not on the showcase


def test_tier_to_plan_key_known_tiers():
    assert tier_to_plan_key("lite") == "free"
    assert tier_to_plan_key("pro") == "pro"
    assert tier_to_plan_key("business") == "business"
    assert tier_to_plan_key("enterprise") == "enterprise"


def test_tier_to_plan_key_legacy_starter():
    # starter is legacy, it maps to free
    assert tier_to_plan_key("starter") == "free"


def test_tier_to_plan_key_fallback():
    assert tier_to_plan_key(None) == "free"
    assert tier_to_plan_key("unknown") == "free"
    assert tier_to_plan_key("") == "free"


def test_all_catalog_tiers_in_tier_order():
    """All the tiers of the catalog (except starter) exist in TIER_ORDER."""
    for plan in CATALOG:
        assert (
            plan.tier in TIER_ORDER
        ), f"Plan {plan.key} tier={plan.tier!r} not in TIER_ORDER"


# --------------------------------------------------------------------------- #
# A TEST LINK (anti-drift): the matrix of the showcase == the gating
# --------------------------------------------------------------------------- #
# The root of the audit R7: the showcase /plans and the thresholds of the gating diverged. These tests
# pin that each field of the Plan matrix is derived from _TIER_LIMITS /
# PROFILE_LIMITS / the rules of _is_pro_plus. Change a threshold → the test fails until
# the showcase reflects the change (a single source).


def test_feature_matrix_matches_gating():
    """All the fields of the Plan matrix are consistent with the gating (SSOT — usage/tiers)."""
    for plan in CATALOG:
        tier = plan.tier
        row = _TIER_LIMITS.get(tier, {})

        # Profiles — PROFILE_LIMITS.
        assert plan.profiles == PROFILE_LIMITS.get(tier), (
            f"{plan.key}: profiles={plan.profiles} != "
            f"PROFILE_LIMITS[{tier!r}]={PROFILE_LIMITS.get(tier)}"
        )
        # The deep analysis — _TIER_LIMITS['deep'] (0=no access is kept).
        assert plan.l3_per_month == row.get(
            "deep"
        ), f"{plan.key}: l3_per_month={plan.l3_per_month} != deep={row.get('deep')}"
        # The draft — _TIER_LIMITS['draft'] (0=no, None=∞).
        assert plan.draft_per_month == row.get("draft"), (
            f"{plan.key}: draft_per_month={plan.draft_per_month} "
            f"!= draft={row.get('draft')}"
        )
        # Boolean features: available ⇔ the limit != 0 in the gating.
        assert plan.ai_match == (row.get("standard") != 0), plan.key
        assert plan.bid == (row.get("bid") != 0), plan.key
        assert plan.checklist == (row.get("checklist") != 0), plan.key
        # The analytics of the customer/competitor/re-procurement — the rule _is_pro_plus.
        assert plan.analytics == _is_pro_plus(tier, _TIER_LIMITS), plan.key
        # The Telegram channel — Pro+ (Lite gate_profile → only email).
        assert plan.telegram == (tier != "lite"), plan.key


def test_gating_zero_means_feature_closed_on_showcase():
    """If _TIER_LIMITS[tier][level]==0 → on the showcase the feature is closed (we do not show it as available)."""
    for plan in CATALOG:
        cells = {c["key"]: c for c in feature_matrix(plan)}
        row = _TIER_LIMITS.get(plan.tier, {})
        # deep→ai_deep, draft→draft, standard→ai_match, bid→bid, checklist→checklist.
        if row.get("deep") == 0:
            assert cells["ai_deep"]["on"] is False, plan.key
        if row.get("draft") == 0:
            assert cells["draft"]["on"] is False, plan.key
        if row.get("standard") == 0:
            assert cells["ai_match"]["on"] is False, plan.key
        if row.get("bid") == 0:
            assert cells["bid"]["on"] is False, plan.key
        if row.get("checklist") == 0:
            assert cells["checklist"]["on"] is False, plan.key


def test_free_plan_has_no_ai():
    """Free (lite) — alerts without AI: all the AI features are closed, only email, 1 profile."""
    free = get_plan("free")
    assert free is not None
    assert free.ai_match is False
    assert free.bid is False
    assert free.checklist is False
    assert free.analytics is False
    assert free.telegram is False
    assert free.l3_per_month == 0
    assert free.draft_per_month == 0
    assert free.profiles == 1


def test_feature_matrix_structure():
    """feature_matrix() gives the cells in the order of FEATURE_KEYS with the correct kind."""
    for plan in CATALOG:
        cells = feature_matrix(plan)
        assert tuple(c["key"] for c in cells) == FEATURE_KEYS
        limit_keys = {"profiles", "ai_deep", "draft"}
        for c in cells:
            expected_kind = "limit" if c["key"] in limit_keys else "bool"
            assert c["kind"] == expected_kind, (plan.key, c["key"])


def test_feature_matrix_limits_values():
    """The limit cells carry the correct values: Pro deep=10/draft=5, Business 50/30, Ent=∞."""
    m = {p.key: {c["key"]: c for c in feature_matrix(p)} for p in CATALOG}
    assert m["pro"]["ai_deep"]["limit"] == 10
    assert m["pro"]["draft"]["limit"] == 5
    assert m["business"]["ai_deep"]["limit"] == 50
    assert m["business"]["draft"]["limit"] == 30
    assert m["enterprise"]["ai_deep"]["limit"] is None  # ∞
    assert m["enterprise"]["draft"]["limit"] is None
    assert m["enterprise"]["profiles"]["limit"] is None


# --------------------------------------------------------------------------- #
# comparison_matrix — the transposed matrix for the table /plans
# --------------------------------------------------------------------------- #


def test_comparison_matrix_row_per_feature():
    """The rows — by the number of SHOWCASE_FEATURE_KEYS (the ladder + «email»), the order is kept."""
    rows = comparison_matrix()
    assert tuple(r["key"] for r in rows) == SHOWCASE_FEATURE_KEYS


def test_comparison_matrix_column_per_display_plan():
    """Each row carries a cell for each plan of the showcase (Pro/Business)."""
    plans = display_plans()
    rows = comparison_matrix()
    for row in rows:
        assert len(row["cells"]) == len(plans)


def test_comparison_matrix_matches_feature_matrix():
    """The cells of the table = the transposed feature_matrix (a single source, without drift)."""
    plans = display_plans()
    per_plan = {p.key: showcase_feature_matrix(p) for p in plans}
    rows = comparison_matrix()
    for idx, row in enumerate(rows):
        assert row["kind"] == per_plan[plans[0].key][idx]["kind"]
        for col, plan in enumerate(plans):
            assert row["cells"][col] == per_plan[plan.key][idx]


def test_showcase_email_row_follows_post_trial_fact():
    """The «email» row of the showcase: for the paid ones — a tick (Free is not on the showcase).

    The fact (REPORT_newcomer_ux, verdict B): after the trial the access_until expires and the matching (STAGE_A_SQL) sends nothing — a Free newcomer does NOT get email.
    The cabinet ladder (FEATURE_KEYS) does not carry the row — there it would be a lie for a Free granted by hand with an active access.
    """
    assert (
        FEATURE_KEYS[:1] + ("email",) + FEATURE_KEYS[1:] + ("reports",)
        == SHOWCASE_FEATURE_KEYS
    )
    assert "email" not in FEATURE_KEYS
    row = next(r for r in comparison_matrix() if r["key"] == "email")
    assert [c["on"] for c in row["cells"]] == [True, True]  # Pro/Business
    assert get_plan("enterprise").email is True


def test_comparison_matrix_excludes_enterprise():
    """The table — only the showcase plans, Enterprise goes as a separate template row."""
    rows = comparison_matrix()
    # 2 columns (Pro/Business) = display_plans, without enterprise and without Free.
    assert all(len(r["cells"]) == 2 for r in rows)


# --------------------------------------------------------------------------- #
# The value ladder of the cabinet (VG-12): upgrade_targets / upgrade_delta
# --------------------------------------------------------------------------- #


def test_upgrade_targets_free_offers_pro_and_business():
    targets = upgrade_targets("free")
    assert [p.key for p in targets] == ["pro", "business"]


def test_upgrade_targets_pro_offers_only_business():
    targets = upgrade_targets("pro")
    assert [p.key for p in targets] == ["business"]


def test_upgrade_targets_business_is_top_no_targets():
    # Enterprise is excluded from the upsell targets → Business has no targets.
    assert upgrade_targets("business") == ()


def test_upgrade_targets_unknown_key_treated_as_free():
    assert [p.key for p in upgrade_targets("nonsense")] == ["pro", "business"]


def test_upgrade_delta_free_to_pro_unlocks_gated_features():
    keys = {c["key"] for c in upgrade_delta("free", "pro")}
    # Everything that is closed on Free and open on Pro.
    assert {"telegram", "ai_match", "ai_deep", "bid", "checklist", "draft"} <= keys
    # The profiles grew 1→3 — they get into the delta (the growth of the limit).
    assert "profiles" in keys


def test_upgrade_delta_pro_to_business_only_limit_growth():
    delta = upgrade_delta("pro", "business")
    keys = {c["key"] for c in delta}
    # Pro and Business share the boolean features → only the grown limits are in the delta.
    assert keys == {"profiles", "ai_deep", "draft"}
    for cell in delta:
        assert cell["kind"] == "limit"


def test_upgrade_delta_same_plan_is_empty():
    assert upgrade_delta("pro", "pro") == ()


def test_upgrade_delta_unknown_target_is_empty():
    assert upgrade_delta("free", "nonsense") == ()


# --------------------------------------------------------------------------- #
# The showcase /plans: the AI rows are NUMBERS from _TIER_LIMITS, not ticks.
# --------------------------------------------------------------------------- #

_AI_ROWS = ("ai_match", "ai_deep", "bid", "checklist", "draft")


def _showcase_limits(plan_key: str) -> list:
    cells = {c["key"]: c for c in showcase_feature_matrix(get_plan(plan_key))}
    assert all(cells[k]["kind"] == "limit" for k in _AI_ROWS)
    return [cells[k]["limit"] for k in _AI_ROWS]


def test_showcase_ai_rows_are_numbers_from_tier_limits():
    """The literals — the fact of 07.10.2026 (Pro 400/10/30/30/5, Business 1500/50/150/150/30).

    Mutation: remove the substitution in ``showcase_feature_matrix`` → a tuple of boolean cells.
    """
    assert _showcase_limits("pro") == [400, 10, 30, 30, 5]
    assert _showcase_limits("business") == [1500, 50, 150, 150, 30]
    assert _showcase_limits("enterprise") == [None] * 5
    for key in ("pro", "business", "enterprise"):
        tier = get_plan(key).tier
        want = [
            _TIER_LIMITS[tier][lvl]
            for lvl in ("standard", "deep", "bid", "checklist", "draft")
        ]
        assert _showcase_limits(key) == want
