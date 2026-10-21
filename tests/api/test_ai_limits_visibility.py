"""Visibility of AI limits in the cabinet: the remaining-quota counter and the explanation of a refusal.

Owner’s requirement of 15.08.2026: "make a counter for the trial and for other plans;
nothing should be switched off silently — the subscriber must see that they have exhausted
the limit". Here TWO promises are checked:

1. **before exhaustion** — the “AI limits” block on the subscription page and the remaining-quota line in the
   profile form show ``N of M`` based on the same usage that the gate reads;
2. **after** — the tender card that arrived WITHOUT auto-analysis names the reason
   (demo ended / plan limit / not on the plan), instead of leaving an empty
   “will appear automatically” — a promise that will not come true in this state.

The expectations are taken from the RENDER of the routes (not from the code model): a test that pinned
the wrong contract already let a 404 through to prod (the project rules).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.ai.usage import TRIAL_AUTO_DEMO_LIMIT, _TIER_LIMITS
from src.api import deps
from src.api.cabinet import ai_limits_service, routes_tender_ai
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession

SESS = {"uid": "u-1", "tid": "11111111-1111-1111-1111-111111111111"}
_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"
_CARD = f"/app/tenders/{_TENDER}?profile_id={_PROFILE}"

_PRO_AUTO_LIMIT = _TIER_LIMITS["pro"]["standard"]


class _LimitsSession(FakeSession):
    """Double session: the tenant’s plan/status, the profile level and the usage by levels.

    A dispatcher by SQL text (the order of queries is an implementation detail).
    """

    def __init__(
        self, *, tier="pro", status="active", profile_level="standard", used=0
    ):
        super().__init__()
        self.tier = tier
        self.status = status
        self.profile_level = profile_level
        self.used = used

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


def _analyses(**over):
    base = dict(
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
    base.update(over)
    return TenderAnalyses(**base)


def _session_factory(session):
    """Factory-double ``session_factory``: the same DB double per request."""

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
    async def _get(*a, **kw):
        return _analyses()

    async def _none(*a, **kw):
        return None

    async def _empty(*a, **kw):
        return []

    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _get)
    monkeypatch.setattr(routes_tender_ai.cust, "get_narucilac_analytics", _none)
    monkeypatch.setattr(routes_tender_ai.workflow_service, "get_tender_state", _none)
    monkeypatch.setattr(routes_tender_ai.workflow_service, "list_folders", _empty)
    return _client(session)


# --------------------------------------------------------------------------- #
#  Kartočka tendera: počemu razbora net
# --------------------------------------------------------------------------- #


def test_card_explains_demo_exhausted(monkeypatch):
    """The trial has used up the demo → the card names the reason AND THE NUMBERS."""
    session = _LimitsSession(status="trial", tier="pro", used=TRIAL_AUTO_DEMO_LIMIT)
    resp = _card_client(monkeypatch, session).get(_CARD)
    assert resp.status_code == 200
    assert "Демо-разборы закончились" in resp.text
    assert f"{TRIAL_AUTO_DEMO_LIMIT} из {TRIAL_AUTO_DEMO_LIMIT}" in resp.text
    # the promise “will appear automatically” in this state is a lie, it must not be there
    assert "Появится автоматически" not in resp.text


def test_card_explains_monthly_limit(monkeypatch):
    session = _LimitsSession(status="active", tier="pro", used=_PRO_AUTO_LIMIT)
    resp = _card_client(monkeypatch, session).get(_CARD)
    assert resp.status_code == 200
    assert "Лимит тарифа исчерпан" in resp.text
    assert f"{_PRO_AUTO_LIMIT} из {_PRO_AUTO_LIMIT}" in resp.text


def test_card_explains_plan_without_auto_analysis(monkeypatch):
    session = _LimitsSession(status="active", tier="lite", used=0)
    resp = _card_client(monkeypatch, session).get(_CARD)
    assert resp.status_code == 200
    assert "недоступен на вашем тарифе" in resp.text


def test_card_stays_quiet_when_quota_is_fine(monkeypatch):
    """The limit is not used up → the usual “will appear automatically”, without a refusal badge."""
    session = _LimitsSession(status="active", tier="pro", used=1)
    resp = _card_client(monkeypatch, session).get(_CARD)
    assert resp.status_code == 200
    assert "Автоматический разбор этого тендера не выполнен" not in resp.text


def test_card_stays_quiet_when_profile_did_not_ask_for_auto(monkeypatch):
    """The profile did not enable auto-analysis → there is nothing to explain (no badge)."""
    session = _LimitsSession(
        status="active", tier="pro", profile_level="none", used=_PRO_AUTO_LIMIT
    )
    resp = _card_client(monkeypatch, session).get(_CARD)
    assert resp.status_code == 200
    assert "Автоматический разбор этого тендера не выполнен" not in resp.text


# --------------------------------------------------------------------------- #
#  Remaining-quota service: one number with the gate
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_auto_state_is_none_when_profile_has_no_auto_analysis():
    session = _LimitsSession(profile_level="none")
    state = await ai_limits_service.get_auto_analysis_state(
        session, tid=SESS["tid"], profile_id=_PROFILE
    )
    assert state is None


@pytest.mark.asyncio
async def test_auto_state_reports_trial_demo_scope():
    session = _LimitsSession(status="trial", tier="lite", used=4)
    state = await ai_limits_service.get_auto_analysis_state(
        session, tid=SESS["tid"], profile_id=_PROFILE
    )
    assert (state.scope, state.limit, state.used) == ("trial", TRIAL_AUTO_DEMO_LIMIT, 4)
    assert state.left == TRIAL_AUTO_DEMO_LIMIT - 4


@pytest.mark.asyncio
async def test_limits_view_lists_every_paid_level_and_marks_unavailable():
    session = _LimitsSession(status="active", tier="lite", used=0)
    view = await ai_limits_service.get_ai_limits(session, tid=SESS["tid"])
    by_level = {row.level: row for row in view.rows}
    assert (
        by_level["standard"].state == "unavailable"
    )  # there is no auto-analysis on lite
    assert by_level["translate"].limit == _TIER_LIMITS["lite"]["translate"]
    assert "quick" not in by_level  # we do not show an unreachable level to the client
