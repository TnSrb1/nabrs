"""#580: the showcase for a logged-in user — whoami and “Admin” in the cabinet header.

The session is a REAL cookie ``tb_session`` (``security.sign_session``), not an override of ``get_optional_session``: the override gives the dependencies a session, but ``_auth_context`` reads the cookie itself and draws “Log in” (a false result).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import CabinetSettings, get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_OPERATOR = "op@example.com"
_USER = "user@company.rs"
_PUBLIC_PAGES = ["/help", "/plans", "/izvori"]


def teardown_function():
    app.dependency_overrides.clear()


def _client(
    *, email: str | None, authed: bool = True
) -> tuple[TestClient, FakeSession]:
    results = [FakeResult(items=[SimpleNamespace(email=email)])] if email else []
    fs = FakeSession(
        results=results, whoami_email=email, whoami_tenant_name="Acme d.o.o."
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_settings] = lambda: CabinetSettings(
        CABINET_ADMIN_EMAILS=_OPERATOR
    )
    client = TestClient(app, follow_redirects=False)
    if authed:
        secret = get_cabinet_settings().cabinet_secret_key
        client.cookies.set("tb_session", security.sign_session(_UID, _TID, secret))
    return client, fs


@pytest.mark.parametrize("path", _PUBLIC_PAGES)
def test_operator_sees_whoami_and_admin_link_on_public_page(path):
    client, _ = _client(email=_OPERATOR)
    resp = client.get(path)
    assert resp.status_code == 200
    assert 'class="whoami"' in resp.text
    assert _OPERATOR in resp.text
    assert '<a href="/admin"' in resp.text


@pytest.mark.parametrize("path", _PUBLIC_PAGES)
def test_regular_user_sees_whoami_but_no_admin_link(path):
    client, _ = _client(email=_USER)
    resp = client.get(path)
    assert resp.status_code == 200
    assert _USER in resp.text
    assert '<a href="/admin"' not in resp.text


@pytest.mark.parametrize("path", _PUBLIC_PAGES)
def test_anonymous_gets_no_identity_and_no_db_lookup(path):
    client, fs = _client(email=None, authed=False)
    resp = client.get(path)
    assert resp.status_code == 200
    assert 'class="whoami"' not in resp.text
    assert '<a href="/admin"' not in resp.text
    assert not [sql for sql, _ in fs.executed if "FROM users" in str(sql)]
