"""To the operator about an EXTENSION — its own heading, not “Trial access request” (10.10).

The banner button (``kind=extension``) creates an application with the extension marker; the e-mail to the operator (the service channel, ru) must name it an extension. An ordinary application for trial access — the former heading. The expectation — a literal.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import admin_notify, deps
from src.api.cabinet import trial_service
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


@pytest.fixture
def captured(monkeypatch):
    sent = {}

    async def _create(db, *, tenant_id, user_id, comment):
        sent["comment"] = comment
        return SimpleNamespace(ok=True, request_id="r-1", email="a@example.com")

    async def _email(**kwargs):
        sent["subject"] = kwargs["subject"]
        sent["text_body"] = kwargs["text_body"]

    monkeypatch.setattr(trial_service, "create_trial_request", _create)
    monkeypatch.setattr(admin_notify, "notify_admins_email", _email)
    return sent


@pytest.fixture
def client():
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: FakeSession())
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    yield TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_extension_notice_has_extension_title(client, captured):
    resp = client.post("/cabinet/trial/request", data={"kind": "extension"})
    assert resp.status_code == 303
    assert captured["subject"] == "Запрос продления"
    assert captured["text_body"].startswith("Запрос продления.")


def test_plain_trial_notice_keeps_trial_title(client, captured):
    resp = client.post("/cabinet/trial/request", data={"comment": "хочу"})
    assert resp.status_code == 303
    assert captured["subject"] == "Новая заявка на тестовый доступ"
    assert captured["text_body"].startswith("Новая заявка на тестовый доступ.")
