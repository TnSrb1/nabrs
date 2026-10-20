"""Tests of the admin audit (VG-29.4).

Three levels:
- Service (admin_service): unit with a mock session — grant/revoke/set_tier write an audit row
  in the SAME transaction (one commit), list_audit parses/paginates.
- Route (routes_admin): GET /admin/audit renders the page under require_admin.
- Regression under the app role (TEST_RLS_DATABASE_URL): the app role writes/reads the journal
  ONLY through SECURITY DEFINER app_admin_*_audit (0030); a direct SELECT from the
  table under the app role is fail-closed (permission denied). Skip if TEST_RLS
  is not configured.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
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
_TARGET_UID = "cccc0000-0000-0000-0000-000000000003"
_OPERATOR_EMAIL = "admin@example.com"
_ADMIN_SESSION = {"uid": _UID, "tid": _TID}


def _operator_settings() -> CabinetSettings:
    return CabinetSettings(CABINET_ADMIN_EMAILS=_OPERATOR_EMAIL)


def teardown_function():
    app.dependency_overrides.clear()


def _audit_write_calls(fs: FakeSession) -> list[tuple[str, dict]]:
    return [
        (sql, params) for sql, params in fs.executed if "app_admin_write_audit" in sql
    ]


# --------------------------------------------------------------------------- #
# (a) Service: mutations write the audit in the same transaction
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_grant_access_writes_audit_row():
    fs = FakeSession(results=[FakeResult(), FakeResult()])
    before = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await svc.grant_access(
        fs,
        admin_uid=_UID,
        tenant_id=_TID,
        days=7,
        actor_email=_OPERATOR_EMAIL,
        target_user=_TARGET_UID,
        ip="203.0.113.7",
        before_until=before,
    )
    # A single commit — the audit is consistent with the mutation (one transaction).
    assert fs.committed == 1
    calls = _audit_write_calls(fs)
    assert len(calls) == 1, "grant_access must write exactly one audit row"
    _, params = calls[0]
    assert params["actor"] == _OPERATOR_EMAIL
    assert params["action"] == "grant_access"
    assert params["target_tenant"] == _TID
    assert params["target_user"] == _TARGET_UID
    assert params["ip"] == "203.0.113.7"
    details = json.loads(params["details"])  # jsonb serialized in Python
    assert details["days"] == 7
    assert details["before_access_until"] == before.isoformat()
    assert "after_access_until" in details


@pytest.mark.asyncio
async def test_revoke_access_writes_audit_row():
    fs = FakeSession(results=[FakeResult(), FakeResult()])
    await svc.revoke_access(
        fs,
        admin_uid=_UID,
        tenant_id=_TID,
        actor_email=_OPERATOR_EMAIL,
        target_user=_TARGET_UID,
        ip=None,
        before_until=None,
    )
    assert fs.committed == 1
    calls = _audit_write_calls(fs)
    assert len(calls) == 1
    _, params = calls[0]
    assert params["action"] == "revoke_access"
    details = json.loads(params["details"])
    assert details["before_access_until"] is None


@pytest.mark.asyncio
async def test_set_tier_writes_audit_row():
    fs = FakeSession(results=[FakeResult(), FakeResult()])
    await svc.set_tier(
        fs,
        admin_uid=_UID,
        tenant_id=_TID,
        tier="pro",
        actor_email=_OPERATOR_EMAIL,
        target_user=_TARGET_UID,
        before_tier="lite",
    )
    assert fs.committed == 1
    calls = _audit_write_calls(fs)
    assert len(calls) == 1
    _, params = calls[0]
    assert params["action"] == "set_tier"
    details = json.loads(params["details"])
    assert details["before_tier"] == "lite"
    assert details["after_tier"] == "pro"


@pytest.mark.asyncio
async def test_actions_without_actor_skip_audit():
    """Backward compat: without actor_email no audit is written (direct service call)."""
    fs = FakeSession(results=[FakeResult()])
    await svc.grant_access(
        fs, admin_uid=_UID, tenant_id=_TID, days=7, before_until=None
    )
    assert fs.committed == 1
    assert _audit_write_calls(fs) == []


@pytest.mark.asyncio
async def test_set_tier_non_canon_writes_no_audit():
    """Non-canonical tier → ValueError BEFORE the write: no mutation, no audit, no commit."""
    fs = FakeSession(results=[FakeResult(), FakeResult()])
    with pytest.raises(ValueError, match="Недопустимый тариф"):
        await svc.set_tier(
            fs,
            admin_uid=_UID,
            tenant_id=_TID,
            tier="free",
            actor_email=_OPERATOR_EMAIL,
        )
    assert fs.committed == 0
    assert _audit_write_calls(fs) == []


# --------------------------------------------------------------------------- #
# (b) Service: list_audit — jsonb parsing (str→dict) and pagination
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_audit_parses_and_paginates():
    row = SimpleNamespace(
        id=42,
        at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        actor_email=_OPERATOR_EMAIL,
        action="grant_access",
        target_tenant_id=_TID,
        target_user_id=_TARGET_UID,
        details='{"days": 7, "after_access_until": "2026-07-21T10:00:00+00:00"}',
        ip="203.0.113.7",
    )
    fs = FakeSession(results=[FakeResult(items=[row]), FakeResult(scalar=123)])
    rows, total = await svc.list_audit(fs, page=3, page_size=50)

    assert total == 123
    assert len(rows) == 1
    got = rows[0]
    assert got.id == 42
    assert got.actor_email == _OPERATOR_EMAIL
    assert got.target_tenant_id == _TID
    # jsonb came as a string (asyncpg raw) — the service parsed it into a dict.
    assert isinstance(got.details, dict)
    assert got.details["days"] == 7

    # Pagination: page=3, page_size=50 → offset 100.
    list_call = next(
        params for sql, params in fs.executed if "app_admin_list_audit" in sql
    )
    assert list_call["limit"] == 50
    assert list_call["offset"] == 100


@pytest.mark.asyncio
async def test_list_audit_coerces_dict_and_bad_json():
    row_dict = SimpleNamespace(
        id=1,
        at=None,
        actor_email="a@b.rs",
        action="set_tier",
        target_tenant_id=None,
        target_user_id=None,
        details={"already": "dict"},
        ip=None,
    )
    row_bad = SimpleNamespace(
        id=2,
        at=None,
        actor_email="a@b.rs",
        action="set_tier",
        target_tenant_id=None,
        target_user_id=None,
        details="not-json",
        ip=None,
    )
    fs = FakeSession(
        results=[FakeResult(items=[row_dict, row_bad]), FakeResult(scalar=2)]
    )
    rows, _ = await svc.list_audit(fs, page=1)
    assert rows[0].details == {"already": "dict"}
    assert rows[1].details == {}  # broken json → {}, without failing
    assert rows[0].target_tenant_id is None


# --------------------------------------------------------------------------- #
# Route: GET /admin/audit
# --------------------------------------------------------------------------- #


def test_route_audit_renders():
    audit_row = svc.AuditRow(
        id=7,
        at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
        actor_email=_OPERATOR_EMAIL,
        action="grant_access",
        target_tenant_id=_TID,
        target_user_id=_TARGET_UID,
        details={"days": 30},
        ip="203.0.113.7",
    )
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),  # require_admin
        FakeResult(items=[audit_row]),  # list_audit rows
        FakeResult(scalar=1),  # audit_total
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/admin/audit")
    assert resp.status_code == 200
    assert "Аудит действий" in resp.text
    assert _OPERATOR_EMAIL in resp.text


def test_route_audit_requires_admin():
    """Anonymous → 303 to the operator login with a return to /admin/audit, not 200/404."""
    fs = FakeSession()
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/admin/audit")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login?next=%2Fadmin%2Faudit"


# --------------------------------------------------------------------------- #
# (c) Regression under the app role — SECURITY DEFINER path + fail-closed
# --------------------------------------------------------------------------- #

_TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")


@pytest.mark.skipif(
    not _TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — app-role регресс аудита пропускается",
)
@pytest.mark.asyncio
async def test_admin_audit_under_app_role():
    """VG-29.4 under the role tendersrb_app (FORCE RLS, 0021; the journal without RLS, 0030).

    Self-contained (own tenant, tier canon 'lite'/0027). Under the app role:
      * grant_access(actor_email=...) writes tenants + admin_audit_log via
        SECURITY DEFINER (app_admin_grant_access + app_admin_write_audit) — without GUC;
      * list_audit sees the written row via app_admin_list_audit (not 0);
      * a direct SELECT from admin_audit_log under the app role is fail-closed
        (permission denied — 0030 revoked the grant from tendersrb_app).
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", _TEST_URL)
    tid = uuid.uuid4()
    actor = f"op-{uuid.uuid4().hex}@example.rs"

    sup_setup = create_async_engine(superuser_url, future=True)
    try:
        async with sup_setup.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status) "
                    "VALUES (:id, 'RS', 'lite', 'active')"
                ),
                {"id": tid},
            )
    finally:
        await sup_setup.dispose()

    app_engine = create_async_engine(_TEST_URL, pool_pre_ping=True, future=True)
    maker = async_sessionmaker(app_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        # Write under the app role — only via SECURITY DEFINER (without GUC).
        async with maker() as session:
            await svc.grant_access(
                session,
                admin_uid=str(uuid.uuid4()),
                tenant_id=str(tid),
                days=30,
                actor_email=actor,
                target_user=None,
                ip="203.0.113.9",
                before_until=None,
            )

        # Read under the app role — sees the written row (not fail-closed 0).
        async with maker() as session:
            rows, total = await svc.list_audit(session, page=1, page_size=1000)
            await session.commit()
        mine = [r for r in rows if r.actor_email == actor]
        assert total >= 1
        assert len(mine) == 1, "list_audit must return the row via SECURITY DEFINER"
        assert mine[0].action == "grant_access"
        assert mine[0].target_tenant_id == str(tid)
        assert mine[0].details.get("days") == 30
        assert mine[0].ip == "203.0.113.9"

        # Direct SELECT from the table under the app role — fail-closed (permission denied).
        async with maker() as session:
            with pytest.raises(DBAPIError, match="permission denied"):
                await session.execute(text("SELECT * FROM admin_audit_log LIMIT 1"))
                await session.commit()
    finally:
        await app_engine.dispose()
        sup_clean = create_async_engine(superuser_url, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text("DELETE FROM admin_audit_log WHERE actor_email = :a"),
                    {"a": actor},
                )
                await conn.execute(
                    text("DELETE FROM tenants WHERE id = :id"), {"id": tid}
                )
        finally:
            await sup_clean.dispose()
