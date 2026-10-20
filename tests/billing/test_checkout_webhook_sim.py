"""An end-to-end SIMULATION of the payment path WITHOUT real Stripe keys (billing-sim).

A delivery artifact for the customer: «the payment is brought to the end-to-end, proven, waits only for
the owner’s keys». The mechanism is NOT touched — only tests. Here a unit assembly WITHOUT
a DB/network: the real ``StripeGateway`` (not a mock!) + the self-signature of the webhook body
with the known ``STRIPE_WEBHOOK_SECRET``:

  (a) the test mode of the checkout: ``StripeGateway(live=False).create_checkout`` returns a deterministic test URL and does NOT go to the network;
  (b) checkout→webhook(route): we build a real Stripe event
      ``checkout.session.completed`` (client_reference_id=tenant, metadata
      plan_key), self-sign it, POST via ``TestClient`` to
      ``/billing/webhook/stripe`` with the REAL ``get_gateway`` (the secret — from the settings)
      and a fake session_factory, where ``apply_billing_event`` is replaced
      by an interceptor → we check the parsed ``BillingEvent`` (activated, tenant,
      plan_key);
  (c) a signature mutation: a broken secret/signature → 400 (reject), the core is not called.

The live E2E (outbox→worker→email) — under ``TEST_RLS_DATABASE_URL`` in
``tests/db/test_billing_sim_e2e.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.billing import routes_billing
from src.api.config import CabinetSettings
from src.api.main import app
from src.billing.stripe import StripeGateway

# We choose the webhook secret ourselves — that is how the simulation works without the owner’s keys:
# we self-sign the body with the same secret and configure the provider in the route.
_WEBHOOK_SECRET = "whsec_sim_billing_123"
_TENANT_ID = "11111111-1111-1111-1111-111111111111"


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET, ts: str = "1700000000") -> str:
    """Build the ``Stripe-Signature`` header (the sample ``_sign`` from test_stripe)."""
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256)
    return f"t={ts},v1={mac.hexdigest()}"


def _checkout_completed_body(
    *, tenant_id: str = _TENANT_ID, plan_key: str = "pro"
) -> bytes:
    """The body of the Stripe event ``checkout.session.completed`` as the provider sends it."""
    return json.dumps(
        {
            "id": "evt_sim_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": tenant_id,
                    "customer": "cus_sim_1",
                    "subscription": "sub_sim_1",
                    "metadata": {"tenant_id": tenant_id, "plan_key": plan_key},
                }
            },
        }
    ).encode()


def _settings() -> CabinetSettings:
    """Settings with our webhook secret → the route builds a REAL StripeGateway."""
    return CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="test-secret",
        CABINET_COOKIE_SECURE=False,
        BILLING_PROVIDER="stripe",
        STRIPE_WEBHOOK_SECRET=_WEBHOOK_SECRET,
    )


class _NoopSession:
    """A stub session: the core is substituted, the DB is not touched."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _noop_factory():
    return _NoopSession()


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _client() -> TestClient:
    return TestClient(app, follow_redirects=False)


# --------------------------------------------------------------------------- #
#  (a) checkout in test mode — a deterministic URL, without the network
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_checkout_test_mode_deterministic_url_no_network():
    """``live=False`` → a test URL with markers, there is no real call to Stripe.

    There is no network by construction: in the test branch ``create_checkout`` does not call httpx at all (a return before the ``AsyncClient`` block). We check the URL contract itself.
    """
    gw = StripeGateway(webhook_secret=_WEBHOOK_SECRET, api_key="", live=False)
    url = await gw.create_checkout(
        tenant_id=_TENANT_ID,
        plan_key="pro",
        success_url="https://nab.rs/app?billing=success",
        cancel_url="https://nab.rs/plans?billing=cancel",
    )
    assert url.startswith("https://nab.rs/app?billing=success")
    assert "billing_test=1" in url
    assert "provider=stripe" in url
    assert "plan=pro" in url
    assert f"tenant={_TENANT_ID}" in url


# --------------------------------------------------------------------------- #
#  (b) webhook(route) via the REAL get_gateway → the parsed BillingEvent
# --------------------------------------------------------------------------- #


def test_webhook_real_gateway_parses_event_and_calls_core(monkeypatch):
    """A self-signed checkout.session.completed → 200 and a correct BillingEvent.

    We do NOT mock the provider: the signature is really checked by the real ``StripeGateway``
    with the secret from the settings. We mock only the core (the interceptor) and session_factory —
    we prove that the chain route→gateway.verify_and_parse→core hands the core the correctly parsed event (kind=activated, tenant_id, plan_key).
    """
    body = _checkout_completed_body(plan_key="pro")
    captured = {}
    factory_used = {"opened": False}

    async def fake_apply(db, event):
        captured["event"] = event

    @asynccontextmanager
    async def fake_factory():
        factory_used["opened"] = True
        yield _NoopSession()

    # get_gateway is NOT touched — the route builds a real StripeGateway from the settings.
    monkeypatch.setattr(routes_billing, "apply_billing_event", fake_apply)
    app.dependency_overrides[deps.get_settings] = _settings
    app.dependency_overrides[deps.get_session_factory] = lambda: fake_factory

    resp = _client().post(
        "/billing/webhook/stripe",
        content=body,
        headers={"Stripe-Signature": _sign(body)},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    event = captured["event"]
    assert event is not None
    assert event.provider == "stripe"
    assert event.provider_event_id == "evt_sim_1"
    assert event.type == "activated"
    assert event.tenant_id == _TENANT_ID
    assert event.plan_key == "pro"
    assert event.provider_customer_id == "cus_sim_1"
    assert event.provider_subscription_id == "sub_sim_1"
    # The core was really called inside the open transaction of session_factory.
    assert factory_used["opened"] is True


# --------------------------------------------------------------------------- #
#  (c) a signature mutation: a broken secret/signature → 400, the core is not called
# --------------------------------------------------------------------------- #


def test_webhook_wrong_secret_rejected_400(monkeypatch):
    """A signature with a foreign secret → 400, the core (apply_billing_event) is not called."""
    body = _checkout_completed_body()
    called = {"apply": False}

    async def fake_apply(db, event):
        called["apply"] = True

    monkeypatch.setattr(routes_billing, "apply_billing_event", fake_apply)
    app.dependency_overrides[deps.get_settings] = _settings
    app.dependency_overrides[deps.get_session_factory] = lambda: _noop_factory

    resp = _client().post(
        "/billing/webhook/stripe",
        content=body,
        headers={"Stripe-Signature": _sign(body, secret="whsec_attacker")},
    )
    assert resp.status_code == 400
    assert called["apply"] is False


def test_webhook_tampered_body_rejected_400(monkeypatch):
    """The body was replaced after signing (the HMAC does not match) → 400, the core is not called."""
    signed_body = _checkout_completed_body(plan_key="pro")
    sig = _sign(signed_body)
    tampered = _checkout_completed_body(plan_key="business")  # the signature under pro
    called = {"apply": False}

    async def fake_apply(db, event):
        called["apply"] = True

    monkeypatch.setattr(routes_billing, "apply_billing_event", fake_apply)
    app.dependency_overrides[deps.get_settings] = _settings
    app.dependency_overrides[deps.get_session_factory] = lambda: _noop_factory

    resp = _client().post(
        "/billing/webhook/stripe",
        content=tampered,
        headers={"Stripe-Signature": sig},
    )
    assert resp.status_code == 400
    assert called["apply"] is False
