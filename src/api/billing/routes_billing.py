"""Billing routes (VG-11.1): the provider webhook and checkout from the cabinet.

Provider-neutral: they depend only on the registry and the core
(:mod:`src.billing.registry` / :mod:`src.billing.service`), not on a specific
gateway. The webhook — without a session (the request comes from the provider), a fast 200 (idempotent);
a bad signature → 400. Checkout — under a session, a redirect to the checkout URL.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from src.api import deps
from src.billing.models import BillingConfigError, BillingSignatureError
from src.billing.registry import get_active_gateway, get_gateway
from src.billing.service import apply_billing_event
from src.plans.catalog import get_plan

logger = logging.getLogger(__name__)

router = APIRouter(tags=["billing"])


@router.post("/billing/webhook/{provider}")
async def billing_webhook(
    provider: str,
    request: Request,
    session_factory: Callable = Depends(deps.get_session_factory),
    settings=Depends(deps.get_settings),
):
    """Receiving a payment provider webhook.

    Raw body + headers → connector ``{provider}`` → signature check and
    normalization → idempotent application. An invalid signature → 400; a valid
    irrelevant event → 200 (accepted, nothing to process).
    """
    raw = await request.body()
    try:
        gateway = get_gateway(settings, provider)
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown provider")

    try:
        event = gateway.verify_and_parse_webhook(raw, request.headers)
    except BillingSignatureError as exc:
        logger.warning("billing webhook %s: invalid signature: %s", provider, exc)
        raise HTTPException(status_code=400, detail="invalid signature")

    if event is None:
        # Valid signature, but the event is irrelevant — accept and exit.
        return {"status": "ignored"}

    async with session_factory() as db:
        await apply_billing_event(db, event)
    return {"status": "ok"}


@router.post("/billing/checkout/{plan_key}")
async def billing_checkout(
    plan_key: str,
    session=Depends(deps.get_optional_session),
    settings=Depends(deps.get_settings),
):
    """Start of payment from the cabinet: create a checkout session of the active provider → redirect.

    Anonymous → ``/login``. Unknown/free plan → back to ``/plans``
    (no payment needed). ``tenant_id`` is passed into the provider’s metadata, so that
    the webhook returns it in the ``BillingEvent``.
    """
    if session is None:
        return RedirectResponse(url="/login", status_code=303)

    plan = get_plan(plan_key)
    if plan is None or plan.key == "free" or plan.price_eur == 0:
        # Free/unknown plan — no checkout needed.
        return RedirectResponse(url="/plans", status_code=303)

    gateway = get_active_gateway(settings)
    base = settings.cabinet_base_url.rstrip("/")
    try:
        url = await gateway.create_checkout(
            tenant_id=session["tid"],
            plan_key=plan_key,
            success_url=f"{base}/app?billing=success",
            cancel_url=f"{base}/plans?billing=cancel",
        )
    except BillingConfigError:
        # The plan’s price_id is not configured → the provider in Stripe was NOT called.
        # We return to the showcase (“Coming soon”), not a 500. Log — without details/secrets.
        logger.warning(
            "billing checkout %s: plan %s is not configured (price_id empty)",
            settings.billing_provider,
            plan_key,
        )
        return RedirectResponse(url="/plans?billing=unavailable", status_code=303)
    return RedirectResponse(url=url, status_code=303)
