"""The plan gate for profiles (ARCHITECTURE §12.1 / §12.4, ADR-018 revised).

Pure functions without DB / network — all the plan logic is tested in isolation and
is called on the backend at the creation / update of a profile (the UI is not the only
point of enforcement — the task §4).

The plans and the limits (ADR-018 revised / DB_SCHEMA §2.6):
- the profile limit: Lite 1 / Starter 1 / Pro 3 / Business 10 / Enterprise ∞;
- the Starter+ fields: procedure_types, contract_types, customers_include / exclude,
  languages, min_days_until_deadline, quiet_hours_start / end;
- ai_analysis_level: the auto analysis of the stream — Starter+ (Lite has it switched off), and
  its only mode — 'standard' (15.08.2026: 'quick' / 'deep' do not run in the auto path,
  see src.domain.tiers.AUTO_ANALYSIS_LEVELS);
- a custom schedule (digest_custom / digest_cron) — Pro+;
- Lite — only the email channel and without AI (ai_analysis_level='none').

The trial is treated as Pro (ARCHITECTURE §4: “all features as for Pro”).
"""

from __future__ import annotations

# The resolver of the effective tier — the SSOT in the neutral domain layer (available to the bot / the web /
# the AI without a dependency bot→api). A re-export for the backward compatibility of the profile
# gate and the external consumers (routes_admin / routes_public / tests).
from src.domain.tiers import (  # noqa: F401
    AUTO_ANALYSIS_LEVELS,
    TIER_ORDER,
    _RANK,
    at_least,
    effective_tier,
    normalize_tier,
    tier_rank,
)

# The profile limit; None = no limit.
PROFILE_LIMITS: dict[str, int | None] = {
    "lite": 1,
    "starter": 1,
    "pro": 3,
    "business": 10,
    "enterprise": None,
}

# The fields available only from Starter and above.
STARTER_PLUS_FIELDS: tuple[str, ...] = (
    "procedure_types",
    "contract_types",
    "customers_include",
    "customers_exclude",
    "languages",
    "min_days_until_deadline",
    "quiet_hours_start",
    "quiet_hours_end",
)

# The historically valid values of ``profiles.ai_analysis_level`` in the order
# of increasing “severity”. The set is wider than what the product OFFERS: the form has
# only ``none`` / ``standard`` (``AUTO_ANALYSIS_LEVELS``), but in the DB of old
# profiles and in a forged POST ``quick`` / ``deep`` may arrive — we must
# recognize them, in order to report the downgrade meaningfully, and not silently get ``none``.
_AI_LEVELS = ("none", "quick", "standard", "deep")


def profile_limit(tier: str | None) -> int | None:
    """The maximum of profiles for the plan (None — no limit)."""
    return PROFILE_LIMITS[normalize_tier(tier)]


def can_create_profile(tier: str | None, current_count: int) -> bool:
    """Whether one more profile can be created at the current count."""
    limit = profile_limit(tier)
    return limit is None or current_count < limit


def _max_ai_level(tier: str) -> str:
    """The maximum available level of the AUTO analysis for the plan.

    The ceiling ``deep`` for Pro+ is removed (15.08.2026): in the auto path ``deep`` does not run at all (the safety guard §14.4), so “available on Pro” was a promise the product did not keep. The plan meaning does NOT change: the auto analysis is available from Starter (for ``lite`` the limit of the ``standard`` level is 0 in ``src.ai.usage._TIER_LIMITS``); Pro / Business differ by the size of the limit, not by the level.
    """
    if tier == "lite":
        return "none"
    return "standard"


