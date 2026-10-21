"""Integration tests of the RLS isolation of the tenants (VG-01, migrations 0021/0022/0023).

They require TEST_RLS_DATABASE_URL — the connection under the role ``tendersrb_app``
(NOBYPASSRLS) to a DB with the migrations rolled up to head. If the variable is not set,
the tests are skipped (the unit environment, the CI without PG).

The setup/cleanup of data bypass the RLS, therefore they need a SEPARATE connection
of the superuser — TEST_SUPERUSER_DATABASE_URL. By default it falls back to
TEST_RLS_DATABASE_URL: under the app role the setup inserts will not pass, so for a meaningful run both variables must be set.

Run on a one-off DB (NOT on production; the production image — pgvector/pgvector:pg16,
the migration 0021 waits for APP_DB_PASSWORD and a DB named ``tendersrb``):
    TEST_RLS_DATABASE_URL=“postgresql+asyncpg://tendersrb_app:PWD@localhost:5432/tendersrb” \
    TEST_SUPERUSER_DATABASE_URL=“postgresql+asyncpg://tendersrb:SUPERPWD@localhost:5432/tendersrb” \
    pytest tests/db/test_rls_isolation.py -v

The structure of the tests:
  1. no_guc_raises        — without the GUC the access to a tenant table raises DBAPIError.
  2. guc_filters          — GUC tenant A → sees only its own rows, not tenant B’s.
  3. guc_non_leakage      — the GUC is transactional: after commit it is reset.
  4. user_channels_isolation — the join policy 0022: tenant A does not see the channels of B.
  5. email_auth_flow_under_app_role — the regression FIX-AUTH-RLS: signup → verify →
     login (magic-link) under the app role (FORCE RLS) does not fall with
     ``UndefinedObjectError`` (the 0024 SECURITY DEFINER resolvers).
  6. admin_service_cross_tenant_under_app_role — the regression FIX-ADMIN-RLS:
     list_users/get_platform_stats/list_profiles/get_user/
     get_user_delivered_count under the app role see the data of ALL the tenants and do not
     fall (0025 SECURITY DEFINER app_admin_*); grant_access/set_tier write the
     tenants of another tenant without an error. An ordinary (non-admin) SELECT * FROM
     users under the same GUC still sees only its own tenant.
  7. delivery_health_cross_tenant_under_app_role — the regression VG-29.3:
     get_delivery_health under the app role counts the backlog of delivery_outbox and
     the freshness of reminder_log / subscription_reminder_log of ANOTHER tenant via
     SECURITY DEFINER app_admin_delivery_backlog / app_admin_reminder_freshness
     (0026) — an aggregate, not 0 (fail-closed). All three tables are under FORCE RLS.
  8. tender_ai_analyses_isolation_under_app_role — the regression web-ai-1: the read-only
     view of the AI artifacts (get_tender_analyses) under the app role. GUC=tid_a →
     its own profile gives the bid, the profile of a foreign tenant → None (the isolation without
     SECURITY DEFINER); a direct SELECT does not show the analysis of the profile tid_b.
  9. tender_ai_generate_isolation_under_app_role — the regression web-ai-2: the generation
     from the cabinet (generate_artifact) under the app role. GUC=tid_a → the bid for its own profile
     is written (success), a repeat → already (UNIQUE, without doubling), for a foreign
     profile → no_profile WITHOUT a write.
  10. admin_disabled_subscriber_visible_and_reachable_under_app_role — the regression
      0039: after app_admin_disable_subscriber (0037) the target user of ANOTHER tenant
      stays visible in app_admin_list_users/app_admin_get_user (deleted_at not NULL, not filtered out), the card /admin/users/{uid} gives 200 (not 404) with the badge “Disabled” and the button “Restore”; after app_admin_enable_subscriber — deleted_at is NULL again, the card gives the button “Disable”.
  11. bot_trial_decision_under_app_role_extends_access — the regression of 06.08.2026:
      the path of the BOT (``bots.trial_service``) under the app role and WITHOUT a GUC — ``/trials`` and
      the decision on a request work through SECURITY DEFINER (0034/0035/0041), and do not
      fall with ``UndefinedObjectError``; an approval for 3 days to a tenant with access
      still for 11 days EXTENDS it to ~14, rather than cutting it to 3.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api import anti_abuse as aa
from src.api import deps
from src.api.admin import admin_service as admin_svc
from src.api.cabinet import auth_service, routes_channels
from src.api.config import CabinetSettings
from src.api.emailer import MockEmailSender
from src.api.main import app

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — RLS-интеграция пропускается",
)


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def engine():
    """The engine per test + NullPool: the connection must not outlive its own event loop.

    asyncpg binds the connection to the loop on which it was created, and
    pytest-asyncio gives each test ITS OWN function-scoped loop. A module-scoped
    engine cached the connection of the first test in the pool and handed it to the next
    one on an already closed loop → 'Event loop is closed' / 'attached to a different
    loop'. So 8 tests fell silently: only the first one that reached the DB was green.
    NullPool does not let the connection go into the pool at all — it lives inside one test.
    """
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def tenant_ids(engine):
    """A pair of fresh tenants per test: the setup under the superuser (bypasses the RLS).

    A fixture per test, not per module: the tenants are created and deleted in the loop of
    their own test, therefore the connection of the setup does not outlive that loop (see
    ``engine``). As a side effect the tests stop sharing the state of the tenants with each other.
    """
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    sup_engine = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    tid_a = uuid.uuid4()
    tid_b = uuid.uuid4()
    try:
        async with sup_engine.begin() as conn:
            # SET LOCAL tenant so that the policy lets the superuser’s INSERT through
            # (the superuser bypasses the RLS, so SET is not needed, but for completeness)
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status) "
                    "VALUES (:id, 'RS', 'lite', 'trial')"
                ),
                {"id": tid_a},
            )
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status) "
                    "VALUES (:id, 'RS', 'lite', 'trial')"
                ),
                {"id": tid_b},
            )
    finally:
        await sup_engine.dispose()
    yield tid_a, tid_b
    # Cleanup
    sup_engine2 = create_async_engine(superuser_url, future=True)
    try:
        async with sup_engine2.begin() as conn:
            # The queue of payment letters references tenants (FK 0033) and is not cleaned
            # by the tests themselves: without this the DELETE of the fixture fell with
            # ForeignKeyViolationError on ALL the tests that put a letter into the
            # queue (apply_billing_event, approve_request) — a pre-existing
            # debt of the scaffolding, red also on the clean main.
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
        await sup_engine2.dispose()


@pytest.mark.asyncio
async def test_no_guc_raises(sessionmaker_):
    """Without the GUC the access to the table tenants raises DBAPIError (missing GUC).

    asyncpg may return ProgrammingError/InternalError — both are a subclass of DBAPIError.
    """
    async with sessionmaker_() as session:
        with pytest.raises(DBAPIError, match="app.current_tenant_id"):
            await session.execute(text("SELECT * FROM tenants LIMIT 1"))
            await session.commit()


@pytest.mark.asyncio
async def test_guc_filters_tenant_data(sessionmaker_, tenant_ids):
    """GUC tenant A → sees only its own row in tenants, not tenant B’s."""
    tid_a, tid_b = tenant_ids
    async with sessionmaker_() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, true)"),
            {"tid": str(tid_a)},
        )
        result = await session.execute(text("SELECT id FROM tenants"))
        rows = [str(r[0]) for r in result.fetchall()]
        await session.commit()

    assert str(tid_a) in rows
    assert str(tid_b) not in rows, "tenant A must not see the data of tenant B"


@pytest.mark.asyncio
async def test_guc_non_leakage_between_transactions(sessionmaker_):
    """The GUC is transactional: after commit it is reset, the next query falls."""
    async with sessionmaker_() as session:
        # Transaction 1: we set the GUC — all OK
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, true)"),
            {"tid": str(uuid.uuid4())},
        )
        await session.commit()

    # A new transaction (or the same pool): the GUC is already reset → must fall
    async with sessionmaker_() as session:
        with pytest.raises(DBAPIError, match="app.current_tenant_id"):
            await session.execute(text("SELECT * FROM tenants LIMIT 1"))
            await session.commit()


@pytest.mark.asyncio
async def test_user_channels_isolation(sessionmaker_, tenant_ids):
    """user_channels: the join policy 0022 — tenant A sees only its own channel, not B’s.

    Setup: the two channels are inserted by the superuser (bypasses the RLS).
    Assert: under GUC=A the chan_a is visible, not chan_b; under GUC=B the chan_b is visible, not chan_a.
    """
    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    sup_engine = create_async_engine(superuser_url, future=True)
    chan_a = uuid.uuid4()
    chan_b = uuid.uuid4()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    try:
        async with sup_engine.begin() as conn:
            for uid, tid in [(user_a, tid_a), (user_b, tid_b)]:
                await conn.execute(
                    text(
                        "INSERT INTO users (id, tenant_id, locale, role) "
                        "VALUES (:id, :tid, 'ru', 'admin')"
                    ),
                    {"id": uid, "tid": tid},
                )
            for cid, uid, eid in [
                (chan_a, user_a, "tg_a"),
                (chan_b, user_b, "tg_b"),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO user_channels "
                        "(id, user_id, channel, external_id, status) "
                        "VALUES (:id, :uid, 'telegram', :eid, 'active')"
                    ),
                    {"id": cid, "uid": uid, "eid": eid},
                )
    finally:
        await sup_engine.dispose()

    try:
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            result = await session.execute(text("SELECT id FROM user_channels"))
            rows_a = {str(r[0]) for r in result.fetchall()}
            await session.commit()

        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_b)},
            )
            result = await session.execute(text("SELECT id FROM user_channels"))
            rows_b = {str(r[0]) for r in result.fetchall()}
            await session.commit()

        assert str(chan_a) in rows_a, "tenant A must see its own channel"
        assert str(chan_b) not in rows_a, "tenant A must not see the channel B"
        assert str(chan_b) in rows_b, "tenant B must see its own channel"
        assert str(chan_a) not in rows_b, "tenant B must not see the channel A"
    finally:
        sup_engine2 = create_async_engine(superuser_url, future=True)
        try:
            async with sup_engine2.begin() as conn:
                await conn.execute(
                    text("DELETE FROM user_channels WHERE id IN (:a, :b)"),
                    {"a": chan_a, "b": chan_b},
                )
                await conn.execute(
                    text("DELETE FROM users WHERE id IN (:a, :b)"),
                    {"a": user_a, "b": user_b},
                )
        finally:
            await sup_engine2.dispose()


def _extract_token(text_body: str) -> str:
    match = re.search(r"token=([A-Za-z0-9_\-]+)", text_body)
    assert match, f"the token is not found in the letter: {text_body!r}"
    return match.group(1)


@pytest.mark.asyncio
async def test_email_auth_flow_under_app_role(sessionmaker_):
    """The regression FIX-AUTH-RLS: signup → verify_email_token → request_magic_link →
    consume_magic_link under the role ``tendersrb_app`` (FORCE RLS, 0021) does not fall.

    Before 0024 the service read ``users`` by email via ``select(User)`` BEFORE the GUC ``app.current_tenant_id`` was set — tendersrb_app raised ``UndefinedObjectError``
    (asyncpg), which caused the production 500 on ``POST /login``/``POST /signup``.
    Now the SECURITY DEFINER resolvers are used: ``app_resolve_user_for_auth``
    (0024) / ``app_resolve_tenant_id_by_user_id`` (0022) + ``set_current_tenant``.
    """
    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs", CABINET_SECRET_KEY="t"
    )
    email = f"rls-auth-{uuid.uuid4().hex}@example.rs"
    sender = MockEmailSender()
    tenant_uuid: uuid.UUID | None = None

    try:
        async with sessionmaker_() as session:
            result = await auth_service.signup(
                session,
                email=email,
                turnstile_token="ok",
                turnstile=aa.NoopTurnstileVerifier(True),
                email_validator=aa.NoopEmailValidator(
                    deliverable=True, disposable=False, risky=False
                ),
                email_sender=sender,
                settings=settings,
            )
        assert result.outcome == auth_service.TRIAL_STARTED
        # the client (verify) first + to the operator at info@nab.rs
        assert [m.to for m in sender.outbox] == [email, "info@nab.rs"]
        verify_token = _extract_token(sender.outbox[0].text_body)

        async with sessionmaker_() as session:
            verify_sess = await auth_service.verify_email_token(
                session, raw_token=verify_token
            )
        assert (
            verify_sess is not None
        ), "verify_email_token must not return None/fall under the app role"
        tenant_uuid = uuid.UUID(verify_sess["tid"])

        async with sessionmaker_() as session:
            sent = await auth_service.request_magic_link(
                session, email=email, email_sender=sender, settings=settings
            )
        assert sent is True
        assert len(sender.outbox) == 3
        magic_token = _extract_token(sender.outbox[2].text_body)

        async with sessionmaker_() as session:
            login_sess = await auth_service.consume_magic_link(
                session, raw_token=magic_token
            )
        assert (
            login_sess is not None
        ), "consume_magic_link must not return None/fall under the app role"
        assert login_sess["tid"] == str(tenant_uuid)
    finally:
        if tenant_uuid is not None:
            superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
            sup_clean = create_async_engine(superuser_url, future=True)
            try:
                async with sup_clean.begin() as conn:
                    await conn.execute(
                        text(
                            "DELETE FROM auth_tokens WHERE user_id IN "
                            "(SELECT id FROM users WHERE tenant_id = :tid)"
                        ),
                        {"tid": tenant_uuid},
                    )
                    await conn.execute(
                        text(
                            "DELETE FROM anti_abuse_identifiers WHERE tenant_id = :tid"
                        ),
                        {"tid": tenant_uuid},
                    )
                    await conn.execute(
                        text("DELETE FROM users WHERE tenant_id = :tid"),
                        {"tid": tenant_uuid},
                    )
                    await conn.execute(
                        text("DELETE FROM tenants WHERE id = :tid"),
                        {"tid": tenant_uuid},
                    )
            finally:
                await sup_clean.dispose()


@pytest.mark.asyncio
async def test_cabinet_telegram_link_under_app_role(sessionmaker_, tenant_ids):
    """The regression FIX-RLS-ROUTE-CTX: POST /cabinet/telegram/link under the app role.

    Before the fix the route called ``channel_service.has_active_telegram`` (SELECT
    ``user_channels``, the policy 0022) without a preliminary ``set_tenant`` —
    under ``tendersrb_app`` (FORCE RLS, 0021) the GUC is empty → ``UndefinedObjectError``
    → 500. Now the route runs ``repo.set_tenant`` as the first action in the
    db block — the query must pass (200), rather than fall.
    """
    tid_a, _tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    user_id = uuid.uuid4()

    sup_engine = create_async_engine(superuser_url, future=True)
    try:
        async with sup_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role) "
                    "VALUES (:id, :tid, 'ru', 'admin')"
                ),
                {"id": user_id, "tid": tid_a},
            )
    finally:
        await sup_engine.dispose()

    def factory():
        return sessionmaker_()

    session = {"uid": str(user_id), "tid": str(tid_a)}
    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs", CABINET_SECRET_KEY="t"
    )

    from types import SimpleNamespace

    original_get_bot_settings = routes_channels.get_bot_settings
    routes_channels.get_bot_settings = lambda: SimpleNamespace(bot_username="testbot")
    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: session
    app.dependency_overrides[deps.get_settings] = lambda: settings
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/cabinet/telegram/link")
        assert resp.status_code == 200, resp.text
        assert "https://t.me/testbot?start=" in resp.text
    finally:
        routes_channels.get_bot_settings = original_get_bot_settings
        app.dependency_overrides.clear()
        sup_clean = create_async_engine(superuser_url, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text("DELETE FROM auth_tokens WHERE user_id = :uid"),
                    {"uid": user_id},
                )
                await conn.execute(
                    text("DELETE FROM users WHERE id = :uid"),
                    {"uid": user_id},
                )
        finally:
            await sup_clean.dispose()


@pytest.mark.asyncio
async def test_admin_service_cross_tenant_under_app_role(sessionmaker_, tenant_ids):
    """The regression FIX-ADMIN-RLS: admin_service under the app role (FORCE RLS, 0021).

    The scenario: GUC = tid_a (the admin session, its own tenant), but admin_service must
    see/write the data of ANY tenant (tid_b) — through SECURITY DEFINER
    app_admin_* (0025), not through set_tenant (it would narrow to tid_a).

    It also checks that an ordinary (non-admin) direct SELECT * FROM users under the same
    GUC still sees only its own tenant — the SECURITY DEFINER does not turn into a blanket bypass of the policies.
    """
    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

    admin_user_id = uuid.uuid4()  # tid_a — the own tenant of the admin session
    other_user_id = uuid.uuid4()  # tid_b — a foreign tenant, must be visible
    profile_id = uuid.uuid4()  # the profile of tid_b

    sup_setup = create_async_engine(superuser_url, future=True)
    try:
        async with sup_setup.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role, email) "
                    "VALUES (:id, :tid, 'ru', 'admin', :email)"
                ),
                {"id": admin_user_id, "tid": tid_a, "email": f"{admin_user_id}@t.rs"},
            )
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role, email) "
                    "VALUES (:id, :tid, 'ru', 'user', :email)"
                ),
                {"id": other_user_id, "tid": tid_b, "email": f"{other_user_id}@t.rs"},
            )
            await conn.execute(
                text(
                    "INSERT INTO profiles (id, tenant_id, name, is_active) "
                    "VALUES (:id, :tid, 'test profile', true)"
                ),
                {"id": profile_id, "tid": tid_b},
            )
            # delivery_outbox.id — bigint autoincrement, we do not set it by hand.
            await conn.execute(
                text(
                    "INSERT INTO delivery_outbox (profile_id, status, created_at) "
                    "VALUES (:pid, 'sent', NOW())"
                ),
                {"pid": profile_id},
            )
    finally:
        await sup_setup.dispose()

    try:
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            users, total = await admin_svc.list_users(session, page=1, page_size=1000)
            stats = await admin_svc.get_platform_stats(session)
            profiles = await admin_svc.list_profiles(session)
            other_user_row = await admin_svc.get_user(session, str(other_user_id))
            delivered = await admin_svc.get_user_delivered_count(session, str(tid_b))
            await session.commit()

        user_ids = {u.id for u in users}
        assert str(admin_user_id) in user_ids
        assert str(other_user_id) in user_ids, (
            "admin_service.list_users must see the user of another "
            "tenant via app_admin_list_users (SECURITY DEFINER)"
        )
        assert total >= 2
        assert stats.users_total >= 2

        profile_ids = {p.id for p in profiles}
        assert str(profile_id) in profile_ids, (
            "admin_service.list_profiles must see the profile of another "
            "tenant via app_admin_list_profiles (SECURITY DEFINER)"
        )

        assert other_user_row is not None
        assert other_user_row.tenant_id == str(tid_b)

        assert delivered == 1, (
            "get_user_delivered_count must count the delivery of another "
            "tenant via app_admin_user_delivered_count (SECURITY DEFINER)"
        )

        # The writes: grant_access/set_tier write the tenants of ANOTHER tenant (tid_b),
        # being under GUC=tid_a — also via SECURITY DEFINER.
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            await admin_svc.grant_access(
                session,
                admin_uid=str(admin_user_id),
                tenant_id=str(tid_b),
                days=30,
                before_until=None,
            )

        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            await admin_svc.set_tier(
                session, admin_uid=str(admin_user_id), tenant_id=str(tid_b), tier="pro"
            )

        sup_verify = create_async_engine(superuser_url, future=True)
        try:
            async with sup_verify.begin() as conn:
                row = (
                    await conn.execute(
                        text("SELECT tier, access_until FROM tenants WHERE id = :tid"),
                        {"tid": tid_b},
                    )
                ).first()
        finally:
            await sup_verify.dispose()
        assert row is not None
        assert row.tier == "pro"
        assert row.access_until is not None

        # Isolation: an ordinary (non-admin) direct SELECT under the same GUC sees
        # ONLY its own tenant (tid_a) — the SECURITY DEFINER did not open users entirely.
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            result = await session.execute(text("SELECT id FROM users"))
            plain_ids = {str(r[0]) for r in result.fetchall()}
            await session.commit()
        assert str(admin_user_id) in plain_ids
        assert str(other_user_id) not in plain_ids, (
            "an ordinary SELECT under GUC=tid_a must not see the user tid_b — "
            "the isolation outside admin_service must be preserved"
        )
    finally:
        sup_clean = create_async_engine(superuser_url, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text("DELETE FROM delivery_outbox WHERE profile_id = :pid"),
                    {"pid": profile_id},
                )
                await conn.execute(
                    text("DELETE FROM profiles WHERE id = :id"), {"id": profile_id}
                )
                await conn.execute(
                    text("DELETE FROM users WHERE id IN (:a, :b)"),
                    {"a": admin_user_id, "b": other_user_id},
                )
                await conn.execute(
                    text(
                        "UPDATE tenants SET tier = 'lite', access_until = NULL "
                        "WHERE id = :tid"
                    ),
                    {"tid": tid_b},
                )
        finally:
            await sup_clean.dispose()


@pytest.mark.asyncio
async def test_delivery_health_cross_tenant_under_app_role(sessionmaker_, tenant_ids):
    """The regression VG-29.3: get_delivery_health under the app role (FORCE RLS, 0021).

    delivery_outbox (JOIN profiles.tenant_id) and reminder_log /
    subscription_reminder_log (a direct tenant_id) — ALL under FORCE RLS. GUC=tid_a
    (the admin session), but the aggregates of the backlog/freshness must be counted over the data of
    ANY tenant (tid_b) through SECURITY DEFINER app_admin_delivery_backlog /
    app_admin_reminder_freshness (0026) — we prove the bypass of FORCE RLS (an aggregate,
    not 0). A direct SELECT under tid_a would not see the data of tid_b.
    """
    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

    profile_id = uuid.uuid4()  # the profile of tid_b
    reminder_id = uuid.uuid4()
    sub_reminder_id = uuid.uuid4()
    tender_id = uuid.uuid4()

    sup_setup = create_async_engine(superuser_url, future=True)
    try:
        async with sup_setup.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO profiles (id, tenant_id, name, is_active) "
                    "VALUES (:id, :tid, 'delivery profile', true)"
                ),
                {"id": profile_id, "tid": tid_b},
            )
            # delivery_outbox.id — bigint autoincrement, we do not set it by hand.
            await conn.execute(
                text(
                    "INSERT INTO delivery_outbox (profile_id, status, created_at, sent_at) "
                    "VALUES (:pid, 'sent', NOW(), NOW())"
                ),
                {"pid": profile_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO delivery_outbox (profile_id, status, created_at) "
                    "VALUES (:pid, 'pending', NOW())"
                ),
                {"pid": profile_id},
            )
            # tender for the FK of reminder_log
            await conn.execute(
                text(
                    "INSERT INTO tenders_normalized "
                    "(id, source_key, country_code, first_seen_at) "
                    "VALUES (:id, :sk, 'RS', NOW())"
                ),
                {"id": tender_id, "sk": f"vg293-{tender_id}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO reminder_log "
                    "(id, tenant_id, tender_id, threshold_days, sent_at) "
                    "VALUES (:id, :tid, :tender, 3, NOW())"
                ),
                {"id": reminder_id, "tid": tid_b, "tender": tender_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO subscription_reminder_log "
                    "(id, tenant_id, threshold_days, access_until_snapshot, sent_at) "
                    "VALUES (:id, :tid, 7, NOW(), NOW())"
                ),
                {"id": sub_reminder_id, "tid": tid_b},
            )
    finally:
        await sup_setup.dispose()

    try:
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            health = await admin_svc.get_delivery_health(session)
            await session.commit()

        # The aggregates see the data of a FOREIGN tenant (tid_b) — the bypass of FORCE RLS is proved.
        assert health.pending_total >= 1, (
            "get_delivery_health must count the pending of the other tenant through "
            "app_admin_delivery_backlog (SECURITY DEFINER), not 0 (fail-closed)"
        )
        assert health.sent_24h >= 1
        assert health.last_sent_at is not None
        assert health.last_deadline_reminder_at is not None, (
            "app_admin_reminder_freshness must return MAX(sent_at) of the reminder_log of "
            "the other tenant through SECURITY DEFINER"
        )
        assert health.last_subscription_reminder_at is not None
    finally:
        sup_clean2 = create_async_engine(superuser_url, future=True)
        try:
            async with sup_clean2.begin() as conn:
                await conn.execute(
                    text("DELETE FROM subscription_reminder_log WHERE id = :id"),
                    {"id": sub_reminder_id},
                )
                await conn.execute(
                    text("DELETE FROM reminder_log WHERE id = :id"),
                    {"id": reminder_id},
                )
                await conn.execute(
                    text("DELETE FROM delivery_outbox WHERE profile_id = :pid"),
                    {"pid": profile_id},
                )
                await conn.execute(
                    text("DELETE FROM profiles WHERE id = :id"), {"id": profile_id}
                )
                await conn.execute(
                    text("DELETE FROM tenders_normalized WHERE id = :id"),
                    {"id": tender_id},
                )
        finally:
            await sup_clean2.dispose()


@pytest.mark.asyncio
async def test_tender_ai_analyses_isolation_under_app_role(sessionmaker_, tenant_ids):
    """The regression web-ai-1: the read-only view of the AI artifacts under the app role (FORCE RLS).

    The artifacts are profile-specific (the policy 0003:
    ``profile_id IS NULL OR profile_id IN the profiles of the tenant``). Under GUC=tid_a:
      * ``get_tender_analyses`` for ITS OWN profile (tid_a) → sees its own bid;
      * for the profile of a foreign tenant (tid_b) → None (isolation WITHOUT SECURITY DEFINER);
      * a direct SELECT does not show the analysis row of the profile tid_b.
    """
    from src.api.cabinet.tender_ai_service import get_tender_analyses

    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

    profile_a = uuid.uuid4()
    profile_b = uuid.uuid4()
    tender_id = uuid.uuid4()

    sup_setup = create_async_engine(superuser_url, future=True)
    try:
        async with sup_setup.begin() as conn:
            for pid, tid, nm in [
                (profile_a, tid_a, "profile A"),
                (profile_b, tid_b, "profile B"),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO profiles (id, tenant_id, name, is_active) "
                        "VALUES (:id, :tid, :nm, true)"
                    ),
                    {"id": pid, "tid": tid, "nm": nm},
                )
            await conn.execute(
                text(
                    "INSERT INTO tenders_normalized "
                    "(id, source_key, country_code, title, first_seen_at) "
                    "VALUES (:id, :sk, 'RS', 'Zajednički tender', NOW())"
                ),
                {"id": tender_id, "sk": f"webai1-{tender_id}"},
            )
            for pid, verdict in [(profile_a, "go"), (profile_b, "no_go")]:
                await conn.execute(
                    text(
                        "INSERT INTO tender_ai_analyses "
                        "(tender_id, profile_id, level, output) "
                        "VALUES (:tid, :pid, 'bid', CAST(:out AS jsonb))"
                    ),
                    {
                        "tid": tender_id,
                        "pid": pid,
                        "out": f'{{"verdict": "{verdict}", "score": 50}}',
                    },
                )
    finally:
        await sup_setup.dispose()

    try:
        # Its own profile under GUC=tid_a — the bid is visible.
        async with sessionmaker_() as session:
            own = await get_tender_analyses(
                session,
                tid=str(tid_a),
                tender_id=str(tender_id),
                profile_id=str(profile_a),
            )
            await session.commit()
        assert own is not None
        assert own.bid is not None
        assert own.bid["verdict"] == "go"

        # A foreign profile (tid_b) under GUC=tid_a — None (fail-closed, without SECURITY DEFINER).
        async with sessionmaker_() as session:
            foreign = await get_tender_analyses(
                session,
                tid=str(tid_a),
                tender_id=str(tender_id),
                profile_id=str(profile_b),
            )
            await session.commit()
        assert foreign is None, "the profile of a foreign tenant must not resolve"

        # A direct SELECT under GUC=tid_a does not see the analysis row of the profile tid_b.
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            result = await session.execute(
                text(
                    "SELECT profile_id FROM tender_ai_analyses " "WHERE tender_id = :t"
                ),
                {"t": tender_id},
            )
            visible = {str(r[0]) for r in result.fetchall()}
            await session.commit()
        assert str(profile_a) in visible
        assert (
            str(profile_b) not in visible
        ), "the analysis of the profile tid_b must not be visible"
    finally:
        sup_clean = create_async_engine(superuser_url, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text("DELETE FROM tender_ai_analyses WHERE tender_id = :t"),
                    {"t": tender_id},
                )
                await conn.execute(
                    text("DELETE FROM profiles WHERE id IN (:a, :b)"),
                    {"a": profile_a, "b": profile_b},
                )
                await conn.execute(
                    text("DELETE FROM tenders_normalized WHERE id = :id"),
                    {"id": tender_id},
                )
        finally:
            await sup_clean.dispose()


class _FakeAiClient:
    """A fake AiClient for the RLS test: no network, returns a valid bid output."""

    async def analyze(self, request):
        from types import SimpleNamespace

        return SimpleNamespace(
            output={"verdict": "go", "score": 55, "pros": ["ok"], "cons": []},
            model="claude-sonnet-4-6",
            tokens_input=100,
            tokens_output=200,
            tokens_cached=0,
            cost_usd=0.0021,
        )


@pytest.mark.asyncio
async def test_tender_ai_generate_isolation_under_app_role(sessionmaker_):
    """The regression web-ai-2: the generation from the cabinet under the app role writes ITS OWN tenant; a foreign
    profile → a refusal without a write.

    Under GUC=tid_a (inside ``generate_artifact`` via ``set_tenant``):
      * the generation of the bid for ITS OWN profile (tid_a) → success, the row appears
        in ``tender_ai_analyses`` with profile_id=its own;
      * a repeat → ``already`` (a cache hit, without doubling — UNIQUE);
      * the generation for the profile of a foreign tenant (tid_b) → ``no_profile``, WITHOUT a write.
    """
    from src.api.cabinet.tender_ai_generate import (
        STATUS_ALREADY,
        STATUS_NO_PROFILE,
        STATUS_SUCCESS,
        generate_artifact,
    )

    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    tid_a = uuid.uuid4()
    tid_b = uuid.uuid4()
    profile_a = uuid.uuid4()
    profile_b = uuid.uuid4()
    tender_id = uuid.uuid4()
    fake_ai = _FakeAiClient()

    sup_setup = create_async_engine(superuser_url, future=True)
    try:
        async with sup_setup.begin() as conn:
            for tid in (tid_a, tid_b):
                await conn.execute(
                    text(
                        "INSERT INTO tenants (id, country_code, tier, status) "
                        "VALUES (:id, 'RS', 'pro', 'active')"
                    ),
                    {"id": tid},
                )
            for pid, tid, nm in [
                (profile_a, tid_a, "profile A"),
                (profile_b, tid_b, "profile B"),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO profiles (id, tenant_id, name, is_active) "
                        "VALUES (:id, :tid, :nm, true)"
                    ),
                    {"id": pid, "tid": tid, "nm": nm},
                )
            await conn.execute(
                text(
                    "INSERT INTO tenders_normalized "
                    "(id, source_key, country_code, title, description, first_seen_at) "
                    "VALUES (:id, :sk, 'RS', 'Zajednički tender', 'Opis TZ', NOW())"
                ),
                {"id": tender_id, "sk": f"webai2-{tender_id}"},
            )
    finally:
        await sup_setup.dispose()

    try:
        # Its own profile under GUC=tid_a — the generation passes and writes its own tenant.
        async with sessionmaker_() as session:
            status = await generate_artifact(
                session,
                tid=str(tid_a),
                tender_id=str(tender_id),
                profile_id=str(profile_a),
                level="bid",
                ai_client=fake_ai,
            )
        assert status == STATUS_SUCCESS

        # A repeat — a cache hit (without doubling, UNIQUE tender_id/profile_id/level).
        async with sessionmaker_() as session:
            again = await generate_artifact(
                session,
                tid=str(tid_a),
                tender_id=str(tender_id),
                profile_id=str(profile_a),
                level="bid",
                ai_client=fake_ai,
            )
        assert again == STATUS_ALREADY

        # A foreign profile (tid_b) under GUC=tid_a — a refusal without a write.
        async with sessionmaker_() as session:
            foreign = await generate_artifact(
                session,
                tid=str(tid_a),
                tender_id=str(tender_id),
                profile_id=str(profile_b),
                level="bid",
                ai_client=fake_ai,
            )
        assert foreign == STATUS_NO_PROFILE

        # A check of the rows (the superuser): exactly one row — for profile_a.
        sup_check = create_async_engine(superuser_url, future=True)
        try:
            async with sup_check.begin() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT profile_id FROM tender_ai_analyses "
                            "WHERE tender_id = :t AND level = 'bid'"
                        ),
                        {"t": tender_id},
                    )
                ).fetchall()
        finally:
            await sup_check.dispose()
        pids = {str(r[0]) for r in rows}
        assert pids == {str(profile_a)}, "must be exactly one row — profile_a"
    finally:
        sup_clean = create_async_engine(superuser_url, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text("DELETE FROM tender_ai_analyses WHERE tender_id = :t"),
                    {"t": tender_id},
                )
                await conn.execute(
                    text("DELETE FROM ai_usage WHERE tenant_id IN (:a, :b)"),
                    {"a": tid_a, "b": tid_b},
                )
                await conn.execute(
                    text("DELETE FROM profiles WHERE id IN (:a, :b)"),
                    {"a": profile_a, "b": profile_b},
                )
                await conn.execute(
                    text("DELETE FROM tenders_normalized WHERE id = :id"),
                    {"id": tender_id},
                )
                await conn.execute(
                    text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                    {"a": tid_a, "b": tid_b},
                )
        finally:
            await sup_clean.dispose()


@pytest.mark.asyncio
async def test_billing_apply_event_cross_tenant_under_app_role(sessionmaker_):
    """The regression VG-11.1: apply_billing_event under the app role (FORCE RLS, 0021/0028).

    The webhook comes WITHOUT a session (the GUC is not set) — all the cross-tenant writes
    go through SECURITY DEFINER (app_billing_upsert_subscription + the reused
    app_admin_set_tier/grant_access from 0025). The test is self-sufficient (its own tenants
    with the canonical tier, R8/0027). We check:
      * activated for tid_b (the tenant_id in the event) under the app role without the GUC → writes
        billing_subscriptions tid_b, tier=pro, access_until is extended;
      * a repeat of the event_id → idempotent (billing_webhook_events UNIQUE), without a duplicate;
      * the resolve of the tenant by customer_id (renewed without tenant_id) → finds tid_b;
      * the RLS isolation: under GUC=tid_a the foreign subscription is not visible, under GUC=tid_b — visible.
    """
    from datetime import datetime, timezone

    from src.billing.models import BillingEvent
    from src.billing.service import apply_billing_event

    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    tid_a = uuid.uuid4()
    tid_b = uuid.uuid4()

    sup_setup = create_async_engine(superuser_url, future=True)
    try:
        async with sup_setup.begin() as conn:
            for tid in (tid_a, tid_b):
                await conn.execute(
                    text(
                        "INSERT INTO tenants (id, country_code, tier, status) "
                        "VALUES (:id, 'RS', 'lite', 'active')"
                    ),
                    {"id": tid},
                )
    finally:
        await sup_setup.dispose()

    period_end = datetime(2030, 1, 1, tzinfo=timezone.utc)
    activated = BillingEvent(
        provider="stripe",
        provider_event_id=f"evt-act-{uuid.uuid4().hex}",
        type="activated",
        provider_customer_id=f"cus-{uuid.uuid4().hex}",
        provider_subscription_id=f"sub-{uuid.uuid4().hex}",
        plan_key="pro",
        current_period_end=period_end,
        tenant_id=str(tid_b),
    )

    try:
        # 1. activated under the app role WITHOUT the GUC — all via SECURITY DEFINER.
        async with sessionmaker_() as session:
            await apply_billing_event(session, activated)

        sup = create_async_engine(superuser_url, future=True)
        try:
            async with sup.begin() as conn:
                sub_rows = (
                    await conn.execute(
                        text(
                            "SELECT tenant_id, status, plan_key FROM "
                            "billing_subscriptions WHERE provider_subscription_id = :s"
                        ),
                        {"s": activated.provider_subscription_id},
                    )
                ).fetchall()
                tenant_row = (
                    await conn.execute(
                        text("SELECT tier, access_until FROM tenants WHERE id = :t"),
                        {"t": tid_b},
                    )
                ).first()
        finally:
            await sup.dispose()

        assert len(sub_rows) == 1, "exactly one subscription of tid_b must appear"
        assert str(sub_rows[0].tenant_id) == str(tid_b)
        assert sub_rows[0].status == "active"
        assert sub_rows[0].plan_key == "pro"
        assert tenant_row.tier == "pro", "activated must apply tier=pro"
        assert tenant_row.access_until is not None

        # 2. Idempotency: a repeat of the same event_id — without a second row.
        async with sessionmaker_() as session:
            await apply_billing_event(session, activated)
        sup2 = create_async_engine(superuser_url, future=True)
        try:
            async with sup2.begin() as conn:
                cnt = (
                    await conn.execute(
                        text(
                            "SELECT COUNT(*) FROM billing_subscriptions "
                            "WHERE provider_subscription_id = :s"
                        ),
                        {"s": activated.provider_subscription_id},
                    )
                ).scalar_one()
                evt_cnt = (
                    await conn.execute(
                        text(
                            "SELECT COUNT(*) FROM billing_webhook_events "
                            "WHERE event_id = :e"
                        ),
                        {"e": activated.provider_event_id},
                    )
                ).scalar_one()
        finally:
            await sup2.dispose()
        assert cnt == 1, "a repeat of the event must not double the subscription"
        assert evt_cnt == 1, "billing_webhook_events dedup by (provider, event_id)"

        # 3. The resolve of the tenant by customer_id: renewed without tenant_id → finds tid_b.
        renewed = BillingEvent(
            provider="stripe",
            provider_event_id=f"evt-ren-{uuid.uuid4().hex}",
            type="renewed",
            provider_customer_id=activated.provider_customer_id,
            provider_subscription_id=activated.provider_subscription_id,
            plan_key="business",
            current_period_end=period_end,
            tenant_id=None,
        )
        async with sessionmaker_() as session:
            await apply_billing_event(session, renewed)
        sup3 = create_async_engine(superuser_url, future=True)
        try:
            async with sup3.begin() as conn:
                tier_now = (
                    await conn.execute(
                        text("SELECT tier FROM tenants WHERE id = :t"),
                        {"t": tid_b},
                    )
                ).scalar_one()
        finally:
            await sup3.dispose()
        assert tier_now == "business", (
            "renewed without tenant_id must resolve tid_b by customer_id "
            "(app_billing_resolve_tenant_by_customer) and apply business"
        )

        # 4. The RLS isolation of the reads of billing_subscriptions under the app role.
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            seen_a = (
                await session.execute(
                    text("SELECT tenant_id FROM billing_subscriptions")
                )
            ).fetchall()
            await session.commit()
        assert all(
            str(r[0]) != str(tid_b) for r in seen_a
        ), "under GUC=tid_a the subscription of tid_b must not be visible (FORCE RLS)"

        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_b)},
            )
            seen_b = {
                str(r[0])
                for r in (
                    await session.execute(
                        text("SELECT tenant_id FROM billing_subscriptions")
                    )
                ).fetchall()
            }
            await session.commit()
        assert str(tid_b) in seen_b, "under GUC=tid_b its own subscription is visible"
    finally:
        sup_clean = create_async_engine(superuser_url, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM billing_webhook_events WHERE event_id IN (:a, :b)"
                    ),
                    {"a": activated.provider_event_id, "b": renewed.provider_event_id},
                )
                await conn.execute(
                    text(
                        "DELETE FROM billing_subscriptions "
                        "WHERE provider_subscription_id = :s"
                    ),
                    {"s": activated.provider_subscription_id},
                )
                # The payment letters (0033) reference tenants — without their deletion
                # DELETE FROM tenants falls with ForeignKeyViolationError (the test was
                # red also on the clean main).
                await conn.execute(
                    text(
                        "DELETE FROM billing_notification_outbox "
                        "WHERE tenant_id IN (:a, :b)"
                    ),
                    {"a": tid_a, "b": tid_b},
                )
                await conn.execute(
                    text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                    {"a": tid_a, "b": tid_b},
                )
        finally:
            await sup_clean.dispose()


@pytest.mark.asyncio
async def test_workflow_tables_isolation_under_app_role(sessionmaker_, tenant_ids):
    """Regression VG-17: tender_folders / folder_items / tender_notes under FORCE RLS.

    Setup (the superuser, bypasses the RLS): one tender + a folder + a membership +
    a note for each tenant. Assert: under GUC=A only the row A is visible, the row B is hidden
    (fail-closed) for all three tables; symmetrically under GUC=B.
    """
    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    sup = create_async_engine(superuser_url, future=True)

    tender_a, tender_b = uuid.uuid4(), uuid.uuid4()
    folder_a, folder_b = uuid.uuid4(), uuid.uuid4()
    item_a, item_b = uuid.uuid4(), uuid.uuid4()
    note_a, note_b = uuid.uuid4(), uuid.uuid4()
    try:
        async with sup.begin() as conn:
            for tnd in (tender_a, tender_b):
                await conn.execute(
                    text("INSERT INTO tenders_normalized (id) VALUES (:id)"),
                    {"id": tnd},
                )
            for fid, tid in ((folder_a, tid_a), (folder_b, tid_b)):
                await conn.execute(
                    text(
                        "INSERT INTO tender_folders (id, tenant_id, name) "
                        "VALUES (:id, :tid, 'Папка')"
                    ),
                    {"id": fid, "tid": tid},
                )
            for iid, tid, fid, tnd in (
                (item_a, tid_a, folder_a, tender_a),
                (item_b, tid_b, folder_b, tender_b),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO folder_items "
                        "(id, tenant_id, folder_id, tender_id) "
                        "VALUES (:id, :tid, :fid, :tnd)"
                    ),
                    {"id": iid, "tid": tid, "fid": fid, "tnd": tnd},
                )
            for nid, tid, tnd in (
                (note_a, tid_a, tender_a),
                (note_b, tid_b, tender_b),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO tender_notes (id, tenant_id, tender_id, body) "
                        "VALUES (:id, :tid, :tnd, 'заметка')"
                    ),
                    {"id": nid, "tid": tid, "tnd": tnd},
                )
    finally:
        await sup.dispose()

    async def _ids(tid, table):
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid)},
            )
            rows = (await session.execute(text(f"SELECT id FROM {table}"))).fetchall()
            await session.commit()
        return {str(r[0]) for r in rows}

    try:
        for table, id_a, id_b in (
            ("tender_folders", folder_a, folder_b),
            ("folder_items", item_a, item_b),
            ("tender_notes", note_a, note_b),
        ):
            seen_a = await _ids(tid_a, table)
            seen_b = await _ids(tid_b, table)
            assert str(id_a) in seen_a, f"{table}: A sees its own row"
            assert (
                str(id_b) not in seen_a
            ), f"{table}: A does NOT see the row B (fail-closed)"
            assert str(id_b) in seen_b, f"{table}: B sees its own row"
            assert (
                str(id_a) not in seen_b
            ), f"{table}: B does NOT see the row A (fail-closed)"
    finally:
        sup2 = create_async_engine(superuser_url, future=True)
        try:
            async with sup2.begin() as conn:
                await conn.execute(
                    text("DELETE FROM tender_notes WHERE id IN (:a, :b)"),
                    {"a": note_a, "b": note_b},
                )
                await conn.execute(
                    text("DELETE FROM folder_items WHERE id IN (:a, :b)"),
                    {"a": item_a, "b": item_b},
                )
                await conn.execute(
                    text("DELETE FROM tender_folders WHERE id IN (:a, :b)"),
                    {"a": folder_a, "b": folder_b},
                )
                await conn.execute(
                    text("DELETE FROM tenders_normalized WHERE id IN (:a, :b)"),
                    {"a": tender_a, "b": tender_b},
                )
        finally:
            await sup2.dispose()


@pytest.mark.asyncio
async def test_channel_dead_notice_log_isolated_under_app_role(
    sessionmaker_, tenant_ids
):
    """Regression 0031: tenant_isolation on channel_dead_notice_log under the app role.

    The journal of incidents “the channel dropped off” is tenant-scoped with a direct column
    tenant_id. The worker writes privileged (the owner bypasses the RLS), but the policy
    must isolate the tenants — the test under the superuser does NOT prove this
    (the project rules), therefore we read under the role tendersrb_app with the GUC.
    """
    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    id_a, id_b = uuid.uuid4(), uuid.uuid4()

    sup = create_async_engine(superuser_url, future=True)
    try:
        async with sup.begin() as conn:
            for row_id, tid in ((id_a, tid_a), (id_b, tid_b)):
                await conn.execute(
                    text(
                        "INSERT INTO channel_dead_notice_log (id, tenant_id, channel) "
                        "VALUES (:id, :tid, 'telegram')"
                    ),
                    {"id": row_id, "tid": tid},
                )
    finally:
        await sup.dispose()

    try:
        for tid, own, foreign in ((tid_a, id_a, id_b), (tid_b, id_b, id_a)):
            async with sessionmaker_() as session:
                await session.execute(
                    text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                    {"tid": str(tid)},
                )
                seen = {
                    str(r[0])
                    for r in await session.execute(
                        text("SELECT id FROM channel_dead_notice_log")
                    )
                }
                await session.commit()
            assert str(own) in seen, "the tenant sees its own incident"
            assert (
                str(foreign) not in seen
            ), "a foreign incident is NOT visible (fail-closed)"
    finally:
        sup2 = create_async_engine(superuser_url, future=True)
        try:
            async with sup2.begin() as conn:
                await conn.execute(
                    text("DELETE FROM channel_dead_notice_log WHERE id IN (:a, :b)"),
                    {"a": id_a, "b": id_b},
                )
        finally:
            await sup2.dispose()


@pytest.mark.asyncio
async def test_tenant_scoped_tables_have_force_rls(engine):
    """Regression 0032: every table with the policy tenant_isolation has force=t.

    A check of the CATALOG, not of the behavior, and this is not a corner cut: under the app role
    (NOBYPASSRLS) the policies are enforced both with ENABLE and with FORCE — a behavioral
    test will not see the difference, exactly why 0031 (rls=t, force=f) slipped past
    the tests to production. The difference shows only on the OWNER of the table, and the owner
    here is the superuser ``tendersrb`` (BYPASSRLS), who bypasses the RLS
    regardless of FORCE. A behavioral regression under the owner in this contour
    is impossible without changing the owner of the tables; relforcerowsecurity=t — a direct and
    complete proof (further, FORCE is ensured by PostgreSQL).

    The static twin without a DB (catches the same on every PR in the CI, where
    TEST_RLS_DATABASE_URL is absent): ``tests/db/test_rls_force_invariant.py``.
    """
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relforcerowsecurity "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND EXISTS ("
                    "  SELECT 1 FROM pg_policy p "
                    "  WHERE p.polrelid = c.oid AND p.polname = 'tenant_isolation')"
                )
            )
        ).fetchall()

    assert rows, "not a single table with the policy tenant_isolation was found"
    unforced = sorted(r.relname for r in rows if not r.relforcerowsecurity)
    assert not unforced, (
        f"tenant-scoped tables without FORCE ROW LEVEL SECURITY: {unforced}. "
        "The table owner bypasses the ENABLE policy and reads the other tenants."
    )

    policied = {r.relname for r in rows}
    # Both tables that 0032 picks up: the incident journal (the policy from 0031) and
    # trial_requests (the policy from 0012, lost by the list of 0021).
    for table in ("channel_dead_notice_log", "trial_requests"):
        assert (
            table in policied
        ), f"{table} must have the policy tenant_isolation and FORCE (0032)"


@pytest.mark.asyncio
async def test_my_tenders_export_isolation_under_app_role(sessionmaker_, tenant_ids):
    """Regression F2: the CSV export “My tenders” under the app role (FORCE RLS).

    The most expensive class of bug in the project — foreign tenders in one’s own export. Both tenants
    have a folder with ITS OWN tender and a watch on ITS OWN; the tenders lie in the
    common ``tenders_normalized`` (it does not know the tenant), therefore the isolation is held
    exactly by ``folder_items`` / ``tender_folders`` / ``tender_user_status``.

    Under GUC=tid_a the export must return only the tenders of A. Mutation
    check: remove ``repo.set_tenant`` from ``get_my_tenders_export`` — the test turns red
    (the GUC is not set → UndefinedObjectError).
    """
    from src.api.cabinet.my_tenders_export import build_my_tenders_csv
    from src.api.cabinet.workflow_service import get_my_tenders_export

    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

    folder_a, folder_b = uuid.uuid4(), uuid.uuid4()
    foldered_a, foldered_b = uuid.uuid4(), uuid.uuid4()
    watched_a, watched_b = uuid.uuid4(), uuid.uuid4()
    all_tenders = [str(t) for t in (foldered_a, foldered_b, watched_a, watched_b)]

    sup_setup = create_async_engine(superuser_url, future=True)
    try:
        async with sup_setup.begin() as conn:
            for tender_id, title in [
                (foldered_a, "Папка A"),
                (foldered_b, "Папка B"),
                (watched_a, "Слежу A"),
                (watched_b, "Слежу B"),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO tenders_normalized "
                        "(id, source_key, country_code, title, amount_original, "
                        " currency_original, first_seen_at) "
                        "VALUES (:id, :sk, 'RS', :t, 1000, 'RSD', NOW())"
                    ),
                    {"id": tender_id, "sk": f"f2-{tender_id}", "t": title},
                )
            for fid, tid, tender_id in [
                (folder_a, tid_a, foldered_a),
                (folder_b, tid_b, foldered_b),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO tender_folders (id, tenant_id, name) "
                        "VALUES (:id, :tid, 'Приоритет')"
                    ),
                    {"id": fid, "tid": tid},
                )
                await conn.execute(
                    text(
                        "INSERT INTO folder_items (tenant_id, folder_id, tender_id) "
                        "VALUES (:tid, :fid, :t)"
                    ),
                    {"tid": tid, "fid": fid, "t": tender_id},
                )
            for tid, tender_id in [(tid_a, watched_a), (tid_b, watched_b)]:
                await conn.execute(
                    text(
                        "INSERT INTO tender_user_status (tenant_id, tender_id, status) "
                        "VALUES (:tid, :t, 'watching')"
                    ),
                    {"tid": tid, "t": tender_id},
                )
    finally:
        await sup_setup.dispose()

    try:
        async with sessionmaker_() as session:
            rows = await get_my_tenders_export(session, str(tid_a))
            await session.commit()

        got = {r.tender_id for r in rows}
        assert got == {str(foldered_a), str(watched_a)}, (
            "the export of tenant A must contain exactly its own tenders, "
            f"received: {sorted(got)}"
        )
        # And the same — at the level of the finished file: there is no name of a foreign tender in it.
        body = build_my_tenders_csv(rows, "ru").decode("utf-8-sig")
        assert "Папка A" in body and "Слежу A" in body
        assert (
            "Папка B" not in body
        ), "a tender of tenant B leaked into the CSV of tenant A"
        assert (
            "Слежу B" not in body
        ), "a tender of tenant B leaked into the CSV of tenant A"

        # Symmetry: under GUC=tid_b exactly the tenders B are visible (not “A is always empty”).
        async with sessionmaker_() as session:
            rows_b = await get_my_tenders_export(session, str(tid_b))
            await session.commit()
        assert {r.tender_id for r in rows_b} == {str(foldered_b), str(watched_b)}
    finally:
        sup_clean = create_async_engine(superuser_url, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM tender_user_status "
                        "WHERE tender_id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": all_tenders},
                )
                await conn.execute(
                    text("DELETE FROM tender_folders WHERE id IN (:a, :b)"),
                    {"a": folder_a, "b": folder_b},
                )
                await conn.execute(
                    text(
                        "DELETE FROM tenders_normalized "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": all_tenders},
                )
        finally:
            await sup_clean.dispose()


@pytest.mark.asyncio
async def test_web_set_status_reaches_reminders_under_app_role(
    sessionmaker_, tenant_ids
):
    """Regression VG-17w: the stage set from the cabinet reaches the reminder.

    The main promise of the feature («marked ✅ I'm in in the cabinet — the reminder will come
    in Telegram») holds not by synchronization, but by the common table ``tender_user_status``.
    Here it is checked fully and for real: the web layer (``set_status`` under the app role, FORCE RLS) writes the stage, and the selection of the candidates is done NOT by a rewritten predicate, but by the imported ``reminders._CANDIDATES_SQL`` — exactly the SQL with which the worker goes in production (privileged, cross-tenant).

    Also — the isolation: tenant B does not see and cannot rewrite the stage of tenant A.

    Mutation-checked on a live PG (all three — red):
      * a hard 'watching' in ``_SET_STATUS_SQL`` → the candidate's status is not 'active';
      * without ``repo.set_tenant`` → ``UndefinedObjectError`` (the contour of the app role is alive);
      * ``DROP POLICY tenant_isolation ON tender_user_status`` + ``DISABLE RLS``
        → the check “without an explicit WHERE” below falls.
    ‼️ The third mutation is caught ONLY by it: the other accesses go through the SQL
    of the service with an explicit ``WHERE tenant_id``, and with the policy removed they pass green
    (checked). The test “under the app role” alone does not prove the RLS.
    """
    from src.api.cabinet.workflow_service import get_tender_state, set_status
    from src.workers.reminders import _CANDIDATES_SQL, _WATCH_STATUSES, DEFAULT_TZ

    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

    tender = uuid.uuid4()
    user_a, chan_a = uuid.uuid4(), uuid.uuid4()

    sup_setup = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    try:
        async with sup_setup.begin() as conn:
            # A tender with a FUTURE deadline — otherwise the worker must not take it at all.
            await conn.execute(
                text(
                    "INSERT INTO tenders_normalized "
                    "(id, source_key, country_code, title, deadline_at, first_seen_at) "
                    "VALUES (:id, :sk, 'RS', 'Тендер воронки', "
                    "        now() + interval '3 days', now())"
                ),
                {"id": tender, "sk": f"vg17w-{tender}"},
            )
            # A live telegram channel of tenant A: without it there will be no candidate even
            # at the correct stage (the channel is a FACT of the addressee, not a setting).
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role) "
                    "VALUES (:id, :tid, 'ru', 'admin')"
                ),
                {"id": user_a, "tid": tid_a},
            )
            await conn.execute(
                text(
                    "INSERT INTO user_channels "
                    "(id, user_id, channel, external_id, status) "
                    "VALUES (:id, :uid, 'telegram', '424242', 'active')"
                ),
                {"id": chan_a, "uid": user_a},
            )
    finally:
        await sup_setup.dispose()

    try:
        # --- The web layer under the app role: subscriber A presses “✅ Participating” in the cabinet. ---
        async with sessionmaker_() as session:
            ok = await set_status(
                session, tenant_id=str(tid_a), tender_id=str(tender), status="active"
            )
        assert ok is True

        # Its own stage is visible on the cabinet card; to a foreign tenant — no (RLS).
        async with sessionmaker_() as session:
            state_a = await get_tender_state(
                session, tenant_id=str(tid_a), tender_id=str(tender)
            )
            await session.commit()
        assert state_a.status == "active"

        async with sessionmaker_() as session:
            state_b = await get_tender_state(
                session, tenant_id=str(tid_b), tender_id=str(tender)
            )
            await session.commit()
        assert state_b.status == "new", "the stage of tenant A is visible to tenant B"

        # ...and the same WITHOUT an explicit WHERE tenant_id — otherwise the test is green thanks to the filter in
        # the SQL of the service, not thanks to the policy, and it would sleep through the removal of tenant_isolation
        # (checked by a mutation: with an explicit WHERE the test does NOT catch the hole). Here the tenant
        # is held only by the RLS.
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_b)},
            )
            leaked = (
                await session.execute(
                    text("SELECT status FROM tender_user_status WHERE tender_id = :t"),
                    {"t": tender},
                )
            ).all()
            await session.commit()
        assert leaked == [], (
            "under GUC=B the funnel row of tenant A is visible — the policy tenant_isolation "
            "on tender_user_status does not work"
        )

        # --- The reminder: the NATIVE predicate of the worker, privileged. ---
        sup_read = create_async_engine(superuser_url, poolclass=NullPool, future=True)
        try:
            async with sup_read.begin() as conn:
                rows = list(
                    await conn.execute(
                        _CANDIDATES_SQL,
                        {
                            "channel": "telegram",
                            "statuses": list(_WATCH_STATUSES),
                            "default_tz": DEFAULT_TZ,
                        },
                    )
                )
        finally:
            await sup_read.dispose()

        mine = [r for r in rows if str(r.tender_id) == str(tender)]
        assert len(mine) == 1, (
            "a tender marked “Participating” from the cabinet did not get into the candidates "
            "of the reminder — the link cabinet→TG is broken"
        )
        assert str(mine[0].tenant_id) == str(tid_a)
        assert mine[0].user_status == "active"
        assert (
            mine[0].target == "424242"
        ), "the reminder would have gone to the wrong channel"

        # --- The isolation of the write: B cannot rewrite the stage of a foreign tender. ---
        async with sessionmaker_() as session:
            await set_status(
                session, tenant_id=str(tid_b), tender_id=str(tender), status="skipped"
            )
        async with sessionmaker_() as session:
            still_a = await get_tender_state(
                session, tenant_id=str(tid_a), tender_id=str(tender)
            )
            await session.commit()
        assert still_a.status == "active", "tenant B rewrote the stage of tenant A"
    finally:
        sup_clean = create_async_engine(superuser_url, poolclass=NullPool, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text("DELETE FROM tender_user_status WHERE tender_id = :t"),
                    {"t": tender},
                )
                await conn.execute(
                    text("DELETE FROM user_channels WHERE id = :c"), {"c": chan_a}
                )
                await conn.execute(
                    text("DELETE FROM users WHERE id = :u"), {"u": user_a}
                )
                await conn.execute(
                    text("DELETE FROM tenders_normalized WHERE id = :t"), {"t": tender}
                )
        finally:
            await sup_clean.dispose()


@pytest.mark.asyncio
async def test_trial_request_approval_billing_path_under_app_role(
    sessionmaker_, tenant_ids
):
    """Regression sub-approval: the web approval of an upgrade request under the app role.

    The scenario: GUC = tid_a (the admin session), the request belongs to tid_b. The operator
    must: (1) see the pending request of tid_b via SECURITY DEFINER
    app_admin_list_trial_requests (0034), while a direct SELECT from trial_requests under
    GUC=tid_a — must NOT; (2) approve it by the billing path (activate_plan_manual):
    tenants.tier=Plan.tier + access_until shifted + billing_subscriptions
    provider='manual' active; (3) a repeat of the approve → idempotent (without a second
    issuing). The RLS of trial_requests is NOT weakened in this.
    """
    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

    admin_user_id = uuid.uuid4()  # tid_a
    req_user_id = uuid.uuid4()  # tid_b — the initiator
    request_id = uuid.uuid4()  # the request of tid_b

    sup_setup = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    try:
        async with sup_setup.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role, email) "
                    "VALUES (:id, :tid, 'ru', 'admin', :email)"
                ),
                {"id": admin_user_id, "tid": tid_a, "email": f"{admin_user_id}@t.rs"},
            )
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role, email) "
                    "VALUES (:id, :tid, 'ru', 'user', :email)"
                ),
                {"id": req_user_id, "tid": tid_b, "email": f"{req_user_id}@t.rs"},
            )
            await conn.execute(
                text(
                    "INSERT INTO trial_requests (id, tenant_id, status, comment) "
                    "VALUES (:id, :tid, 'pending', 'upgrade_request:pro')"
                ),
                {"id": request_id, "tid": tid_b},
            )
    finally:
        await sup_setup.dispose()

    try:
        # (1) The list via SECURITY DEFINER sees the request of a foreign tenant.
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            rows, total = await admin_svc.list_trial_requests(session, page=1)
            await session.commit()
        by_id = {r.id: r for r in rows}
        assert str(request_id) in by_id, (
            "app_admin_list_trial_requests must see the pending request of another "
            "tenant (SECURITY DEFINER)"
        )
        assert by_id[str(request_id)].plan == "pro"
        assert by_id[str(request_id)].email == f"{req_user_id}@t.rs"
        assert total >= 1

        # A direct SELECT under GUC=tid_a does NOT see the foreign request (the RLS is not weakened).
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            direct = await session.execute(text("SELECT id FROM trial_requests"))
            direct_ids = {str(r[0]) for r in direct.fetchall()}
            await session.commit()
        assert str(request_id) not in direct_ids, (
            "a direct SELECT from trial_requests under GUC=tid_a must not see "
            "the request of tid_b — the RLS must remain in force"
        )

        # (2) The approval by the billing path (provider='manual').
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            outcome = await admin_svc.approve_request(
                session,
                admin_uid=str(admin_user_id),
                request_id=str(request_id),
                days=15,
                actor_email=f"{admin_user_id}@t.rs",
            )
        assert outcome == "approved_subscription"

        sup_verify = create_async_engine(superuser_url, poolclass=NullPool, future=True)
        try:
            async with sup_verify.begin() as conn:
                trow = (
                    await conn.execute(
                        text("SELECT tier, access_until FROM tenants WHERE id = :tid"),
                        {"tid": tid_b},
                    )
                ).first()
                srow = (
                    await conn.execute(
                        text(
                            "SELECT provider, status, plan_key FROM "
                            "billing_subscriptions WHERE tenant_id = :tid"
                        ),
                        {"tid": tid_b},
                    )
                ).first()
                status = (
                    await conn.execute(
                        text("SELECT status FROM trial_requests WHERE id = :id"),
                        {"id": request_id},
                    )
                ).scalar_one()
        finally:
            await sup_verify.dispose()
        assert trow is not None
        assert trow.tier == "pro", "approve of an upgrade request must set tier=pro"
        assert trow.access_until is not None
        assert srow is not None
        assert srow.provider == "manual"
        assert srow.status == "active"
        assert srow.plan_key == "pro"
        assert status == "approved"

        # (3) Idempotency: a repeat of approve → already (there is no second issuing).
        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            repeat = await admin_svc.approve_request(
                session,
                admin_uid=str(admin_user_id),
                request_id=str(request_id),
                days=30,
                actor_email=f"{admin_user_id}@t.rs",
            )
        assert repeat == "already"
    finally:
        sup_clean = create_async_engine(superuser_url, poolclass=NullPool, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text("DELETE FROM billing_subscriptions WHERE tenant_id = :t"),
                    {"t": tid_b},
                )
                await conn.execute(
                    text("DELETE FROM trial_requests WHERE id = :id"),
                    {"id": request_id},
                )
                await conn.execute(
                    text("DELETE FROM users WHERE id IN (:a, :b)"),
                    {"a": admin_user_id, "b": req_user_id},
                )
        finally:
            await sup_clean.dispose()


@pytest.mark.asyncio
async def test_track_record_isolation_under_app_role(sessionmaker_, tenant_ids):
    """Regression ai-company-memory: load_track_record under the app role counts the funnel
    ONLY of its own tenant.

    Setup (the superuser, bypasses the RLS): one tender + one ``won`` mark each for
    tenant A and tenant B, different CPVs. Assert: under GUC=A the track record sees won=1 and
    the CPV of the tender A, does NOT see the mark of B (neither in the counters, nor in the top CPV).
    """
    import json

    from src.ai.track_record import load_track_record

    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    sup_engine = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    tender_id = uuid.uuid4()
    tus_a = uuid.uuid4()
    tus_b = uuid.uuid4()
    cls_a = [{"scheme": "CPV", "code": "45000000", "label": "Строительство"}]
    try:
        async with sup_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenders_normalized "
                    "(id, source_key, country_code, classifications, first_seen_at) "
                    "VALUES (:id, :sk, 'RS', CAST(:cls AS jsonb), NOW())"
                ),
                {"id": tender_id, "sk": f"trk-{tender_id}", "cls": json.dumps(cls_a)},
            )
            for sid, tid, status in [
                (tus_a, tid_a, "won"),
                (tus_b, tid_b, "won"),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO tender_user_status "
                        "(id, tenant_id, tender_id, status) "
                        "VALUES (:id, :tid, :tender, :st)"
                    ),
                    {"id": sid, "tid": tid, "tender": tender_id, "st": status},
                )

        async with sessionmaker_() as session:
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            tr = await load_track_record(session, str(tid_a))
            await session.commit()

        assert tr.won == 1, "under GUC=A only the mark of A must be counted, not B"
        assert tr.submitted == 0 and tr.lost == 0
        assert any(c.startswith("45000000") for c in tr.top_cpv)
    finally:
        sup_clean = create_async_engine(superuser_url, poolclass=NullPool, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text("DELETE FROM tender_user_status WHERE tender_id = :t"),
                    {"t": tender_id},
                )
                await conn.execute(
                    text("DELETE FROM tenders_normalized WHERE id = :t"),
                    {"t": tender_id},
                )
        finally:
            await sup_clean.dispose()


@pytest.mark.asyncio
async def test_admin_disabled_subscriber_visible_and_reachable_under_app_role(
    sessionmaker_, tenant_ids
):
    """Regression 0039: a disabled subscriber (0037) must not disappear from
    the admin — before the fix app_admin_get_user/app_admin_list_users filtered
    ``u.deleted_at IS NULL``, and the card of the disabled one was unreachable (get_user
    → None → 404 in routes_admin.admin_user_detail), while the “Restore” button lived
    exactly on this card.

    The scenario: GUC = tid_a (the operator’s session), the target subscriber — tid_b (another
    tenant, as in admin_service_cross_tenant_under_app_role). We disable through
    SECURITY DEFINER app_admin_disable_subscriber, check the visibility in
    list_users/get_user AND the real HTTP code of the card’s response (not a substring in
    the HTML — an assertion by one’s own model of the code already let through a 404 on production, #462→#463).
    Then we restore through app_admin_enable_subscriber and check the reverse.
    """
    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

    admin_user_id = uuid.uuid4()
    target_user_id = uuid.uuid4()
    admin_email = f"{admin_user_id}@t.rs"

    sup_setup = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    try:
        async with sup_setup.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role, email) "
                    "VALUES (:id, :tid, 'ru', 'admin', :email)"
                ),
                {"id": admin_user_id, "tid": tid_a, "email": admin_email},
            )
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role, email) "
                    "VALUES (:id, :tid, 'ru', 'user', :email)"
                ),
                {
                    "id": target_user_id,
                    "tid": tid_b,
                    "email": f"{target_user_id}@t.rs",
                },
            )
    finally:
        await sup_setup.dispose()

    settings = CabinetSettings(CABINET_ADMIN_EMAILS=admin_email)
    session = {"uid": str(admin_user_id), "tid": str(tid_a)}

    def factory():
        return sessionmaker_()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: session
    app.dependency_overrides[deps.get_settings] = lambda: settings
    try:
        # 1. We disable tid_b through SECURITY DEFINER, being under GUC=tid_a.
        async with sessionmaker_() as db:
            await db.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            await admin_svc.disable_subscriber(
                db, admin_uid=str(admin_user_id), tenant_id=str(tid_b)
            )

        # 2. list_users/get_user under the app role MUST see the disabled one
        # (deleted_at is filled, not filtered out).
        async with sessionmaker_() as db:
            await db.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            users, _total = await admin_svc.list_users(db, page=1, page_size=1000)
            target_row = await admin_svc.get_user(db, str(target_user_id))
            await db.commit()

        listed = {u.id: u for u in users}
        assert str(target_user_id) in listed, (
            "the disabled subscriber disappeared from app_admin_list_users — "
            "the same defect that 0039 closes"
        )
        assert listed[str(target_user_id)].deleted_at is not None
        assert listed[str(target_user_id)].is_disabled is True
        assert target_row is not None, (
            "app_admin_get_user returned None for the disabled one — the card "
            "is unreachable (404), the “Restore” button would be unreachable too"
        )
        assert target_row.deleted_at is not None
        assert target_row.is_disabled is True

        # 3. The card gives 200 (not 404) with the badge and the button “Restore”.
        client = TestClient(app, follow_redirects=False)
        resp = client.get(f"/admin/users/{target_user_id}")
        assert resp.status_code == 200, resp.text
        assert "Отключён" in resp.text
        assert "Вернуть подписчика" in resp.text
        assert "Отключить подписчика" not in resp.text

        # 4. The return — deleted_at is NULL again, the card is “Disable” again.
        async with sessionmaker_() as db:
            await db.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            await admin_svc.enable_subscriber(
                db, admin_uid=str(admin_user_id), tenant_id=str(tid_b)
            )

        async with sessionmaker_() as db:
            await db.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tid_a)},
            )
            restored_row = await admin_svc.get_user(db, str(target_user_id))
            await db.commit()
        assert restored_row is not None
        assert restored_row.deleted_at is None
        assert restored_row.is_disabled is False

        resp = client.get(f"/admin/users/{target_user_id}")
        assert resp.status_code == 200, resp.text
        assert "Отключён" not in resp.text
        assert "Отключить подписчика" in resp.text
        assert "Вернуть подписчика" not in resp.text
    finally:
        app.dependency_overrides.clear()
        sup_clean = create_async_engine(superuser_url, poolclass=NullPool, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text("DELETE FROM auth_tokens WHERE user_id IN (:a, :b)"),
                    {"a": admin_user_id, "b": target_user_id},
                )
                await conn.execute(
                    text("DELETE FROM users WHERE id IN (:a, :b)"),
                    {"a": admin_user_id, "b": target_user_id},
                )
                await conn.execute(
                    text("UPDATE tenants SET access_until = NULL WHERE id = :tid"),
                    {"tid": tid_b},
                )
        finally:
            await sup_clean.dispose()


@pytest.mark.asyncio
async def test_bot_trial_decision_under_app_role_extends_access(sessionmaker_):
    """Regression of 06.08.2026: the decision buttons in the bot WORK and EXTEND the access.

    The defect the test catches: ``bots.trial_service`` went by raw SQL over ``trial_requests``/``tenants``/``users``, while the bot runs under the app role and does NOT set the GUC (a cross-tenant operation has no tenant of its own) — any press of “3 / 15 / 30 days / reject” and the ``/trials`` command fell with ``UndefinedObjectError``. The mock tests do not see this: the mock executes any SQL.

    The session here is WITHOUT a GUC — exactly like ``session_scope()`` in the bot.

    The second expectation — the arithmetic (#497): the tenant has access until ``now()+11d``,
    we approve for 3 days → it must become ``≈now()+14d``. Setting ``now()+3d``
    would cut 8 days of the already granted access.
    """
    from src.bots import trial_service

    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    tid = uuid.uuid4()
    user_id = uuid.uuid4()
    chan_id = uuid.uuid4()
    request_id = uuid.uuid4()
    chat_id = "770077"

    sup_setup = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    try:
        async with sup_setup.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status, access_until) "
                    "VALUES (:id, 'RS', 'lite', 'trial', now() + INTERVAL '11 days')"
                ),
                {"id": tid},
            )
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role, email) "
                    "VALUES (:id, :tid, 'ru', 'user', :email)"
                ),
                {"id": user_id, "tid": tid, "email": f"{user_id}@t.rs"},
            )
            await conn.execute(
                text(
                    "INSERT INTO user_channels "
                    "(id, user_id, channel, external_id, status) "
                    "VALUES (:id, :uid, 'telegram', :eid, 'active')"
                ),
                {"id": chan_id, "uid": user_id, "eid": chat_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO trial_requests (id, tenant_id, status, comment) "
                    "VALUES (:id, :tid, 'pending', 'нужен доступ')"
                ),
                {"id": request_id, "tid": tid},
            )
    finally:
        await sup_setup.dispose()

    try:
        # (1) /trials under the bot role WITHOUT a GUC — earlier it fell with UndefinedObjectError.
        async with sessionmaker_() as session:
            pending = await trial_service.list_pending(session)
            await session.commit()
        assert str(request_id) in {p.request_id for p in pending}

        # (2) The decision on a request — also without a GUC, cross-tenant.
        async with sessionmaker_() as session:
            outcome = await trial_service.decide(
                session, request_id=str(request_id), approve=True, days=3
            )
        assert outcome.status == "approved"
        assert outcome.days == 3
        assert outcome.chat_id == chat_id, "the recipient of the notice must resolve"
        assert outcome.email == f"{user_id}@t.rs"

        # (3) An EXTENSION, not a setting: 11 days remaining + 3 = ~14, not 3.
        sup_verify = create_async_engine(superuser_url, poolclass=NullPool, future=True)
        try:
            async with sup_verify.begin() as conn:
                days_left = (
                    await conn.execute(
                        text(
                            "SELECT EXTRACT(EPOCH FROM (access_until - now())) / 86400 "
                            "FROM tenants WHERE id = :tid"
                        ),
                        {"tid": tid},
                    )
                ).scalar_one()
                status = (
                    await conn.execute(
                        text("SELECT status FROM trial_requests WHERE id = :id"),
                        {"id": request_id},
                    )
                ).scalar_one()
        finally:
            await sup_verify.dispose()
        assert status == "approved"
        assert 13.9 < float(days_left) < 14.1, (
            f"an approval for 3 days gave {days_left} days of access: the extension must "
            "be computed from the larger of the dates (the effective term / now), otherwise it "
            "cuts the already granted access"
        )
        assert outcome.access_until is not None

        # (4) Idempotency: a repeat of the press — already, there is no second issuing.
        async with sessionmaker_() as session:
            repeat = await trial_service.decide(
                session, request_id=str(request_id), approve=True, days=30
            )
        assert repeat.status == "already"
    finally:
        sup_clean = create_async_engine(superuser_url, poolclass=NullPool, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM billing_notification_outbox WHERE tenant_id = :t"
                    ),
                    {"t": tid},
                )
                await conn.execute(
                    text("DELETE FROM trial_requests WHERE id = :id"),
                    {"id": request_id},
                )
                await conn.execute(
                    text("DELETE FROM user_channels WHERE id = :id"), {"id": chan_id}
                )
                await conn.execute(
                    text("DELETE FROM users WHERE id = :id"), {"id": user_id}
                )
                await conn.execute(
                    text("DELETE FROM tenants WHERE id = :id"), {"id": tid}
                )
        finally:
            await sup_clean.dispose()


@pytest.mark.asyncio
async def test_bot_pause_and_list_isolated_under_app_role(sessionmaker_, tenant_ids):
    """The bot: /pause and “📋 My tenders” under the app role (FORCE RLS), the path src.bots.manage.

    Each tenant has its own working template, its own tender in the status
    «watching» and its own tender sent to Telegram. Under tenant A:

    * the toggle by the id of template B → ``gone``, at B ``is_active`` does not change
      (the callback is forged — a foreign template is not found);
    * the toggle of its own → the pause, “enable all” → back, B is not touched;
    * the list — exactly the tenders of A, the links “Open in the cabinet” — to the template A.
    """
    from src.bots import manage

    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    prof_a, prof_b = uuid.uuid4(), uuid.uuid4()
    watch_a, watch_b, sent_a, sent_b = (uuid.uuid4() for _ in range(4))
    all_tenders = [str(t) for t in (watch_a, watch_b, sent_a, sent_b)]

    sup = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    try:
        async with sup.begin() as conn:
            for pid, tid in [(prof_a, tid_a), (prof_b, tid_b)]:
                await conn.execute(
                    text(
                        "INSERT INTO profiles (id, tenant_id, name, is_active) "
                        "VALUES (:id, :tid, 'bot pause', true)"
                    ),
                    {"id": pid, "tid": tid},
                )
            for tender_id, title in [
                (watch_a, "Слежу A"),
                (watch_b, "Слежу B"),
                (sent_a, "Прислано A"),
                (sent_b, "Прислано B"),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO tenders_normalized "
                        "(id, source_key, country_code, title, amount_original, "
                        " currency_original, first_seen_at) "
                        "VALUES (:id, :sk, 'RS', :t, 1000, 'RSD', NOW())"
                    ),
                    {"id": tender_id, "sk": f"bm-{tender_id}", "t": title},
                )
            for tid, tender_id in [(tid_a, watch_a), (tid_b, watch_b)]:
                await conn.execute(
                    text(
                        "INSERT INTO tender_user_status (tenant_id, tender_id, status) "
                        "VALUES (:tid, :t, 'watching')"
                    ),
                    {"tid": tid, "t": tender_id},
                )
            for pid, tender_id in [(prof_a, sent_a), (prof_b, sent_b)]:
                await conn.execute(
                    text(
                        "INSERT INTO delivery_outbox "
                        "(profile_id, tender_id, channel, status, created_at, sent_at) "
                        "VALUES (:pid, :t, 'telegram', 'sent', NOW(), NOW())"
                    ),
                    {"pid": pid, "t": tender_id},
                )
    finally:
        await sup.dispose()

    async def _active(pid) -> bool:
        eng = create_async_engine(superuser_url, poolclass=NullPool, future=True)
        try:
            async with eng.connect() as conn:
                return (
                    await conn.execute(
                        text("SELECT is_active FROM profiles WHERE id = :id"),
                        {"id": pid},
                    )
                ).scalar_one()
        finally:
            await eng.dispose()

    try:
        async with sessionmaker_() as session:
            assert (
                await manage.toggle_profile(session, str(tid_a), str(prof_b))
                == manage.GONE
            )
        assert await _active(prof_b) is True, "the toggle of A switched the template B"

        async with sessionmaker_() as session:
            assert (
                await manage.toggle_profile(session, str(tid_a), str(prof_a))
                == manage.PAUSED
            )
        assert await _active(prof_a) is False
        assert await _active(prof_b) is True

        async with sessionmaker_() as session:
            result = await manage.set_all_profiles(session, str(tid_a), True)
        assert result == manage.BulkResult(changed=1, blocked=0)
        assert await _active(prof_a) is True

        async with sessionmaker_() as session:
            items, has_more = await manage.load_my_tenders(session, str(tid_a))
            await session.commit()
        assert [i.tender_id for i in items] == [str(watch_a), str(sent_a)]
        assert {i.profile_id for i in items} == {str(prof_a)}
        assert has_more is False
    finally:
        sup_clean = create_async_engine(superuser_url, poolclass=NullPool, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text("DELETE FROM delivery_outbox " "WHERE profile_id IN (:a, :b)"),
                    {"a": prof_a, "b": prof_b},
                )
                await conn.execute(
                    text(
                        "DELETE FROM tender_user_status "
                        "WHERE tender_id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": all_tenders},
                )
                await conn.execute(
                    text("DELETE FROM profiles WHERE id IN (:a, :b)"),
                    {"a": prof_a, "b": prof_b},
                )
                await conn.execute(
                    text(
                        "DELETE FROM tenders_normalized "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": all_tenders},
                )
        finally:
            await sup_clean.dispose()


@pytest.mark.asyncio
async def test_calendar_deadlines_isolation_under_app_role(sessionmaker_, tenant_ids):
    """Regression «Svi rokovi (.ics)»: the calendar of the deadlines under the app role (FORCE RLS).

    Both tenants have “following”/“active” with a FUTURE deadline — exactly its own must get
    into the file of each. A foreign tender, a tender in the status “applied” and a tender with a
    passed deadline — are not in the selection. The mutation check: remove
    ``repo.set_tenant`` from ``get_calendar_deadlines`` — the test turns red
    (the GUC is not set → UndefinedObjectError).
    """
    from datetime import datetime, timedelta, timezone

    from src.api.cabinet.workflow_service import get_calendar_deadlines

    tid_a, tid_b = tenant_ids
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=10)
    past = now - timedelta(days=10)

    watch_a, active_b = uuid.uuid4(), uuid.uuid4()
    submitted_a, expired_a = uuid.uuid4(), uuid.uuid4()
    all_tenders = [str(t) for t in (watch_a, active_b, submitted_a, expired_a)]

    sup_setup = create_async_engine(superuser_url, future=True)
    try:
        async with sup_setup.begin() as conn:
            for tender_id, title, deadline in [
                (watch_a, "Календарь A", future),
                (active_b, "Календарь B", future),
                (submitted_a, "Подано A", future),
                (expired_a, "Прошло A", past),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO tenders_normalized "
                        "(id, source_key, country_code, title, amount_original, "
                        " currency_original, deadline_at, first_seen_at) "
                        "VALUES (:id, :sk, 'RS', :t, 1000, 'RSD', :d, NOW())"
                    ),
                    {
                        "id": tender_id,
                        "sk": f"ics-{tender_id}",
                        "t": title,
                        "d": deadline,
                    },
                )
            for tid, tender_id, status in [
                (tid_a, watch_a, "watching"),
                (tid_b, active_b, "active"),
                (tid_a, submitted_a, "submitted"),
                (tid_a, expired_a, "active"),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO tender_user_status (tenant_id, tender_id, status) "
                        "VALUES (:tid, :t, :s)"
                    ),
                    {"tid": tid, "t": tender_id, "s": status},
                )
    finally:
        await sup_setup.dispose()

    try:
        async with sessionmaker_() as session:
            rows_a = await get_calendar_deadlines(session, str(tid_a), now)
            await session.commit()
        assert {r.tender_id for r in rows_a} == {str(watch_a)}, (
            "the calendar of tenant A: only “following” with a future deadline, "
            f"received: {sorted(r.tender_id for r in rows_a)}"
        )

        async with sessionmaker_() as session:
            rows_b = await get_calendar_deadlines(session, str(tid_b), now)
            await session.commit()
        assert {r.tender_id for r in rows_b} == {str(active_b)}
    finally:
        sup_clean = create_async_engine(superuser_url, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM tender_user_status "
                        "WHERE tender_id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": all_tenders},
                )
                await conn.execute(
                    text(
                        "DELETE FROM tenders_normalized "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": all_tenders},
                )
        finally:
            await sup_clean.dispose()
