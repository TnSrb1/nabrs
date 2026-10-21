"""Tests of the separate operator login /admin/login and of the return parameter next.

The main invariant of the task: there is no second authentication contour. /admin/login is
the existing magic-link form of the cabinet with a different URL, heading and hidden next;
POST goes to the shared /login, and the link is redeemed by the shared /auth/magic.

We cover:
- an anonymous user on an admin path → 303 to login with a correct return (test_admin.py);
- an already logged-in operator on /admin/login → straight inside;
- open redirect: all forms of a malicious next → ignored, the default is used;
- the “Admin” link in the header is visible to the operator and not visible to a regular user.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import routes_auth
from src.api.cabinet import routes_profiles
from src.api.config import CabinetSettings, get_cabinet_settings
from src.api.emailer import MockEmailSender
from src.api.main import app
from tests.api._routes import iter_effective_routes
from tests.api.conftest import FakeResult, FakeSession, make_session_factory, make_user

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_SESSION = {"uid": _UID, "tid": _TID}
_OPERATOR_EMAIL = "op@example.com"
_USER_EMAIL = "user@company.rs"


def _operator_settings() -> CabinetSettings:
    return CabinetSettings(CABINET_ADMIN_EMAILS=_OPERATOR_EMAIL)


def _session_cookie() -> str:
    """Signed cookie: the base.html header is drawn by _auth_context, which reads the
    cookie directly, bypassing dependency_overrides."""
    settings = get_cabinet_settings()
    return security.sign_session(_UID, _TID, settings.cabinet_secret_key)


def _client(*, session=None, email=None, settings_provider=_operator_settings):
    """Client with the `session` session; `email` — what the DB will return for this uid."""
    results = [FakeResult(items=[SimpleNamespace(email=email)])] if email else []
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: session
    app.dependency_overrides[deps.get_settings] = settings_provider
    client = TestClient(app, follow_redirects=False)
    if session is not None:
        client.cookies.set("tb_session", _session_cookie())
    return client


def teardown_function():
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# GET /admin/login
# --------------------------------------------------------------------------- #


def test_admin_login_form_renders_for_anon():
    resp = _client().get("/admin/login")
    assert resp.status_code == 200
    assert "Вход для оператора платформы" in resp.text
    # The form posts to the SHARED login contour — there is no separate auth here.
    assert 'action="/login"' in resp.text
    assert '<input type="hidden" name="next" value="/admin">' in resp.text


def test_admin_login_form_keeps_next():
    resp = _client().get("/admin/login?next=/admin/audit")
    assert resp.status_code == 200
    assert '<input type="hidden" name="next" value="/admin/audit">' in resp.text


def test_admin_login_redirects_logged_in_operator():
    """An operator with a live session does not see the form again."""
    resp = _client(session=_SESSION, email=_OPERATOR_EMAIL).get(
        "/admin/login?next=/admin/audit"
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/audit"


def test_admin_login_operator_without_next_goes_to_admin_home():
    resp = _client(session=_SESSION, email=_OPERATOR_EMAIL).get("/admin/login")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"


def test_admin_login_shows_form_to_logged_in_non_operator():
    """A non-operator with a session is not an operator: we do not let them inside."""
    resp = _client(session=_SESSION, email=_USER_EMAIL).get("/admin/login")
    assert resp.status_code == 200
    assert "Вход для оператора платформы" in resp.text


def test_admin_login_operator_with_hostile_next_goes_to_admin_home():
    """Not even the operator is redirected to a foreign host via next."""
    resp = _client(session=_SESSION, email=_OPERATOR_EMAIL).get(
        "/admin/login?next=https://evil.com/x"
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"


def test_operator_email_resolved_once_per_request():
    """The gate and the flag for the header are one dependency: one SELECT of email per request,
    not two (FastAPI caches Depends within a request)."""
    fs = FakeSession(
        results=[FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)])]
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    resp = TestClient(app, follow_redirects=False).get("/admin")
    assert resp.status_code == 303
    selects = [sql for sql, _ in fs.executed if "SELECT email FROM users" in sql]
    assert len(selects) == 1


def test_admin_login_does_not_leak_operator_emails():
    """The allowlist is a configuration secret, it does not leak into the HTML."""
    resp = _client().get("/admin/login")
    assert _OPERATOR_EMAIL not in resp.text


# --------------------------------------------------------------------------- #
# Open redirect: security.safe_next_path
# --------------------------------------------------------------------------- #

_HOSTILE_NEXT = [
    "//evil.com",  # protocol-relative → foreign host
    "///evil.com",
    "https://evil.com/x",  # scheme
    "http:/evil.com",
    "javascript:alert(1)",  # script scheme
    "/\\evil.com",  # backslash → some browsers read it as //
    "\\\\evil.com",
    "/app\nLocation: https://evil.com",  # CRLF injection into the header
    "/app\r\nSet-Cookie: a=b",
    "/app\x00",  # control character
    "evil.com",  # does not start with a slash
    "app/dashboard",  # relative without a slash
    "",
]


@pytest.mark.parametrize("value", _HOSTILE_NEXT)
def test_safe_next_path_rejects_hostile_values(value):
    assert security.safe_next_path(value) is None


def test_safe_next_path_rejects_none_and_overlong():
    assert security.safe_next_path(None) is None
    assert security.safe_next_path("/" + "a" * 512) is None


@pytest.mark.parametrize(
    "value", ["/app", "/admin/users", "/admin/audit?page=3", "/app/my"]
)
def test_safe_next_path_accepts_internal_paths(value):
    assert security.safe_next_path(value) == value


@pytest.mark.parametrize("value", _HOSTILE_NEXT)
def test_magic_link_ignores_hostile_next_and_falls_back_to_cabinet(value, monkeypatch):
    """The default for an invalid return is the cabinet dashboard, NOT the admin panel."""

    async def _consume(*args, **kwargs):
        return {"uid": _UID, "tid": _TID}

    monkeypatch.setattr(routes_auth.svc, "consume_magic_link", _consume)
    resp = _client().get("/auth/magic", params={"token": "t", "next": value})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


def test_magic_link_honours_valid_next(monkeypatch):
    async def _consume(*args, **kwargs):
        return {"uid": _UID, "tid": _TID}

    monkeypatch.setattr(routes_auth.svc, "consume_magic_link", _consume)
    resp = _client().get("/auth/magic", params={"token": "t", "next": "/admin/users"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/users"


def test_login_form_ignores_hostile_next():
    """A malicious next does not even reach the hidden field of the form."""
    resp = _client().get("/login", params={"next": "//evil.com"})
    assert resp.status_code == 200
    assert "evil.com" not in resp.text
    assert 'name="next"' not in resp.text


def test_login_form_keeps_valid_next():
    resp = _client().get("/login", params={"next": "/admin/users"})
    assert '<input type="hidden" name="next" value="/admin/users">' in resp.text


# --------------------------------------------------------------------------- #
# POST /login → next reaches the link in the email
# --------------------------------------------------------------------------- #


def _login_client(sender: MockEmailSender):
    fs = FakeSession(results=[FakeResult(items=[make_user(email=_OPERATOR_EMAIL)])])
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    app.dependency_overrides[deps.get_settings] = _operator_settings
    app.dependency_overrides[deps.get_email_sender] = lambda: sender
    return TestClient(app, follow_redirects=False)


def test_login_post_puts_next_into_magic_link():
    sender = MockEmailSender()
    routes_auth._MAGIC_LINK_PER_EMAIL.reset()
    routes_auth._MAGIC_LINK_PER_IP.reset()
    resp = _login_client(sender).post(
        "/login", data={"email": _OPERATOR_EMAIL, "next": "/admin/audit"}
    )
    assert resp.status_code == 200
    assert len(sender.outbox) == 1
    assert "next=%2Fadmin%2Faudit" in sender.outbox[0].text_body


def test_login_post_drops_hostile_next_from_magic_link():
    sender = MockEmailSender()
    routes_auth._MAGIC_LINK_PER_EMAIL.reset()
    routes_auth._MAGIC_LINK_PER_IP.reset()
    resp = _login_client(sender).post(
        "/login", data={"email": _OPERATOR_EMAIL, "next": "https://evil.com/x"}
    )
    assert resp.status_code == 200
    assert len(sender.outbox) == 1
    assert "next=" not in sender.outbox[0].text_body
    assert "evil.com" not in sender.outbox[0].text_body


# --------------------------------------------------------------------------- #
# The “Admin” link in the cabinet header
# --------------------------------------------------------------------------- #


def _async(value):
    async def _stub(*args, **kwargs):
        return value

    return _stub


@pytest.fixture(autouse=True)
def _stub_dashboard(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )


def test_operator_sees_admin_link_in_cabinet_header():
    resp = _client(session=_SESSION, email=_OPERATOR_EMAIL).get("/app")
    assert resp.status_code == 200
    assert '<a href="/admin"' in resp.text
    assert "Админка" in resp.text


def test_regular_user_does_not_see_admin_link():
    resp = _client(session=_SESSION, email=_USER_EMAIL).get("/app")
    assert resp.status_code == 200
    assert "Админка" not in resp.text


def test_operator_link_hidden_when_allowlist_empty():
    """Fail-closed is visible in the header too: no operators — no link.

    The email in the DB is deliberately not provided: with an empty allowlist the resolve must
    answer False without asking the DB at all."""
    resp = _client(
        session=_SESSION,
        settings_provider=lambda: CabinetSettings(CABINET_ADMIN_EMAILS=""),
    ).get("/app")
    assert resp.status_code == 200
    assert "Админка" not in resp.text


def test_anon_does_not_see_admin_link():
    resp = _client().get("/login")
    assert resp.status_code == 200
    assert "Админка" not in resp.text


# --------------------------------------------------------------------------- #
# Where the flag resolve hangs: exactly on the routers with base.html
# --------------------------------------------------------------------------- #


def _dependant_tree_has_admin_flag(dependant) -> bool:
    """A recursive walk of `dependant.dependencies` without the private
    `fastapi.dependencies.utils.get_flat_dependant` (removed in recent fastapi,
    see pyproject.toml — the gap pulled an ImportError on a clean main in CI)."""
    for sub in dependant.dependencies:
        if sub.call is deps.resolve_platform_admin_flag:
            return True
        if _dependant_tree_has_admin_flag(sub):
            return True
    return False


def _flag_dependency_paths() -> set[str]:
    """Paths whose dependency tree contains resolve_platform_admin_flag."""
    return {
        route.path
        for route in iter_effective_routes(app)
        if _dependant_tree_has_admin_flag(route.dependant)
    }


@pytest.mark.parametrize(
    "path",
    ["/app", "/app/my", "/library", "/cabinet/subscription", "/login", "/admin/login"],
)
def test_cabinet_routers_resolve_admin_flag(path):
    """The templates of these routes inherit base.html → the header needs the operator flag.
    If the dependency is forgotten on a new router, the “Admin” link will silently disappear there.
    """
    assert path in _flag_dependency_paths()


@pytest.mark.parametrize("path", ["/", "/help", "/plans", "/healthz"])
def test_public_routers_do_not_resolve_admin_flag(path):
    """The showcase does NOT have the cabinet bundle `_CABINET_DEPS` (the locale marker
    would switch `t` to the cabinet catalog). The showcase gets the operator flag and whoami
    from `deps.resolve_public_authed_header` — only with a session (#580,
    tests/api/test_public_authed_header.py)."""
    assert path not in _flag_dependency_paths()


# --------------------------------------------------------------------------- #
# The flag resolve must not break other pages
# --------------------------------------------------------------------------- #


def _broken_db_client():
    class _BoomSession(FakeSession):
        async def execute(self, statement, params=None):
            raise RuntimeError("db is down")

    fs = _BoomSession()
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("tb_session", _session_cookie())
    return client


def test_public_page_survives_failed_admin_resolve():
    """A failed operator resolve does not turn a public page into a 500."""
    resp = _broken_db_client().get("/login")
    assert resp.status_code == 200
    assert "Админка" not in resp.text


def test_admin_gate_fails_closed_on_failed_resolve():
    """If we cannot confirm the operator — we do not let them in (403), rather than letting them in “just in case”."""
    assert _broken_db_client().get("/admin/users").status_code == 403
