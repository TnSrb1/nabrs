"""The Stripe connector: verify_and_parse_webhook (the signature, the mapping) + checkout."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from src.billing import stripe as stripe_mod
from src.billing.models import BillingConfigError, BillingSignatureError
from src.billing.stripe import (
    StripeGateway,
    _epoch_to_dt,
    _parse_signature_header,
    _sub_type_from_status,
)

_SECRET = "whsec_test_123"


def _sign(body: bytes, secret: str = _SECRET, ts: str = "1700000000") -> str:
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256)
    return f"t={ts},v1={mac.hexdigest()}"


def _body(**payload) -> bytes:
    return json.dumps(payload).encode()


def _gw() -> StripeGateway:
    return StripeGateway(webhook_secret=_SECRET)


def test_valid_signature_parses_checkout_completed():
    body = _body(
        id="evt_1",
        type="checkout.session.completed",
        data={
            "object": {
                "client_reference_id": "tenant-abc",
                "customer": "cus_1",
                "subscription": "sub_1",
                "metadata": {"plan_key": "pro"},
            }
        },
    )
    event = _gw().verify_and_parse_webhook(body, {"Stripe-Signature": _sign(body)})
    assert event is not None
    assert event.provider == "stripe"
    assert event.provider_event_id == "evt_1"
    assert event.type == "activated"
    assert event.tenant_id == "tenant-abc"
    assert event.plan_key == "pro"
    assert event.provider_customer_id == "cus_1"
    assert event.provider_subscription_id == "sub_1"


def test_subscription_updated_status_maps_to_type():
    body = _body(
        id="evt_2",
        type="customer.subscription.updated",
        data={
            "object": {
                "id": "sub_9",
                "customer": "cus_9",
                "status": "past_due",
                "current_period_end": 1893456000,
                "metadata": {"tenant_id": "t9", "plan_key": "business"},
            }
        },
    )
    event = _gw().verify_and_parse_webhook(body, {"Stripe-Signature": _sign(body)})
    assert event is not None
    assert event.type == "past_due"
    assert event.provider_subscription_id == "sub_9"
    assert event.current_period_end is not None
    assert event.current_period_end.year == 2030


def test_bad_signature_raises():
    body = _body(id="evt_3", type="checkout.session.completed", data={"object": {}})
    bad = _sign(body, secret="wrong_secret")
    with pytest.raises(BillingSignatureError):
        _gw().verify_and_parse_webhook(body, {"Stripe-Signature": bad})


def test_missing_signature_header_raises():
    body = _body(id="evt_4", type="checkout.session.completed", data={"object": {}})
    with pytest.raises(BillingSignatureError):
        _gw().verify_and_parse_webhook(body, {})


def test_empty_secret_raises():
    body = _body(id="evt_5", type="checkout.session.completed", data={"object": {}})
    with pytest.raises(BillingSignatureError):
        StripeGateway(webhook_secret="").verify_and_parse_webhook(
            body, {"Stripe-Signature": _sign(body)}
        )


def test_irrelevant_event_returns_none():
    """A valid signature, but an irrelevant type → None (the route returns 200)."""
    body = _body(id="evt_6", type="customer.created", data={"object": {}})
    assert (
        _gw().verify_and_parse_webhook(body, {"Stripe-Signature": _sign(body)}) is None
    )


@pytest.mark.asyncio
async def test_create_checkout_test_mode_returns_url():
    """Test mode (not live / no key) → a deterministic test URL without the network."""
    gw = StripeGateway(api_key="", live=False)
    url = await gw.create_checkout(
        tenant_id="t1",
        plan_key="pro",
        success_url="https://nab.rs/app",
        cancel_url="https://nab.rs/plans",
    )
    assert url.startswith("https://nab.rs/app")
    assert "provider=stripe" in url
    assert "plan=pro" in url
    assert "tenant=t1" in url


# --------------------------------------------------------------------------- #
#  Live checkout: line_items[price_id] + fail-closed without price_id
# --------------------------------------------------------------------------- #


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):  # noqa: D401 — stub
        return None

    def json(self):
        return self._payload


class _CaptureClient:
    """A fake httpx.AsyncClient: records the POST data, returns the url response."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, auth=None):
        _CaptureClient.captured = {"url": url, "data": data, "auth": auth}
        return _FakeResp({"url": "https://checkout.stripe.com/c/sess_live_1"})


def _live_gw() -> StripeGateway:
    return StripeGateway(
        api_key="sk_live_x",
        live=True,
        prices={"pro": "price_pro_123", "business": "price_biz_456"},
    )


@pytest.mark.asyncio
async def test_create_checkout_live_includes_line_items(monkeypatch):
    """Live mode: data contains line_items[0][price]=price_id from the mapping + qty=1."""
    _CaptureClient.captured = {}
    monkeypatch.setattr(stripe_mod.httpx, "AsyncClient", _CaptureClient)
    url = await _live_gw().create_checkout(
        tenant_id="t-live",
        plan_key="business",
        success_url="https://nab.rs/app",
        cancel_url="https://nab.rs/plans",
    )
    assert url == "https://checkout.stripe.com/c/sess_live_1"
    data = _CaptureClient.captured["data"]
    # price_id is taken from the mapping by plan_key (business → price_biz_456).
    assert data["line_items[0][price]"] == "price_biz_456"
    assert data["line_items[0][quantity]"] == "1"
    assert data["mode"] == "subscription"
    assert data["client_reference_id"] == "t-live"
    # the api key is in auth, not in data/logs.
    assert _CaptureClient.captured["auth"] == ("sk_live_x", "")


