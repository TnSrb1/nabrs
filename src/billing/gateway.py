"""Abstraction of the payment gateway (VG-11.1).

:class:`PaymentGateway` — the contract that each connector implements. The core
(:mod:`src.billing.service`) and the routes (:mod:`src.api.billing.routes_billing`)
depend ONLY on this protocol and on :class:`~src.billing.models.BillingEvent`,
never on a concrete provider. A new gateway = a new class implementing the
protocol + registration in :mod:`src.billing.registry`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from src.billing.models import BillingEvent


@runtime_checkable
class PaymentGateway(Protocol):
    """The contract of a payment connector.

    Implementations MUST keep all the provider specifics inside themselves and expose
    outward only provider-neutral values (a ``str`` checkout URL and
    :class:`BillingEvent`).
    """

    #: The provider name ('paddle' | 'stripe' | ...). Matches the registry key.
    provider_name: str

    async def create_checkout(
        self,
        *,
        tenant_id: str,
        plan_key: str,
        success_url: str,
        cancel_url: str,
    ) -> str:
        """Create a checkout session and return the URL for the user’s redirect.

        ``tenant_id`` MUST be passed into the provider’s metadata/custom_data,
        so that the webhook returns it back into :class:`BillingEvent`. In test mode
        (``BILLING_LIVE=false`` / an empty api key) there is no real charge —
        a deterministic test URL is returned.
        """
        ...

    def verify_and_parse_webhook(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> BillingEvent | None:
        """Verify the signature and parse the webhook into :class:`BillingEvent`.

        - An invalid signature → :class:`~src.billing.models.BillingSignatureError`
          (the route answers 400).
        - A valid signature, but the event is irrelevant (not a subscription one / not from
          the processed set) → ``None`` (the route answers 200, there is nothing to process).
        - Otherwise → the normalized :class:`BillingEvent`.
        """
        ...
