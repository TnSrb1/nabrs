"""HEAD does not execute side effects of the cabinet GET routes (TASK public-nav-small).

Previously ``_HeadAsGet`` rewrote HEAD→GET for ALL routes. ``/auth/magic``
burns the one-time login token, ``/logout`` clears the session — a mail scanner checking links by HEAD would burn the link before the client clicked. Now HEAD→GET
only for the white list of the public showcase; the rest — 405 (as before the branch).

Expectations — literals from the route’s response (405 / 303 / Set-Cookie), not from the model of the code.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession, make_token, make_user

_UID = "00000000-0000-0000-0000-0000000000aa"
_TID = "00000000-0000-0000-0000-0000000000bb"


def teardown_function():
    app.dependency_overrides.clear()


def _client(session: FakeSession, *, authed: bool = False) -> TestClient:
    def factory():
        return session

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    client = TestClient(app, follow_redirects=False)
    if authed:
        settings = get_cabinet_settings()
        client.cookies.set(
            "tb_session",
            security.sign_session(_UID, _TID, settings.cabinet_secret_key),
        )
    return client


def test_head_root_is_200_without_body():
    resp = _client(FakeSession(results=[])).head("/")
    assert resp.status_code == 200
    assert resp.content == b""


def test_head_magic_link_is_405_and_token_still_works_for_get():
    user = make_user()
    token = make_token(purpose="magic_link", user_id=user.id)
    session = FakeSession(
        results=[FakeResult(items=[token]), FakeResult(items=[user.tenant_id])],
        get_map={"User": user},
    )
    client = _client(session)

    head = client.head("/auth/magic", params={"token": "raw-token"})
    assert head.status_code == 405
    # The route was not called: not a single SELECT, the token is not burned.
    assert session.executed == []
    assert token.consumed_at is None

    get = client.get("/auth/magic", params={"token": "raw-token"})
    assert get.status_code == 303
    assert get.headers["location"] == "/app"
    assert token.consumed_at is not None


def test_head_logout_is_405_and_session_cookie_is_not_cleared():
    client = _client(FakeSession(results=[]), authed=True)

    head = client.head("/logout")
    assert head.status_code == 405
    assert "set-cookie" not in head.headers
    assert client.cookies.get("tb_session") is not None
