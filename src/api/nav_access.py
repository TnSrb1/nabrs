"""The tenant’s access for the cabinet header: the status badge and the plan marks on the tools.

The source of rights is THE SAME pure gates that close the pages themselves (``analytics_gate`` for “Analytics”, ``narucilac_gate`` for “Customers”): there is no third list of plans here. The menu does not hide items — it only marks the ones the current tenant has no access to (the click leads to an empty state).

A pure module: no DB; ``now`` is passed explicitly (tz-aware UTC).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from src.domain.access import is_frozen
from src.domain.tiers import effective_tier
from src.plans.catalog import tier_to_plan_key

STATE_TRIAL = "trial"
STATE_ACTIVE = "active"
STATE_FREE = "free"
STATE_FROZEN = "frozen"


@dataclass(frozen=True)
class NavAccess:
    """``state`` — trial | active | free | frozen; ``days`` — days to the end (None = unlimited).

    ``narucilac_label`` / ``analitika_label`` — ``None`` (access exists) | ``pro`` | ``business`` | ``paused`` (frozen).
    """

    state: str
    plan_key: str
    days: int | None
    narucilac_label: str | None
    analitika_label: str | None


def _label(gate: str | None, locked_label: str) -> str | None:
    if gate is None:
        return None
    return "paused" if gate == "frozen" else locked_label


def resolve_nav_access(
    status: str | None,
    tier: str | None,
    access_until: datetime | None,
    now: datetime,
) -> NavAccess:
    # Lazy imports: the cabinet services pull in the bot / analytics, while this module
    # imports deps (the header resolution on every page).
    from src.api.cabinet.analitika_service import analytics_gate
    from src.api.cabinet.customer_analytics_service import narucilac_gate

    is_trial = (status or "").strip().lower() == "trial"
    plan_key = "pro" if is_trial else tier_to_plan_key(effective_tier(status, tier))
    days = (
        None
        if access_until is None
        else math.ceil((access_until - now).total_seconds() / 86400)
    )
    if is_frozen(access_until, now):
        state = STATE_FROZEN
    elif is_trial:
        state = STATE_TRIAL
    elif plan_key == "free":
        state = STATE_FREE
    else:
        state = STATE_ACTIVE
    return NavAccess(
        state=state,
        plan_key=plan_key,
        days=days,
        narucilac_label=_label(narucilac_gate(status, tier, access_until, now), "pro"),
        analitika_label=_label(
            analytics_gate(status, tier, access_until, now), "business"
        ),
    )
