"""Integration: the reminder letter “a template exists, no channel” (channel_dead_notify).

The real SQL against a throwaway PG under the superuser (the worker goes privileged,
as in production: ``SELECT current_user`` = ``tendersrb``). The expectations — literals.

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.emailer import MockEmailSender
from src.workers.channel_dead_notify import ChannelDeadNotifyWorker

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="TEST_RLS_DATABASE_URL не задан — DB-тест nudge пропущен"
)

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def env():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    maker = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    created: list[uuid.UUID] = []

    @asynccontextmanager
    async def factory():
        async with maker() as s:
            yield s

    async def make(
        *,
        age_h=30,
        channel=None,
        channel_status="active",
        external_id="424242",
        deleted=False,
        access_until=None,
        profile_active=True,
        locale="sr",
    ):
        tid, uid = uuid.uuid4(), uuid.uuid4()
        created.append(tid)
        async with eng.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status, access_until) "
                    "VALUES (:t, 'RS', 'lite', 'trial', :au)"
                ),
                {"t": tid, "au": access_until},
            )
            await c.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, locale, role, deleted_at) "
                    "VALUES (:u, :t, :e, :l, 'admin', :d)"
                ),
                {
                    "u": uid,
                    "t": tid,
                    "e": f"{uid.hex[:8]}@example.rs",
                    "l": locale,
                    "d": NOW if deleted else None,
                },
            )
            await c.execute(
                text(
                    "INSERT INTO profiles (tenant_id, name, is_active, created_at) "
                    "VALUES (:t, 'Šablon', :a, :ca)"
                ),
                {"t": tid, "a": profile_active, "ca": NOW - timedelta(hours=age_h)},
            )
            if channel:
                await c.execute(
                    text(
                        "INSERT INTO user_channels (user_id, channel, external_id, status) "
                        "VALUES (:u, :ch, :x, :st)"
                    ),
                    {"u": uid, "ch": channel, "x": external_id, "st": channel_status},
                )
        return f"{uid.hex[:8]}@example.rs"

    yield make, factory

    async with eng.begin() as c:
        for tid in created:
            await c.execute(
                text("DELETE FROM channel_dead_notice_log WHERE tenant_id = :t"),
                {"t": tid},
            )
            await c.execute(
                text(
                    "DELETE FROM user_channels WHERE user_id IN "
                    "(SELECT id FROM users WHERE tenant_id = :t)"
                ),
                {"t": tid},
            )
            await c.execute(
                text("DELETE FROM profiles WHERE tenant_id = :t"), {"t": tid}
            )
            await c.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": tid})
            await c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
    await eng.dispose()


def _worker(factory):
    mail = MockEmailSender()
    return ChannelDeadNotifyWorker(email_sender=mail, session_factory=factory), mail


async def _mails_for(make, factory, **kw) -> list:
    """Letters to our tenant after TWO passes of the worker (the second — idempotency)."""
    email = await make(**kw)
    w, mail = _worker(factory)
    await w.poll_nudges_once()
    await w.poll_nudges_once()
    return [m for m in mail.outbox if m.to == email]


@pytest.mark.asyncio
async def test_sends_exactly_once_per_tenant_in_sr(env):
    make, factory = env
    got = await _mails_for(make, factory)
    assert len(got) == 1  # two passes — one letter
    assert got[0].subject == "TenderSRB — povežite kanal da biste primali tendere"
    assert "@nabrs_bot" in got[0].text_body
    assert "„Moj kabinet“ → „Kanali“" in got[0].text_body
    assert "https://nab.rs/cabinet/account#kanali" in got[0].text_body


@pytest.mark.asyncio
async def test_not_sent_if_template_younger_than_24h(env):
    make, factory = env
    assert await _mails_for(make, factory, age_h=2) == []


@pytest.mark.asyncio
async def test_not_sent_to_old_abandoned_tenants(env):
    make, factory = env
    assert await _mails_for(make, factory, age_h=24 * 30) == []


@pytest.mark.asyncio
async def test_not_sent_with_live_channel(env):
    make, factory = env
    assert await _mails_for(make, factory, channel="telegram") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_status", "external_id"),
    [("blocked", "424242"), ("revoked", "424242"), ("active", "")],
)
async def test_not_sent_when_channel_died(env, channel_status, external_id):
    # A channel was set up, but it dropped off: the letter about it is sent by poll_once (dead-channel),
    # and the nudge “the channel is not yet connected” is wrong — we write zero.
    make, factory = env
    assert (
        await _mails_for(
            make,
            factory,
            channel="telegram",
            channel_status=channel_status,
            external_id=external_id,
        )
        == []
    )


@pytest.mark.asyncio
async def test_not_sent_to_deleted_user(env):
    make, factory = env
    assert await _mails_for(make, factory, deleted=True) == []


@pytest.mark.asyncio
async def test_not_sent_when_access_expired(env):
    make, factory = env
    got = await _mails_for(make, factory, access_until=NOW - timedelta(days=1))
    assert got == []


@pytest.mark.asyncio
async def test_not_sent_without_active_template(env):
    make, factory = env
    assert await _mails_for(make, factory, profile_active=False) == []
