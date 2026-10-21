"""Provider-neutral billing models (VG-11.1).

The core and the routes work ONLY with :class:`BillingEvent` — the normalized
representation of a subscription event, common for ANY payment gateway. The provider-
specifics (webhook schemas, event names, signatures) are isolated in the adapters
``src/billing/paddle.py`` and ``src/billing/stripe.py`` and do not leak outward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# The normalized types of subscription events. Each adapter maps the provider-
# specific event names into one of these four (irrelevant ones → the event
# is discarded, verify_and_parse_webhook returns None).
BillingEventType = Literal["activated", "renewed", "past_due", "canceled"]


class BillingSignatureError(Exception):
    """The webhook signature failed the check (HMAC mismatch / the header is missing).

    It differs from an “irrelevant event” (for which the adapter returns
    ``None``): an invalid signature is a 400 in the route (a signal to the provider to retry/
    check the secret), an irrelevant event is a 200 (accepted, nothing to process).
    """


class BillingConfigError(Exception):
    """The provider is not configured for the paid checkout of this plan.

    Fail-closed: the adapter raises IT BEFORE contacting the provider, if no
    price_id is set for the plan (``STRIPE_PRICE_*`` is empty). The route catches it and returns the user to
    the storefront (“Soon”), and does NOT send a raw request without ``line_items``. The message
    contains only ``plan_key`` — there are no secrets/requisites in the text.
    """


@dataclass(frozen=True)
class BillingEvent:
    """A normalized subscription event from the payment gateway.

    The fields are provider-neutral; ``tenant_id`` is present if the provider
    returned it in metadata/custom_data (we pass it on ``create_checkout``),
    otherwise the core resolves it by ``provider_customer_id`` from ``billing_subscriptions``.
    """

    provider: str  # 'paddle' | 'stripe'
    provider_event_id: str  # the provider’s event id — the idempotency key
    type: BillingEventType
    provider_customer_id: str | None
    provider_subscription_id: str | None
    plan_key: (
        str | None
    )  # the public slug of the plan (catalog): free|pro|business|enterprise
    current_period_end: datetime | None
    tenant_id: str | None = None
