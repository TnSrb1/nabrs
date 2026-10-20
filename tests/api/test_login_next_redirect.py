"""Logged out on a cabinet page → ``/login?next=<this page>``, not just ``/login``.

Onboarding sends to ``/profiles/new``, ``/cabinet/email``, ``/cabinet/account``;
without ``next`` after login the client landed on ``/app`` and the goal was lost. The model is
``/cabinet/telegram`` (``/login?next=%2Fcabinet%2Ftelegram``).

Open redirect: in ``next`` — only a relative path (``security.safe_next_path``),
both on issue (``deps.cabinet_login_url``) and on receipt (``/login``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from tests.api.conftest import FakeSession


def _client():
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "path,location",
    [
        ("/app", "/login?next=%2Fapp"),
        ("/profiles/new", "/login?next=%2Fprofiles%2Fnew"),
        (
            "/profiles/new?from_template=gradnja",
            "/login?next=%2Fprofiles%2Fnew%3Ffrom_template%3Dgradnja",
        ),
        ("/cabinet/email", "/login?next=%2Fcabinet%2Femail"),
        ("/cabinet/account", "/login?next=%2Fcabinet%2Faccount"),
        ("/cabinet/telegram", "/login?next=%2Fcabinet%2Ftelegram"),
    ],
)
def test_anonymous_page_redirect_keeps_target(path, location):
    resp = _client().get(path)
    assert resp.status_code == 303
    assert resp.headers["location"] == location


def test_login_page_drops_absolute_next():
    """``/login?next=https://evil`` — the value does NOT get into the login form."""
    resp = _client().get("/login?next=https://evil.example/x")
    assert resp.status_code == 200
    assert "evil.example" not in resp.text
    assert 'name="next"' not in resp.text


def test_login_page_keeps_relative_next():
    resp = _client().get("/login?next=%2Fcabinet%2Femail")
    assert resp.status_code == 200
    assert '<input type="hidden" name="next" value="/cabinet/email">' in resp.text


def _req(method, path, query=""):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path, query=query))


@pytest.mark.parametrize(
    "request_,expected",
    [
        (_req("GET", "//evil.example/x"), "/login"),
        (_req("GET", "/app", "a=https://evil.example"), "/login"),
        (_req("POST", "/profiles"), "/login"),
        (
            _req("GET", "/app", "teaser=deadline"),
            "/login?next=%2Fapp%3Fteaser%3Ddeadline",
        ),
    ],
)
def test_cabinet_login_url_only_relative_get_targets(request_, expected):
    assert deps.cabinet_login_url(request_) == expected
