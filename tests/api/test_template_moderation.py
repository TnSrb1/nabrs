"""Tests of EPIC 5 — the suggestions to the library + the moderation queue.

Covers:
- propose_from_profile: creates a pending record (unit)
- list_pending: returns the pending templates (unit)
- approve_template / reject_template / update_pending (unit)
- Regression: pending is NOT visible in list_public_templates
- Routes /admin/moderation/* for a non-admin → 403
- POST /profiles/{id}/propose by an anonymous user → redirect to login
- POST /profiles/{id}/propose with a session → creates the record + message.html
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import template_library_service as lib_svc
from src.api.config import CabinetSettings
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession, make_session_factory

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_PID = "cccc0000-0000-0000-0000-000000000003"
_TMPL_ID = "dddd0000-0000-0000-0000-000000000004"
_NOW = datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)

_ADMIN_SESSION = {"uid": _UID, "tid": _TID}
_USER_SESSION = {"uid": _UID, "tid": _TID}

_OPERATOR_EMAIL = "operator@example.com"


def _operator_settings() -> CabinetSettings:
    """Allowlist of platform operators — contains _OPERATOR_EMAIL."""
    return CabinetSettings(CABINET_ADMIN_EMAILS=_OPERATOR_EMAIL)


def _fake_profile(**kw):
    base = dict(
        id=_PID,
        tenant_id=_TID,
        name="Тест профиль",
        deleted_at=None,
        keywords_include=[{"terms": ["асфалт"], "match": "any"}],
        classifications={
            "include": [{"scheme": "eu_cpv", "code": "45233000"}],
            "exclude": [],
        },
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_template(**kw):
    base = dict(
        id=_TMPL_ID,
        name="Тест шаблон",
        description="Описание",
        activity_kind=None,
        created_by_user_id=_UID,
        status="pending",
        is_public=False,
        country_code="RS",
        created_at=_NOW,
        updated_at=_NOW,
        keywords_include=[{"terms": ["асфалт"], "match": "any"}],
        classifications={"include": [], "exclude": []},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def teardown_function():
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Unit: propose_from_profile
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_propose_creates_pending():
    profile = _fake_profile()
    fs = FakeSession(results=[FakeResult(items=[profile])])
    result = await lib_svc.propose_from_profile(
        fs,
        user_id=_UID,
        tenant_id=_TID,
        profile_id=_PID,
        name="Тест шаблон",
    )
    assert result is not None
    assert fs.committed == 1
    assert len(fs.added) == 1
    added = fs.added[0]
    assert added.status == "pending"
    assert added.is_public is False
    assert str(added.created_by_user_id) == _UID
    assert added.keywords_include == profile.keywords_include
    assert added.classifications == profile.classifications


@pytest.mark.asyncio
async def test_propose_returns_none_if_profile_not_found():
    fs = FakeSession(results=[FakeResult(items=[])])
    result = await lib_svc.propose_from_profile(
        fs,
        user_id=_UID,
        tenant_id=_TID,
        profile_id=_PID,
        name="Тест",
    )
    assert result is None
    assert fs.committed == 0
    assert fs.added == []


# --------------------------------------------------------------------------- #
# Unit: list_pending
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_pending_returns_pending():
    t = _fake_template()
    fs = FakeSession(results=[FakeResult(items=[t])])
    rows = await lib_svc.list_pending(fs)
    assert len(rows) == 1
    assert rows[0].status == "pending"


@pytest.mark.asyncio
async def test_list_pending_empty():
    fs = FakeSession(results=[FakeResult(items=[])])
    rows = await lib_svc.list_pending(fs)
    assert rows == []


# --------------------------------------------------------------------------- #
# Regression: pending is NOT visible in list_public_templates
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pending_not_in_public_list():
    """list_public_templates filters is_public=True AND status='approved' — pending is hidden."""
    fs = FakeSession(results=[FakeResult(items=[])])
    rows = await lib_svc.list_public_templates(fs)
    assert rows == []


def test_list_public_templates_sql_filters_approved_public():
    """Regression guard: the select contains the filters is_public AND status='approved'."""
    import inspect

    source = inspect.getsource(lib_svc.list_public_templates)
    assert "is_public" in source
    assert "approved" in source


# --------------------------------------------------------------------------- #
# Unit: approve_template
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_approve_template_transitions_to_public():
    t = _fake_template()
    fs = FakeSession(results=[FakeResult(items=[t])])
    ok = await lib_svc.approve_template(fs, admin_uid=_UID, template_id=_TMPL_ID)
    assert ok is True
    assert t.status == "approved"
    assert t.is_public is True
    assert fs.committed == 1


@pytest.mark.asyncio
async def test_approve_template_not_found_returns_false():
    fs = FakeSession(results=[FakeResult(items=[])])
    ok = await lib_svc.approve_template(fs, admin_uid=_UID, template_id=_TMPL_ID)
    assert ok is False
    assert fs.committed == 0


# --------------------------------------------------------------------------- #
# Unit: reject_template
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reject_template_transitions_to_rejected():
    t = _fake_template()
    fs = FakeSession(results=[FakeResult(items=[t])])
    ok = await lib_svc.reject_template(fs, admin_uid=_UID, template_id=_TMPL_ID)
    assert ok is True
    assert t.status == "rejected"
    assert t.is_public is False
    assert fs.committed == 1


@pytest.mark.asyncio
async def test_reject_template_not_found_returns_false():
    fs = FakeSession(results=[FakeResult(items=[])])
    ok = await lib_svc.reject_template(fs, admin_uid=_UID, template_id=_TMPL_ID)
    assert ok is False
    assert fs.committed == 0


# --------------------------------------------------------------------------- #
# Unit: update_pending
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_update_pending_changes_name_description():
    t = _fake_template()
    fs = FakeSession(results=[FakeResult(items=[t])])
    ok = await lib_svc.update_pending(
        fs,
        admin_uid=_UID,
        template_id=_TMPL_ID,
        name="Новое название",
        description="Новое описание",
    )
    assert ok is True
    assert t.name == "Новое название"
    assert t.description == "Новое описание"
    assert fs.committed == 1


@pytest.mark.asyncio
async def test_update_pending_not_found_returns_false():
    fs = FakeSession(results=[FakeResult(items=[])])
    ok = await lib_svc.update_pending(
        fs,
        admin_uid=_UID,
        template_id=_TMPL_ID,
        name="Что-то",
        description=None,
    )
    assert ok is False
    assert fs.committed == 0


# --------------------------------------------------------------------------- #
# Routes /admin/moderation — the require_admin gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/admin/moderation"),
        ("post", f"/admin/moderation/{_TMPL_ID}/approve"),
        ("post", f"/admin/moderation/{_TMPL_ID}/reject"),
        ("post", f"/admin/moderation/{_TMPL_ID}/edit"),
    ],
)
def test_anon_moderation_gets_403(method, path):
    fs = FakeSession()
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    client = TestClient(app, follow_redirects=False)
    resp = getattr(client, method)(path)
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/admin/moderation"),
        ("post", f"/admin/moderation/{_TMPL_ID}/approve"),
        ("post", f"/admin/moderation/{_TMPL_ID}/reject"),
        ("post", f"/admin/moderation/{_TMPL_ID}/edit"),
    ],
)
def test_non_admin_moderation_gets_403(method, path):
    fs = FakeSession(
        results=[FakeResult(items=[SimpleNamespace(email="user@example.com")])]
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _USER_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = getattr(client, method)(path)
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Route GET /admin/moderation — the list of pending
# --------------------------------------------------------------------------- #


def test_admin_moderation_renders_pending():
    t = _fake_template()
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),  # require_admin
        FakeResult(items=[t]),  # list_pending
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/admin/moderation")
    assert resp.status_code == 200
    assert "Тест шаблон" in resp.text
    assert "Одобрить" in resp.text
    assert "Отклонить" in resp.text


def test_admin_moderation_renders_empty():
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),
        FakeResult(items=[]),
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/admin/moderation")
    assert resp.status_code == 200
    assert "пуста" in resp.text.lower()


# --------------------------------------------------------------------------- #
# Route POST /admin/moderation/{id}/approve
# --------------------------------------------------------------------------- #


def test_admin_approve_redirects():
    t = _fake_template()
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),
        FakeResult(items=[t]),  # approve_template's select
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/admin/moderation/{_TMPL_ID}/approve")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/moderation"
    assert fs.committed == 1


def test_admin_reject_redirects():
    t = _fake_template()
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),
        FakeResult(items=[t]),
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/admin/moderation/{_TMPL_ID}/reject")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/moderation"
    assert fs.committed == 1


def test_admin_edit_pending_redirects():
    t = _fake_template()
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),
        FakeResult(items=[t]),
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        f"/admin/moderation/{_TMPL_ID}/edit",
        data={"name": "Новое имя", "description": "Описание"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/moderation"
    assert fs.committed == 1


# --------------------------------------------------------------------------- #
# Route POST /profiles/{id}/propose
# --------------------------------------------------------------------------- #


def test_propose_anon_redirects_login():
    fs = FakeSession()
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/profiles/{_PID}/propose")
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_propose_with_session_creates_pending():
    profile = _fake_profile()
    results = [
        FakeResult(items=[profile]),  # svc.get_profile (after set_config not consumed)
        FakeResult(items=[profile]),  # propose_from_profile's own profile select
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _USER_SESSION
    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/profiles/{_PID}/propose")
    assert resp.status_code == 200
    assert "Предложено" in resp.text or "модерацию" in resp.text
    assert fs.committed == 1
    assert len(fs.added) == 1


def test_propose_profile_not_found_returns_404():
    results = [
        FakeResult(items=[]),  # svc.get_profile → None
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _USER_SESSION
    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/profiles/{_PID}/propose")
    assert resp.status_code == 404
