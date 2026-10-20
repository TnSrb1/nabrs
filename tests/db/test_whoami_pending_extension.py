"""Integration: the banner “the request for an extension is waiting” — only for an extension request.

``deps._WHOAMI_SQL`` sets ``whoami_has_pending`` by ANY pending request of the tenant.
A request for a plan (``upgrade_request:<plan>``) is also pending — the banner “extension waiting” lied. We run the REAL SQL under the app role (FORCE RLS), with the GUC of the tenant: two tenants — one with a pending ``upgrade_request:pro`` (the flag False), the other with a pending ``extension_request`` (the flag True).

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.deps import _WHOAMI_SQL
from src.api.repo import set_tenant

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция баннера продления пропускается",
)


@pytest_asyncio.fixture
async def tenants():
    sup = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    app = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    made = []

    async def make(comment: str) -> tuple[str, str]:
        tid, uid, rid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        async with sup.begin() as c:
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
                    "VALUES (:id, :tid, 'ru', 'user')"
                ),
                {"id": uid, "tid": tid},
            )
            await c.execute(
                text(
                    "INSERT INTO trial_requests (id, tenant_id, status, comment) "
                    "VALUES (:id, :tid, 'pending', :c)"
                ),
                {"id": rid, "tid": tid, "c": comment},
            )
        made.append((tid, uid, rid))
        return str(tid), str(uid)

    try:
        yield make, app
    finally:
        async with sup.begin() as c:
            for tid, uid, rid in made:
                await c.execute(
                    text("DELETE FROM trial_requests WHERE id = :r"), {"r": rid}
                )
                await c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
                await c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await sup.dispose()
        await app.dispose()


async def _has_pending(app, tid: str, uid: str) -> bool:
    async with app.begin() as c:
        await set_tenant(c, tid)
        row = (await c.execute(_WHOAMI_SQL, {"uid": uid, "tid": tid})).first()
    return bool(row.whoami_has_pending)


@pytest.mark.asyncio
async def test_upgrade_request_does_not_raise_extension_banner(tenants):
    make, app = tenants
    tid, uid = await make("upgrade_request:pro")
    assert await _has_pending(app, tid, uid) is False


@pytest.mark.asyncio
async def test_extension_request_raises_banner(tenants):
    make, app = tenants
    tid, uid = await make("extension_request")
    assert await _has_pending(app, tid, uid) is True
