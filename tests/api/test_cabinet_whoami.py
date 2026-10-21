"""Identification of the session in the cabinet header (whoami, owner’s complaint 20.09.2026).

The client logs in under several accounts and cannot see which one exactly
they are in the cabinet right now (on 04.08.2026 this already cost an hour in vain — the diagnosis
was analyzed under the wrong account, see the project rules). The header (base.html, the common
ancestor, and admin_base.html) must show the email, and the tenant name too,
if there is one (``tenants.display_name``).

The expectations are literals from the fact (the real string from the `/app` response), not via the
catalog/template helper that the code itself uses (the project rules: a tautology
is always green).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import routes_profiles
from src.api.config import CabinetSettings, get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeSession, make_session_factory

_UID = "cccc0000-0000-0000-0000-000000000001"
_TID = "dddd0000-0000-0000-0000-000000000002"
_SESSION = {"uid": _UID, "tid": _TID}
_EMAIL = "victor.tester+2@example.rs"
_TENANT_NAME = "ООО «Тестовая Фирма»"


def teardown_function():
    app.dependency_overrides.clear()


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


def _client(
    *, session=None, whoami_email=None, whoami_tenant_name=None, admin_emails=""
):
    fs = FakeSession(whoami_email=whoami_email, whoami_tenant_name=whoami_tenant_name)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: session
    app.dependency_overrides[deps.get_settings] = lambda: CabinetSettings(
        CABINET_ADMIN_EMAILS=admin_emails
    )
    client = TestClient(app, follow_redirects=False)
    if session is not None:
        settings = get_cabinet_settings()
        token = security.sign_session(
            session["uid"], session["tid"], settings.cabinet_secret_key
        )
        client.cookies.set("tb_session", token)
    return client, fs


# --------------------------------------------------------------------------- #
# A logged-in user sees the email (and the tenant name, if there is one)
# --------------------------------------------------------------------------- #


def test_authed_user_sees_email_in_header():
    client, _ = _client(session=_SESSION, whoami_email=_EMAIL)
    resp = client.get("/app")
    assert resp.status_code == 200
    assert _EMAIL in resp.text


def test_authed_user_sees_tenant_name_and_email_when_both_present():
    client, _ = _client(
        session=_SESSION, whoami_email=_EMAIL, whoami_tenant_name=_TENANT_NAME
    )
    resp = client.get("/app")
    assert resp.status_code == 200
    assert _EMAIL in resp.text
    assert _TENANT_NAME in resp.text


def test_authed_user_without_tenant_name_sees_only_email_not_blank():
    """There is no tenant name — we show the email; there is never an empty space."""
    client, _ = _client(session=_SESSION, whoami_email=_EMAIL, whoami_tenant_name=None)
    resp = client.get("/app")
    assert resp.status_code == 200
    assert _EMAIL in resp.text
    assert 'class="whoami"' in resp.text


# --------------------------------------------------------------------------- #
# An anonymous user on a public screen of this block (base.html, without a session) does not see
# --------------------------------------------------------------------------- #


def test_anon_does_not_see_any_whoami_identity():
    client, _ = _client(session=None)
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'class="whoami"' not in resp.text
    assert _EMAIL not in resp.text


# --------------------------------------------------------------------------- #
# admin_base.html inherits base.html — the operator sees the identification too
# --------------------------------------------------------------------------- #


def test_admin_console_header_also_shows_identity(monkeypatch):
    from src.api import admin_access
    from src.api.admin import routes_admin

    monkeypatch.setattr(admin_access, "is_platform_admin", _async(True))
    monkeypatch.setattr(routes_admin.svc, "list_users", _async(([], 0)))
    monkeypatch.setattr(
        routes_admin.svc,
        "get_platform_stats",
        _async(
            SimpleNamespace(
                tenders_all_time=0, tenders_month=0, tenders_day=0, users_total=0
            )
        ),
    )
    client, _ = _client(session=_SESSION, whoami_email=_EMAIL, admin_emails=_EMAIL)
    resp = client.get("/admin/users")
    assert resp.status_code == 200
    assert _EMAIL in resp.text


# --------------------------------------------------------------------------- #
# A resolve error does not break the page — the identification is simply absent
# --------------------------------------------------------------------------- #


def test_db_error_on_whoami_resolve_survives_render():
    class _BoomSession(FakeSession):
        async def execute(self, statement, params=None):
            text = str(statement)
            if "whoami_email" in text:
                raise RuntimeError("db is down")
            return await super().execute(statement, params)

    fs = _BoomSession()
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _SESSION
    app.dependency_overrides[deps.get_settings] = lambda: CabinetSettings(
        CABINET_ADMIN_EMAILS=""
    )
    client = TestClient(app, follow_redirects=False)
    settings = get_cabinet_settings()
    client.cookies.set(
        "tb_session",
        security.sign_session(_UID, _TID, settings.cabinet_secret_key),
    )
    resp = client.get("/app")
    assert resp.status_code == 200
    assert 'class="whoami"' not in resp.text
