"""Logged in, on a service 404 page of a tender: “To the home page” leads to the cabinet.

card-truth, 07.10: ``routes_tender_ai._not_found`` rendered ``message.html`` without
``home_href`` → the link fell to ``/login``. Here without ``profile_id`` the route goes
straight to ``_not_found`` (the DB is not needed). The expectation — a literal from the template.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


def _client():
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: FakeSession())
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    client = TestClient(app, follow_redirects=False)
    settings = get_cabinet_settings()
    client.cookies.set(
        "tb_session",
        security.sign_session(SESS["uid"], SESS["tid"], settings.cabinet_secret_key),
    )
    return client


def teardown_function():
    app.dependency_overrides.clear()


def test_logged_in_tender_not_found_home_link_goes_to_cabinet():
    client = _client()
    resp = client.get("/app/tenders/11111111-1111-4111-8111-111111111111")
    assert resp.status_code == 404
    assert '<a href="/app" class="text-slate-600 underline">' in resp.text
    assert 'href="/login" class="text-slate-600 underline"' not in resp.text
