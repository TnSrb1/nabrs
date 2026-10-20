"""Tests of anti-abuse: Turnstile (ok/fail), Kickbox (deliverable/disposable),
risk scoring, step-up stub. The network is mocked by substituting httpx.AsyncClient."""

from __future__ import annotations

import sys
import types

import pytest

from src.api import anti_abuse as aa


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Substitution of httpx.AsyncClient: returns a preset JSON on post/get."""

    last_instance = None

    def __init__(self, *args, **kwargs):
        self.payload = {}
        self.calls = []
        type(self).last_instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, **kwargs):
        self.calls.append(("post", url, data))
        return _FakeResponse(self.payload)

    async def get(self, url, params=None, **kwargs):
        self.calls.append(("get", url, params))
        return _FakeResponse(self.payload)


def _patch_httpx(monkeypatch, payload):
    fake_module = types.SimpleNamespace(AsyncClient=_FakeAsyncClient)
    _FakeAsyncClient.payload_default = payload
    # anti_abuse imports httpx lazily inside the method → we substitute it in sys.modules
    monkeypatch.setitem(sys.modules, "httpx", fake_module)

    # each new client must return the payload
    orig_init = _FakeAsyncClient.__init__

    def init(self, *a, **k):
        orig_init(self, *a, **k)
        self.payload = payload

    monkeypatch.setattr(_FakeAsyncClient, "__init__", init)


@pytest.mark.asyncio
async def test_turnstile_success(monkeypatch):
    _patch_httpx(monkeypatch, {"success": True})
    verifier = aa.LiveTurnstileVerifier("secret")
    assert await verifier.verify("token") is True


@pytest.mark.asyncio
async def test_turnstile_failure(monkeypatch):
    _patch_httpx(monkeypatch, {"success": False})
    verifier = aa.LiveTurnstileVerifier("secret")
    assert await verifier.verify("token") is False


@pytest.mark.asyncio
async def test_turnstile_empty_token_short_circuits():
    verifier = aa.LiveTurnstileVerifier("secret")
    assert await verifier.verify("") is False  # no network


@pytest.mark.asyncio
async def test_kickbox_deliverable(monkeypatch):
    _patch_httpx(monkeypatch, {"result": "deliverable", "disposable": False})
    v = await aa.KickboxValidator("key").validate("user@company.rs")
    assert v.deliverable is True and v.disposable is False and v.risky is False


@pytest.mark.asyncio
async def test_kickbox_disposable(monkeypatch):
    _patch_httpx(monkeypatch, {"result": "undeliverable", "disposable": True})
    # The domain is deliberately NOT from the local list `_DISPOSABLE_DOMAINS`: the test is about
    # the Kickbox branch, while a known disposable domain is cut off earlier, before the network
    # (see test_signup_abuse_hardening.py::test_disposable_check_precedes_network_call).
    v = await aa.KickboxValidator("key").validate("x@some-unknown-temp.example")
    assert v.disposable is True and v.deliverable is False and v.undeliverable is True


@pytest.mark.asyncio
async def test_kickbox_risky_accept_all(monkeypatch):
    _patch_httpx(monkeypatch, {"result": "risky", "accept_all": True})
    v = await aa.KickboxValidator("key").validate("info@corp.rs")
    assert v.risky is True


def test_score_risk_levels():
    EV = aa.EmailVerdict
    # turnstile fail → HIGH
    assert aa.score_risk(False, EV(True, False, False)) == aa.RISK_HIGH
    # disposable → HIGH
    assert aa.score_risk(True, EV(True, True, False)) == aa.RISK_HIGH
    # explicitly undeliverable → HIGH
    assert (
        aa.score_risk(True, EV(False, False, False, undeliverable=True)) == aa.RISK_HIGH
    )
    # risky (unknown/accept_all) → MEDIUM (not a block)
    assert aa.score_risk(True, EV(True, False, True)) == aa.RISK_MEDIUM
    # unknown: deliverable=False, undeliverable=False, risky=True → MEDIUM (Gmail case)
    assert aa.score_risk(True, EV(False, False, True)) == aa.RISK_MEDIUM
    # clean deliverable → LOW
    assert aa.score_risk(True, EV(True, False, False)) == aa.RISK_LOW


@pytest.mark.asyncio
async def test_kickbox_no_api_key_fail_open():
    """No key — fail-open: risky=True, undeliverable=False (does not block)."""
    v = await aa.KickboxValidator("").validate("user@gmail.com")
    assert v.risky is True
    assert v.undeliverable is False
    assert v.disposable is False
    assert v.reason == "no_api_key"


@pytest.mark.asyncio
async def test_kickbox_network_error_fail_open(monkeypatch):
    """On a network error — fail-open: risky=True, undeliverable=False."""
    import sys
    import types

    class _ErrorClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, *a, **k):
            raise RuntimeError("connection refused")

    monkeypatch.setitem(
        sys.modules, "httpx", types.SimpleNamespace(AsyncClient=_ErrorClient)
    )
    v = await aa.KickboxValidator("real-key").validate("user@gmail.com")
    assert v.risky is True
    assert v.undeliverable is False
    assert v.reason == "kickbox_error"


@pytest.mark.asyncio
async def test_kickbox_unknown_result_not_undeliverable(monkeypatch):
    """result=unknown (Gmail) → deliverable=False, undeliverable=False, risky=True."""
    _patch_httpx(monkeypatch, {"result": "unknown", "disposable": False})
    v = await aa.KickboxValidator("key").validate("user@gmail.com")
    assert v.deliverable is False
    assert v.undeliverable is False
    assert v.risky is True


def test_step_up_not_implemented():
    with pytest.raises(NotImplementedError, match="Sprint-21"):
        aa.perform_setup_intent_step_up()
