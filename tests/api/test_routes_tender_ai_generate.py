"""Tests of the POST generation and the HTMX polling of the status (web-ai-2).

DB/session/service are mocked (ADR-013); templates render for real. We check:
- the session gate (anonymous on HTMX → 401 + HX-Redirect);
- the validation of level/profile_id (404);
- the synchronous precheck branches → the corresponding partial (gate_pro/frozen/already);
- OK → 202 “being generated…” + the queuing of BackgroundTasks (generate_artifact called with its own session) + an hx-get poll every 3s;
- the status fragment: ready (the section render, the polling stops) / not yet (pending, polls+1) / timeout (polling stopped, retry button).
The service logic is not duplicated (see test_tender_ai_generate.py).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_tender_ai
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}
_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"


def _data(outputs):
    return TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title="Набавка рачунара",
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        source_url="https://portal.rs/t/1",
        outputs=outputs,
    )


def _client(*, sess=None):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


# --------------------------------------------------------------------------- #
# POST /generate — gates and validation
# --------------------------------------------------------------------------- #


def test_anonymous_post_returns_hx_redirect():
    resp = _client(sess=None).post(
        f"/app/tenders/{_TENDER}/generate/bid?profile_id={_PROFILE}"
    )
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/login"


def test_invalid_level_404():
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/generate/standard?profile_id={_PROFILE}"
    )
    assert resp.status_code == 404


def test_missing_profile_id_404():
    resp = _client(sess=SESS).post(f"/app/tenders/{_TENDER}/generate/bid")
    assert resp.status_code == 404


def test_gate_pro_partial(monkeypatch):
    monkeypatch.setattr(
        routes_tender_ai.gen,
        "precheck_generation",
        _async(routes_tender_ai.gen.STATUS_GATE_PRO),
    )
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/generate/bid?profile_id={_PROFILE}"
    )
    assert resp.status_code == 200
    assert "Pro" in resp.text
    assert 'id="ai-slot-bid"' in resp.text
    assert "every 3s" not in resp.text  # terminal — no polling
    # A clear CTA to the plans, not a bare text/403 (web-ai-5 UX polish).
    assert 'href="/plans"' in resp.text
    assert "Смотреть тарифы" in resp.text


def test_frozen_partial(monkeypatch):
    monkeypatch.setattr(
        routes_tender_ai.gen,
        "precheck_generation",
        _async(routes_tender_ai.gen.STATUS_FROZEN),
    )
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/generate/checklist?profile_id={_PROFILE}"
    )
    assert resp.status_code == 200
    assert "приостановлен" in resp.text


def test_already_renders_saved_artifact(monkeypatch):
    monkeypatch.setattr(
        routes_tender_ai.gen,
        "precheck_generation",
        _async(routes_tender_ai.gen.STATUS_ALREADY),
    )
    monkeypatch.setattr(
        routes_tender_ai.svc,
        "get_tender_analyses",
        _async(_data({"bid": {"verdict": "go", "score": 80, "pros": ["ок"]}})),
    )
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/generate/bid?profile_id={_PROFILE}"
    )
    assert resp.status_code == 200
    assert "Рекомендую" in resp.text
    assert "ок" in resp.text
    assert "every 3s" not in resp.text  # already ready — no polling needed


def test_in_progress_renders_pending_without_scheduling(monkeypatch):
    # the precheck returned in_progress (another request is already generating, the advisory lock is taken):
    # the same UX “being generated…” + polling, but we do NOT queue a NEW background task.
    calls: list = []

    async def _record(db, **kwargs):
        calls.append(kwargs)
        return routes_tender_ai.gen.STATUS_SUCCESS

    monkeypatch.setattr(
        routes_tender_ai.gen,
        "precheck_generation",
        _async(routes_tender_ai.gen.STATUS_IN_PROGRESS),
    )
    monkeypatch.setattr(routes_tender_ai.gen, "generate_artifact", _record)

    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/generate/bid?profile_id={_PROFILE}"
    )
    assert resp.status_code == 202
    assert "Генерируем" in resp.text
    assert f"/app/tenders/{_TENDER}/status/bid" in resp.text
    assert "every 3s" in resp.text
    assert calls == []  # a second background task was NOT queued


def test_ok_schedules_background_and_returns_pending(monkeypatch):
    calls: list[tuple] = []

    async def _record(db, **kwargs):
        calls.append((kwargs.get("level"), kwargs.get("tender_id"), kwargs.get("tid")))
        return routes_tender_ai.gen.STATUS_SUCCESS

    monkeypatch.setattr(
        routes_tender_ai.gen,
        "precheck_generation",
        _async(routes_tender_ai.gen.STATUS_OK),
    )
    monkeypatch.setattr(routes_tender_ai.gen, "generate_artifact", _record)

    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/generate/bid?profile_id={_PROFILE}"
    )
    assert resp.status_code == 202
    assert "Генерируем" in resp.text
    # A skeleton placeholder instead of emptiness (web-ai-5 UX polish).
    assert "skeleton" in resp.text
    # The polling is set to the status endpoint every 3s.
    assert f"/app/tenders/{_TENDER}/status/bid" in resp.text
    assert "every 3s" in resp.text
    # The background task is executed (TestClient runs the BackgroundTasks) with its own session.
    assert calls == [("bid", _TENDER, "t-1")]


# --------------------------------------------------------------------------- #
# GET /status — the polling render
# --------------------------------------------------------------------------- #


def test_status_ready_renders_section_and_stops_polling(monkeypatch):
    monkeypatch.setattr(
        routes_tender_ai.svc,
        "get_tender_analyses",
        _async(
            _data(
                {
                    "checklist": {
                        "documents": [{"title": "Извод из АПР", "mandatory": True}],
                        "summary": "Готово",
                    }
                }
            )
        ),
    )
    resp = _client(sess=SESS).get(
        f"/app/tenders/{_TENDER}/status/checklist?profile_id={_PROFILE}&polls=3"
    )
    assert resp.status_code == 200
    assert "Извод из АПР" in resp.text
    assert "every 3s" not in resp.text  # the artifact is ready — polling stopped


def test_status_not_ready_keeps_polling(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    resp = _client(sess=SESS).get(
        f"/app/tenders/{_TENDER}/status/bid?profile_id={_PROFILE}&polls=2"
    )
    assert resp.status_code == 200
    assert "Генерируем" in resp.text
    assert "every 3s" in resp.text
    assert "polls=3" in resp.text  # the next poll is incremented


def test_status_timeout_stops_polling(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    resp = _client(sess=SESS).get(
        f"/app/tenders/{_TENDER}/status/bid?profile_id={_PROFILE}&polls=99"
    )
    assert resp.status_code == 200
    assert "дольше обычного" in resp.text
    assert "every 3s" not in resp.text  # poll ceiling — polling stopped


def test_status_anonymous_hx_redirect():
    resp = _client(sess=None).get(
        f"/app/tenders/{_TENDER}/status/bid?profile_id={_PROFILE}"
    )
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/login"


def test_deep_limit_exhausted_shows_remaining_flash_and_plans_button(monkeypatch):
    # The refusal by the deep limit is VISIBLE: a flash at the moment of refusal + a button to /plans.
    async def _flash(*args, **kwargs):
        return {
            "level": "deep",
            "used": 10,
            "limit": 10,
            "upsell_name": "Business",
            "upsell_price": 79,
            "upsell_limit": 50,
        }

    monkeypatch.setattr(
        routes_tender_ai.gen,
        "precheck_generation",
        _async(routes_tender_ai.gen.STATUS_LIMIT_EXHAUSTED),
    )
    monkeypatch.setattr(routes_tender_ai, "_flash_for_level", _flash)
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/generate/deep?profile_id={_PROFILE}"
    )
    assert resp.status_code == 200
    assert 'id="ai-slot-deep"' in resp.text
    assert "limit-flash" in resp.text
    assert 'href="/plans"' in resp.text


def test_deep_ok_schedules_background_with_deep_level(monkeypatch):
    calls: list = []

    async def _record(db, **kwargs):
        calls.append(kwargs.get("level"))
        return routes_tender_ai.gen.STATUS_SUCCESS

    monkeypatch.setattr(
        routes_tender_ai.gen,
        "precheck_generation",
        _async(routes_tender_ai.gen.STATUS_OK),
    )
    monkeypatch.setattr(routes_tender_ai.gen, "generate_artifact", _record)
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/generate/deep?profile_id={_PROFILE}"
    )
    assert resp.status_code == 202
    assert f"/app/tenders/{_TENDER}/status/deep" in resp.text
    assert calls == ["deep"]


def test_deep_status_ready_renders_analysis(monkeypatch):
    monkeypatch.setattr(
        routes_tender_ai.svc,
        "get_tender_analyses",
        _async(
            _data({"deep": {"summary": "Глубокий итог", "work_breakdown": ["Шаг 1"]}})
        ),
    )
    resp = _client(sess=SESS).get(
        f"/app/tenders/{_TENDER}/status/deep?profile_id={_PROFILE}&polls=2"
    )
    assert resp.status_code == 200
    assert "Глубокий итог" in resp.text
    assert "Шаг 1" in resp.text
    assert "every 3s" not in resp.text
