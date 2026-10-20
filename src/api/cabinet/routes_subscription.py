"""The “My subscription” section of the cabinet (VG-11.2) — now a section of the “Moj kabinet” hub.

As of owner 10.10.2026 there is no separate page: ``GET /cabinet/subscription`` answers 303 to ``/cabinet/account#pretplata`` (not 301 — it would be cached forever). The blocks (plan / status / renewal date from ``billing_subscriptions`` (RLS, own only), AI limits, the value ladder) are rendered by the hub through the ``_sub_*.html`` partials; the context for them is assembled by :func:`build_subscription_context` — one resolver for the hub,
there is no third source of the plan / term.

    RLS invariant: ``repo.set_tenant`` FIRST as the transaction’s action (otherwise FORCE RLS
on ``billing_subscriptions`` returns 0 rows / the query fails).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from src.api import deps
from src.api.cabinet import ai_limits_service
from src.api.cabinet import stats_service
from src.api.cabinet import subscription_service as svc
from src.plans.catalog import (
    feature_matrix,
    get_plan,
    tier_to_plan_key,
    upgrade_delta,
    upgrade_targets,
)

router = APIRouter(tags=["cabinet"])


HUB_SUBSCRIPTION_URL = "/cabinet/account#pretplata"


async def build_subscription_context(db, tid: str) -> dict:
    """Data of the “Pretplata i limiti” section (for the “Moj kabinet” hub).

    Requires an open transaction with ``repo.set_tenant`` FIRST (RLS) — this is the caller’s duty. Besides the template keys it gives the tenant’s ``tier`` / ``status``: the hub computes the profile limit from them, without a second query.
    """
    subscription = await svc.get_subscription(db, tid)
    tier, status = await svc.get_tenant_tier(db, tid)
    last_decision = await svc.get_last_decision(db, tid)
    # The “days left” counter — the SAME source as the cockpit
    # (tenants.access_until via stats_service), not an own arithmetic over the
    # billing dates: otherwise the tile on the dashboard and the tile here would diverge.
    access_days = await stats_service.get_days_left(db, tid)
    # AI limits: the remainder for EACH limited level — "nothing switches off
    # silently" (owner, 15.08.2026). The same spend predicate as the gates
    # (src.ai.usage.used_count), not an own SQL.
    ai_limits = await ai_limits_service.get_ai_limits(db, tid=tid)

    # Trial access is shown as “Trial” (Pro rights) when there is no subscription of its own.
    is_trial = (status or "").strip().lower() == "trial"

    sub_plan = (
        get_plan(subscription.plan_key)
        if subscription and subscription.plan_key
        else None
    )

    # --- Value ladder (VG-12): “what my plan gives” + “what an upgrade would unlock”. ---
    # A view derived from the gating (catalog.feature_matrix). The trial access
    # we treat as Pro rights; otherwise — the subscription plan, otherwise the tenant’s tier.
    if is_trial:
        current_key = "pro"
    elif sub_plan is not None:
        current_key = sub_plan.key
    else:
        current_key = tier_to_plan_key(tier)

    current_plan = get_plan(current_key)
    current_features = (
        tuple(c for c in feature_matrix(current_plan) if c["on"])
        if current_plan is not None
        else ()
    )
    upgrades = tuple(
        {"plan": tgt, "delta": upgrade_delta(current_key, tgt.key)}
        for tgt in upgrade_targets(current_key)
    )
    return {
        "tier": tier,
        "status": status,
        "subscription": subscription,
        "sub_plan": sub_plan,
        "is_trial": is_trial,
        "current_key": current_key,
        "current_features": current_features,
        "upgrades": upgrades,
        "last_decision": last_decision,
        "access_days": access_days,
        "ai_limits": ai_limits,
    }


@router.get("/cabinet/subscription")
async def subscription_page(
    request: Request, session=Depends(deps.get_optional_session)
):
    """The old address (emails, bot, bookmarks): 303 to the hub section.

    An anonymous user — to login, with a return here; after login this same redirect leads to the hub. 303, not 301: a 301 is cached by the browser forever, and it cannot be rolled back.
    """
    if session is None:
        return RedirectResponse(url=deps.cabinet_login_url(request), status_code=303)
    return RedirectResponse(url=HUB_SUBSCRIPTION_URL, status_code=303)
