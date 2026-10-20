"""Registry of the payment connectors (VG-11.1).

The choice of the active gateway by config. Adding a third provider = import its
adapter and add a row to :data:`_FACTORIES` — the core/routes do not change.

``settings`` — any object with the attributes ``billing_provider``, ``billing_live``
and the provider keys (``CabinetSettings`` fits). The registry does not depend on
``src.api`` at the import level — it reads the attributes by name (duck-typing).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from src.billing.gateway import PaymentGateway
from src.billing.paddle import PaddleGateway
from src.billing.stripe import StripeGateway
from src.plans.catalog import checkout_plan_keys


class BillingSettingsLike(Protocol):
    """The minimal settings contract needed by the registry (see ``CabinetSettings``)."""

    billing_provider: str
    billing_live: bool
    paddle_api_key: str
    paddle_webhook_secret: str
    stripe_api_key: str
    stripe_webhook_secret: str
    stripe_price_pro: str
    stripe_price_business: str


def _stripe_prices(settings: BillingSettingsLike) -> dict[str, str]:
    """The plan_key→price_id mapping from the env ``STRIPE_PRICE_{PLAN_KEY}``.

    The keys — the storefront plans going into the paid checkout (``checkout_plan_keys()``,
    the source of truth — the catalog). The settings field name = ``stripe_price_{plan_key}``
    (duck-typing, like the whole registry). An empty/whitespace value → an empty string
    (price_id is not set).
    """
    return {
        key: (getattr(settings, f"stripe_price_{key}", "") or "").strip()
        for key in checkout_plan_keys()
    }


def _make_paddle(settings: BillingSettingsLike) -> PaymentGateway:
    return PaddleGateway(
        api_key=settings.paddle_api_key,
        webhook_secret=settings.paddle_webhook_secret,
        live=settings.billing_live,
    )


def _make_stripe(settings: BillingSettingsLike) -> PaymentGateway:
    return StripeGateway(
        api_key=settings.stripe_api_key,
        webhook_secret=settings.stripe_webhook_secret,
        live=settings.billing_live,
        prices=_stripe_prices(settings),
    )


# Provider registry: name → factory. The only place of extension.
_FACTORIES: dict[str, Callable[[BillingSettingsLike], PaymentGateway]] = {
    "paddle": _make_paddle,
    "stripe": _make_stripe,
}


def supported_providers() -> tuple[str, ...]:
    """The names of the registered providers."""
    return tuple(_FACTORIES)


def get_gateway(settings: BillingSettingsLike, provider: str) -> PaymentGateway:
    """The connector by an explicit provider name (for the webhook route ``/{provider}``).

    :raises ValueError: the provider is not registered.
    """
    factory = _FACTORIES.get(provider)
    if factory is None:
        raise ValueError(f"неизвестный платёжный провайдер: {provider!r}")
    return factory(settings)


def get_active_gateway(settings: BillingSettingsLike) -> PaymentGateway:
    """The active connector by ``settings.billing_provider`` (for checkout)."""
    return get_gateway(settings, settings.billing_provider)


# The keys required for a “configured” provider: without them the checkout cannot be created
# and the webhook confirmation of the subscription will not pass. The names match the fields
# of ``CabinetSettings`` (read by name, duck-typing — like the whole registry).
_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "paddle": ("paddle_api_key", "paddle_webhook_secret"),
    "stripe": ("stripe_api_key", "stripe_webhook_secret"),
}


def billing_configured(settings: BillingSettingsLike) -> bool:
    """True if the active provider is registered AND ready to accept payment.

    The UI gate of the storefront/cabinet: without keys the checkout buttons are INACTIVE (“Soon”),
    so as not to lead the user into a non-working payment (VG-11.2). The base condition — all the keys
    of the provider are non-empty. In test mode (``billing_live=false``) this is enough
    (``create_checkout`` gives a deterministic test URL, a real charge and a price_id are not
    required). In **live mode of Stripe** a price_id for EACH storefront plan (``STRIPE_PRICE_*``) is additionally
    required — otherwise the live checkout is fail-closed, so the storefront honestly shows “Soon” until the owner
    enters the requisites. An unknown provider / an empty key / a missing live price_id
    → ``False`` (fail-closed).
    """
    provider = (getattr(settings, "billing_provider", "") or "").strip()
    required = _REQUIRED_KEYS.get(provider)
    if not required:
        return False
    if not all((getattr(settings, name, "") or "").strip() for name in required):
        return False
    if provider == "stripe" and bool(getattr(settings, "billing_live", False)):
        prices = _stripe_prices(settings)
        if not all(prices.get(key) for key in checkout_plan_keys()):
            return False
    return True
