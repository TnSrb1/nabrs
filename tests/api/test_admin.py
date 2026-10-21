"""Tests of the administrative cabinet (EPIC 6).

Two levels:
- Service (admin_service): unit tests with a mock session — list_users, grant, revoke, set_tier.
- Routes (routes_admin): TestClient + dependency_overrides — route guard (admin/non-admin/anon),
  user list, grant/revoke/set-tier actions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.admin import admin_service as svc
from src.api.admin import routes_admin
from src.api.cabinet import template_library_service as lib_svc
from src.api.config import CabinetSettings
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession, make_session_factory

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
_FUTURE = _NOW + timedelta(days=30)
_PAST = _NOW - timedelta(days=1)

_ADMIN_SESSION = {"uid": _UID, "tid": _TID}
_USER_SESSION = {"uid": _UID, "tid": _TID}

_OPERATOR_EMAIL = "admin@example.com"


def _operator_settings() -> CabinetSettings:
    """Allowlist of platform operators — contains _OPERATOR_EMAIL."""
    return CabinetSettings(CABINET_ADMIN_EMAILS=_OPERATOR_EMAIL)


def _user_row(**kw):
    base = dict(
        id=_UID,
        email="admin@example.com",
        created_at=_NOW,
        tenant_id=_TID,
        tier="pro",
        access_until=_FUTURE,
        profile_count=2,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# Helper TestClient factories
# --------------------------------------------------------------------------- #


def _client_admin(extra_results=None):
    """Client with an operator session. The first FakeSession result is the operator’s email,
    in the allowlist CABINET_ADMIN_EMAILS."""
    results = [FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)])]
    if extra_results:
        results.extend(extra_results)
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    return TestClient(app, follow_redirects=False), fs


def _client_non_admin():
    """Client with the session of a user whose email is NOT in the operator allowlist."""
    fs = FakeSession(
        results=[FakeResult(items=[SimpleNamespace(email="user@example.com")])]
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _USER_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    return TestClient(app, follow_redirects=False)


def _client_anon(settings_provider=_operator_settings):
    """Client without a session (anonymous). The default allowlist is NOT empty — otherwise
    fail-closed would mask the behavior for anonymous users."""
    fs = FakeSession()
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    app.dependency_overrides[deps.get_settings] = settings_provider
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Route-guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/",
        "/admin/users",
        f"/admin/users/{_UID}",
        "/admin/templates",
        "/admin/moderation",
        "/admin/health",
    ],
)
def test_anon_redirected_to_admin_login(path):
    """An anonymous user on any admin path (including bare /admin) → 303 to login, not 404
    and not a bare 403 (that read as “broken”, not as “log in”)."""
    resp = _client_anon().get(path)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/login?next={quote(path, safe='')}"


@pytest.mark.parametrize(
    "path",
    [
        f"/admin/users/{_UID}/grant",
        f"/admin/users/{_UID}/revoke",
        f"/admin/users/{_UID}/set-tier",
    ],
)
def test_anon_post_redirects_to_admin_home_not_to_post_path(path):
    """A POST path cannot be the return target: the browser would come back with GET and get 405.
    Anonymous on an action route → login with a return to the admin root."""
    resp = _client_anon().post(path)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login?next=%2Fadmin"


@pytest.mark.parametrize("method,path", [("get", "/admin/users"), ("get", "/admin")])
def test_anon_gets_403_when_allowlist_empty(method, path):
    """Fail-closed: no operators configured — nobody is sent to login."""
    client = _client_anon(
        settings_provider=lambda: CabinetSettings(CABINET_ADMIN_EMAILS="")
    )
    assert getattr(client, method)(path).status_code == 403


def test_anon_htmx_request_stays_403():
    """An anonymous HTMX request expects a fragment — a redirect to the HTML form would break it."""
    client = _client_anon()
    resp = client.get("/admin/users", headers={"HX-Request": "true"})
    assert resp.status_code == 403
    assert "location" not in resp.headers


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/admin"),
        ("get", "/admin/"),
        ("get", "/admin/users"),
        ("get", "/admin/templates"),
        ("get", "/admin/moderation"),
        ("get", "/admin/health"),
    ],
)
def test_non_admin_gets_403(method, path):
    client = _client_non_admin()
    resp = getattr(client, method)(path)
    assert resp.status_code == 403


@pytest.mark.parametrize("path", ["/admin", "/admin/"])
def test_admin_index_redirects_to_users(path):
    """Bare /admin (and /admin/) under an admin session → 303 to /admin/users —
    the operator’s workplace (owner on 04.08 could not find user management,
    because the index led to “Health”; “Health” remains a navigation item)."""
    client, _ = _client_admin()
    resp = client.get(path)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/users"


# --------------------------------------------------------------------------- #
# /admin/users — list
# --------------------------------------------------------------------------- #


def test_admin_users_list_renders():
    rows = [
        SimpleNamespace(
            id=_UID,
            email="user@example.com",
            created_at=_NOW,
            tenant_id=_TID,
            tier="starter",
            access_until=_FUTURE,
            profile_count=1,
        )
    ]
    # require_admin: email-allowlist check
    # list_users: paged rows + total count (2 queries)
    # get_platform_stats: tender stats row + users total (2 queries)
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),
        FakeResult(items=rows),
        FakeResult(scalar=1),
        FakeResult(
            items=[SimpleNamespace(total_all_time=0, total_month=0, total_day=0)]
        ),
        FakeResult(scalar=1),
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/admin/users")
    assert resp.status_code == 200
    assert "user@example.com" in resp.text


# --------------------------------------------------------------------------- #
# /admin/users — search (email/tenant) + plan filter (web-ux-polish).
# Read-only, filtering in Python on top of the SECURITY DEFINER selection (no migrations).
# --------------------------------------------------------------------------- #


def _scan_row(**kw):
    base = dict(
        id=_UID,
        email="alpha@company.rs",
        created_at=_NOW,
        tenant_id=_TID,
        tier="pro",
        access_until=_FUTURE,
        profile_count=1,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_list_users_search_narrows_results():
    """Search by email substring narrows the selection. Mutation: remove applying the
    filter in _user_matches → both rows come back → the assert fails."""
    rows = [
        _scan_row(email="alpha@company.rs"),
        _scan_row(
            id="cccc0000-0000-0000-0000-000000000009",
            email="beta@other.com",
            tenant_id="dddd0000-0000-0000-0000-000000000010",
            tier="lite",
            access_until=None,
        ),
    ]
    fs = FakeSession(results=[FakeResult(items=rows)])
    users, total = await svc.list_users(fs, page=1, search="company.rs")
    assert total == 1
    assert [u.email for u in users] == ["alpha@company.rs"]


@pytest.mark.asyncio
async def test_list_users_tier_filter_narrows_results():
    """The plan filter narrows the selection to the rows of the chosen plan."""
    rows = [
        _scan_row(email="pro@company.rs", tier="pro"),
        _scan_row(
            id="cccc0000-0000-0000-0000-000000000009",
            email="lite@company.rs",
            tier="lite",
        ),
    ]
    fs = FakeSession(results=[FakeResult(items=rows)])
    users, total = await svc.list_users(fs, page=1, tier="lite")
    assert total == 1
    assert users[0].tier == "lite"


@pytest.mark.asyncio
async def test_list_users_no_filter_uses_sql_pagination():
    """Without search/filter — the old path: pagination in SQL (LIMIT/OFFSET) +
    a separate COUNT, not a window selection with filtering in Python."""
    fs = FakeSession(results=[FakeResult(items=[_scan_row()]), FakeResult(scalar=42)])
    users, total = await svc.list_users(fs, page=1)
    assert total == 42  # from app_admin_users_total, not len(page)
    assert len(users) == 1
    executed = " ".join(sql for sql, _ in fs.executed)
    assert "app_admin_users_total" in executed


def test_admin_users_search_filters_rendered_rows():
    """The route GET /admin/users?q=... returns only the matching rows. Mutation:
    remove applying the filter → beta@other.com would appear in the HTML → the assert fails.
    """
    rows = [
        _scan_row(email="alpha@company.rs"),
        _scan_row(
            id="cccc0000-0000-0000-0000-000000000009",
            email="beta@other.com",
            tenant_id="dddd0000-0000-0000-0000-000000000010",
            tier="lite",
        ),
    ]
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),  # require_admin
        FakeResult(items=rows),  # list_users scan (filter active → 1 query)
        FakeResult(
            items=[SimpleNamespace(total_all_time=0, total_month=0, total_day=0)]
        ),  # tender stats
        FakeResult(scalar=2),  # users total (for the statistics cards)
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/admin/users?q=company.rs")
    assert resp.status_code == 200
    assert "alpha@company.rs" in resp.text
    assert "beta@other.com" not in resp.text
    # the search form keeps the entered query
    assert 'value="company.rs"' in resp.text


# --------------------------------------------------------------------------- #
# /admin/users/{uid} — detail page
# --------------------------------------------------------------------------- #


def test_admin_user_detail_renders():
    user_row = _user_row()
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),
        FakeResult(items=[user_row]),
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.get(f"/admin/users/{_UID}")
    assert resp.status_code == 200
    assert "admin@example.com" in resp.text


# --------------------------------------------------------------------------- #
# grant / revoke — actions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_service_grant_access():
    fs = FakeSession(results=[FakeResult()])
    await svc.grant_access(
        fs,
        admin_uid="aaaa0000-0000-0000-0000-000000000001",
        tenant_id=_TID,
        days=7,
        before_until=None,  # the extension base is mandatory (see test_grant_access_never_shrinks)
    )
    assert fs.committed == 1
    executed_sql = " ".join(
        sql for sql, _ in fs.executed if "set_config" not in sql.lower()
    )
    assert "app_admin_grant_access" in executed_sql


@pytest.mark.asyncio
async def test_service_revoke_access():
    fs = FakeSession(results=[FakeResult()])
    await svc.revoke_access(
        fs, admin_uid="aaaa0000-0000-0000-0000-000000000001", tenant_id=_TID
    )
    assert fs.committed == 1


@pytest.mark.asyncio
async def test_service_set_tier():
    fs = FakeSession(results=[FakeResult()])
    await svc.set_tier(
        fs, admin_uid="aaaa0000-0000-0000-0000-000000000001", tenant_id=_TID, tier="pro"
    )
    assert fs.committed == 1


@pytest.mark.asyncio
async def test_service_set_tier_rejects_non_canon_tier():
    """R8: the service validates the canonical value BEFORE the write — a non-canonical value → a clear ValueError,
    not a raw DB exception from ck_tenants_tier (0027). No write happens."""
    fs = FakeSession(results=[FakeResult()])
    with pytest.raises(ValueError, match="Недопустимый тариф"):
        await svc.set_tier(
            fs,
            admin_uid="aaaa0000-0000-0000-0000-000000000001",
            tenant_id=_TID,
            tier="free",  # the showcase, but NOT the internal canon
        )
    assert fs.committed == 0


def test_route_grant_redirects():
    user_row = _user_row()
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),  # require_admin
        FakeResult(items=[user_row]),  # get_user
        FakeResult(),  # grant_access UPDATE
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/admin/users/{_UID}/grant", data={"days": "14"})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/users/{_UID}"


def test_route_revoke_redirects():
    user_row = _user_row()
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),
        FakeResult(items=[user_row]),
        FakeResult(),
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/admin/users/{_UID}/revoke")
    assert resp.status_code == 303


# --------------------------------------------------------------------------- #
# set-tier
# --------------------------------------------------------------------------- #


def test_route_set_tier_redirects():
    user_row = _user_row()
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),
        FakeResult(items=[user_row]),
        FakeResult(),
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/admin/users/{_UID}/set-tier", data={"tier": "starter"})
    assert resp.status_code == 303


def test_route_set_tier_invalid_rejects():
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/admin/users/{_UID}/set-tier", data={"tier": "invalid_tier"})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# /admin/templates — list of profiles
# --------------------------------------------------------------------------- #

_PID = "cccc0000-0000-0000-0000-000000000003"


def _profile_row(**kw):
    base = dict(
        id=_PID,
        name="Test Profile",
        tenant_id=_TID,
        is_active=True,
        owner_email="owner@example.com",
        match_7d=5,
        created_at=None,
        delivery_channels=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_admin_templates_renders():
    rows = [_profile_row()]
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),  # require_admin
        FakeResult(items=rows),  # list_profiles
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/admin/templates")
    assert resp.status_code == 200
    assert "Test Profile" in resp.text
    assert "owner@example.com" in resp.text
    assert "5" in resp.text


@pytest.mark.parametrize(
    "attr,expected_fn",
    [
        ("_USERS_LIST_PAGED_SQL", "app_admin_list_users"),
        ("_USERS_TOTAL_SQL", "app_admin_users_total"),
        ("_USER_DETAIL_SQL", "app_admin_get_user"),
        ("_PROFILES_LIST_SQL", "app_admin_list_profiles"),
        ("_USER_DELIVERED_SQL", "app_admin_user_delivered_count"),
        ("_GRANT_ACCESS_SQL", "app_admin_grant_access"),
        ("_REVOKE_ACCESS_SQL", "app_admin_revoke_access"),
        ("_SET_TIER_SQL", "app_admin_set_tier"),
    ],
)
def test_cross_tenant_sql_calls_security_definer_function(attr, expected_fn):
    """FIX-ADMIN-RLS: every cross-tenant SQL constant of admin_service calls the corresponding
    SECURITY DEFINER function app_admin_* (migration 0025), and not a raw SELECT/UPDATE on users/tenants/profiles/delivery_outbox.
    """
    sql = str(getattr(svc, attr))
    assert expected_fn in sql
    assert "UPDATE tenants" not in sql
    assert "FROM users" not in sql
    assert "FROM profiles" not in sql
    assert "FROM delivery_outbox" not in sql


# --------------------------------------------------------------------------- #
# VG-29.1 — admin console on the design system (frame, badges, empty state,
# confirm on destructive actions). The page data is mocked at the
# service level, so that only the markup is checked.
# --------------------------------------------------------------------------- #


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _client_admin_monkeypatched():
    """Admin client; require_admin reads the operator’s email from the first FakeSession result,
    the page data is set by the service mock (no DB access)."""
    fs = FakeSession(
        results=[FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)])]
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    return TestClient(app, follow_redirects=False)


def test_admin_layout_marks_users_nav_active(monkeypatch):
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
    resp = _client_admin_monkeypatched().get("/admin/users")
    assert resp.status_code == 200
    assert 'href="/admin/users" class="nav-link is-active"' in resp.text
    assert 'href="/admin/moderation" class="nav-link"' in resp.text
    assert 'href="/admin/templates" class="nav-link"' in resp.text


def test_admin_users_empty_state(monkeypatch):
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
    resp = _client_admin_monkeypatched().get("/admin/users")
    assert resp.status_code == 200
    assert "empty-state" in resp.text
    assert "Пользователей нет" in resp.text


def test_admin_users_renders_tier_and_access_badges(monkeypatch):
    rows = [
        SimpleNamespace(
            id=_UID,
            email="user@example.com",
            created_at=_NOW,
            tenant_id=_TID,
            tier="pro",
            access_active=True,
            access_until=_FUTURE,
            profile_count=1,
        )
    ]
    monkeypatch.setattr(routes_admin.svc, "list_users", _async((rows, 1)))
    monkeypatch.setattr(
        routes_admin.svc,
        "get_platform_stats",
        _async(
            SimpleNamespace(
                tenders_all_time=0, tenders_month=0, tenders_day=0, users_total=1
            )
        ),
    )
    resp = _client_admin_monkeypatched().get("/admin/users")
    assert resp.status_code == 200
    # Plan — metadata: blue is reserved for “there is still time” and the
    # match rating (the canon of role colors); the labels live in .badge-muted.
    assert "badge-muted" in resp.text  # plan
    assert "badge-success" in resp.text  # active access
    assert 'class="card' in resp.text  # stats as cards


def test_admin_user_detail_revoke_has_confirm(monkeypatch):
    user = _user_row(access_active=True, days_left=30)
    monkeypatch.setattr(routes_admin.svc, "get_user", _async(user))
    monkeypatch.setattr(routes_admin.svc, "get_user_delivered_count", _async(3))
    resp = _client_admin_monkeypatched().get(f"/admin/users/{_UID}")
    assert resp.status_code == 200
    assert 'href="/admin/users" class="nav-link is-active"' in resp.text
    assert "btn-danger" in resp.text
    assert 'data-confirm="Заморозить аккаунт?"' in resp.text


def test_admin_templates_marks_nav_and_active_badge(monkeypatch):
    monkeypatch.setattr(routes_admin.svc, "list_profiles", _async([_profile_row()]))
    resp = _client_admin_monkeypatched().get("/admin/templates")
    assert resp.status_code == 200
    assert 'href="/admin/templates" class="nav-link is-active"' in resp.text
    assert "badge-success" in resp.text
    assert "Test Profile" in resp.text


def test_admin_templates_empty_state(monkeypatch):
    monkeypatch.setattr(routes_admin.svc, "list_profiles", _async([]))
    resp = _client_admin_monkeypatched().get("/admin/templates")
    assert resp.status_code == 200
    assert "empty-state" in resp.text
    assert "Шаблонов нет" in resp.text


def test_admin_moderation_pending_has_confirm_and_active_nav(monkeypatch):
    pending = [
        SimpleNamespace(
            id="dddd0000-0000-0000-0000-000000000004",
            name="Предложенный шаблон",
            description="Описание",
            activity_kind="строительство",
            created_by_user_id="owner",
            created_at=_NOW,
        )
    ]
    monkeypatch.setattr(lib_svc, "list_pending", _async(pending))
    resp = _client_admin_monkeypatched().get("/admin/moderation")
    assert resp.status_code == 200
    assert 'href="/admin/moderation" class="nav-link is-active"' in resp.text
    assert "btn-primary" in resp.text  # Approve
    assert "btn-danger" in resp.text  # Reject
    assert 'data-confirm="Отклонить шаблон?"' in resp.text
    assert "Предложенный шаблон" in resp.text


def test_admin_moderation_empty_state(monkeypatch):
    monkeypatch.setattr(lib_svc, "list_pending", _async([]))
    resp = _client_admin_monkeypatched().get("/admin/moderation")
    assert resp.status_code == 200
    assert "empty-state" in resp.text
    assert "Очередь модерации пуста" in resp.text


def test_profiles_sql_calls_security_definer_function():
    """FIX-ADMIN-RLS: _PROFILES_LIST_SQL calls app_admin_list_profiles()
    (SECURITY DEFINER, migration 0025), and not a raw cross-tenant SELECT.

    A regression on delivery_outbox/profile_id (not tender_user_status) — in the function body,
    see tests/db/test_0025_admin_rls_functions_migration.py."""
    import src.api.admin.admin_service as svc_module

    sql = str(svc_module._PROFILES_LIST_SQL)
    assert "app_admin_list_profiles" in sql
    assert (
        "FROM profiles" not in sql
    ), "the cross-tenant SELECT must go through SECURITY DEFINER"


# --------------------------------------------------------------------------- #
# VG-29.2 — ingest health panel /admin/health (read-only, only NON-RLS
# tables). The rendering mocks the service at the routes_admin.svc level; the service — at
# FakeSession, following the VG-29.1 pattern
# --------------------------------------------------------------------------- #


def _health(**kw):
    """Real IngestHealth (the tender_status/*_label properties are computed by themselves)."""
    base = dict(
        last_tender_at=_NOW,
        tenders_1h=3,
        tenders_24h=40,
        tenders_7d=280,
        last_award_at=_NOW,
        awards_24h=5,
        awards_7d=30,
    )
    base.update(kw)
    return svc.IngestHealth(**base)


def test_admin_health_marks_nav_active_and_cards(monkeypatch):
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(
        routes_admin.svc,
        "get_ingest_health",
        _async(_health(last_tender_at=fresh, last_award_at=fresh)),
    )
    resp = _client_admin_monkeypatched().get("/admin/health")
    assert resp.status_code == 200
    assert 'href="/admin/health" class="nav-link is-active"' in resp.text
    assert 'class="card' in resp.text  # metrics as cards
    assert "badge-success" in resp.text  # fresh inflow
    assert "за 1 час" in resp.text


def test_admin_health_stale_shows_error_badge(monkeypatch):
    """60h — two night windows in a row were missed (threshold 52h) → error."""
    stale = datetime.now(timezone.utc) - timedelta(hours=60)
    monkeypatch.setattr(
        routes_admin.svc,
        "get_ingest_health",
        _async(_health(last_tender_at=stale, last_award_at=None)),
    )
    resp = _client_admin_monkeypatched().get("/admin/health")
    assert resp.status_code == 200
    assert "badge-error" in resp.text  # inflow stopped


def test_admin_health_urgent_badge(monkeypatch):
    """30h — one night window was missed (threshold 28h), but not two (52h) → urgent."""
    late = datetime.now(timezone.utc) - timedelta(hours=30)
    monkeypatch.setattr(
        routes_admin.svc,
        "get_ingest_health",
        _async(_health(last_tender_at=late)),
    )
    resp = _client_admin_monkeypatched().get("/admin/health")
    assert resp.status_code == 200
    assert "badge-urgent" in resp.text


def test_admin_health_empty_state(monkeypatch):
    monkeypatch.setattr(
        routes_admin.svc,
        "get_ingest_health",
        _async(_health(last_tender_at=None, last_award_at=None)),
    )
    resp = _client_admin_monkeypatched().get("/admin/health")
    assert resp.status_code == 200
    assert "empty-state" in resp.text
    assert "Данных ingest пока нет" in resp.text


def test_ingest_health_status_thresholds():
    """The threshold measures the source’s RHYTHM (night batch 22:00-00:59 UTC, the maximum
    gap 24.3h was measured on 06.08), not absolute hours: ~10h and ~24h after a
    night batch is normal, not an alarm (the old assert `10h → urgent` pinned
    a false alarm — exactly the case that burned on a healthy prod)."""
    now = datetime.now(timezone.utc)
    assert _health(last_tender_at=now - timedelta(hours=1)).tender_status == "success"
    assert _health(last_tender_at=now - timedelta(hours=10)).tender_status == "success"
    assert _health(last_tender_at=now - timedelta(hours=24)).tender_status == "success"
    assert _health(last_tender_at=now - timedelta(hours=30)).tender_status == "urgent"
    assert _health(last_tender_at=now - timedelta(hours=60)).tender_status == "error"
    assert _health(last_tender_at=None).tender_status == "error"
    assert _health(last_tender_at=None, last_award_at=None).is_empty is True
    assert _health(last_tender_at=now).is_empty is False


@pytest.mark.asyncio
async def test_service_get_ingest_health_maps_rows():
    tender_row = SimpleNamespace(last_at=_NOW, cnt_1h=2, cnt_24h=20, cnt_7d=100)
    award_row = SimpleNamespace(last_at=_NOW, cnt_24h=4, cnt_7d=12)
    fs = FakeSession(
        results=[FakeResult(items=[tender_row]), FakeResult(items=[award_row])]
    )
    health = await svc.get_ingest_health(fs)
    assert health.tenders_1h == 2
    assert health.tenders_24h == 20
    assert health.tenders_7d == 100
    assert health.awards_24h == 4
    assert health.awards_7d == 12
    assert health.last_tender_at == _NOW
    assert health.last_award_at == _NOW
    # requests go only to NON-RLS tables, without app_admin_*/SECURITY DEFINER
    executed = " ".join(sql for sql, _ in fs.executed)
    assert "tenders_normalized" in executed
    assert "tender_awards" in executed
    assert "app_admin_" not in executed


def test_ingest_health_sql_only_non_rls_tables():
    """INVARIANT VG-29.2: the health panel reads ONLY NON-RLS tables —
    no app_admin_*/SECURITY DEFINER and no cross-tenant users/tenants."""
    for attr in ("_INGEST_TENDERS_SQL", "_INGEST_AWARDS_SQL"):
        sql = str(getattr(svc, attr))
        assert "app_admin_" not in sql
        assert "FROM users" not in sql
        assert "FROM tenants" not in sql
        assert "delivery_outbox" not in sql


# --------------------------------------------------------------------------- #
# VG-29.3 — delivery/queue health panel on /admin/health. delivery_outbox
# and the reminder journals are under FORCE RLS → cross-tenant aggregates ONLY through
# SECURITY DEFINER app_admin_* (migration 0026). The rendering mocks the service at the
# routes_admin.svc level; the service — at FakeSession.
# --------------------------------------------------------------------------- #


def _delivery(**kw):
    """Real DeliveryHealth (backlog_status/*_label are computed by themselves)."""
    base = dict(
        pending_total=4,
        failed_24h=0,
        sent_1h=6,
        sent_24h=120,
        last_sent_at=_NOW,
        last_deadline_reminder_at=_NOW,
        last_subscription_reminder_at=_NOW,
    )
    base.update(kw)
    return svc.DeliveryHealth(**base)


def _patch_health(monkeypatch, *, ingest=None, delivery=None):
    """Mocks both /admin/health sources at the routes_admin.svc level."""
    monkeypatch.setattr(
        routes_admin.svc, "get_ingest_health", _async(ingest or _health())
    )
    monkeypatch.setattr(
        routes_admin.svc, "get_delivery_health", _async(delivery or _delivery())
    )


def test_admin_health_delivery_section_renders(monkeypatch):
    fresh = datetime.now(timezone.utc) - timedelta(minutes=10)
    _patch_health(
        monkeypatch,
        delivery=_delivery(last_sent_at=fresh, pending_total=2),
    )
    resp = _client_admin_monkeypatched().get("/admin/health")
    assert resp.status_code == 200
    assert "Доставка и очередь" in resp.text
    assert "ждут доставки" in resp.text  # backlog card
    assert "badge-success" in resp.text  # queue is normal


def test_admin_health_delivery_stalled_error_badge(monkeypatch):
    """pending_total > 0 + last delivery long ago → queue stalled → error."""
    stale = datetime.now(timezone.utc) - timedelta(hours=12)
    _patch_health(
        monkeypatch,
        delivery=_delivery(pending_total=50, last_sent_at=stale, failed_24h=0),
    )
    resp = _client_admin_monkeypatched().get("/admin/health")
    assert resp.status_code == 200
    assert "badge-error" in resp.text
    assert "Очередь встала" in resp.text


def test_admin_health_delivery_failed_urgent_badge(monkeypatch):
    """There are failures in the last day, but the queue is moving → urgent."""
    fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
    _patch_health(
        monkeypatch,
        delivery=_delivery(pending_total=1, last_sent_at=fresh, failed_24h=3),
    )
    resp = _client_admin_monkeypatched().get("/admin/health")
    assert resp.status_code == 200
    assert "badge-urgent" in resp.text


def test_admin_health_delivery_empty_state(monkeypatch):
    _patch_health(
        monkeypatch,
        delivery=_delivery(
            pending_total=0,
            failed_24h=0,
            sent_1h=0,
            sent_24h=0,
            last_sent_at=None,
            last_deadline_reminder_at=None,
            last_subscription_reminder_at=None,
        ),
    )
    resp = _client_admin_monkeypatched().get("/admin/health")
    assert resp.status_code == 200
    assert "Доставок пока нет" in resp.text


def test_delivery_health_backlog_status_thresholds():
    now = datetime.now(timezone.utc)
    # queue is moving, no failures → success
    assert _delivery(pending_total=5, last_sent_at=now).backlog_status == "success"
    # failures in the last day, but the queue is fresh → urgent
    assert (
        _delivery(pending_total=1, last_sent_at=now, failed_24h=2).backlog_status
        == "urgent"
    )
    # there are unsent items, delivery stalled long ago → error (more important than urgent)
    stale = now - timedelta(hours=12)
    assert (
        _delivery(pending_total=3, last_sent_at=stale, failed_24h=9).backlog_status
        == "error"
    )
    # unsent items exist, but there were no deliveries at all → error
    assert _delivery(pending_total=2, last_sent_at=None).backlog_status == "error"
    # empty on all axes → empty state
    assert (
        _delivery(
            pending_total=0,
            failed_24h=0,
            last_sent_at=None,
            last_deadline_reminder_at=None,
            last_subscription_reminder_at=None,
        ).is_empty
        is True
    )


@pytest.mark.asyncio
async def test_service_get_delivery_health_maps_rows():
    backlog_row = SimpleNamespace(
        pending_total=7,
        failed_24h=2,
        sent_1h=3,
        sent_24h=48,
        last_sent_at=_NOW,
    )
    reminder_row = SimpleNamespace(
        last_deadline_reminder_at=_NOW,
        last_subscription_reminder_at=_PAST,
    )
    fs = FakeSession(
        results=[FakeResult(items=[backlog_row]), FakeResult(items=[reminder_row])]
    )
    health = await svc.get_delivery_health(fs)
    assert health.pending_total == 7
    assert health.failed_24h == 2
    assert health.sent_1h == 3
    assert health.sent_24h == 48
    assert health.last_sent_at == _NOW
    assert health.last_deadline_reminder_at == _NOW
    assert health.last_subscription_reminder_at == _PAST
    # cross-tenant aggregates — ONLY through SECURITY DEFINER app_admin_*
    executed = " ".join(sql for sql, _ in fs.executed)
    assert "app_admin_delivery_backlog" in executed
    assert "app_admin_reminder_freshness" in executed


def test_delivery_health_sql_uses_security_definer_only():
    """INVARIANT VG-29.3: delivery_outbox/reminder journals are under FORCE RLS →
    the service calls SECURITY DEFINER app_admin_*, NEVER a direct SELECT."""
    for attr, fn in (
        ("_DELIVERY_BACKLOG_SQL", "app_admin_delivery_backlog"),
        ("_REMINDER_FRESHNESS_SQL", "app_admin_reminder_freshness"),
    ):
        sql = str(getattr(svc, attr))
        assert fn in sql
        assert "FROM delivery_outbox" not in sql
        assert "FROM reminder_log" not in sql
        assert "FROM subscription_reminder_log" not in sql


# --------------------------------------------------------------------------- #
# /admin/users — sorting by columns (sort/dir, owner 14.10).
# Fixture of 4 rows: different emails, plans including NULL, access_until with NULL,
# profiles 0/3/1 (a tie for 1). Input — in SQL order (created_at DESC), as in prod.
# Expected orders — literals (email), not via the same sorting code.
# --------------------------------------------------------------------------- #

_SORT_ROWS = [
    # created desc: R4, R2, R3, R1
    _scan_row(
        id="cccc0000-0000-0000-0000-000000000004",
        email="charlie@x.rs",
        tenant_id="cccc0000-0000-0000-0000-00000000000c",
        tier="enterprise",
        access_until=datetime(2026, 7, 1, tzinfo=timezone.utc),
        profile_count=1,
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    ),
    _scan_row(
        id="dddd0000-0000-0000-0000-000000000002",
        email="alpha@x.rs",
        tenant_id="dddd0000-0000-0000-0000-00000000000d",
        tier=None,
        access_until=None,
        profile_count=0,
        created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    ),
    _scan_row(
        id="eeee0000-0000-0000-0000-000000000003",
        email="delta@x.rs",
        tenant_id="bbbb0000-0000-0000-0000-00000000000b",
        tier="lite",
        access_until=datetime(2026, 9, 1, tzinfo=timezone.utc),
        profile_count=1,
        created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    ),
    _scan_row(
        id="ffff0000-0000-0000-0000-000000000001",
        email="bravo@x.rs",
        tenant_id="aaaa0000-0000-0000-0000-00000000000a",
        tier="pro",
        access_until=datetime(2026, 5, 1, tzinfo=timezone.utc),
        profile_count=3,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ),
]

# (column, dir) → expected email order. NULL (plan/access) — at the end.
_SORT_EXPECTED = {
    ("email", "asc"): ["alpha@x.rs", "bravo@x.rs", "charlie@x.rs", "delta@x.rs"],
    ("email", "desc"): ["delta@x.rs", "charlie@x.rs", "bravo@x.rs", "alpha@x.rs"],
    ("tenant", "asc"): ["bravo@x.rs", "delta@x.rs", "charlie@x.rs", "alpha@x.rs"],
    ("tenant", "desc"): ["alpha@x.rs", "charlie@x.rs", "delta@x.rs", "bravo@x.rs"],
    # TIER_ORDER, not alphabetical: lite < pro < enterprise; NULL — last.
    ("tier", "asc"): ["delta@x.rs", "bravo@x.rs", "charlie@x.rs", "alpha@x.rs"],
    ("tier", "desc"): ["charlie@x.rs", "bravo@x.rs", "delta@x.rs", "alpha@x.rs"],
    # access_until: NULL (“unlimited”) — last in both directions.
    ("access_until", "asc"): ["bravo@x.rs", "charlie@x.rs", "delta@x.rs", "alpha@x.rs"],
    ("access_until", "desc"): [
        "delta@x.rs",
        "charlie@x.rs",
        "bravo@x.rs",
        "alpha@x.rs",
    ],
    # profiles: tie for 1 (charlie, delta) — the input order is preserved.
    ("profiles", "asc"): ["alpha@x.rs", "charlie@x.rs", "delta@x.rs", "bravo@x.rs"],
    ("profiles", "desc"): ["bravo@x.rs", "charlie@x.rs", "delta@x.rs", "alpha@x.rs"],
    ("created", "asc"): ["bravo@x.rs", "delta@x.rs", "alpha@x.rs", "charlie@x.rs"],
    ("created", "desc"): ["charlie@x.rs", "alpha@x.rs", "delta@x.rs", "bravo@x.rs"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("key,direction", list(_SORT_EXPECTED))
async def test_list_users_sort_each_column_both_directions(key, direction):
    """Each of the 6 columns in both directions → literal email order.
    Mutation: remove the sorting in list_users → the order stays created desc."""
    # 2nd query — COUNT for the SQL path (default created desc); the window does not read it.
    fs = FakeSession(results=[FakeResult(items=list(_SORT_ROWS)), FakeResult(scalar=4)])
    users, total = await svc.list_users(fs, page=1, sort=key, direction=direction)
    assert total == 4
    assert [u.email for u in users] == _SORT_EXPECTED[(key, direction)]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["tier", "access_until"])
async def test_list_users_sort_nulls_last_in_both_directions(key):
    """NULL values (plan, access_until) are always at the end — both for asc and desc.
    Mutation: remove moving NULLs to the end (sort None as the minimum) → red."""
    for direction in ("asc", "desc"):
        fs = FakeSession(results=[FakeResult(items=list(_SORT_ROWS))])
        users, _ = await svc.list_users(fs, page=1, sort=key, direction=direction)
        assert users[-1].email == "alpha@x.rs", (key, direction)


@pytest.mark.parametrize(
    "sort,direction,expected",
    [
        ("xxx", "yyy", ("created", "desc")),  # garbage → default, no 422
        ("", "", ("created", "desc")),
        (None, None, ("created", "desc")),
        (
            "email",
            "sideways",
            ("email", "asc"),
        ),  # garbage dir → the column’s first direction
        ("access_until", "bogus", ("access_until", "desc")),
        ("TENANT", " ASC ", ("tenant", "asc")),
    ],
)
def test_normalize_sort_whitelist(sort, direction, expected):
    assert svc.normalize_sort(sort, direction) == expected


def test_admin_users_garbage_sort_keeps_default_order():
    """?sort=xxx&dir=yyy → 200 and the default order (created desc), no 422.
    Without filters — the SQL path: row order as returned by SECURITY DEFINER."""
    results = [
        FakeResult(items=list(_SORT_ROWS)),
        FakeResult(scalar=4),
        FakeResult(
            items=[SimpleNamespace(total_all_time=0, total_month=0, total_day=0)]
        ),
        FakeResult(scalar=4),
    ]
    client, _ = _client_admin(results)
    resp = client.get("/admin/users?sort=xxx&dir=yyy")
    assert resp.status_code == 200
    html = resp.text
    order = [
        html.index(e)
        for e in ("charlie@x.rs", "alpha@x.rs", "delta@x.rs", "bravo@x.rs")
    ]
    assert order == sorted(order)


def test_admin_users_sort_links_keep_filters_and_mark_active_column():
    """Headers are links: they keep q and tier, reset page; the active <th>
    carries aria-sort. Mutation: do not pass filter_qs to the macro → q/tier disappear.
    """
    results = [
        FakeResult(items=[_SORT_ROWS[3]]),  # bravo: tier=pro → passes the filter
        FakeResult(
            items=[SimpleNamespace(total_all_time=0, total_month=0, total_day=0)]
        ),
        FakeResult(scalar=1),
    ]
    client, _ = _client_admin(results)
    resp = client.get("/admin/users?q=x.rs&tier=pro&sort=access_until&dir=asc")
    assert resp.status_code == 200
    html = resp.text
    # active column: direction asc, the next click — desc
    assert 'aria-sort="ascending"' in html
    assert "sort=access_until&dir=desc&q=x.rs&tier=pro" in html
    # inactive column: the first click — asc (Email), for dates — desc (Registered)
    assert "sort=email&dir=asc&q=x.rs&tier=pro" in html
    assert "sort=created&dir=desc&q=x.rs&tier=pro" in html
    # the search form carries the current sorting in hidden fields
    assert '<input type="hidden" name="sort" value="access_until">' in html
    assert '<input type="hidden" name="dir" value="asc">' in html


def test_admin_users_pager_keeps_sort_and_dir():
    """The pager keeps sort/dir (together with q/tier). 25 rows → 2 pages."""
    rows = [
        _scan_row(
            id=f"aaaa0000-0000-0000-0000-{i:012d}",
            email=f"u{i:02d}@x.rs",
            access_until=datetime(2026, 5, 1 + i, tzinfo=timezone.utc),
        )
        for i in range(25)
    ]
    results = [
        FakeResult(items=rows),  # list_users: sort ≠ default → window, 1 query
        FakeResult(
            items=[SimpleNamespace(total_all_time=0, total_month=0, total_day=0)]
        ),  # tender stats
        FakeResult(scalar=25),  # users total
    ]
    client, _ = _client_admin(results)
    resp = client.get("/admin/users?sort=access_until&dir=asc")
    assert resp.status_code == 200
    assert "?page=2&sort=access_until&dir=asc" in resp.text
