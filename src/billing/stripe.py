"""The Stripe connector (VG-11.1) — all the Stripe specifics are isolated here.

Implements :class:`~src.billing.gateway.PaymentGateway`. The webhook signature —
the scheme ``Stripe-Signature`` (``t=...,v1=...``, HMAC-SHA256 of ``"{t}.{payload}"``
with the signing secret). The mapping of Checkout/Subscription events → ``BillingEvent``.

The provider specifics about which there is no 100% certainty from the current documentation
(the exact JSON paths of the fields, the set of relevant event types, the parameters of the Checkout
Session API) are marked ``# TODO(verify)`` — we do not invent them silently.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone

import httpx

from src.billing.models import (
    BillingConfigError,
    BillingEvent,
    BillingEventType,
    BillingSignatureError,
)
from src.billing.util import header_get, test_checkout_url

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "Stripe-Signature"
_API_BASE = "https://api.stripe.com/v1"

# Stripe event.type → the normalized BillingEventType.
# TODO(verify): check the composition of the events and the exact type strings against the current
# Stripe API version (we rely on the stable core Billing events).
_EVENT_MAP: dict[str, BillingEventType] = {
    "checkout.session.completed": "activated",
    "customer.subscription.created": "activated",
    "invoice.paid": "renewed",
    "invoice.payment_succeeded": "renewed",
    "invoice.payment_failed": "past_due",
    "customer.subscription.deleted": "canceled",
}


def _parse_signature_header(raw: str) -> tuple[str | None, list[str]]:
    """Parse ``t=...,v1=...`` → (timestamp, [v1 signatures]).

    The header may contain several ``v1`` (key rotation). We return all of them.
    """
    ts: str | None = None
    v1: list[str] = []
    for part in raw.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            ts = value
        elif key == "v1":
            v1.append(value)
    return ts, v1


def _sub_type_from_status(status: str | None) -> BillingEventType | None:
    """customer.subscription.updated: the subscription status → the normalized type."""
    if status in ("active", "trialing"):
        return "renewed"
    if status == "past_due":
        return "past_due"
    if status in ("canceled", "unpaid", "incomplete_expired"):
        return "canceled"
    return None


def _epoch_to_dt(value: object) -> datetime | None:
    """Unix timestamp (Stripe gives seconds) → an aware datetime UTC."""
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    return None


class StripeGateway:
    """Payment connector Stripe."""

    provider_name = "stripe"

    def __init__(
        self,
        *,
        api_key: str = "",
        webhook_secret: str = "",
        live: bool = False,
        prices: Mapping[str, str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._webhook_secret = webhook_secret
        self._live = live
        # The plan_key→price_id mapping (Stripe Price API). Builds the registry from env
        # STRIPE_PRICE_* by the storefront keys; an empty value = price_id is not set.
        self._prices: dict[str, str] = dict(prices or {})

    # ── checkout ─────────────────────────────────────────────────────────────
    async def create_checkout(
        self,
        *,
        tenant_id: str,
        plan_key: str,
        success_url: str,
        cancel_url: str,
    ) -> str:
        """Checkout Session (``client_reference_id=tenant_id``) → the session URL.

        Test mode (``not live`` or an empty api key): there is no real call to
        Stripe — we return a deterministic test URL “as if the payment works”
        (the owner will enable live with one flag in VG-11b).
        """
        if not self._live or not self._api_key:
            return test_checkout_url("stripe", plan_key, tenant_id, success_url)

        # Live mode: Stripe will not create a subscription without line_items[price]. price_id —
        # from the mapping plan_key→price_id (env STRIPE_PRICE_*, the owner’s requisites). Empty →
        # fail-closed BEFORE the network: we do not send a raw request, the route returns the user to “Soon”.
        price_id = self._prices.get(plan_key, "")
        if not price_id:
            raise BillingConfigError(
                f"The Stripe price_id for the plan {plan_key!r} is not set (STRIPE_PRICE_*) "
                "— live checkout not started"
            )
        data = {
            "mode": "subscription",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": tenant_id,
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "metadata[tenant_id]": tenant_id,
            "metadata[plan_key]": plan_key,
            "subscription_data[metadata][tenant_id]": tenant_id,
            "subscription_data[metadata][plan_key]": plan_key,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_API_BASE}/checkout/sessions",
                data=data,
                auth=(self._api_key, ""),
            )
            resp.raise_for_status()
            payload = resp.json()
        url = payload.get("url")
        if not url:
            raise RuntimeError("Stripe Checkout Session without a url in the answer")
        return str(url)

    # ── webhook ──────────────────────────────────────────────────────────────
    def verify_and_parse_webhook(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> BillingEvent | None:
        self._verify_signature(raw_body, headers)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.warning("stripe webhook: the body is not JSON")
            return None
        return self._parse_event(payload)

    def _verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> None:
        if not self._webhook_secret:
            raise BillingSignatureError("STRIPE_WEBHOOK_SECRET is not set")
        raw_sig = header_get(headers, _SIGNATURE_HEADER)
        if not raw_sig:
            raise BillingSignatureError("Stripe-Signature is missing")
        ts, v1_list = _parse_signature_header(raw_sig)
        if ts is None or not v1_list:
            raise BillingSignatureError("invalid Stripe-Signature format")
        signed_payload = f"{ts}.".encode("utf-8") + raw_body
        expected = hmac.new(
            self._webhook_secret.encode("utf-8"), signed_payload, hashlib.sha256
        ).hexdigest()
        if not any(hmac.compare_digest(expected, v) for v in v1_list):
            raise BillingSignatureError("Stripe-Signature HMAC mismatch")

    def _parse_event(self, payload: dict) -> BillingEvent | None:
        event_type = payload.get("type")
        event_id = payload.get("id")
        if not isinstance(event_type, str) or not isinstance(event_id, str):
            return None
        obj = (payload.get("data") or {}).get("object") or {}

        if event_type == "customer.subscription.updated":
            norm = _sub_type_from_status(obj.get("status"))
        else:
            norm = _EVENT_MAP.get(event_type)
        if norm is None:
            return None  # irrelevant event — the route will return 200

        # tenant_id: client_reference_id (of the Checkout Session) OR metadata.tenant_id.
        metadata = obj.get("metadata") or {}
        tenant_id = obj.get("client_reference_id") or metadata.get("tenant_id")
        plan_key = metadata.get("plan_key")

        # customer / subscription id: different objects keep them differently.
        # TODO(verify): for invoice.* the object is the invoice (customer + subscription
        # as direct fields); for subscription.* — the subscription (id=sub, customer);
        # for checkout.session.completed — the session (customer, subscription).
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription") or (
            obj.get("id")
            if str(event_type).startswith("customer.subscription")
            else None
        )

        # current_period_end is present on subscription objects (unix seconds).
        # TODO(verify): invoice/session has no period — it stays None (access
        # is extended at the next subscription.updated).
        period_end = _epoch_to_dt(obj.get("current_period_end"))

        return BillingEvent(
            provider=self.provider_name,
            provider_event_id=event_id,
            type=norm,
            provider_customer_id=str(customer_id) if customer_id else None,
            provider_subscription_id=(
                str(subscription_id) if subscription_id else None
            ),
            plan_key=str(plan_key) if plan_key else None,
            current_period_end=period_end,
            tenant_id=str(tenant_id) if tenant_id else None,
        )
