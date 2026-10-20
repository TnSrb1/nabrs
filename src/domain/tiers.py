"""The SSOT resolver of the effective tier for AI gates (a neutral domain layer).

Moved from ``src/api/tiers.py`` so that the bot, the web and ``src.ai`` use ONE
resolver without a bot→api layer dependency. ``src/api/tiers.py`` re-exports these names
for backward compatibility (the profile gate), the new names are here.

The tier axis (this module) and the freeze axis (``src/domain/access.py``,
``is_frozen``) are SEPARATE. The freeze is checked FIRST (before the effective tier):
a frozen trial (``access_until`` expired) does NOT get Pro.

A trial is treated as Pro (ARCHITECTURE §4: “all features as Pro”).
"""

from __future__ import annotations

# The order of plans by ascending rights.
TIER_ORDER: tuple[str, ...] = ("lite", "starter", "pro", "business", "enterprise")
_RANK = {name: i for i, name in enumerate(TIER_ORDER)}


def normalize_tier(tier: str | None) -> str:
    """Brings an arbitrary value to a known plan (the fallback is lite)."""
    t = (tier or "").strip().lower()
    return t if t in _RANK else "lite"


def effective_tier(status: str | None, tier: str | None) -> str:
    """The effective plan for the gate.

    On a trial the profile gets Pro rights (ARCHITECTURE §4). In other cases — the
    tenant's own plan (the fallback is lite). The freeze (``access_until``) must be
    checked SEPARATELY and EARLIER — see the module docstring.
    """
    if (status or "").strip().lower() == "trial":
        return "pro"
    return normalize_tier(tier)


# The auto-analysis levels that the product allows in a subscription at all. Only
# ``standard``: for ``quick`` (L1) the output is service metadata that no
# module reads and no template renders (measured 15.08.2026), and ``deep``
# is forbidden on the auto path by a safeguard and lives only on a button.
AUTO_ANALYSIS_LEVELS: tuple[str, ...] = ("none", "standard")

# ‼️ On a TRIAL the auto-analysis works as a DEMONSTRATION: the subscriber must see
# what it looks like, but not receive a paid flow for free. Owner’s decision of
# 15.08.2026: a strictly limited number of analyzes (see TRIAL_AUTO_DEMO_LIMIT in
# src/ai/usage.py), further — only on a chosen plan.
#
# This does not change the rights for BUTTON analyzes: there a human action pays, and
# a trial still gets Pro rights (effective_tier).


def tier_rank(tier: str | None) -> int:
    return _RANK[normalize_tier(tier)]


def at_least(tier: str | None, minimum: str) -> bool:
    """True if ``tier`` is not lower than ``minimum``."""
    return tier_rank(tier) >= _RANK[minimum]
