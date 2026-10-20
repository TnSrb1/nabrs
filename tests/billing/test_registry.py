"""The registry of connectors: the choice of the provider by the config + the conformity to the Protocol."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.billing.gateway import PaymentGateway
from src.billing.paddle import PaddleGateway
from src.billing.registry import (
    _stripe_prices,
    billing_configured,
    get_active_gateway,
    get_gateway,
    supported_providers,
)
from src.billing.stripe import StripeGateway


def _settings(provider: str = "stripe", live: bool = False):
    return SimpleNamespace(
        billing_provider=provider,
        billing_live=live,
        paddle_api_key="",
        paddle_webhook_secret="whs_paddle",
        stripe_api_key="",
        stripe_webhook_secret="whs_stripe",
        stripe_price_pro="",
        stripe_price_business="",
    )


def test_both_gateways_conform_to_protocol():
    """Both connectors satisfy PaymentGateway (runtime_checkable)."""
    assert isinstance(StripeGateway(), PaymentGateway)
    assert isinstance(PaddleGateway(), PaymentGateway)


def test_supported_providers():
    assert set(supported_providers()) == {"paddle", "stripe"}


@pytest.mark.parametrize(
    "provider,cls",
    [("paddle", PaddleGateway), ("stripe", StripeGateway)],
)
def test_get_gateway_returns_requested_provider(provider, cls):
    gw = get_gateway(_settings(), provider)
    assert isinstance(gw, cls)
    assert gw.provider_name == provider


def test_get_active_gateway_follows_config():
    assert isinstance(get_active_gateway(_settings("paddle")), PaddleGateway)
    assert isinstance(get_active_gateway(_settings("stripe")), StripeGateway)


def test_get_gateway_unknown_raises():
    with pytest.raises(ValueError, match="неизвестный платёжный провайдер"):
        get_gateway(_settings(), "braintree")


def _cfg(provider="stripe", **keys):
    base = dict(
        billing_provider=provider,
        billing_live=False,
        paddle_api_key="",
        paddle_webhook_secret="",
        stripe_api_key="",
        stripe_webhook_secret="",
        stripe_price_pro="",
        stripe_price_business="",
    )
    base.update(keys)
    return SimpleNamespace(**base)


def test_billing_configured_true_when_stripe_keys_set():
    st = _cfg("stripe", stripe_api_key="sk_test", stripe_webhook_secret="whsec")
    assert billing_configured(st) is True


def test_billing_configured_true_when_paddle_keys_set():
    st = _cfg("paddle", paddle_api_key="pk_test", paddle_webhook_secret="whs")
    assert billing_configured(st) is True


def test_billing_configured_false_when_no_keys():
    # Default: the provider is chosen, but the keys are empty → “Soon” on the showcase.
    assert billing_configured(_cfg("stripe")) is False


def test_billing_configured_false_when_partial_keys():
    # There is an api_key, but no webhook_secret — the webhook will not confirm the subscription.
    st = _cfg("stripe", stripe_api_key="sk_test")
    assert billing_configured(st) is False


def test_billing_configured_false_for_unknown_provider():
    st = _cfg("braintree", stripe_api_key="sk", stripe_webhook_secret="wh")
    assert billing_configured(st) is False


def test_billing_configured_ignores_other_provider_keys():
    # stripe is active, but only the paddle keys are set → not configured.
    st = _cfg("stripe", paddle_api_key="pk", paddle_webhook_secret="wh")
    assert billing_configured(st) is False


def test_billing_configured_whitespace_key_is_empty():
    st = _cfg("stripe", stripe_api_key="  ", stripe_webhook_secret="whsec")
    assert billing_configured(st) is False


def test_gateway_receives_secrets_from_settings():
    """The registry passes the secrets/flag from the settings into the connector."""
    st = _settings("stripe", live=True)
    st.stripe_webhook_secret = "sekret"
    gw = get_active_gateway(st)
    # An internal field — checked via a private attribute (white box).
    assert gw._webhook_secret == "sekret"
    assert gw._live is True


# ── the mapping plan_key→price_id + the live gate of price_id ────────────────────────────


def test_stripe_prices_maps_from_env():
    """_stripe_prices reads STRIPE_PRICE_{plan} by the keys of the showcase from the catalog."""
    st = _cfg("stripe", stripe_price_pro="price_p", stripe_price_business="price_b")
    prices = _stripe_prices(st)
    assert prices == {"pro": "price_p", "business": "price_b"}


def test_stripe_prices_strips_whitespace_empty():
    st = _cfg("stripe", stripe_price_pro="  ", stripe_price_business="")
    assert _stripe_prices(st) == {"pro": "", "business": ""}


def test_stripe_gateway_receives_prices():
    """The registry passes the price_id mapping into the connector."""
    st = _settings("stripe", live=True)
    st.stripe_price_pro = "price_p"
    st.stripe_price_business = "price_b"
    gw = get_active_gateway(st)
    assert gw._prices == {"pro": "price_p", "business": "price_b"}


def test_billing_configured_live_requires_price_ids():
    """Live + the keys are present, but price_id is empty → False (showcase “Soon”, fail-closed)."""
    st = _cfg(
        "stripe",
        billing_live=True,
        stripe_api_key="sk_live",
        stripe_webhook_secret="whsec",
    )
    assert billing_configured(st) is False


def test_billing_configured_live_partial_price_ids_false():
    """Live: price_id is set only for pro, business is empty → False."""
    st = _cfg(
        "stripe",
        billing_live=True,
        stripe_api_key="sk_live",
        stripe_webhook_secret="whsec",
        stripe_price_pro="price_p",
    )
    assert billing_configured(st) is False


def test_billing_configured_live_true_with_all_price_ids():
    st = _cfg(
        "stripe",
        billing_live=True,
        stripe_api_key="sk_live",
        stripe_webhook_secret="whsec",
        stripe_price_pro="price_p",
        stripe_price_business="price_b",
    )
    assert billing_configured(st) is True


def test_billing_configured_test_mode_ignores_price_ids():
    """Test mode: price_id is not required — the test keys are enough (“configured”)."""
    st = _cfg(
        "stripe",
        billing_live=False,
        stripe_api_key="sk_test",
        stripe_webhook_secret="whsec",
    )
    assert billing_configured(st) is True
