"""Universal payment connector (VG-11.1).

Adapter pattern: one abstraction :class:`~src.billing.gateway.PaymentGateway`,
two connector implementations (Paddle, Stripe), the choice of the provider via config
(:func:`~src.billing.registry.get_active_gateway`). The core of applying a payment to a
tenant (:func:`~src.billing.service.apply_billing_event`) works ONLY with the
provider-neutral :class:`~src.billing.models.BillingEvent` — adding a
new gateway = a new adapter file + registration, without editing the core.

Test mode (``BILLING_LIVE=false``): there is no real charge, ``create_checkout``
returns a deterministic test URL “as if the payment works”. Live —
with one flag later (VG-11b).
"""

from __future__ import annotations

from src.billing.models import BillingEvent, BillingEventType, BillingSignatureError

__all__ = ["BillingEvent", "BillingEventType", "BillingSignatureError"]
