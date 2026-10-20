"""Tests of the plan gate (pure logic, without DB).

The gate silently edits the subscriber’s choice (downgrades the AI level, collapses the channels into e-mail for Lite, switches off the custom schedule below Pro) — a regression here means that the subscriber gets not what they bought. The line coverage of the module is already 100%: the tests below pin the SEMANTICS that the coverage does not see — the exact COMPOSITION of the ``suppressed`` keys (gate_sup_*), their coverage by the cabinet chrome catalog, the idempotence and the absence of a mutation of the input.
"""

from __future__ import annotations

import pytest

from src.api import tiers
from src.api.cabinet.i18n import CABINET

# gate_profile gives the KEYS of the cabinet chrome catalog (gate_sup_*), not a ready text:
# the language is substituted by the template via ``t`` (the two i18n axes are not mixed — CLAUDE.md).
# The tests check BOTH the contract of the keys, AND that each key is covered by all languages, AND
# that the final (ru) text has kept the semantics (names the plan / the level).
_RU = CABINET["ru"]


def test_profile_limits_per_tier():
    assert tiers.profile_limit("lite") == 1
    assert tiers.profile_limit("starter") == 1
    assert tiers.profile_limit("pro") == 3
    assert tiers.profile_limit("business") == 10
    assert tiers.profile_limit("enterprise") is None


def test_effective_tier_trial_is_pro():
    assert tiers.effective_tier("trial", None) == "pro"
    assert tiers.effective_tier("active", "starter") == "starter"
    assert tiers.effective_tier(None, None) == "lite"


def test_can_create_profile_respects_limit():
    assert tiers.can_create_profile("lite", 0) is True
    assert tiers.can_create_profile("lite", 1) is False
    assert tiers.can_create_profile("pro", 2) is True
    assert tiers.can_create_profile("pro", 3) is False
    assert tiers.can_create_profile("enterprise", 999) is True


def test_gate_lite_blocks_starter_fields_ai_and_non_email():
    payload = {
        "procedure_types": ["open"],
        "languages": ["sr-Latn"],
        "min_days_until_deadline": 5,
        "quiet_hours_start": "22:00",
        "ai_analysis_level": "deep",
        "delivery_channels": ["email", "telegram"],
    }
    clean, suppressed = tiers.gate_profile("lite", payload)
    assert clean["procedure_types"] == []
    assert clean["languages"] == []
    assert clean["min_days_until_deadline"] is None
    assert clean["quiet_hours_start"] is None
    assert clean["ai_analysis_level"] == "none"
    assert clean["delivery_channels"] == ["email"]
    assert suppressed  # there are downgrade notifications


def test_gate_starter_clamps_deep_ai_to_standard():
    clean, suppressed = tiers.gate_profile(
        "starter", {"ai_analysis_level": "deep", "procedure_types": ["open"]}
    )
    assert clean["ai_analysis_level"] == "standard"
    assert clean["procedure_types"] == ["open"]  # the Starter field is kept
    assert "gate_sup_ai_deep" in suppressed


def test_gate_starter_blocks_custom_schedule():
    clean, suppressed = tiers.gate_profile(
        "starter", {"delivery_schedule": "digest_custom", "digest_cron": "0 9 * * *"}
    )
    assert clean["delivery_schedule"] == "immediate"
    assert clean["digest_cron"] is None
    assert "gate_sup_schedule_custom" in suppressed


def test_normalize_and_rank_helpers():
    assert tiers.normalize_tier("  PRO ") == "pro"
    assert tiers.normalize_tier("bogus") == "lite"
    assert tiers.normalize_tier(None) == "lite"
    assert tiers.tier_rank("enterprise") == len(tiers.TIER_ORDER) - 1
    assert tiers.at_least("business", "pro") is True
    assert tiers.at_least("starter", "pro") is False


def test_gate_unknown_ai_level_falls_back_to_none():
    # An invalid ai_analysis_level is brought to "none" (line 115).
    clean, _ = tiers.gate_profile("pro", {"ai_analysis_level": "ultra"})
    assert clean["ai_analysis_level"] == "none"


