"""Integration: a channel linked AFTER the template reaches the delivery.

The onboarding of ``/app`` — “template → channel”: a new client has no live channel, and the template is saved with ``delivery_channels=[]``. Earlier the Telegram/email binding did not touch the profiles, matching skipped such a profile → the tenders never arrived. Now the binding calls
:func:`src.domain.profile_channels.adopt_channel_into_empty_profiles`.

‼️ The bot and the API go under ``tendersrb_app`` with FORCE RLS — therefore the binding here
goes through the REAL ``bind_channel_by_token``/``confirm_email`` under the app role
(``TEST_RLS_DATABASE_URL``), and the delivery — through the real
``RoutingWorker.route_tender`` under the superuser (as routing in production).
The UPDATE under RLS is not proved on mocks (``.claude/rules/tests-known-defects.md``).

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production) —
without it a skip, as the other ``tests/db`` integrations.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api import security
from src.api.cabinet.channel_service import _email_confirm_namespace, confirm_email
from src.bots.telegram import bind_channel_by_token
from src.db.session import set_current_tenant
from src.workers.routing import RoutingWorker

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция привязки канала пропускается",
)

NOW = datetime.now(timezone.utc)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def world():
    """Two tenants with templates without channels; the channels are bound by the test."""
    su = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    app = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    key = f"onbch-{uuid.uuid4().hex[:8]}"
    created: dict[str, list] = {"tenants": [], "users": [], "tenders": []}

    async with su.begin() as c:
        await c.execute(
            text(
                "INSERT INTO sources (source_key, enabled, enabled_at, baseline_at, "
                "country_codes) VALUES (:k, true, :ea, :ba, ARRAY['RS']::char(2)[])"
            ),
            {"k": key, "ea": NOW - timedelta(days=60), "ba": EPOCH},
        )

    class W:
        source_key = key

        async def tenant(self, *, status="trial", tier="lite"):
            tid, uid = uuid.uuid4(), uuid.uuid4()
            async with su.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO tenants (id, country_code, tier, status) "
                        "VALUES (:id, 'RS', :tier, :st)"
                    ),
                    {"id": tid, "tier": tier, "st": status},
                )
                await c.execute(
                    text(
                        "INSERT INTO users (id, tenant_id, locale, role) "
                        "VALUES (:id, :tid, 'ru', 'admin')"
                    ),
                    {"id": uid, "tid": tid},
                )
            created["tenants"].append(tid)
            created["users"].append(uid)
            return tid, uid

        async def profile(self, tid, *, channels=(), deleted=False):
            pid = uuid.uuid4()
            async with su.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO profiles (id, tenant_id, country_code, is_active, "
                        "match_all, enabled_source_keys, delivery_channels, "
                        "ai_analysis_level, created_at, deleted_at) VALUES "
                        "(:id, :tid, 'RS', true, true, :keys, :ch, 'none', :ca, :del)"
                    ),
                    {
                        "id": pid,
                        "tid": tid,
                        "keys": [key],
                        "ch": list(channels),
                        "ca": NOW - timedelta(days=1),
                        "del": NOW if deleted else None,
                    },
                )
            return pid

        async def channels_of(self, pid):
            async with su.connect() as c:
                return (
                    await c.execute(
                        text("SELECT delivery_channels FROM profiles WHERE id = :p"),
                        {"p": pid},
                    )
                ).scalar_one()

        async def token(self, uid, token_hash):
            async with su.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO auth_tokens (user_id, purpose, token_hash, "
                        "expires_at) VALUES (:u, 'channel_bind', :h, :exp)"
                    ),
                    {"u": uid, "h": token_hash, "exp": NOW + timedelta(minutes=15)},
                )

        async def bind_telegram(self, uid, chat_id=777001) -> str:
            raw = uuid.uuid4().hex
            await self.token(uid, security.hash_token(raw))
            async with AsyncSession(app, expire_on_commit=False) as s:
                return await bind_channel_by_token(
                    s, raw_token=raw, telegram_id=chat_id
                )

        async def confirm_email(self, tid, uid, address="klijent@example.rs") -> bool:
            async with su.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO user_channels (user_id, channel, external_id, "
                        "status) VALUES (:u, 'email', :a, 'pending')"
                    ),
                    {"u": uid, "a": address},
                )
            raw = uuid.uuid4().hex
            await self.token(
                uid, security.hash_token(_email_confirm_namespace(address) + raw)
            )
            async with AsyncSession(app, expire_on_commit=False) as s:
                await set_current_tenant(s, tid)
                return await confirm_email(
                    s, user_id=str(uid), tenant_id=str(tid), raw_token=raw
                )

        async def route_new_tender(self):
            tender_id = uuid.uuid4()
            async with su.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                        "country_code, status, deadline_at, first_seen_at, "
                        "title_normalized) VALUES (:id, :k, :uid, 'RS', 'active', "
                        ":dl, :fs, 'nabavka')"
                    ),
                    {
                        "id": tender_id,
                        "k": key,
                        "uid": tender_id.hex,
                        "dl": NOW + timedelta(days=20),
                        "fs": NOW,
                    },
                )
            created["tenders"].append(tender_id)
            worker = RoutingWorker(
                session_factory=async_sessionmaker(
                    su, class_=AsyncSession, expire_on_commit=False
                )
            )
            await worker.route_tender(
                {"tender_id": str(tender_id), "change_kind": "INSERT"}
            )
            async with su.connect() as c:
                rows = await c.execute(
                    text(
                        "SELECT profile_id, channel FROM delivery_outbox "
                        "WHERE tender_id = :t"
                    ),
                    {"t": tender_id},
                )
                return {(r.profile_id, r.channel) for r in rows}

    try:
        yield W()
    finally:
        async with su.begin() as c:
            await c.execute(
                text("DELETE FROM delivery_outbox WHERE tender_id = ANY(:t)"),
                {"t": created["tenders"]},
            )
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:t)"),
                {"t": created["tenders"]},
            )
            await c.execute(
                text("DELETE FROM profiles WHERE tenant_id = ANY(:t)"),
                {"t": created["tenants"]},
            )
            for tbl in ("auth_tokens", "user_channels"):
                await c.execute(
                    text(f"DELETE FROM {tbl} WHERE user_id = ANY(:u)"),
                    {"u": created["users"]},
                )
            await c.execute(
                text("DELETE FROM users WHERE id = ANY(:u)"), {"u": created["users"]}
            )
            await c.execute(
                text("DELETE FROM tenants WHERE id = ANY(:t)"),
                {"t": created["tenants"]},
            )
            await c.execute(
                text("DELETE FROM sources WHERE source_key = :k"), {"k": key}
            )
        await su.dispose()
        await app.dispose()


@pytest.mark.asyncio
async def test_template_then_telegram_then_tender_is_delivered(world):
    """The onboarding scenario: registration → a template without a channel → Telegram → the tender arrived."""
    tid, uid = await world.tenant()
    empty = await world.profile(tid)

    # BEFORE linking — there is no channel: the tender does not go into the messenger, but the cabinet sees it
    # (third channel, owner 18.10): a web_only row, not telegram/email.
    assert await world.route_new_tender() == {(empty, "web")}

    assert await world.bind_telegram(uid) == "bound"
    assert await world.channels_of(empty) == ["telegram"]
    assert await world.route_new_tender() == {(empty, "telegram")}


@pytest.mark.asyncio
async def test_bind_touches_only_empty_live_profiles_of_own_tenant(world):
    tid, uid = await world.tenant()
    empty = await world.profile(tid)
    chosen = await world.profile(tid, channels=["email"])  # the client’s choice
    deleted = await world.profile(tid, deleted=True)
    other_tid, _ = await world.tenant()
    foreign = await world.profile(other_tid)

    assert await world.bind_telegram(uid) == "bound"

    assert await world.channels_of(empty) == ["telegram"]
    assert await world.channels_of(chosen) == ["email"]
    assert await world.channels_of(deleted) == []
    assert await world.channels_of(foreign) == []


@pytest.mark.asyncio
async def test_lite_tenant_gets_no_telegram_in_template(world):
    """Lite = email only (tiers.gate_profile §3): we do not write Telegram into the template."""
    tid, uid = await world.tenant(status="active", tier="lite")
    empty = await world.profile(tid)

    assert await world.bind_telegram(uid) == "bound"
    assert await world.channels_of(empty) == []


@pytest.mark.asyncio
async def test_email_confirm_adopts_email_into_empty_profiles(world):
    tid, uid = await world.tenant(status="active", tier="lite")
    empty = await world.profile(tid)
    chosen = await world.profile(tid, channels=["telegram"])

    assert await world.confirm_email(tid, uid) is True

    assert await world.channels_of(empty) == ["email"]
    assert await world.channels_of(chosen) == ["telegram"]
    # for chosen (telegram configured but not connected) — only a row for the cabinet
    assert await world.route_new_tender() == {(empty, "email"), (chosen, "web")}
