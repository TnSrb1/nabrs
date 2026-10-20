"""“Extend access” in the admin EXTENDS, it does not set the term.

Incident 04.08.2026 (visible in admin_audit_log): a tenant with an active subscription and
access until 18.08 — the operator pressed “Extend by 3 days” — ``grant_access`` wrote
``now() + 3d``, and the access SHRANK by 11 days. At the same time the client saw on
/cabinet/subscription “2 days of access left” next to "next renewal
18.08".

Three levels:
- the service (``admin_service.grant_access``): which term goes into
  ``app_admin_grant_access`` and what goes into the audit;
- the route (``POST /admin/users/{uid}/grant``): the current term actually reaches the service as the renewal base — without this wiring the fix is dead;
- the markup (``GET /admin/users/{uid}``): the operator sees the current term next to
  the form, otherwise the shrinkage has nothing to alert them.

Expectations are taken from the FACT (the parameter of the SQL call / the response code of the route), not from the model of the code. Without DB/network: FakeSession (ADR-013).
"""

from __future__ import annotations

import json
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
_OPERATOR_EMAIL = "admin@example.com"
_ADMIN_SESSION = {"uid": _UID, "tid": _TID}

# Tolerance for the test’s run time: the term is computed from datetime.now() inside the
# service, so we compare to the precision of a minute, not of a microsecond.
_TOLERANCE = timedelta(minutes=1)


def _operator_settings() -> CabinetSettings:
    return CabinetSettings(CABINET_ADMIN_EMAILS=_OPERATOR_EMAIL)


def teardown_function():
    app.dependency_overrides.clear()


def _grant_params(fs: FakeSession) -> dict:
    """The parameters of the single call of app_admin_grant_access."""
    calls = [params for sql, params in fs.executed if "app_admin_grant_access" in sql]
    assert len(calls) == 1, "the access grant must be exactly one"
    return calls[0] or {}


def _audit_details(fs: FakeSession) -> dict:
    calls = [params for sql, params in fs.executed if "app_admin_write_audit" in sql]
    assert len(calls) == 1
    return json.loads((calls[0] or {})["details"])


def _user_row(access_until):
    return SimpleNamespace(
        id=_UID,
        email=_OPERATOR_EMAIL,
        created_at=datetime.now(timezone.utc) - timedelta(days=60),
        tenant_id=_TID,
        tier="pro",
        access_until=access_until,
        profile_count=1,
        deleted_at=None,
    )


# --------------------------------------------------------------------------- #
# Service: the renewal base
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_grant_extends_live_access_instead_of_resetting_it():
    """before=+11d, days=3 → after ≈ +14d (NOT +3d): the figures of the 04.08 incident."""
    now = datetime.now(timezone.utc)
    before = now + timedelta(days=11)
    fs = FakeSession(results=[FakeResult(), FakeResult()])
    await svc.grant_access(
        fs,
        admin_uid=_UID,
        tenant_id=_TID,
        days=3,
        before_until=before,
        actor_email=_OPERATOR_EMAIL,
        target_user=_UID,
    )
    until = _grant_params(fs)["until"]
    assert abs(until - (before + timedelta(days=3))) < _TOLERANCE
    assert until > before, "the renewal must strictly increase the term"
    assert until - now > timedelta(days=13), "the term must not collapse into now+3d"


@pytest.mark.asyncio
async def test_grant_from_now_when_access_expired():
    """Expired access: base — now (nothing to extend, we grant anew)."""
    now = datetime.now(timezone.utc)
    fs = FakeSession(results=[FakeResult()])
    await svc.grant_access(
        fs,
        admin_uid=_UID,
        tenant_id=_TID,
        days=30,
        before_until=now - timedelta(days=5),
    )
    until = _grant_params(fs)["until"]
    assert abs(until - (now + timedelta(days=30))) < _TOLERANCE


@pytest.mark.asyncio
async def test_grant_from_now_when_no_access_yet():
    """The term is not granted (NULL) — base now: a tenant pending_verification is created
    precisely with access_until IS NULL, and the approval must give a finite term."""
    now = datetime.now(timezone.utc)
    fs = FakeSession(results=[FakeResult()])
    await svc.grant_access(
        fs, admin_uid=_UID, tenant_id=_TID, days=14, before_until=None
    )
    until = _grant_params(fs)["until"]
    assert abs(until - (now + timedelta(days=14))) < _TOLERANCE


