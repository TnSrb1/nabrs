"""POST /signup/reregister: the operator’s notification goes to the background (#658, review 10.10).

Previously ``await _notify_operator_reregister`` ran BEFORE the response and only when a request was created: by the response delay one could find out by enumeration of addresses that an address was deleted. Now — ``BackgroundTasks``. Without DB and network: the request and the notifier are substituted.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.background import BackgroundTasks

from src.api import anti_abuse as aa
from src.api import deps
from src.api.cabinet import auth_service as svc
from src.api.cabinet import routes_auth
from src.api.emailer import MockEmailSender
from src.api.main import app


@asynccontextmanager
async def _no_db():
    yield object()


@pytest.fixture
def client():
    app.dependency_overrides[deps.get_session_factory] = lambda: _no_db
    app.dependency_overrides[deps.get_turnstile] = lambda: aa.NoopTurnstileVerifier(
        True
    )
    app.dependency_overrides[deps.get_email_sender] = lambda: MockEmailSender()
    try:
        c = TestClient(app)
        c.cookies.set("lang", "sr")
        yield c
    finally:
        app.dependency_overrides.clear()


def _post(client: TestClient, email: str):
    return client.post(
        "/signup/reregister", data={"email": email, "cf-turnstile-response": "x"}
    )


def test_created_request_schedules_notice_in_background(client, monkeypatch):
    """The response is given; the notifier is QUEUED to the background, not called in the request."""
    notify = AsyncMock()
    monkeypatch.setattr(routes_auth, "_notify_operator_reregister", notify)
    monkeypatch.setattr(svc, "request_reregistration", AsyncMock(return_value="rid-1"))
    scheduled: list[tuple] = []

    def spy_add_task(self, func, *args, **kwargs):
        # We do not execute it: we check the queuing, not the sending (without network).
        scheduled.append((func, kwargs))

    monkeypatch.setattr(BackgroundTasks, "add_task", spy_add_task)

    r = _post(client, "gone@firma.rs")

    assert r.status_code == 200
    assert "Zahtev je primljen" in r.text
    notify.assert_not_awaited()
    assert len(scheduled) == 1
    func, kwargs = scheduled[0]
    assert func is notify
    assert kwargs["email"] == "gone@firma.rs"
    assert kwargs["lang"] == "sr"


def test_background_notice_is_delivered_after_response(client, monkeypatch):
    """The background task really executes (Starlette) — the notifier is called once."""
    notify = AsyncMock()
    monkeypatch.setattr(routes_auth, "_notify_operator_reregister", notify)
    monkeypatch.setattr(svc, "request_reregistration", AsyncMock(return_value="rid-2"))

    r = _post(client, "gone2@firma.rs")

    assert r.status_code == 200
    notify.assert_awaited_once()
    assert notify.await_args.kwargs["email"] == "gone2@firma.rs"


def test_no_request_created_schedules_nothing(client, monkeypatch):
    """A live/unknown address: there is no request — and no notification (the response is the same)."""
    notify = AsyncMock()
    monkeypatch.setattr(routes_auth, "_notify_operator_reregister", notify)
    monkeypatch.setattr(svc, "request_reregistration", AsyncMock(return_value=None))
    scheduled: list = []
    monkeypatch.setattr(
        BackgroundTasks,
        "add_task",
        lambda self, func, *a, **kw: scheduled.append(func),
    )

    r = _post(client, "live@firma.rs")

    assert r.status_code == 200
    assert "Zahtev je primljen" in r.text
    assert scheduled == []
    notify.assert_not_awaited()