def test_gate_lite_without_channels_defaults_to_email():
    # Lite without the specified channels gets e-mail set if it is LIVE (the default
    # ``email_channel_live=True`` — the old calls without the DB fact do not break).
    clean, _ = tiers.gate_profile("lite", {"delivery_channels": []})
    assert clean["delivery_channels"] == ["email"]


def test_gate_lite_email_only_channel_kept_silently():
    clean, suppressed = tiers.gate_profile("lite", {"delivery_channels": ["email"]})
    assert clean["delivery_channels"] == ["email"]
    assert "gate_sup_channels_email" not in suppressed


def test_gate_lite_without_channels_and_dead_email_stays_empty():
    # Without a live e-mail we do not substitute a dead address — otherwise the banner "no
    # channels" (routes_profiles ``?no_channels=1``) stays silent, and the delivery silently
    # does not go (REPORT_lite_email).
    clean, suppressed = tiers.gate_profile(
        "lite", {"delivery_channels": []}, email_channel_live=False
    )
    assert clean["delivery_channels"] == []
    assert "gate_sup_channels_email" not in suppressed


def test_gate_lite_email_only_but_dead_clears_silently():
    # Only e-mail was requested (already the right choice for Lite), but it is dead — we silently
    # empty it (without a false “only e-mail is available” — it is not a plan reason).
    clean, suppressed = tiers.gate_profile(
        "lite", {"delivery_channels": ["email"]}, email_channel_live=False
    )
    assert clean["delivery_channels"] == []
    assert "gate_sup_channels_email" not in suppressed


def test_gate_lite_non_email_channels_and_dead_email_clears():
    # Telegram is unavailable to Lite (we cut back to e-mail), and the e-mail is also dead —
    # the result is empty, but the suppression about the cut-back to e-mail is still informative.
    clean, suppressed = tiers.gate_profile(
        "lite", {"delivery_channels": ["telegram"]}, email_channel_live=False
    )
    assert clean["delivery_channels"] == []
    assert "gate_sup_channels_email" in suppressed


def test_gate_starter_strips_orphan_digest_cron():
    # Non-Pro with digest_cron, but without digest_custom — the cron is cleared (line 145).
    clean, _ = tiers.gate_profile(
        "starter", {"delivery_schedule": "immediate", "digest_cron": "0 9 * * *"}
    )
    assert clean["digest_cron"] is None


def test_gate_pro_allows_everything():
    payload = {
        "procedure_types": ["open"],
        "ai_analysis_level": "deep",
        "delivery_schedule": "digest_custom",
        "digest_cron": "0 9 * * 1-5",
        "delivery_channels": ["email", "telegram"],
    }
    clean, suppressed = tiers.gate_profile("pro", payload)
    # ‼️ The expectation changed by the owner’s decision of 15.08.2026: ``deep`` in the auto path is not
    # executed at all (the worker’s fuse), therefore “keep deep” was
    # a promise the product did not keep — now the request is lowered to
    # the single existing mode, and the subscriber is TOLD about it.
    assert clean["ai_analysis_level"] == "standard"
    assert clean["delivery_schedule"] == "digest_custom"
    assert clean["digest_cron"] == "0 9 * * 1-5"
    assert suppressed == ["gate_sup_ai_deep"]


def test_max_ai_level_matrix():
    # The explicit matrix of the AI ceiling by plans (an internal helper — the SSOT of the gate).
    # The ceiling is ONE for all the paid ones: the auto analysis is only 'standard'
    # (AUTO_ANALYSIS_LEVELS). The plans differ by the SIZE of the limit, not by the level.
    assert tiers._max_ai_level("lite") == "none"
    assert tiers._max_ai_level("starter") == "standard"
    assert tiers._max_ai_level("pro") == "standard"
    assert tiers._max_ai_level("business") == "standard"
    assert tiers._max_ai_level("enterprise") == "standard"


def test_gate_business_allows_deep_ai_and_custom_schedule():
    # Business ≥ Pro: the custom schedule is not switched off. The requested `deep`
    # is lowered to the single auto mode on ANY paid plan — this is about
    # the canon of the levels, not about the rights of the plan (see test_gate_pro_allows_everything).
    payload = {
        "procedure_types": ["open"],
        "ai_analysis_level": "deep",
        "delivery_schedule": "digest_custom",
        "digest_cron": "0 9 * * 1-5",
        "delivery_channels": ["email", "telegram"],
    }
    clean, suppressed = tiers.gate_profile("business", payload)
    assert clean["ai_analysis_level"] == "standard"
    assert clean["delivery_schedule"] == "digest_custom"
    assert clean["digest_cron"] == "0 9 * * 1-5"
    assert clean["delivery_channels"] == ["email", "telegram"]
    assert suppressed == ["gate_sup_ai_deep"]


