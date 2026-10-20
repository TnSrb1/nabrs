"""The billing routes: the webhook (signature/idempotency/routing) and checkout.

Via TestClient with dependency_overrides; the connector/core are mocked (no network/DB).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.billing import routes_billing
from src.api.config import CabinetSettings
from src.api.main import app
from src.billing.models import (
    BillingConfigError,
    BillingEvent,
    BillingSignatureError,
)


def _settings(provider: str = "stripe") -> CabinetSettings:
    return CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="test-secret",
        CABINET_COOKIE_SECURE=False,
        BILLING_PROVIDER=provider,
    )


class _FakeGateway:
    provider_name = "stripe"

    def __init__(
        self,
        *,
        event=None,
        raises=False,
        checkout_url="https://pay/x",
        checkout_raises=None,
    ):
        self._event = event
        self._raises = raises
        self._checkout_url = checkout_url
        self._checkout_raises = checkout_raises
        self.verify_calls = 0

    def verify_and_parse_webhook(self, raw_body, headers):
        self.verify_calls += 1
        if self._raises:
            raise BillingSignatureError("bad")
        return self._event

    async def create_checkout(self, *, tenant_id, plan_key, success_url, cancel_url):
        self.last = dict(
            tenant_id=tenant_id,
            plan_key=plan_key,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        if self._checkout_raises is not None:
            raise self._checkout_raises
        return self._checkout_url


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _client() -> TestClient:
    return TestClient(app, follow_redirects=False)


# ── webhook ──────────────────────────────────────────────────────────────────


def test_webhook_valid_event_applies_and_200(monkeypatch):
    event = BillingEvent(
        provider="stripe",
        provider_event_id="evt_1",
        type="activated",
        provider_customer_id="cus",
        provider_subscription_id="sub",
        plan_key="pro",
        current_period_end=None,
        tenant_id="t1",
    )
    gw = _FakeGateway(event=event)
    applied = {}

    async def fake_apply(db, ev):
        applied["event"] = ev

    monkeypatch.setattr(routes_billing, "get_gateway", lambda s, p: gw)
    monkeypatch.setattr(routes_billing, "apply_billing_event", fake_apply)
    app.dependency_overrides[deps.get_settings] = _settings
    app.dependency_overrides[deps.get_session_factory] = lambda: _fake_factory

    resp = _client().post("/billing/webhook/stripe", content=b"{}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert applied["event"].provider_event_id == "evt_1"


def test_webhook_bad_signature_400(monkeypatch):
    gw = _FakeGateway(raises=True)
    monkeypatch.setattr(routes_billing, "get_gateway", lambda s, p: gw)
    app.dependency_overrides[deps.get_settings] = _settings

    resp = _client().post("/billing/webhook/stripe", content=b"{}")
    assert resp.status_code == 400


def test_webhook_irrelevant_event_200_ignored(monkeypatch):
    gw = _FakeGateway(event=None)
    called = {"apply": False}

    async def fake_apply(db, ev):
        called["apply"] = True

    monkeypatch.setattr(routes_billing, "get_gateway", lambda s, p: gw)
    monkeypatch.setattr(routes_billing, "apply_billing_event", fake_apply)
    app.dependency_overrides[deps.get_settings] = _settings

    resp = _client().post("/billing/webhook/stripe", content=b"{}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert called["apply"] is False


def test_webhook_unknown_provider_404():
    app.dependency_overrides[deps.get_settings] = _settings
    resp = _client().post("/billing/webhook/braintree", content=b"{}")
    assert resp.status_code == 404


# ── checkout ─────────────────────────────────────────────────────────────────


def test_checkout_redirects_to_gateway_url(monkeypatch):
    gw = _FakeGateway(checkout_url="https://checkout.stripe/x")
    monkeypatch.setattr(routes_billing, "get_active_gateway", lambda s: gw)
    app.dependency_overrides[deps.get_settings] = _settings
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": "u1",
        "tid": "tenant-1",
    }

    resp = _client().post("/billing/checkout/pro")
    assert resp.status_code == 303
    assert resp.headers["location"] == "https://checkout.stripe/x"
    assert gw.last["tenant_id"] == "tenant-1"
    assert gw.last["plan_key"] == "pro"


def test_checkout_anonymous_redirects_login():
    app.dependency_overrides[deps.get_settings] = _settings
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    resp = _client().post("/billing/checkout/pro")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_checkout_free_plan_redirects_plans(monkeypatch):
    gw = _FakeGateway()
    monkeypatch.setattr(routes_billing, "get_active_gateway", lambda s: gw)
    app.dependency_overrides[deps.get_settings] = _settings
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": "u1",
        "tid": "t1",
    }
    resp = _client().post("/billing/checkout/free")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/plans"


def test_checkout_unknown_plan_redirects_plans():
    app.dependency_overrides[deps.get_settings] = _settings
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": "u1",
        "tid": "t1",
    }
    resp = _client().post("/billing/checkout/mythic")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/plans"


def test_checkout_price_not_configured_fail_closed(monkeypatch):
    """price_id is not set (BillingConfigError) → back to /plans, not a 500."""
    gw = _FakeGateway(checkout_raises=BillingConfigError("no price_id for pro"))
    monkeypatch.setattr(routes_billing, "get_active_gateway", lambda s: gw)
    app.dependency_overrides[deps.get_settings] = _settings
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": "u1",
        "tid": "t1",
    }
    resp = _client().post("/billing/checkout/pro")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/plans?billing=unavailable"


# ── helpers ──────────────────────────────────────────────────────────────────


class _NoopSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_factory():
    return _NoopSession()