@pytest.mark.asyncio
async def test_create_checkout_live_maps_pro_price(monkeypatch):
    """The mapping plan_key→price_id: pro → its own price_id (not business)."""
    _CaptureClient.captured = {}
    monkeypatch.setattr(stripe_mod.httpx, "AsyncClient", _CaptureClient)
    await _live_gw().create_checkout(
        tenant_id="t2",
        plan_key="pro",
        success_url="https://nab.rs/app",
        cancel_url="https://nab.rs/plans",
    )
    assert _CaptureClient.captured["data"]["line_items[0][price]"] == "price_pro_123"


@pytest.mark.asyncio
async def test_create_checkout_live_empty_price_fail_closed(monkeypatch):
    """price_id not set → BillingConfigError BEFORE the network (the raw request does not go out)."""
    _CaptureClient.captured = {}
    monkeypatch.setattr(stripe_mod.httpx, "AsyncClient", _CaptureClient)
    gw = StripeGateway(api_key="sk_live_x", live=True, prices={"pro": ""})
    with pytest.raises(BillingConfigError):
        await gw.create_checkout(
            tenant_id="t3",
            plan_key="pro",
            success_url="https://nab.rs/app",
            cancel_url="https://nab.rs/plans",
        )
    # Not a single call to Stripe: the capture dictionary is empty.
    assert _CaptureClient.captured == {}


@pytest.mark.asyncio
async def test_create_checkout_live_unknown_plan_fail_closed(monkeypatch):
    """a plan_key outside the mapping → fail-closed too (not a KeyError, not a raw request)."""
    _CaptureClient.captured = {}
    monkeypatch.setattr(stripe_mod.httpx, "AsyncClient", _CaptureClient)
    with pytest.raises(BillingConfigError):
        await _live_gw().create_checkout(
            tenant_id="t4",
            plan_key="enterprise",
            success_url="https://nab.rs/app",
            cancel_url="https://nab.rs/plans",
        )
    assert _CaptureClient.captured == {}


# --------------------------------------------------------------------------- #
#  event-map: the uncovered direct events
# --------------------------------------------------------------------------- #


def _obj_body(event_id, event_type, obj):
    return _body(id=event_id, type=event_type, data={"object": obj})


def test_event_map_direct_entries():
    cases = [
        ("customer.subscription.created", "activated"),
        ("invoice.paid", "renewed"),
        ("invoice.payment_succeeded", "renewed"),
        ("invoice.payment_failed", "past_due"),
        ("customer.subscription.deleted", "canceled"),
    ]
    for i, (etype, expected) in enumerate(cases):
        body = _obj_body(f"evt_map_{i}", etype, {"customer": "cus", "metadata": {}})
        ev = _gw().verify_and_parse_webhook(body, {"Stripe-Signature": _sign(body)})
        assert ev is not None and ev.type == expected, etype


def test_subscription_created_subscription_id_from_object_id():
    # For customer.subscription.* the subscription_id is taken from object.id.
    body = _obj_body(
        "evt_sc",
        "customer.subscription.created",
        {"id": "sub_new", "customer": "cus", "metadata": {"tenant_id": "t"}},
    )
    ev = _gw().verify_and_parse_webhook(body, {"Stripe-Signature": _sign(body)})
    assert ev is not None and ev.provider_subscription_id == "sub_new"


# --------------------------------------------------------------------------- #
#  Pure helpers (without the signature/network)
# --------------------------------------------------------------------------- #


def test_sub_type_from_status_pure():
    assert _sub_type_from_status("active") == "renewed"
    assert _sub_type_from_status("trialing") == "renewed"
    assert _sub_type_from_status("past_due") == "past_due"
    assert _sub_type_from_status("canceled") == "canceled"
    assert _sub_type_from_status("unpaid") == "canceled"
    assert _sub_type_from_status("incomplete_expired") == "canceled"
    assert _sub_type_from_status("incomplete") is None
    assert _sub_type_from_status(None) is None


def test_epoch_to_dt_pure():
    dt = _epoch_to_dt(1893456000)
    assert dt is not None and dt.year == 2030 and dt.tzinfo is not None
    assert _epoch_to_dt(0) is None  # zero → None
    assert _epoch_to_dt(-5) is None  # negative → None
    assert _epoch_to_dt("1893456000") is None  # not a number
    assert _epoch_to_dt(None) is None


def test_parse_signature_header_pure():
    ts, v1 = _parse_signature_header("t=1700000000,v1=abc")
    assert ts == "1700000000" and v1 == ["abc"]
    # Several v1 (key rotation) — all.
    ts2, v1_2 = _parse_signature_header("t=1,v1=a,v1=b")
    assert ts2 == "1" and v1_2 == ["a", "b"]
    ts3, v1_3 = _parse_signature_header("v1=only")
    assert ts3 is None and v1_3 == ["only"]