def test_gate_unknown_tier_falls_back_to_lite_semantics():
    # An unknown plan is normalized to lite → it blocks the Starter+ fields and the AI.
    clean, suppressed = tiers.gate_profile(
        "bogus", {"languages": ["sr-Latn"], "ai_analysis_level": "deep"}
    )
    assert clean["languages"] == []
    assert clean["ai_analysis_level"] == "none"
    assert suppressed


def test_gate_lite_suppresses_quiet_hours_end():
    # Both Starter+ quiet-hours fields are switched off for Lite (incl. quiet_hours_end).
    clean, suppressed = tiers.gate_profile("lite", {"quiet_hours_end": "08:00"})
    assert clean["quiet_hours_end"] is None
    assert "gate_sup_quiet_end" in suppressed
    assert "конец" in _RU["gate_sup_quiet_end"]  # the key is covered and the semantics are intact


def test_gate_none_tier_defaults_to_lite():
    # tier=None → lite: the AI is switched off, the channels collapse into e-mail.
    clean, _ = tiers.gate_profile(
        None, {"ai_analysis_level": "standard", "delivery_channels": ["telegram"]}
    )
    assert clean["ai_analysis_level"] == "none"
    assert clean["delivery_channels"] == ["email"]


def test_tier_rank_is_monotonic_over_order():
    ranks = [tiers.tier_rank(t) for t in tiers.TIER_ORDER]
    assert ranks == sorted(ranks)
    assert ranks == list(range(len(tiers.TIER_ORDER)))


def test_at_least_reflexive_and_enterprise_dominates():
    assert tiers.at_least("pro", "pro") is True
    assert tiers.at_least("lite", "lite") is True
    for minimum in tiers.TIER_ORDER:
        assert tiers.at_least("enterprise", minimum) is True
    assert tiers.at_least("lite", "starter") is False


def test_can_create_profile_business_boundary():
    assert tiers.can_create_profile("business", 9) is True
    assert tiers.can_create_profile("business", 10) is False


# --- Honesty of suppressed: the subscriber is told WHAT was lowered and BY WHAT it is cured ----


def test_gate_lite_reports_one_honest_key_per_suppressed_field():
    # The exact COMPOSITION and ORDER of the keys, not “suppressed is non-empty”: this is exactly
    # what the template shows the subscriber when the gate has rewritten their choice.
    payload = {
        "languages": ["sr-Latn"],
        "quiet_hours_start": "22:00",
        "ai_analysis_level": "deep",
        "delivery_channels": ["email", "telegram"],
    }
    _, suppressed = tiers.gate_profile("lite", payload)
    assert suppressed == [
        "gate_sup_languages",
        "gate_sup_quiet_start",
        "gate_sup_ai_off",
        "gate_sup_channels_email",
    ]


def test_gate_suppressed_keys_are_catalogued_and_name_a_tier():
    # A key without an entry in the catalog → an empty <li> for the subscriber; a text without the name
    # of the plan does not say what to buy. We check both the catalog coverage and the semantics.
    # The names are the showcase ones (src/plans/catalog.py): "Starter"/"Lite" are not sold,
    # the chrome text must call to a plan that can really be bought.
    tier_names = ("Pro", "Business", "Enterprise", "Free")
    payload = {
        "procedure_types": ["open"],
        "languages": ["sr-Latn"],
        "min_days_until_deadline": 5,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "08:00",
        "ai_analysis_level": "deep",
        "delivery_channels": ["email", "telegram"],
        "delivery_schedule": "digest_custom",
        "digest_cron": "0 9 * * *",
    }
    seen = []
    for tier in tiers.TIER_ORDER:
        _, suppressed = tiers.gate_profile(tier, payload)
        seen.extend(suppressed)
    assert seen, "the plan matrix must suppress at least something"
    # ‼️ The exception is ONE and justified: gate_sup_ai_deep is no longer about the plan.
    # Since 15.08.2026 the auto analysis has a single mode on ALL the paid
    # plans, and the downgrade quick/deep → standard happens not because of the plan —
    # to call for an upgrade in it would mean selling what the upgrade does not give.
    not_about_tier = {"gate_sup_ai_deep"}
    for key in seen:
        assert key in _RU, f"the key {key} is not set up in the cabinet chrome catalog"
        if key in not_about_tier:
            continue
        assert any(name in _RU[key] for name in tier_names), _RU[key]


