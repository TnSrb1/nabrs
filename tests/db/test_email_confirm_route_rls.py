"""The confirmation of the email channel THROUGH THE ROUTE under ``tendersrb_app`` (FORCE RLS).

The bug (audit 07.10, UX-01): ``GET /cabinet/email/confirm`` answered 500 after
the binding was ALREADY written. ``confirm_email`` does ``session.commit()``, while
``set_tenant`` — ``set_config(..., true)`` (SET LOCAL) — lives until the end of the
transaction; the next read in the same session (``email_channel_state``) went without
the tenant, the ``user_channels`` policy cast ``''`` to uuid:
``InvalidTextRepresentationError: invalid input syntax for type uuid: ""``.

It is not reproduced on the session mocks — a real SQL under the app role is needed.
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
from src.api.cabinet.channel_service import _email_confirm_namespace
from src.api.config import CabinetSettings
from src.api.main import app

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — роут подтверждения email пропускается",
)

SECRET = "t" * 32
ADDRESS = "buyer@example.com"


def _seed() -> dict:
    """A tenant + a user + a pending channel + a live confirmation token (by the superuser)."""

    async def go() -> dict:
        su = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
        tid, uid = uuid.uuid4(), uuid.uuid4()
        raw = security.generate_token()
        async with su.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status) "
                    "VALUES (:id, 'RS', 'lite', 'trial')"
                ),
                {"id": tid},
            )
            await c.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role) "
                    "VALUES (:id, :tid, 'ru', 'admin')"
                ),
                {"id": uid, "tid": tid},
            )
            await c.execute(
                text(
                    "INSERT INTO user_channels (user_id, channel, external_id, "
                    "status, linked_at) VALUES (:u, 'email', :a, 'pending', now())"
                ),
                {"u": uid, "a": ADDRESS},
            )
            await c.execute(
                text(
                    "INSERT INTO auth_tokens (user_id, purpose, token_hash, "
                    "expires_at) VALUES (:u, 'channel_bind', :h, :e)"
                ),
                {
                    "u": uid,
                    "h": security.hash_token(_email_confirm_namespace(ADDRESS) + raw),
                    "e": datetime.now(timezone.utc) + timedelta(hours=1),
                },
            )
        await su.dispose()
        return {"tid": str(tid), "uid": str(uid), "raw": raw}

    return asyncio.run(go())


def _channel_status(uid: str) -> str | None:
    async def go():
        su = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
        async with su.begin() as c:
            row = (
                await c.execute(
                    text("SELECT status FROM user_channels WHERE user_id = :u"),
                    {"u": uid},
                )
            ).first()
        await su.dispose()
        return row.status if row else None

    return asyncio.run(go())


@pytest.fixture
def client():
    app_engine = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs", CABINET_SECRET_KEY=SECRET
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: maker
    app.dependency_overrides[deps.get_settings] = lambda: settings
    # raise_server_exceptions=False: what the browser will see (the 500 page) is of interest.
    yield TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _cookie(world: dict) -> dict:
    token = security.sign_session(world["uid"], world["tid"], SECRET, locale="ru")
    return {deps.SESSION_COOKIE: token}


def test_confirm_email_route_returns_success_page_not_500(client):
    world = _seed()
    resp = client.get(
        "/cabinet/email/confirm",
        params={"token": world["raw"]},
        cookies=_cookie(world),
        headers={"accept": "text/html"},
    )
    # The result — 303 to the hub «Moj kabinet» (owner 10.10) with the flash key of the catalog.
    assert resp.status_code == 303, resp.text[:300]
    assert (
        resp.headers["location"] == "/cabinet/account?ch_notice=emp_confirm_ok#kanali"
    )
    # The channel is really confirmed, not “written half-way”.
    assert _channel_status(world["uid"]) == "active"
    # The hub under the REAL RLS (tendersrb_app, FORCE) renders, rather than a 500; the expectation
    # is a literal from the fact (the key emp_confirm_ok, ru): not via t().
    page = client.get(
        resp.headers["location"],
        cookies=_cookie(world),
        headers={"accept": "text/html"},
    )
    assert page.status_code == 200, page.text[:300]
    assert "Адрес подтверждён — теперь тендеры приходят письмами." in page.text
