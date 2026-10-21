"""The free extension after the end of access — THROUGH THE ROUTES under ``tendersrb_app``.

The owner’s decision 10.10.2026: a frozen tenant (``access_until`` in the past) enters the cabinet, sees the banner «Pretplata je završena», sends an extension request; a repeat while a request is pending does not multiply a duplicate; the approval extends MONOTONICALLY and by no more than 30 days (whatever the operator chose). RLS FORCE — the real SQL, not mocks.
Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api import deps, security
from src.api.admin import admin_service
from src.api.config import CabinetSettings
from src.api.main import app

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — flow продления пропускается",
)

SECRET = "t" * 32
BANNER_TITLE = "Pretplata je završena"
BANNER_BTN = "Produži besplatno"
EXPIRED = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _run(coro_fn):
    async def go():
        su = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
        try:
            async with su.begin() as c:
                return await coro_fn(c)
        finally:
            await su.dispose()

    return asyncio.run(go())


def _seed(access_until=EXPIRED) -> dict:
    tid, uid = uuid.uuid4(), uuid.uuid4()

    async def go(c):
        await c.execute(
            text(
                "INSERT INTO tenants (id, country_code, tier, status, access_until) "
                "VALUES (:id, 'RS', 'lite', 'trial', :au)"
            ),
            {"id": tid, "au": access_until},
        )
        await c.execute(
            text(
                "INSERT INTO users (id, tenant_id, locale, role, email) "
                "VALUES (:id, :tid, 'sr', 'admin', :em)"
            ),
            {"id": uid, "tid": tid, "em": f"{uid}@example.com"},
        )

    _run(go)
    return {"tid": str(tid), "uid": str(uid)}


def _requests(tid: str) -> list:
    async def go(c):
        rows = await c.execute(
            text(
                "SELECT id, status, comment, days FROM trial_requests "
                "WHERE tenant_id = :t ORDER BY requested_at"
            ),
            {"t": tid},
        )
        return rows.all()

    return _run(go)


def _access_until(tid: str):
    async def go(c):
        return (
            await c.execute(
                text("SELECT access_until FROM tenants WHERE id = :t"), {"t": tid}
            )
        ).scalar()

    return _run(go)


def _set_access(tid: str, value) -> None:
    async def go(c):
        await c.execute(
            text("UPDATE tenants SET access_until = :v WHERE id = :t"),
            {"v": value, "t": tid},
        )

    _run(go)


@pytest.fixture
def client():
    app_engine = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs", CABINET_SECRET_KEY=SECRET
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: maker
    app.dependency_overrides[deps.get_settings] = lambda: settings
    yield TestClient(app, follow_redirects=False, raise_server_exceptions=False), maker
    app.dependency_overrides.clear()


def _cookie(world: dict) -> dict:
    token = security.sign_session(world["uid"], world["tid"], SECRET, locale="sr")
    return {deps.SESSION_COOKIE: token}


def _approve(maker, request_id: str, days: int) -> str:
    async def go():
        async with maker() as db:
            return await admin_service.approve_request(
                db, admin_uid="op", request_id=request_id, days=days
            )

    return asyncio.run(go())


def test_frozen_tenant_enters_cabinet_with_banner(client):
    c, _ = client
    world = _seed()
    for path in ("/app", "/app/feed", "/cabinet/account", "/app/my"):
        resp = c.get(path, cookies=_cookie(world))
        assert resp.status_code == 200, path
        assert BANNER_TITLE in resp.text, path
        assert BANNER_BTN in resp.text, path


def test_active_tenant_sees_no_banner(client):
    c, _ = client
    world = _seed(datetime.now(timezone.utc) + timedelta(days=10))
    resp = c.get("/app/feed", cookies=_cookie(world))
    assert resp.status_code == 200
    assert BANNER_TITLE not in resp.text


def test_extension_request_created_once_and_banner_shows_pending(client):
    c, _ = client
    world = _seed()
    first = c.post(
        "/cabinet/trial/request", data={"kind": "extension"}, cookies=_cookie(world)
    )
    assert first.status_code == 303
    second = c.post(
        "/cabinet/trial/request", data={"kind": "extension"}, cookies=_cookie(world)
    )
    assert second.status_code == 303
    rows = _requests(world["tid"])
    assert (
        len(rows) == 1
    ), "A repeat while a request is pending must not multiply a duplicate"
    assert rows[0].status == "pending"
    assert rows[0].comment == "extension_request"
    page = c.get("/app/feed", cookies=_cookie(world))
    assert "Zahtev za produženje je poslat" in page.text
    assert BANNER_BTN not in page.text


def test_approval_is_capped_at_30_days(client):
    c, maker = client
    world = _seed()
    c.post("/cabinet/trial/request", data={"kind": "extension"}, cookies=_cookie(world))
    rid = str(_requests(world["tid"])[0].id)
    before = datetime.now(timezone.utc)
    assert _approve(maker, rid, days=90) == "approved"
    until = _access_until(world["tid"])
    # The base is now (the term 01.01.2026 in the past), not 90 days and not an old term.
    assert (
        before + timedelta(days=30) <= until <= before + timedelta(days=30, minutes=5)
    )
    # The request journal writes EXACTLY what was issued to the client: 30, not the operator’s decision of 90.
    assert _requests(world["tid"])[0].days == 30


def test_approval_is_monotonic_when_access_already_longer(client):
    c, maker = client
    world = _seed()
    c.post("/cabinet/trial/request", data={"kind": "extension"}, cookies=_cookie(world))
    rid = str(_requests(world["tid"])[0].id)
    # While the request was waiting, the term managed to be extended by 20 days by another path.
    far = datetime.now(timezone.utc) + timedelta(days=20)
    _set_access(world["tid"], far)
    _approve(maker, rid, days=90)
    until = _access_until(world["tid"])
    # The operator chose 90, the cap is 30, the count — from the distant term (monotonically), not from now().
    assert until >= far + timedelta(days=30) - timedelta(seconds=1)
    assert until <= far + timedelta(days=30, minutes=1)
    assert _requests(world["tid"])[0].days == 30


def test_banner_disappears_after_approval(client):
    c, maker = client
    world = _seed()
    c.post("/cabinet/trial/request", data={"kind": "extension"}, cookies=_cookie(world))
    rid = str(_requests(world["tid"])[0].id)
    _approve(maker, rid, days=30)
    page = c.get("/app/feed", cookies=_cookie(world))
    assert BANNER_TITLE not in page.text


def test_disabled_user_is_still_locked_out(client):
    c, _ = client
    world = _seed()

    async def go(conn):
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"),
            {"u": world["uid"]},
        )

    _run(go)
    resp = c.get("/app/feed", cookies=_cookie(world))
    assert resp.status_code == 303
    assert resp.headers["location"] == "/plans"
