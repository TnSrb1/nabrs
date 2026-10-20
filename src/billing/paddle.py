"""The Paddle Billing connector (VG-11.1) — all the Paddle specifics are isolated here.

Implements :class:`~src.billing.gateway.PaymentGateway`. The webhook signature — the header scheme
``Paddle-Signature`` (``ts=<ts>;h1=<hmac>``, HMAC-SHA256 of
``"{ts}:{raw_body}"`` with the notification secret). The mapping of subscription events →
``BillingEvent``; ``tenant_id``/``plan_key`` are passed through ``custom_data``.

The provider specifics without 100% certainty (the exact JSON paths, the set of event types,
the API for creating a transaction/checkout) are marked ``# TODO(verify)``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from datetime import datetime

from src.billing.models import BillingEvent, BillingEventType, BillingSignatureError
from src.billing.util import header_get, test_checkout_url

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "Paddle-Signature"

# Paddle event_type → the normalized BillingEventType (direct mappings).
# subscription.updated is handled separately by status (see _parse_event).
# TODO(verify): check the composition/names of the events against the current Paddle Billing docs.
_EVENT_MAP: dict[str, BillingEventType] = {
    "subscription.activated": "activated",
    "subscription.resumed": "activated",
    "subscription.past_due": "past_due",
    "subscription.canceled": "canceled",
    "subscription.paused": "canceled",
    "transaction.completed": "renewed",
}


def _parse_signature_header(raw: str) -> tuple[str | None, list[str]]:
    """Parse ``ts=<ts>;h1=<hash>[;h1=<hash>]`` → (ts, [h1 signatures])."""
    ts: str | None = None
    h1: list[str] = []
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key == "ts":
            ts = value
        elif key == "h1":
            h1.append(value)
    return ts, h1


def _sub_type_from_status(status: str | None) -> BillingEventType | None:
    """subscription.updated: the subscription status → the normalized type."""
    if status in ("active", "trialing"):
        return "renewed"
    if status == "past_due":
        return "past_due"
    if status in ("canceled", "paused"):
        return "canceled"
    return None


def _iso_to_dt(value: object) -> datetime | None:
    """ISO8601 (Paddle gives ``...Z``) → an aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class PaddleGateway:
    """Payment connector Paddle Billing."""

    provider_name = "paddle"

    def __init__(
        self,
        *,
        api_key: str = "",
        webhook_secret: str = "",
        live: bool = False,
    ) -> None:
        self._api_key = api_key
        self._webhook_secret = webhook_secret
        self._live = live

    # ── checkout ─────────────────────────────────────────────────────────────
    async def create_checkout(
        self,
        *,
        tenant_id: str,
        plan_key: str,
        success_url: str,
        cancel_url: str,
    ) -> str:
        """Hosted checkout / transaction → URL.

        Test mode (``not live`` or an empty api key): without contacting Paddle —
        a deterministic test URL. ``tenant_id``/``plan_key`` in live mode
        go into the transaction’s ``custom_data`` (they come back in the webhook).
        """
        if not self._live or not self._api_key:
            return test_checkout_url("paddle", plan_key, tenant_id, success_url)

        # TODO(verify): live — creating a transaction via the Paddle Billing API
        # (POST /transactions with items[price_id] + custom_data) and obtaining the
        # hosted-checkout URL. The plan_key→price_id mapping is not configured yet
        # (VG-11b); check the exact request schema against the current API.
        raise NotImplementedError(
            "The Paddle live checkout is not configured (VG-11b): no price_id mapping"
        )

    # ── webhook ──────────────────────────────────────────────────────────────
    def verify_and_parse_webhook(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> BillingEvent | None:
        self._verify_signature(raw_body, headers)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.warning("paddle webhook: the body is not JSON")
            return None
        return self._parse_event(payload)

    def _verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> None:
        if not self._webhook_secret:
            raise BillingSignatureError("PADDLE_WEBHOOK_SECRET is not set")
        raw_sig = header_get(headers, _SIGNATURE_HEADER)
        if not raw_sig:
            raise BillingSignatureError("Paddle-Signature is missing")
        ts, h1_list = _parse_signature_header(raw_sig)
        if ts is None or not h1_list:
            raise BillingSignatureError("invalid Paddle-Signature format")
        signed_payload = f"{ts}:".encode("utf-8") + raw_body
        expected = hmac.new(
            self._webhook_secret.encode("utf-8"), signed_payload, hashlib.sha256
        ).hexdigest()
        if not any(hmac.compare_digest(expected, h) for h in h1_list):
            raise BillingSignatureError("Paddle-Signature HMAC mismatch")

    def _parse_event(self, payload: dict) -> BillingEvent | None:
        event_type = payload.get("event_type")
        event_id = payload.get("event_id")
        if not isinstance(event_type, str) or not isinstance(event_id, str):
            return None
        data = payload.get("data") or {}

        if event_type == "subscription.updated":
            norm = _sub_type_from_status(data.get("status"))
        else:
            norm = _EVENT_MAP.get(event_type)
        if norm is None:
            return None  # irrelevant event — the route will return 200

        # tenant_id / plan_key — from custom_data (we put them in on create_checkout).
        custom = data.get("custom_data") or {}
        tenant_id = custom.get("tenant_id")
        plan_key = custom.get("plan_key")

        # subscription id: for subscription.* — data.id; for transaction.completed —
        # data.subscription_id. customer — data.customer_id.
        # TODO(verify): check the exact fields for transaction.* against the docs.
        if event_type.startswith("subscription."):
            subscription_id = data.get("id")
        else:
            subscription_id = data.get("subscription_id")
        customer_id = data.get("customer_id")

        # current_period_end — data.current_billing_period.ends_at (ISO8601).
        period = data.get("current_billing_period") or {}
        period_end = _iso_to_dt(period.get("ends_at"))

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