@pytest.mark.asyncio
async def test_audit_records_extension_not_shrink():
    """In the audit, after is strictly more than before — the same thing the owner catches the incident by."""
    before = datetime.now(timezone.utc) + timedelta(days=11)
    fs = FakeSession(results=[FakeResult(), FakeResult()])
    await svc.grant_access(
        fs,
        admin_uid=_UID,
        tenant_id=_TID,
        days=3,
        before_until=before,
        actor_email=_OPERATOR_EMAIL,
    )
    details = _audit_details(fs)
    assert details["days"] == 3
    assert details["before_access_until"] == before.isoformat()
    after = datetime.fromisoformat(details["after_access_until"])
    assert after > before
    # The audit and the write into tenants — ONE value (not two different computations).
    assert after == _grant_params(fs)["until"]


def test_grant_access_requires_before_until():
    """The base cannot be “forgotten”: the argument is mandatory, there is no default.

    With a default value the defect would return silently — a new caller would not
    pass the term, and the renewal would again become setting now()+days.
    """
    import inspect

    param = inspect.signature(svc.grant_access).parameters["before_until"]
    assert param.default is inspect.Parameter.empty


# --------------------------------------------------------------------------- #
# Route: the current term reaches the service
# --------------------------------------------------------------------------- #


def test_route_grant_passes_current_access_until_as_base():
    now = datetime.now(timezone.utc)
    before = now + timedelta(days=11)
    fs = FakeSession(
        results=[
            FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),  # require_admin
            FakeResult(items=[_user_row(before)]),  # get_user
            FakeResult(),  # app_admin_grant_access
        ]
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/admin/users/{_UID}/grant", data={"days": "3"})

    assert resp.status_code == 303
    until = _grant_params(fs)["until"]
    assert abs(until - (before + timedelta(days=3))) < _TOLERANCE
    assert until > before


# --------------------------------------------------------------------------- #
# Markup: the operator sees the initial state
# --------------------------------------------------------------------------- #


def _render_detail(access_until) -> str:
    fs = FakeSession(
        results=[
            FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),  # require_admin
            FakeResult(items=[_user_row(access_until)]),  # get_user
            FakeResult(scalar=7),  # delivered_count
        ]
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    client = TestClient(app, follow_redirects=False)
    resp = client.get(f"/admin/users/{_UID}")
    assert resp.status_code == 200
    return resp.text


def test_detail_shows_current_term_next_to_grant_form():
    until = datetime.now(timezone.utc) + timedelta(days=11)
    html = _render_detail(until)
    form_block = html.split("Продлить доступ", 1)[1].split("</form>", 1)[0]
    assert until.strftime("%d.%m.%Y") in form_block, (
        "next to the renewal form there must be the current term — otherwise "
        "the operator does not see exactly what he is changing"
    )
    assert "не сокращает" in form_block


def test_detail_says_when_no_term_granted():
    form_block = (
        _render_detail(None).split("Продлить доступ", 1)[1].split("</form>", 1)[0]
    )
    assert "срок не выдан" in form_block


@pytest.mark.asyncio
async def test_grant_activates_pending_verification_tenant():
    """Approval of pending_verification: the term is granted AND the status is moved to trial —
    otherwise matching (status IN ('trial','active')) would keep silent."""
    fs = FakeSession(results=[FakeResult(), FakeResult()])
    await svc.grant_access(
        fs, admin_uid=_UID, tenant_id=_TID, days=14, before_until=None
    )
    updates = [
        (sql, p) for sql, p in fs.executed if "status = 'pending_verification'" in sql
    ]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "SET status = 'trial'" in sql and params == {"tid": _TID}
    # order: first the term, then the status change (it is gated by access_until > now())
    order = [sql for sql, _ in fs.executed]
    assert order.index(updates[0][0]) > next(
        i for i, q in enumerate(order) if "app_admin_grant_access" in q
    )
