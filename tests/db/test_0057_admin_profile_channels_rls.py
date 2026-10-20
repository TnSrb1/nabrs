"""RLS regression of migration 0057 under the tendersrb_app role (FORCE RLS).

The admin reads ``profiles`` and ``user_channels`` of a foreign tenant only through
SECURITY DEFINER ``app_admin_list_profiles`` / ``app_admin_user_channels``. A direct
SELECT from the app role would return nothing — so we check the service call under
``TEST_RLS_DATABASE_URL`` with the GUC of ANOTHER tenant (as an admin session).

Skipped without TEST_RLS_DATABASE_URL (gate spins up a throwaway PG itself).
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.admin import admin_service as admin_svc

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPERUSER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — RLS-интеграция пропускается",
)


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def sessionmaker_():
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


@pytest_asyncio.fixture
async def seeded():
    """Two tenants: A has a user with a telegram channel and a template with a channel;
    B has a template with an empty delivery_channels. Setup as superuser."""
    tid_a, tid_b = uuid.uuid4(), uuid.uuid4()
    uid_a = uuid.uuid4()
    prof_a, prof_b = uuid.uuid4(), uuid.uuid4()
    sup = create_async_engine(SUPERUSER_URL, poolclass=NullPool, future=True)
    try:
        async with sup.begin() as conn:
            for tid in (tid_a, tid_b):
                await conn.execute(
                    text(
                        "INSERT INTO tenants (id, country_code, tier, status) "
                        "VALUES (:id, 'RS', 'lite', 'trial')"
                    ),
                    {"id": tid},
                )
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role, email) "
                    "VALUES (:id, :tid, 'ru', 'user', :email)"
                ),
                {"id": uid_a, "tid": tid_a, "email": f"{uid_a}@t.rs"},
            )
            await conn.execute(
                text(
                    "INSERT INTO profiles (id, tenant_id, name, is_active, delivery_channels) "
                    "VALUES (:id, :tid, 'с каналом', true, ARRAY['telegram'])"
                ),
                {"id": prof_a, "tid": tid_a},
            )
            await conn.execute(
                text(
                    "INSERT INTO profiles (id, tenant_id, name, is_active, delivery_channels) "
                    "VALUES (:id, :tid, 'чужой без канала', true, '{}')"
                ),
                {"id": prof_b, "tid": tid_b},
            )
            await conn.execute(
                text(
                    "INSERT INTO user_channels (id, user_id, channel, external_id, status, linked_at) "
                    "VALUES (gen_random_uuid(), :uid, 'telegram', 'ext-0057', 'active', "
                    "        TIMESTAMPTZ '2026-10-11 09:55:00+00')"
                ),
                {"uid": uid_a},
            )
    finally:
        await sup.dispose()
    yield tid_a, tid_b, uid_a, prof_a, prof_b
    sup2 = create_async_engine(SUPERUSER_URL, poolclass=NullPool, future=True)
    try:
        async with sup2.begin() as conn:
            await conn.execute(
                text("DELETE FROM user_channels WHERE user_id = :uid"), {"uid": uid_a}
            )
            await conn.execute(
                text("DELETE FROM profiles WHERE tenant_id IN (:a, :b)"),
                {"a": tid_a, "b": tid_b},
            )
            await conn.execute(
                text("DELETE FROM users WHERE tenant_id IN (:a, :b)"),
                {"a": tid_a, "b": tid_b},
            )
            await conn.execute(
                text(
                    "DELETE FROM billing_notification_outbox WHERE tenant_id IN (:a, :b)"
                ),
                {"a": tid_a, "b": tid_b},
            )
            await conn.execute(
                text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                {"a": tid_a, "b": tid_b},
            )
    finally:
        await sup2.dispose()


@pytest.mark.asyncio
async def test_list_profiles_under_app_role_returns_dates_and_channels(
    sessionmaker_, seeded
):
    tid_a, tid_b, _uid_a, prof_a, prof_b = seeded
    async with sessionmaker_() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, true)"),
            {"tid": str(tid_a)},
        )
        profiles = await admin_svc.list_profiles(session)
        await session.commit()

    by_id = {p.id: p for p in profiles}
    # Cross-tenant via SECURITY DEFINER: both templates are visible, although the GUC is tenant A.
    assert str(prof_a) in by_id and str(prof_b) in by_id
    a = by_id[str(prof_a)]
    assert a.created_at is not None
    assert a.delivery_channels == ["telegram"]
    b = by_id[str(prof_b)]
    assert b.created_at is not None
    assert b.delivery_channels == []


@pytest.mark.asyncio
async def test_list_profiles_tenant_filter_excludes_foreign_template(
    sessionmaker_, seeded
):
    """A mutation of the tenant_id filter in list_profiles turns this test red: without it
    a template of another tenant gets into the card."""
    tid_a, tid_b, _uid_a, prof_a, prof_b = seeded
    async with sessionmaker_() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, true)"),
            {"tid": str(tid_a)},
        )
        scoped = await admin_svc.list_profiles(session, tenant_id=str(tid_a))
        await session.commit()

    ids = {p.id for p in scoped}
    assert str(prof_a) in ids
    assert str(prof_b) not in ids


@pytest.mark.asyncio
async def test_list_user_channels_under_app_role_returns_telegram(
    sessionmaker_, seeded
):
    tid_a, _tid_b, uid_a, _pa, _pb = seeded
    async with sessionmaker_() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, true)"),
            {"tid": str(tid_a)},
        )
        channels = await admin_svc.list_user_channels(session, str(uid_a))
        await session.commit()

    assert len(channels) == 1
    ch = channels[0]
    assert ch.channel == "telegram"
    assert ch.status == "active"
    assert ch.linked_at is not None
