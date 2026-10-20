"""A deleted user with an old cookie does not re-link a channel (10.10).

The gate ``deps.require_active_access`` closes the cabinet content on ``users.deleted_at``, but the channel-linking routes and “My account” held ``get_optional_session`` — a cookie issued on another device passed. Expectation as a literal: 303 to
``/plans`` (``deps.SUBSCRIPTION_PATH``) (as for the other cabinet paths), nothing is written to the DB.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}
SUBSCRIPTION = "/plans"
_FUTURE = datetime(2030, 1, 1, tzinfo=timezone.utc)
_PAST_DELETE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _client(**fake_kw):
    def factory():
        return FakeSession(**fake_kw)

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


def teardown_function():
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "method, path, data",
    [
        ("POST", "/cabinet/telegram/link", {}),
        ("POST", "/cabinet/email/link", {"email": "a@example.com"}),
        ("GET", "/cabinet/account", None),
    ],
)
def test_deleted_user_is_redirected_to_subscription(method, path, data):
    resp = _client(access_until=_FUTURE, user_deleted_at=_PAST_DELETE).request(
        method, path, data=data
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == SUBSCRIPTION


def test_active_user_telegram_link_is_not_redirected_by_gate():
    resp = _client(access_until=_FUTURE, user_deleted_at=None).post(
        "/cabinet/telegram/link"
    )
    assert resp.status_code != 303
