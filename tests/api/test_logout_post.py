"""Logout: GET /logout does NOT log out, POST /logout requires the session CSRF token (owner 10.10.2026).

Expectations — literals (codes, cookie names), not via ``t()``/code helpers.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_IAT = 1_900_000_000


def _secret() -> str:
    return get_cabinet_settings().cabinet_secret_key


def _client(*, authed: bool = True) -> TestClient:
    """A client with a real cookie; the session dependency is NOT substituted."""
    fs = FakeSession(results=[])
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    client = TestClient(app, follow_redirects=False)
    if authed:
        import time

        client.cookies.set(
            "tb_session",
            security.sign_session(
                _UID, _TID, _secret(), locale="ru", now=int(time.time())
            ),
        )
    return client


def _token(client: TestClient) -> str:
    sess = security.verify_session(client.cookies.get("tb_session"), _secret())
    return security.csrf_token(sess, _secret())


def teardown_function():
    app.dependency_overrides.clear()


def test_get_logout_shows_form_and_does_not_clear_cookie():
    client = _client()
    resp = client.get("/logout")
    assert resp.status_code == 200
    assert 'method="post" action="/logout"' in resp.text
    assert 'name="csrf" value="' + _token(client) + '"' in resp.text
    assert "set-cookie" not in resp.headers


def test_get_logout_anonymous_goes_to_login():
    resp = _client(authed=False).get("/logout")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_post_logout_with_valid_csrf_clears_cookie():
    client = _client()
    resp = client.post("/logout", data={"csrf": _token(client)})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert (
        'tb_session=""' in resp.headers["set-cookie"]
        or "tb_session=;" in resp.headers["set-cookie"]
    )


def test_post_logout_without_or_with_wrong_csrf_is_403_and_keeps_session():
    client = _client()
    for data in ({}, {"csrf": "forged"}, {"csrf": ""}):
        resp = client.post("/logout", data=data)
        assert resp.status_code == 403
        assert "set-cookie" not in resp.headers


def test_csrf_token_of_another_session_is_rejected():
    client = _client()
    other = security.csrf_token(
        {"uid": "cccc0000-0000-0000-0000-000000000003", "iat": _IAT}, _secret()
    )
    assert client.post("/logout", data={"csrf": other}).status_code == 403


def test_post_logout_anonymous_is_idempotent_redirect():
    resp = _client(authed=False).post("/logout", data={})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_csrf_token_contract():
    sess = {"uid": _UID, "iat": _IAT}
    tok = security.csrf_token(sess, "k1")
    assert tok and security.verify_csrf(tok, sess, "k1")
    assert not security.verify_csrf(tok, sess, "k2")  # a different secret
    assert not security.verify_csrf(tok, {"uid": _UID, "iat": _IAT + 1}, "k1")
    assert security.csrf_token(None, "k1") == ""
    assert not security.verify_csrf("", None, "k1")