def test_gate_starter_ai_downgrade_key_names_resulting_level():
    """For the subscriber it matters not only “lowered”, but also TO WHAT it was lowered.

    The message must name the mode in THE SAME words as the option in the form (``ai_lvl_standard``): the subscriber no longer sees the word “standard” in the interface, and referring to it would explain by means of the incomprehensible.
    """
    _, suppressed = tiers.gate_profile("starter", {"ai_analysis_level": "deep"})
    assert suppressed == ["gate_sup_ai_deep"]
    assert _RU["ai_lvl_standard"].lower() in _RU["gate_sup_ai_deep"].lower()


def test_gate_orphan_digest_cron_stripped_silently_pinned():
    # A FIXATION OF A FACT, not an approval: digest_cron without digest_custom below Pro
    # is cleared WITHOUT a notification — the only silent downgrade in the gate
    # (see tiers.gate_profile §4). If the behavior is changed deliberately — this test
    # must go red and force the contract to be updated.
    clean, suppressed = tiers.gate_profile(
        "starter", {"delivery_schedule": "immediate", "digest_cron": "0 9 * * *"}
    )
    assert clean["digest_cron"] is None
    assert suppressed == []


# --- The downgrade of the AI level by plan ------------------------------------------


@pytest.mark.parametrize("requested", ["quick", "standard", "deep"])
def test_gate_lite_zeroes_every_ai_level(requested):
    clean, suppressed = tiers.gate_profile("lite", {"ai_analysis_level": requested})
    assert clean["ai_analysis_level"] == "none"
    assert "gate_sup_ai_off" in suppressed


@pytest.mark.parametrize("requested", ["none", "standard"])
def test_gate_starter_keeps_allowed_ai_levels_silently(requested):
    # A level from the white list (AUTO_ANALYSIS_LEVELS) is not touched and not “notified”.
    clean, suppressed = tiers.gate_profile("starter", {"ai_analysis_level": requested})
    assert clean["ai_analysis_level"] == requested
    assert suppressed == []


@pytest.mark.parametrize("tier", ["starter", "pro", "business", "enterprise"])
@pytest.mark.parametrize("requested", ["quick", "deep"])
def test_gate_clamps_non_auto_levels_to_standard_on_every_paid_tier(tier, requested):
    """‼️ quick/deep are NOT executed in the auto path on any plan.

    Previously ``quick`` on Pro “passed within the ceiling” and silently settled in the profile: the form did not offer it, the worker did not do it, the client saw a profile with the AI switched on and zero analyzes.
    """
    clean, suppressed = tiers.gate_profile(tier, {"ai_analysis_level": requested})
    assert clean["ai_analysis_level"] == "standard"
    assert suppressed == ["gate_sup_ai_deep"]


def test_gate_ai_level_normalized_before_clamp():
    # The case/spaces from the form must not silently drop the level to "none":
    # "  DEEP " is recognized as a level request → a downgrade to standard WITH a
    # message, not a quiet switch-off.
    clean, suppressed = tiers.gate_profile("pro", {"ai_analysis_level": "  DEEP "})
    assert clean["ai_analysis_level"] == "standard"
    assert suppressed == ["gate_sup_ai_deep"]

    clean, suppressed = tiers.gate_profile("pro", {"ai_analysis_level": " STANDARD "})
    assert clean["ai_analysis_level"] == "standard"
    assert suppressed == []


def test_gate_missing_ai_level_defaults_to_none_without_message():
    clean, suppressed = tiers.gate_profile("pro", {})
    assert clean["ai_analysis_level"] == "none"
    assert suppressed == []


# --- A custom schedule below Pro (Lite, not only Starter) ----------------


