"""The Paddle connector: verify_and_parse_webhook (the signature, the mapping) + checkout."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from src.billing.models import BillingSignatureError
from src.billing.paddle import (
    PaddleGateway,
    _iso_to_dt,
    _parse_signature_header,
    _sub_type_from_status,
)

_SECRET = "pdl_ntfset_test"


def _sign(body: bytes, secret: str = _SECRET, ts: str = "1700000000") -> str:
    mac = hmac.new(secret.encode(), f"{ts}:".encode() + body, hashlib.sha256)
    return f"ts={ts};h1={mac.hexdigest()}"


def _body(**payload) -> bytes:
    return json.dumps(payload).encode()


def _gw() -> PaddleGateway:
    return PaddleGateway(webhook_secret=_SECRET)


def test_valid_signature_parses_subscription_activated():
    body = _body(
        event_id="evt_pad_1",
        event_type="subscription.activated",
        data={
            "id": "sub_pad_1",
            "customer_id": "ctm_1",
            "status": "active",
            "custom_data": {"tenant_id": "tenant-xyz", "plan_key": "pro"},
            "current_billing_period": {"ends_at": "2030-01-01T00:00:00Z"},
        },
    )
    event = _gw().verify_and_parse_webhook(body, {"Paddle-Signature": _sign(body)})
    assert event is not None
    assert event.provider == "paddle"
    assert event.provider_event_id == "evt_pad_1"
    assert event.type == "activated"
    assert event.tenant_id == "tenant-xyz"
    assert event.plan_key == "pro"
    assert event.provider_customer_id == "ctm_1"
    assert event.provider_subscription_id == "sub_pad_1"
    assert event.current_period_end is not None
    assert event.current_period_end.year == 2030


def test_subscription_canceled_maps():
    body = _body(
        event_id="evt_pad_2",
        event_type="subscription.canceled",
        data={"id": "sub_2", "customer_id": "ctm_2", "custom_data": {}},
    )
    event = _gw().verify_and_parse_webhook(body, {"Paddle-Signature": _sign(body)})
    assert event is not None
    assert event.type == "canceled"


def test_transaction_completed_maps_to_renewed():
    body = _body(
        event_id="evt_pad_3",
        event_type="transaction.completed",
        data={"subscription_id": "sub_3", "customer_id": "ctm_3", "custom_data": {}},
    )
    event = _gw().verify_and_parse_webhook(body, {"Paddle-Signature": _sign(body)})
    assert event is not None
    assert event.type == "renewed"
    assert event.provider_subscription_id == "sub_3"


def test_bad_signature_raises():
    body = _body(event_id="evt_pad_4", event_type="subscription.activated", data={})
    bad = _sign(body, secret="nope")
    with pytest.raises(BillingSignatureError):
        _gw().verify_and_parse_webhook(body, {"Paddle-Signature": bad})


def test_missing_header_raises():
    body = _body(event_id="evt_pad_5", event_type="subscription.activated", data={})
    with pytest.raises(BillingSignatureError):
        _gw().verify_and_parse_webhook(body, {})


def test_irrelevant_event_returns_none():
    body = _body(event_id="evt_pad_6", event_type="address.created", data={})
    assert (
        _gw().verify_and_parse_webhook(body, {"Paddle-Signature": _sign(body)}) is None
    )


@pytest.mark.asyncio
async def test_create_checkout_test_mode_returns_url():
    gw = PaddleGateway(api_key="", live=False)
    url = await gw.create_checkout(
        tenant_id="t2",
        plan_key="business",
        success_url="https://nab.rs/app",
        cancel_url="https://nab.rs/plans",
    )
    assert "provider=paddle" in url
    assert "plan=business" in url
    assert "tenant=t2" in url


@pytest.mark.asyncio
async def test_create_checkout_live_without_config_raises():
    gw = PaddleGateway(api_key="pdl_live", live=True)
    with pytest.raises(NotImplementedError):
        await gw.create_checkout(
            tenant_id="t3",
            plan_key="pro",
            success_url="https://nab.rs/app",
            cancel_url="https://nab.rs/plans",
        )


# --------------------------------------------------------------------------- #
#  event-map: the uncovered direct events
# --------------------------------------------------------------------------- #


def test_subscription_resumed_maps_to_activated():
    body = _body(
        event_id="evt_r", event_type="subscription.resumed", data={"id": "sub"}
    )
    ev = _gw().verify_and_parse_webhook(body, {"Paddle-Signature": _sign(body)})
    assert ev is not None and ev.type == "activated"


def test_subscription_paused_maps_to_canceled():
    body = _body(event_id="evt_p", event_type="subscription.paused", data={"id": "sub"})
    ev = _gw().verify_and_parse_webhook(body, {"Paddle-Signature": _sign(body)})
    assert ev is not None and ev.type == "canceled"


def test_subscription_past_due_maps():
    body = _body(
        event_id="evt_pd", event_type="subscription.past_due", data={"id": "sub"}
    )
    ev = _gw().verify_and_parse_webhook(body, {"Paddle-Signature": _sign(body)})
    assert ev is not None and ev.type == "past_due"


def test_subscription_updated_status_branches():
    # subscription.updated is handled by the status via _sub_type_from_status.
    for status, expected in [
        ("active", "renewed"),
        ("trialing", "renewed"),
        ("past_due", "past_due"),
        ("canceled", "canceled"),
        ("paused", "canceled"),
    ]:
        body = _body(
            event_id=f"evt_u_{status}",
            event_type="subscription.updated",
            data={"id": "sub", "status": status},
        )
        ev = _gw().verify_and_parse_webhook(body, {"Paddle-Signature": _sign(body)})
        assert ev is not None and ev.type == expected, status


def test_subscription_updated_unknown_status_is_none():
    body = _body(
        event_id="evt_u_x",
        event_type="subscription.updated",
        data={"id": "sub", "status": "some_new_status"},
    )
    assert (
        _gw().verify_and_parse_webhook(body, {"Paddle-Signature": _sign(body)}) is None
    )


# --------------------------------------------------------------------------- #
#  Pure helpers (without the signature/network)
# --------------------------------------------------------------------------- #


def test_sub_type_from_status_pure():
    assert _sub_type_from_status("active") == "renewed"
    assert _sub_type_from_status("trialing") == "renewed"
    assert _sub_type_from_status("past_due") == "past_due"
    assert _sub_type_from_status("canceled") == "canceled"
    assert _sub_type_from_status("paused") == "canceled"
    assert _sub_type_from_status("weird") is None
    assert _sub_type_from_status(None) is None


def test_iso_to_dt_pure():
    dt = _iso_to_dt("2030-01-01T00:00:00Z")
    assert dt is not None and dt.year == 2030 and dt.tzinfo is not None
    assert _iso_to_dt("") is None
    assert _iso_to_dt(None) is None
    assert _iso_to_dt(12345) is None  # not a string
    assert _iso_to_dt("not-a-date") is None  # the ValueError branch


def test_parse_signature_header_pure():
    ts, h1 = _parse_signature_header("ts=1700000000;h1=aaa")
    assert ts == "1700000000" and h1 == ["aaa"]
    # Several h1 (key rotation) — we return all.
    ts2, h1_2 = _parse_signature_header("ts=1;h1=aaa;h1=bbb")
    assert ts2 == "1" and h1_2 == ["aaa", "bbb"]
    # No ts → None; no h1 → an empty list.
    ts3, h1_3 = _parse_signature_header("h1=only")
    assert ts3 is None and h1_3 == ["only"]