def gate_profile(
    tier: str | None, payload: dict, *, email_channel_live: bool = True
) -> tuple[dict, list[str]]:
    """Switches off the fields unavailable on the plan; returns (clean_payload, suppressed).

    It is applied both at creation and at update: on a downgrade the fields that became unavailable are removed from the payload (the profile is not deleted — ARCHITECTURE §12.4). ``suppressed`` — the list of the KEYS of the cabinet chrome catalog (``gate_sup_*``), and NOT a ready text: the gate is a pure function without a locale; the template substitutes the language through ``t`` by the subscriber’s locale (we do not mix the two i18n axes — the project rules). Each key is self-sufficient (without runtime parameters): the field name and the resulting AI level are built into the text of the key.

    ``email_channel_live`` — a fact from the DB (whether the tenant’s email is alive NOW, the same source of truth as :func:`src.api.cabinet.routes_profiles._live_delivery_channels`), and not a setting: the function stays pure, the caller must check it itself (see the calls in :mod:`src.api.cabinet.profile_service`). The default ``True`` — for the non-Lite plans and the old calls, where the liveness of the channel does not affect the outcome (see §3 below).
    """
    t = normalize_tier(tier)
    clean = dict(payload)
    suppressed: list[str] = []

    # 1. The Starter+ fields — unavailable for Lite.
    if not at_least(t, "starter"):
        for field in STARTER_PLUS_FIELDS:
            if _has_value(clean.get(field)):
                clean[field] = _empty_like(clean.get(field))
                suppressed.append(_FIELD_SUPPRESS_KEYS[field])

    # 2. The AI level of the auto analysis: we clamp by the plan AND by the canon of the levels.
    #    Two different reasons for the downgrade — two different messages:
    #    Lite → there is no auto analysis at all; Starter+ with quick / deep → the only
    #    existing mode ``standard`` (see AUTO_ANALYSIS_LEVELS).
    requested = (clean.get("ai_analysis_level") or "none").strip().lower()
    if requested not in _AI_LEVELS:
        requested = "none"
    ceiling = _max_ai_level(t)
    if requested == "none":
        clean["ai_analysis_level"] = "none"
    elif ceiling == "none":
        clean["ai_analysis_level"] = "none"
        suppressed.append("gate_sup_ai_off")
    elif requested not in AUTO_ANALYSIS_LEVELS:
        clean["ai_analysis_level"] = ceiling  # "standard"
        suppressed.append("gate_sup_ai_deep")
    else:
        clean["ai_analysis_level"] = requested

    # 3. Lite — only the email channel, and only if it is ALIVE. A setting ≠ a fact
    #    (.claude/rules/matching-delivery.md): substituting a dead email for
    #    an empty list would mean that the “no channels” banner does not pop up, and
    #    the delivery would silently go nowhere (see REPORT_lite_email).
    if t == "lite":
        channels = list(clean.get("delivery_channels") or [])
        email_only = [c for c in channels if c == "email"]
        if channels and channels != email_only:
            clean["delivery_channels"] = ["email"] if email_channel_live else []
            suppressed.append("gate_sup_channels_email")
        elif not channels:
            clean["delivery_channels"] = ["email"] if email_channel_live else []
        elif not email_channel_live:
            clean["delivery_channels"] = []

    # 4. A custom schedule — Pro+.
    if not at_least(t, "pro"):
        if (clean.get("delivery_schedule") or "") == "digest_custom":
            clean["delivery_schedule"] = "immediate"
            clean["digest_cron"] = None
            suppressed.append("gate_sup_schedule_custom")
        if _has_value(clean.get("digest_cron")):
            clean["digest_cron"] = None

    return clean, suppressed


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, str, dict)):
        return len(value) > 0
    return True


def _empty_like(value):
    if isinstance(value, list):
        return []
    if isinstance(value, str):
        return None
    return None


# A Starter+ field → a KEY of the cabinet chrome catalog with a ready message about the switch-off.
# A separate key per field (rather than a common template with a parameter): it keeps suppressed
# without runtime substitutions; the template resolves them through ``t`` by a single `t.get`.
_FIELD_SUPPRESS_KEYS = {
    "procedure_types": "gate_sup_procedure_types",
    "contract_types": "gate_sup_contract_types",
    "customers_include": "gate_sup_customers_include",
    "customers_exclude": "gate_sup_customers_exclude",
    "languages": "gate_sup_languages",
    "min_days_until_deadline": "gate_sup_min_days",
    "quiet_hours_start": "gate_sup_quiet_start",
    "quiet_hours_end": "gate_sup_quiet_end",
}
