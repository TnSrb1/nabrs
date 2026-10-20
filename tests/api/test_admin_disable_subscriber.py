"""Disabling/restoring a subscriber in admin (0037): service + routes.

No DB (ADR-013): a mock session and `dependency_overrides`, as in `test_admin.py`.
The contract of the SQL functions is checked by `tests/db/test_0037_admin_disable_subscriber.py`,
the RLS contour — by `tests/db/test_rls_isolation.py` (skip without TEST_RLS_DATABASE_URL).

Why a separate button alongside the existing “Freeze”: `revoke_access` moves
only `tenants.access_until`, so the bot kept answering the disabled user via the linked channel,
and login by magic link stayed open.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.admin import admin_service as svc
from src.api.config import CabinetSettings
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
_FUTURE = _NOW + timedelta(days=30)
_OPERATOR_EMAIL = "admin@example.com"
_ADMIN_SESSION = {"uid": _UID, "tid": _TID}


def _operator_settings() -> CabinetSettings:
    return CabinetSettings(CABINET_ADMIN_EMAILS=_OPERATOR_EMAIL)


def _user_row(**kw):
    base = dict(
        id=_UID,
        email=_OPERATOR_EMAIL,
        created_at=_NOW,
        tenant_id=_TID,
        tier="pro",
        access_until=_FUTURE,
        profile_count=1,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _admin_client(extra_results):
    results = [FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)])]
    results.extend(extra_results)
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    return TestClient(app, follow_redirects=False), fs


def _sql(fs) -> str:
    return " ".join(sql for sql, _ in fs.executed if "set_config" not in sql.lower())


def _params(fs, needle: str) -> dict:
    for sql, params in fs.executed:
        if needle in sql:
            return params or {}
    raise AssertionError(f"call {needle} not found: {[s for s, _ in fs.executed]}")


def teardown_function():
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_service_disable_calls_definer_and_writes_audit():
    fs = FakeSession(results=[FakeResult(), FakeResult()])
    await svc.disable_subscriber(
        fs,
        admin_uid=_UID,
        tenant_id=_TID,
        actor_email=_OPERATOR_EMAIL,
        target_user=_UID,
    )
    assert "app_admin_disable_subscriber" in _sql(fs)
    assert "app_admin_write_audit" in _sql(fs) or "audit" in _sql(fs).lower()
    assert fs.committed == 1


@pytest.mark.asyncio
async def test_service_enable_passes_days_through():
    fs = FakeSession(results=[FakeResult(), FakeResult()])
    await svc.enable_subscriber(
        fs,
        admin_uid=_UID,
        tenant_id=_TID,
        days=14,
        actor_email=_OPERATOR_EMAIL,
    )
    assert _params(fs, "app_admin_enable_subscriber")["days"] == 14
    assert fs.committed == 1


@pytest.mark.asyncio
async def test_service_enable_defaults_to_no_term_change():
    """`days=0` — restores login and channels; the term is issued by a separate button."""
    fs = FakeSession(results=[FakeResult()])
    await svc.enable_subscriber(fs, admin_uid=_UID, tenant_id=_TID)
    assert _params(fs, "app_admin_enable_subscriber")["days"] == 0


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("action", ["disable", "enable"])
def test_route_redirects_back_to_user_card(action):
    client, fs = _admin_client([FakeResult(items=[_user_row()]), FakeResult()])
    resp = client.post(f"/admin/users/{_UID}/{action}")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/users/{_UID}"
    assert f"app_admin_{action}_subscriber" in _sql(fs)


@pytest.mark.parametrize("action", ["disable", "enable"])
def test_route_unknown_user_is_404_and_writes_nothing(action):
    client, fs = _admin_client([FakeResult(items=[])])
    resp = client.post(f"/admin/users/{_UID}/{action}")
    assert resp.status_code == 404
    assert "subscriber" not in _sql(fs)


@pytest.mark.parametrize("action", ["disable", "enable"])
def test_route_requires_operator(action):
    """Non-operator (email outside the allowlist) → 403, the action is not executed."""
    fs = FakeSession(
        results=[FakeResult(items=[SimpleNamespace(email="user@example.com")])]
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/admin/users/{_UID}/{action}")
    assert resp.status_code == 403
    assert "subscriber" not in _sql(fs)


@pytest.mark.parametrize(
    "raw,expected", [("30", 30), ("", 0), ("abc", 0), ("-5", 0), ("9999", 0)]
)
def test_route_enable_sanitizes_days(raw, expected):
    """A typo or garbage in the field must not grant years of access with one click."""
    client, fs = _admin_client([FakeResult(items=[_user_row()]), FakeResult()])
    resp = client.post(f"/admin/users/{_UID}/enable", data={"days": raw})
    assert resp.status_code == 303
    assert _params(fs, "app_admin_enable_subscriber")["days"] == expected


# --------------------------------------------------------------------------- #
# 0039: a disabled subscriber stays visible (list/card) and restorable
# --------------------------------------------------------------------------- #


def test_row_to_user_carries_deleted_at_through():
    """Contract of the service with the 0039 SQL row: deleted_at gets into UserRow and
    is not lost halfway between app_admin_get_user and the template."""
    raw = SimpleNamespace(
        id=_UID,
        email=_OPERATOR_EMAIL,
        created_at=_NOW,
        tenant_id=_TID,
        tier="pro",
        access_until=_FUTURE,
        profile_count=1,
        deleted_at=_NOW,
    )
    row = svc._row_to_user(raw)
    assert row.deleted_at == _NOW
    assert row.is_disabled is True


def test_row_to_user_defaults_deleted_at_when_column_absent():
    """Rows without the deleted_at column (old mocks/pre-0039) must not fail —
    getattr with a default, not direct attribute access."""
    raw = SimpleNamespace(
        id=_UID,
        email=_OPERATOR_EMAIL,
        created_at=_NOW,
        tenant_id=_TID,
        tier="pro",
        access_until=_FUTURE,
        profile_count=1,
    )
    row = svc._row_to_user(raw)
    assert row.deleted_at is None
    assert row.is_disabled is False


def test_admin_user_detail_disabled_user_returns_200_not_404():
    """‼️ The core of the task: before 0039 app_admin_get_user filtered deleted_at IS NULL,
    so the card of a disabled user was unreachable (get_user → None → 404 in the route).
    The expectation is taken from the FACT — the response code, not a substring in the HTML."""
    user = _user_row(deleted_at=_NOW)
    client, _fs = _admin_client([FakeResult(items=[user]), FakeResult(scalar=0)])
    resp = client.get(f"/admin/users/{_UID}")
    assert resp.status_code == 200
    assert "Отключён" in resp.text


def test_admin_user_detail_disabled_shows_enable_not_disable_block():
    user = _user_row(deleted_at=_NOW)
    client, _fs = _admin_client([FakeResult(items=[user]), FakeResult(scalar=0)])
    resp = client.get(f"/admin/users/{_UID}")
    assert resp.status_code == 200
    assert "Вернуть подписчика" in resp.text
    assert "Отключить подписчика" not in resp.text


def test_admin_user_detail_active_shows_disable_not_enable_block():
    user = _user_row(deleted_at=None)
    client, _fs = _admin_client([FakeResult(items=[user]), FakeResult(scalar=0)])
    resp = client.get(f"/admin/users/{_UID}")
    assert resp.status_code == 200
    assert "Отключить подписчика" in resp.text
    assert "Вернуть подписчика" not in resp.text
    assert "Отключён" not in resp.text


def test_admin_users_list_shows_disabled_badge():
    disabled_row = _user_row(deleted_at=_NOW)
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),  # require_admin
        FakeResult(items=[disabled_row]),  # list_users paged rows
        FakeResult(scalar=1),  # list_users total
        FakeResult(
            items=[SimpleNamespace(total_all_time=0, total_month=0, total_day=0)]
        ),  # tender stats
        FakeResult(scalar=1),  # users_total
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
    assert "badge-muted" in resp.text
    assert "Отключён" in resp.text
