"""The “limit exhausted” flash and the button state in the tender card.

Owner requirement of 15.08.2026: when an attempt to use AI is refused for the limit, the refusal must FLASH large and bright, offer a plan with a bigger limit, and go away by itself after a few seconds. Hence three promises, pinned here:

1. the flash appears on the ACTION (pressed the button → refused for the limit) and carries an upsell — the specific plan, its limit and price;
2. on other refusals (model failure, freeze) there is no flash: it means “hit the money”, not “something went wrong”;
3. the state outlives the flash — an exhausted level changes the button itself, and a level close to exhaustion shows the remainder BEFORE the refusal.

Expectations are taken from the RENDER of the routes (call the route, read the HTML), not from the model of the code: a test that pinned a wrong contract already let a 404 through to production (CLAUDE.md).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.ai.usage import _TIER_LIMITS
from src.api import deps
from src.api.cabinet import routes_tender_ai
from src.api.cabinet import tender_ai_generate as gen
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession

SESS = {"uid": "u-1", "tid": "11111111-1111-1111-1111-111111111111"}
_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"
_CARD = f"/app/tenders/{_TENDER}?profile_id={_PROFILE}"
_GENERATE = f"/app/tenders/{_TENDER}/generate/bid?profile_id={_PROFILE}"

_PRO_BID = _TIER_LIMITS["pro"]["bid"]


class _LimitsSession(FakeSession):
    """A double session: the tenant’s plan/status and the usage per level."""

    def __init__(self, *, tier="pro", status="active", used=0, profile_level="none"):
        super().__init__()
        self.tier = tier
        self.status = status
        self.used = used
        self.profile_level = profile_level

    async def execute(self, statement, params=None):
        sql = str(statement)
        if not any(k in sql for k in ("tenants", "ai_usage", "ai_analysis_level")):
            return await super().execute(statement, params)
        self.executed.append((sql, params))
        if "ai_analysis_level" in sql:
            return FakeResult(items=[SimpleNamespace(level=self.profile_level)])
        if "FROM tenants" in sql:
            return FakeResult(
                items=[SimpleNamespace(tier=self.tier, status=self.status)]
            )
        return FakeResult(items=[SimpleNamespace(count=self.used)])


def _session_factory(session):
    class _CM:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    return lambda: _CM()


def _client(session):
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    app.dependency_overrides[deps.get_session_factory] = lambda: _session_factory(
        session
    )
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _card_client(monkeypatch, session):
    async def _analyses(*a, **kw):
        return TenderAnalyses(
            tender_id=_TENDER,
            profile_id=_PROFILE,
            profile_name="Профиль",
            title="Набавка рачунара",
            customer_name="Град",
            deadline_at=None,
            source_url=None,
            outputs={},
            description="Опис набавке",
        )

    async def _none(*a, **kw):
        return None

    async def _empty(*a, **kw):
        return []

    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _analyses)
    monkeypatch.setattr(routes_tender_ai.cust, "get_narucilac_analytics", _none)
    monkeypatch.setattr(routes_tender_ai.workflow_service, "get_tender_state", _none)
    monkeypatch.setattr(routes_tender_ai.workflow_service, "list_folders", _empty)
    return _client(session)


def _generate_client(monkeypatch, session, status):
    async def _precheck(*a, **kw):
        return status

    monkeypatch.setattr(gen, "precheck_generation", _precheck)
    return _client(session)


# --------------------------------------------------------------------------- #
#  The flash on the client’s action
# --------------------------------------------------------------------------- #


def test_limit_refusal_flashes_with_upsell(monkeypatch):
    """Pressed the button with an exhausted limit → the flash + a specific upsell."""
    session = _LimitsSession(tier="pro", status="active", used=_PRO_BID)
    resp = _generate_client(monkeypatch, session, gen.STATUS_LIMIT_EXHAUSTED).post(
        _GENERATE
    )

    assert resp.status_code == 200
    assert 'class="limit-flash"' in resp.text  # the banner itself
    assert "Лимит исчерпан" in resp.text
    assert f"{_PRO_BID} из {_PRO_BID}" in resp.text  # the usage figures
    # upsell: the plan, its limit and price — from the catalog, not invented
    assert "Business" in resp.text
    assert str(_TIER_LIMITS["business"]["bid"]) in resp.text
    assert "/plans" in resp.text


def test_trial_sees_monthly_limit_not_demo(monkeypatch):
    """On the trial the button level is capped by the MONTHLY limit, not by the demo.

    The expectation is taken from a fact (the render of the route), not from intuition: the demo quota (10) exists only for the auto-analysis of the stream, which the client does not press. The trial gives Pro rights, so the verdict/checklist/draft are counted per month — and the flash must say exactly this, otherwise it lies about the cause of the refusal.
    """
    session = _LimitsSession(tier="pro", status="trial", used=99)
    resp = _generate_client(monkeypatch, session, gen.STATUS_LIMIT_EXHAUSTED).post(
        _GENERATE
    )

    assert resp.status_code == 200
    assert 'class="limit-flash"' in resp.text
    assert "Лимит исчерпан" in resp.text
    assert "Демо" not in resp.text and "демо" not in resp.text


def test_model_error_does_not_flash(monkeypatch):
    """A model failure is not money: there must be no flash and no upsell."""
    session = _LimitsSession(tier="pro", status="active")
    resp = _generate_client(monkeypatch, session, gen.STATUS_MODEL_ERROR).post(
        _GENERATE
    )

    assert resp.status_code == 200
    assert "limit-flash" not in resp.text


# --------------------------------------------------------------------------- #
#  The state outlives the flash
# --------------------------------------------------------------------------- #


def test_exhausted_level_turns_button_into_upsell(monkeypatch):
    """The level is exhausted → instead of “Generate” a link to the plan.

    The flash lives ~6 seconds; a client who comes back later must see the cause without a single click — otherwise the button leads to the refusal again.
    """
    session = _LimitsSession(tier="pro", status="active", used=_PRO_BID)
    resp = _card_client(monkeypatch, session).get(_CARD)

    assert resp.status_code == 200
    assert "Перейти на тариф выше" in resp.text
    assert f"Использовано {_PRO_BID} из {_PRO_BID}" in resp.text
    # there must be no generation launch button for an exhausted level
    assert f'hx-post="/app/tenders/{_TENDER}/generate/bid' not in resp.text


def test_near_limit_warns_before_refusal(monkeypatch):
    """80% of the limit → the remainder is shown BEFORE the refusal, the button stays working."""
    used = int(_PRO_BID * 0.8)
    session = _LimitsSession(tier="pro", status="active", used=used)
    resp = _card_client(monkeypatch, session).get(_CARD)

    assert resp.status_code == 200
    assert f"Осталось {_PRO_BID - used} из {_PRO_BID}" in resp.text
    assert f'hx-post="/app/tenders/{_TENDER}/generate/bid' in resp.text


def test_plenty_left_stays_quiet(monkeypatch):
    """Usage far from the limit → no warnings (noise is not needed)."""
    session = _LimitsSession(tier="pro", status="active", used=1)
    resp = _card_client(monkeypatch, session).get(_CARD)

    assert resp.status_code == 200
    assert "Осталось" not in resp.text
    assert "limit-flash" not in resp.text
