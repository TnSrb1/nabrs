"""SSOT catalog of the public plans of the showcase (VG-12).

The owner edits prices and features in this module — without migrations.
Limits are agreed with:
- src/api/tiers.py   → PROFILE_LIMITS (lite=1, pro=3, business=10, enterprise=None)
- src/ai/usage.py    → _TIER_LIMITS['deep'] (pro=10, business=50, enterprise=None)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, cast

# Editable constant: the EUR → RSD rate for the label (current at launch).
EUR_RSD: int = 117

# Contact e-mail for the Enterprise CTA — the owner edits it here.
ENTERPRISE_CONTACT_EMAIL: str = "info@nab.rs"
# The welcome e-mail is sent FROM the operator’s mailbox (From/Reply-To), not from noreply:
# the client replies to the welcome message directly to info@. The nab.rs domain is confirmed in Brevo.
WELCOME_FROM_EMAIL: str = f"nab.rs <{ENTERPRISE_CONTACT_EMAIL}>"

# The project’s Telegram channel (news and beta testing) — owner 14.10.2026; the link is in
# the showcase footer, in “Help” and in the cabinet hub. The address is one here, not a literal in the templates.
TG_CHANNEL_URL: str = "https://t.me/nabrs_news"


@dataclass(frozen=True)
class Plan:
    key: str  # public slug: free|pro|business|enterprise
    tier: str  # internal tier: lite|pro|business|enterprise
    price_eur: Optional[int]  # None = on request
    profiles: Optional[int]  # None = ∞
    l3_per_month: Optional[int]  # None = ∞, 0 = no access

    # --- Value matrix: derived from the gating, pinned by a linking test ---
    # (tests/plans/test_catalog.py::test_feature_matrix_matches_gating).
    # THE SHOWCASE REFLECTS THE GATING — thresholds are edited in usage._TIER_LIMITS /
    # tiers.PROFILE_LIMITS, here is only their display. Do NOT diverge.
    telegram: (
        bool  # email+telegram (Pro+) vs email-only (Free): gate_profile lite→email
    )
    ai_match: bool  # AI matching and relevance explanation (standard): _TIER_LIMITS['standard']
    bid: bool  # bid verdict (VG-08), Pro+: _TIER_LIMITS['bid']
    checklist: bool  # document checklist (VG-09), Pro+: _TIER_LIMITS['checklist']
    draft_per_month: Optional[
        int
    ]  # bid draft (VG-10): _TIER_LIMITS['draft'] (0=no, None=∞)
    analytics: bool  # analytics of the customer/competitor/re-procurements (VG-06), Pro+ (_is_pro_plus)
    # The “Email notifications” row — ONLY for the /plans showcase (SHOWCASE_FEATURE_KEYS),
    # it is not part of the cabinet ladder (FEATURE_KEYS). Free — False, because
    # a newcomer does not get onto Free by themselves: after the trial (TRIAL_DAYS) ``access_until``
    # expires and matching (STAGE_A_SQL) stops sending at all (verdict B,
    # docs/reports/REPORT_newcomer_ux.md). Not a gate, but a promise of the showcase.
    email: bool = True
    # Whether the plan is shown on the public showcase. Free is an internal state
    # (a manual grant by an admin): owner decision 30.09.2026 — there is no public Free,
    # a new account gets only TRIAL_DAYS days of Pro.
    showcase: bool = True
    # “Analytics and reports (Excel)” — the report builder and export (P3/P4
    # ANALYTICS_PRODUCT, owner decision 10.10.2026: only Business and Enterprise).
    # The row ONLY for the showcase (SHOWCASE_FEATURE_KEYS), like ``email``: there is no gate in
    # the cabinet yet — it will appear together with /app/analitika and must read
    # this flag, and not create a second source.
    reports: bool = False


# The values are agreed with PROFILE_LIMITS (tiers.py), _TIER_LIMITS (usage.py) and
# the rule _is_pro_plus (awards_analytics_service.py). starter — legacy, hidden.
# l3_per_month == _TIER_LIMITS['deep']; draft_per_month == _TIER_LIMITS['draft'].
CATALOG: tuple[Plan, ...] = (
    Plan(
        key="free",
        tier="lite",
        price_eur=0,
        profiles=1,
        l3_per_month=0,
        telegram=False,
        ai_match=False,
        bid=False,
        checklist=False,
        draft_per_month=0,
        analytics=False,
        email=False,
        showcase=False,
    ),
    Plan(
        key="pro",
        tier="pro",
        price_eur=29,
        profiles=3,
        l3_per_month=10,
        telegram=True,
        ai_match=True,
        bid=True,
        checklist=True,
        draft_per_month=5,
        analytics=True,
    ),
    Plan(
        key="business",
        tier="business",
        price_eur=79,
        profiles=10,
        l3_per_month=50,
        telegram=True,
        ai_match=True,
        bid=True,
        checklist=True,
        draft_per_month=30,
        analytics=True,
        reports=True,
    ),
    Plan(
        key="enterprise",
        tier="enterprise",
        price_eur=None,
        profiles=None,
        l3_per_month=None,
        telegram=True,
        ai_match=True,
        bid=True,
        checklist=True,
        draft_per_month=None,
        analytics=True,
        reports=True,
    ),
)

_BY_KEY: dict[str, Plan] = {p.key: p for p in CATALOG}

# Mapping of the internal tier → the public slug. starter — legacy → free.
_TIER_TO_KEY: dict[str, str] = {p.tier: p.key for p in CATALOG}
_TIER_TO_KEY["starter"] = "free"


def get_plan(key: str) -> Optional[Plan]:
    """Find a plan by the public slug. None if the key is unknown."""
    return _BY_KEY.get(key)


# Marker of the upgrade-request comment for a plan upgrade (SSOT). The showcase (routes_public
# /plans/upgrade) writes the request with ``comment = f"{UPGRADE_REQUEST_PREFIX}{plan}"``;
# the cabinet (create_trial_request) and the approval (bot/web) recognize it by this
# prefix. One source of the string — do not hardcode it in three places.
UPGRADE_REQUEST_PREFIX: str = "upgrade_request:"


def parse_upgrade_plan(comment: str | None) -> Optional[Plan]:
    """The plan from the request ``comment`` ``upgrade_request:<plan>``, or ``None``.

    ``None`` — if this is NOT an upgrade request (an ordinary trial access) OR the plan is not
    sold in the paid checkout (``checkout_plan_keys`` = pro/business): protection from a
    broken/outdated marker, so that the approval does not set a tier from garbage.
    Exactly this plan goes into :func:`Plan.tier` on a manual grant.
    """
    if not comment or not comment.startswith(UPGRADE_REQUEST_PREFIX):
        return None
    key = comment[len(UPGRADE_REQUEST_PREFIX) :].strip()
    plan = _BY_KEY.get(key)
    if plan is None or plan.key not in checkout_plan_keys():
        return None
    return plan


def display_plans() -> tuple[Plan, ...]:
    """Showcase cards: Pro / Business (without Enterprise and the internal Free)."""
    return tuple(p for p in CATALOG if p.showcase and p.key != "enterprise")


def checkout_plan_keys() -> tuple[str, ...]:
    """Public slugs of the plans that go into the paid checkout (a price_id is needed).

    Paid ⇔ ``price_eur`` is a positive number: ``free`` (0) and ``enterprise``
    (None, the contact CTA) are excluded. The source of truth for the mapping
    ``plan_key→price_id`` is taken FROM the catalog, not hardcoded (VG-11b).
    """
    return tuple(
        p.key for p in CATALOG if isinstance(p.price_eur, int) and p.price_eur > 0
    )


def enterprise_plan() -> Plan:
    """The Enterprise row “on request”."""
    return _BY_KEY["enterprise"]


def tier_to_plan_key(tier: str | None) -> str:
    """Internal tier → public slug to highlight the current plan.

    Unknown/None tier → 'free'.
    """
    return _TIER_TO_KEY.get(tier or "", "free")


# Order of the value-matrix rows on the showcase (top to bottom). Keys ↔ i18n PLANS.features.
# 'limit' rows show the number/∞; 'bool' rows — a tick/a dash.
FEATURE_KEYS: tuple[str, ...] = (
    "profiles",  # limit  (PROFILE_LIMITS)
    "telegram",  # bool   (email+telegram)
    "ai_match",  # bool   (AI matching + relevance)
    "ai_deep",  # limit  (deep AI analysis, _TIER_LIMITS['deep'])
    "bid",  # bool   (bid verdict)
    "checklist",  # bool   (document checklist)
    "draft",  # limit  (bid draft, _TIER_LIMITS['draft'])
    "analytics",  # bool   (analytics of the customer/competitor/re-procurements)
)

# The rows of the /plans showcase (cards + “Poređenje planova”): the FEATURE_KEYS ladder
# plus “email” — the delivery promise that is not in the cabinet ladder.
# At the end — “reports” (Business+), also only a showcase row.
SHOWCASE_FEATURE_KEYS: tuple[str, ...] = (
    FEATURE_KEYS[:1] + ("email",) + FEATURE_KEYS[1:] + ("reports",)
)


def feature_matrix(
    plan: Plan, keys: tuple[str, ...] = FEATURE_KEYS
) -> tuple[dict[str, object], ...]:
    """The value-matrix cells of a plan in the order of ``keys`` (by default
    :data:`FEATURE_KEYS`; the showcase passes :data:`SHOWCASE_FEATURE_KEYS`).

    Each cell is a dict:
    - ``key`` — the i18n label key (``PLANS[lang]['features'][key]``);
    - ``kind`` — ``'bool'`` or ``'limit'``;
    - ``on`` — whether the feature is available on the plan (for 'bool' and for the mark of 'limit');
    - ``limit`` — for 'limit': ``int`` (a number), ``None`` (∞) or ``0`` (none).

    The values are read from the fields of :class:`Plan` — the derivatives of the gating (see the
    linking test ``test_feature_matrix_matches_gating``).
    """
    cells: list[dict[str, object]] = []
    for key in keys:
        if key == "profiles":
            cells.append(
                {
                    "key": key,
                    "kind": "limit",
                    "on": True,
                    "limit": plan.profiles,  # None=∞, int
                }
            )
        elif key == "ai_deep":
            cells.append(
                {
                    "key": key,
                    "kind": "limit",
                    "on": plan.l3_per_month != 0,
                    "limit": plan.l3_per_month,  # 0=none, None=∞, int
                }
            )
        elif key == "draft":
            cells.append(
                {
                    "key": key,
                    "kind": "limit",
                    "on": plan.draft_per_month != 0,
                    "limit": plan.draft_per_month,  # 0=none, None=∞, int
                }
            )
        else:
            cells.append(
                {
                    "key": key,
                    "kind": "bool",
                    "on": bool(getattr(plan, key)),
                    "limit": None,
                }
            )
    return tuple(cells)


# The showcase rows where a NUMBER of AI requests per month is shown instead of a tick:
# row key → level in ``src.ai.usage._TIER_LIMITS`` (auto-analysis — standard,
# participation assessment — bid, …). The numbers are read from there on the fly, not copied here.
SHOWCASE_AI_LEVELS: dict[str, str] = {
    "ai_match": "standard",
    "ai_deep": "deep",
    "bid": "bid",
    "checklist": "checklist",
    "draft": "draft",
}


def showcase_feature_matrix(plan: Plan) -> tuple[dict[str, object], ...]:
    """The cells of the plan card on the showcase /plans (:data:`SHOWCASE_FEATURE_KEYS`).

    The AI rows (:data:`SHOWCASE_AI_LEVELS`) are numeric limits of the gating
    ``_TIER_LIMITS[plan.tier]`` (``None`` = ∞, ``0`` = none), not boolean ticks.
    """
    from src.ai.usage import _TIER_LIMITS  # local import: to avoid a cycle

    tier_limits = _TIER_LIMITS.get(plan.tier, {})
    cells: list[dict[str, object]] = []
    for cell in feature_matrix(plan, SHOWCASE_FEATURE_KEYS):
        level = SHOWCASE_AI_LEVELS.get(str(cell["key"]))
        if level is not None:
            limit = tier_limits.get(level, 0)
            cell = {
                "key": cell["key"],
                "kind": "limit",
                "on": limit != 0,
                "limit": limit,
            }
        cells.append(cell)
    return tuple(cells)


# The upsell ladder (bottom to top). Enterprise (“on request”) is not among the cabinet’s
# upgrade targets — it has a separate contact CTA on the showcase.
_LADDER_ORDER: tuple[str, ...] = ("free", "pro", "business", "enterprise")


def _limit_gt(new: Optional[int], old: Optional[int]) -> bool:
    """``new`` is strictly “greater” than ``old`` as a limit (``None`` = ∞ — greater than any int)."""
    if new is None:
        return old is not None  # ∞ is greater than a finite one, but not greater than ∞
    if old is None:
        return False  # a finite one is not greater than ∞
    return new > old


def upgrade_targets(current_key: str) -> tuple[Plan, ...]:
    """Plans strictly above ``current_key`` on the ladder (for the upsell in the cabinet).

    Without Enterprise (it has a separate contact CTA). An unknown key → like ``free``.
    """
    key = current_key if current_key in _LADDER_ORDER else "free"
    idx = _LADDER_ORDER.index(key)
    targets = []
    for k in _LADDER_ORDER[idx + 1 :]:
        plan = _BY_KEY.get(k)
        if plan is not None and plan.key != "enterprise":
            targets.append(plan)
    return tuple(targets)


def upsell_for_level(
    level: str,
    tier: str | None,
    *,
    tier_limits: dict[str, dict[str, Optional[int]]] | None = None,
) -> Optional[Plan]:
    """The nearest on-sale plan where the limit of the level ``level`` is STRICTLY greater than the current one.

    It answers the question “the client hit the limit — what to offer them”: the owner’s requirement of 15.08.2026
    “offer a pricier tier with a larger limit of AI requests”. Returns ``None`` if there is nothing to offer — the client is already at the top of the ladder or has unlimited at this level. ``None`` does NOT mean “stay silent”: the caller must still show the fact of exhaustion, just without a CTA.

    ‼️ The axes are not mixed (the project rules): the thresholds are taken ONLY from the gating (``src.ai.usage._TIER_LIMITS``), the name and the price — ONLY from here. There is no third place with numbers, so “150 verdicts in Business” cannot diverge from what the gate actually lets through.

    ``starter`` does not get into the output: it is legacy and hidden from the showcase (:func:`upgrade_targets` goes along the ladder of the sold plans). Enterprise does not get in either — it has a separate contact CTA, not self-service.
    """
    from src.ai.usage import _TIER_LIMITS  # local import: to avoid a cycle

    limits = tier_limits if tier_limits is not None else _TIER_LIMITS
    current_key = tier_to_plan_key(tier)
    current = limits.get(_BY_KEY[current_key].tier, {}).get(level, 0)
    for plan in upgrade_targets(current_key):
        candidate = limits.get(plan.tier, {}).get(level, 0)
        if candidate == 0:
            continue  # at this plan there is no level at all — not an upsell but a downgrade
        if _limit_gt(candidate, current):
            return plan
    return None


def level_limit_for_plan(
    plan: Plan,
    level: str,
    *,
    tier_limits: dict[str, dict[str, Optional[int]]] | None = None,
) -> Optional[int]:
    """The limit of the level ``level`` on the plan ``plan`` (``None`` = no limit).

    Needed for the upsell label (“Business — 150 per month”): the number is taken from the gating,
    and is not rewritten into the catalog text, otherwise the promise in the interface would diverge
    from what the gate lets through.
    """
    from src.ai.usage import _TIER_LIMITS  # local import: to avoid a cycle

    limits = tier_limits if tier_limits is not None else _TIER_LIMITS
    return limits.get(plan.tier, {}).get(level, 0)


def upgrade_delta(current_key: str, target_key: str) -> tuple[dict[str, object], ...]:
    """The cells of :func:`feature_matrix` that the upgrade ``target_key`` opens or
    extends relative to ``current_key`` (for the block “in Pro/Business you will get …”).

    A bool feature gets into the delta if on the target it is ``on`` and on the current plan ``off``.
    A limit feature — if on the target it is ``on`` and either on the current plan it is ``off``, or the limit grew
    (see :func:`_limit_gt`, ``None`` = ∞). A derivative of the gating — it does not introduce a new
    source of truth (the same :func:`feature_matrix`).
    """
    cur = _BY_KEY.get(current_key) or _BY_KEY["free"]
    tgt = _BY_KEY.get(target_key)
    if tgt is None:
        return ()
    cur_cells = {c["key"]: c for c in feature_matrix(cur)}
    delta: list[dict[str, object]] = []
    for cell in feature_matrix(tgt):
        base = cur_cells[cell["key"]]
        if not cell["on"]:
            continue
        if cell["kind"] == "bool":
            if not base["on"]:
                delta.append(cell)
        else:  # limit
            if not base["on"] or _limit_gt(
                cast("Optional[int]", cell["limit"]),
                cast("Optional[int]", base["limit"]),
            ):
                delta.append(cell)
    return tuple(delta)


def comparison_matrix() -> tuple[dict[str, object], ...]:
    """The comparison table of the showcase: rows — features (the order of :data:`SHOWCASE_FEATURE_KEYS`),
    columns — the plans of :func:`display_plans` (Pro / Business). For /plans.

    Each row is a dict:
    - ``key`` — the i18n label key (``PLANS[lang]['features'][key]``);
    - ``kind`` — ``'bool'`` or ``'limit'`` (the same for the row on all plans);
    - ``cells`` — a tuple of the :func:`feature_matrix` cells, one per plan,
      in the order of :func:`display_plans`.

    It is the transposed :func:`feature_matrix`: the same derivatives of the gating,
    the same linking test. It does not introduce a separate source of truth.
    """
    plans = display_plans()
    per_plan = {p.key: showcase_feature_matrix(p) for p in plans}
    rows: list[dict[str, object]] = []
    for idx, key in enumerate(SHOWCASE_FEATURE_KEYS):
        rows.append(
            {
                "key": key,
                "kind": per_plan[plans[0].key][idx]["kind"],
                "cells": tuple(per_plan[p.key][idx] for p in plans),
            }
        )
    return tuple(rows)