def test_gate_lite_blocks_custom_schedule():
    clean, suppressed = tiers.gate_profile(
        "lite", {"delivery_schedule": "digest_custom", "digest_cron": "0 9 * * *"}
    )
    assert clean["delivery_schedule"] == "immediate"
    assert clean["digest_cron"] is None
    assert "gate_sup_schedule_custom" in suppressed


@pytest.mark.parametrize("tier", ["pro", "business", "enterprise"])
def test_gate_pro_plus_keeps_custom_schedule(tier):
    clean, suppressed = tiers.gate_profile(
        tier, {"delivery_schedule": "digest_custom", "digest_cron": "0 9 * * *"}
    )
    assert clean["delivery_schedule"] == "digest_custom"
    assert clean["digest_cron"] == "0 9 * * *"
    assert suppressed == []


@pytest.mark.parametrize("schedule", ["immediate", "digest_daily"])
def test_gate_lite_keeps_non_custom_schedule(schedule):
    # We switch off only digest_custom — the standard schedules of Lite are not broken.
    clean, _ = tiers.gate_profile("lite", {"delivery_schedule": schedule})
    assert clean["delivery_schedule"] == schedule


# --- The forced e-mail channel for Lite --------------------------------------


def test_gate_lite_telegram_only_becomes_email():
    # No channels remain after the filter — the gate must substitute the e-mail,
    # otherwise a Lite subscriber would be left with no delivery addressee at all.
    clean, suppressed = tiers.gate_profile("lite", {"delivery_channels": ["telegram"]})
    assert clean["delivery_channels"] == ["email"]
    assert "gate_sup_channels_email" in suppressed


@pytest.mark.parametrize("tier", ["starter", "pro", "business", "enterprise"])
def test_gate_starter_plus_keeps_non_email_channels(tier):
    # The forced e-mail — only for Lite; higher up it would take away the purchased channel.
    clean, suppressed = tiers.gate_profile(
        tier, {"delivery_channels": ["telegram", "email"]}
    )
    assert clean["delivery_channels"] == ["telegram", "email"]
    assert suppressed == []


def test_gate_starter_plus_does_not_invent_default_channel():
    # The substitution of the e-mail — also only for Lite: higher up an empty list stays empty.
    clean, _ = tiers.gate_profile("pro", {"delivery_channels": []})
    assert clean["delivery_channels"] == []


# --- The contract of the function: does not mutate the input, is idempotent, does not lose fields -------


def test_gate_does_not_mutate_caller_payload():
    # The route gives its own dict; a mutation of the input would quietly spoil the original choice.
    payload = {
        "languages": ["sr-Latn"],
        "ai_analysis_level": "deep",
        "delivery_channels": ["email", "telegram"],
        "delivery_schedule": "digest_custom",
    }
    before = {
        "languages": ["sr-Latn"],
        "ai_analysis_level": "deep",
        "delivery_channels": ["email", "telegram"],
        "delivery_schedule": "digest_custom",
    }
    tiers.gate_profile("lite", payload)
    assert payload == before


@pytest.mark.parametrize("tier", list(tiers.TIER_ORDER) + [None, "bogus"])
def test_gate_is_idempotent(tier):
    # The profile update runs the gate again over the already cleaned data: the second
    # pass must change nothing and must NOT alarm the subscriber by repeating
    # the downgrade notifications that have already been applied.
    payload = {
        "procedure_types": ["open"],
        "languages": ["sr-Latn"],
        "min_days_until_deadline": 5,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "08:00",
        "ai_analysis_level": "deep",
        "delivery_channels": ["email", "telegram"],
        "delivery_schedule": "digest_custom",
        "digest_cron": "0 9 * * *",
    }
    once, _ = tiers.gate_profile(tier, payload)
    twice, suppressed = tiers.gate_profile(tier, once)
    assert twice == once
    assert suppressed == []


def test_gate_passes_through_untariffed_fields():
    # The gate judges only the plan fields; everything else must be passed on without edits.
    clean, _ = tiers.gate_profile(
        "lite", {"name": "Мой профиль", "keywords": ["asfalt"], "is_active": True}
    )
    assert clean["name"] == "Мой профиль"
    assert clean["keywords"] == ["asfalt"]
    assert clean["is_active"] is True
