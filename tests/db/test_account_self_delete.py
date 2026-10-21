"""The self-deletion of an account under ``tendersrb_app`` (FORCE RLS) on a real PG (LEG-06).

It skips without ``TEST_RLS_DATABASE_URL`` (like the whole tests/db/test_rls_isolation).
The run and the mutations — ``docs/reports/TESTING_account-delete-logout.md``.
The expectations — literals from the fact, not through the code under test.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api import anti_abuse as aa
from src.api import deps, security
from src.api.cabinet import account_delete_service as svc
from src.api.cabinet import auth_service
from src.api.config import CabinetSettings
from src.api.emailer import MockEmailSender

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="TEST_RLS_DATABASE_URL не задан — RLS-интеграция пропускается"
)


@pytest_asyncio.fixture
async def sm():
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


@pytest_asyncio.fixture
async def sup():
    """The superuser: the setup and the check (bypasses RLS), at the end — the cleanup."""
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    created: list[str] = []
    tenders: list = (
        []
    )  # the rows of tenders_normalized created by the test (the note is deleted by the service itself)
    yield eng, created, tenders
    async with eng.begin() as conn:
        for tid in created:
            await conn.execute(
                text(
                    "DELETE FROM user_channels WHERE user_id IN "
                    "(SELECT id FROM users WHERE tenant_id = CAST(:t AS uuid))"
                ),
                {"t": tid},
            )
            await conn.execute(
                text(
                    "DELETE FROM tenders_normalized WHERE id IN (SELECT tender_id "
                    "FROM tender_notes WHERE tenant_id = CAST(:t AS uuid))"
                ),
                {"t": tid},
            )
            for tbl in ("tender_notes", "profiles", "users", "billing_subscriptions"):
                await conn.execute(
                    text(f"DELETE FROM {tbl} WHERE tenant_id = CAST(:t AS uuid)"),
                    {"t": tid},
                )
            await conn.execute(
                text("DELETE FROM tenants WHERE id = CAST(:t AS uuid)"), {"t": tid}
            )
        for (
            tender
        ) in (
            tenders
        ):  # the note was deleted by the service itself — we do not look through them
            await conn.execute(
                text("DELETE FROM tenders_normalized WHERE id = :id"), {"id": tender}
            )
        await conn.execute(
            text(
                "DELETE FROM admin_audit_log WHERE actor_email = 'self-service' "
                "AND target_tenant_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": created},
        )
    await eng.dispose()


_SETTINGS = CabinetSettings(CABINET_BASE_URL="https://nab.rs", CABINET_SECRET_KEY="t")


async def _signup(sm, email: str) -> auth_service.SignupResult:
    async with sm() as s:
        return await auth_service.signup(
            s,
            email=email,
            turnstile_token="ok",
            turnstile=aa.NoopTurnstileVerifier(True),
            email_validator=aa.NoopEmailValidator(
                deliverable=True, disposable=False, risky=False
            ),
            email_sender=MockEmailSender(),
            settings=_SETTINGS,
        )


async def _one(eng, sql: str, **p):
    async with eng.connect() as c:
        return (await c.execute(text(sql), p)).first()


async def _make_account(sm, sup, email: str):
    """A real registration (signup) + a channel/profile/note by the superuser."""
    eng, created, tenders = sup
    res = await _signup(sm, email)
    assert res.outcome == auth_service.TRIAL_STARTED
    row = await _one(eng, "SELECT id, tenant_id FROM users WHERE email = :e", e=email)
    uid, tid = str(row.id), str(row.tenant_id)
    created.append(tid)
    async with eng.begin() as c:
        await c.execute(
            text(
                "UPDATE users SET phone = '+38160111222', phone_verified_at = now() "
                "WHERE id = CAST(:u AS uuid)"
            ),
            {"u": uid},
        )
        await c.execute(
            text(
                "INSERT INTO user_channels (user_id, channel, external_id, status) "
                "VALUES (CAST(:u AS uuid), 'telegram', '777000111', 'active')"
            ),
            {"u": uid},
        )
        await c.execute(
            text(
                "INSERT INTO profiles (tenant_id, name, is_active) "
                "VALUES (CAST(:t AS uuid), 'Moj profil', true)"
            ),
            {"t": tid},
        )
        tender = (
            await c.execute(
                text("INSERT INTO tenders_normalized DEFAULT VALUES RETURNING id")
            )
        ).scalar_one()
        tenders.append(tender)
        await c.execute(
            text(
                "INSERT INTO tender_notes (tenant_id, tender_id, body) "
                "VALUES (CAST(:t AS uuid), :td, 'tajna beleška')"
            ),
            {"t": tid, "td": tender},
        )
    return uid, tid


def _email() -> str:
    return f"del-{uuid.uuid4().hex[:10]}@example.rs"


@pytest.mark.asyncio
async def test_last_user_delete_three_axes_and_anonymisation(sm, sup):
    eng = sup[0]
    email = _email()
    uid, tid = await _make_account(sm, sup, email)
    # A foreign tenant — must not be touched.
    other_uid, other_tid = await _make_account(sm, sup, _email())

    async with sm() as s:
        res = await svc.delete_own_account(
            s, tenant_id=tid, user_id=uid, confirm_email=email.upper()
        )
    assert res.outcome == "ok" and res.tenant_deleted is True
    assert res.email == email  # for the confirmation letter, before the anonymisation

    # Axis 1: the access is closed.
    t = await _one(
        eng,
        "SELECT access_until <= now() AS closed, status, display_name, tax_id "
        "FROM tenants WHERE id = CAST(:t AS uuid)",
        t=tid,
    )
    assert t.closed is True and t.status == "deleted"
    # Axis 2: the channel is disabled, the chat_id is erased.
    ch = await _one(
        eng,
        "SELECT status, external_id FROM user_channels "
        "WHERE user_id = CAST(:u AS uuid)",
        u=uid,
    )
    assert (ch.status, ch.external_id) == ("disabled", None)
    # Axis 3 + the anonymisation of the user.
    u = await _one(
        eng,
        "SELECT deleted_at IS NOT NULL AS gone, email, phone, "
        "email_verified_at, email_status FROM users "
        "WHERE id = CAST(:u AS uuid)",
        u=uid,
    )
    assert u.gone is True
    assert u.email == f"deleted+{uid}@deleted.invalid"
    assert u.phone is None and u.email_verified_at is None
    assert u.email_status == "deleted"
    # The profile and the notes.
    p = await _one(
        eng,
        "SELECT is_active, deleted_at IS NOT NULL AS gone, name "
        "FROM profiles WHERE tenant_id = CAST(:t AS uuid)",
        t=tid,
    )
    assert (p.is_active, p.gone, p.name) == (False, True, None)
    n = await _one(
        eng,
        "SELECT count(*) AS n FROM tender_notes " "WHERE tenant_id = CAST(:t AS uuid)",
        t=tid,
    )
    assert n.n == 0
    # Audit: without the email.
    a = await _one(
        eng,
        "SELECT action, details::text AS d FROM admin_audit_log "
        "WHERE actor_email = 'self-service' "
        "AND target_user_id = CAST(:u AS uuid)",
        u=uid,
    )
    assert a.action == "account_self_delete"
    assert '"tenant_deleted": true' in a.d and "@" not in a.d

    # The foreign tenant is intact.
    ot = await _one(
        eng,
        "SELECT status, access_until > now() AS open FROM tenants "
        "WHERE id = CAST(:t AS uuid)",
        t=other_tid,
    )
    assert ot.status == "trial" and ot.open is True
    ou = await _one(
        eng,
        "SELECT deleted_at, email FROM users " "WHERE id = CAST(:u AS uuid)",
        u=other_uid,
    )
    assert ou.deleted_at is None and ou.email.endswith("@example.rs")
    och = await _one(
        eng,
        "SELECT status, external_id FROM user_channels "
        "WHERE user_id = CAST(:u AS uuid)",
        u=other_uid,
    )
    assert (och.status, och.external_id) == ("active", "777000111")
    on = await _one(
        eng,
        "SELECT count(*) AS n FROM tender_notes " "WHERE tenant_id = CAST(:t AS uuid)",
        t=other_tid,
    )
    assert on.n == 1


@pytest.mark.asyncio
async def test_deleted_user_cannot_login_or_use_old_cookie(sm, sup):
    email = _email()
    uid, tid = await _make_account(sm, sup, email)
    async with sm() as s:
        assert (
            await svc.delete_own_account(
                s, tenant_id=tid, user_id=uid, confirm_email=email
            )
        ).outcome == "ok"

    # The login resolver (0024) by the old address — empty; the magic-link is not issued.
    async with sm() as s:
        r = await s.execute(
            auth_service._RESOLVE_USER_FOR_AUTH_SQL,
            {"email": security.normalize_email(email)},
        )
        assert r.first() is None
        sender = MockEmailSender()
        assert (
            await auth_service.request_magic_link(
                s, email=email, email_sender=sender, settings=_SETTINGS
            )
            is False
        )
        assert sender.outbox == []
    # An already issued cookie: the gate require_active_access sees deleted_at.
    async with sm() as s:
        await s.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": tid}
        )
        row = (await s.execute(deps._ACCESS_GATE_SQL, {"tid": tid, "uid": uid})).first()
    assert row.deleted_at is not None
    # A repeated deletion — not_found, writes nothing.
    async with sm() as s:
        again = await svc.delete_own_account(
            s, tenant_id=tid, user_id=uid, confirm_email=email
        )
    assert again.outcome == "not_found"


@pytest.mark.asyncio
async def test_other_users_in_tenant_keep_tenant(sm, sup):
    eng = sup[0]
    email = _email()
    uid, tid = await _make_account(sm, sup, email)
    async with eng.begin() as c:
        mate = (
            await c.execute(
                text(
                    "INSERT INTO users (tenant_id, email, role) VALUES "
                    "(CAST(:t AS uuid), 'mate-' || :s || '@example.rs', 'user') RETURNING id"
                ),
                {"t": tid, "s": uuid.uuid4().hex[:8]},
            )
        ).scalar_one()
        await c.execute(
            text(
                "INSERT INTO user_channels (user_id, channel, external_id, status) "
                "VALUES (:u, 'telegram', '555', 'active')"
            ),
            {"u": mate},
        )
    async with sm() as s:
        res = await svc.delete_own_account(
            s, tenant_id=tid, user_id=uid, confirm_email=email
        )
    assert res.outcome == "ok" and res.tenant_deleted is False
    t = await _one(
        eng,
        "SELECT status, access_until > now() AS open FROM tenants "
        "WHERE id = CAST(:t AS uuid)",
        t=tid,
    )
    assert t.status == "trial" and t.open is True
    m = await _one(eng, "SELECT deleted_at FROM users WHERE id = :u", u=mate)
    assert m.deleted_at is None
    mc = await _one(
        eng, "SELECT status, external_id FROM user_channels WHERE user_id = :u", u=mate
    )
    assert (mc.status, mc.external_id) == ("active", "555")
    n = await _one(
        eng,
        "SELECT count(*) AS n FROM tender_notes " "WHERE tenant_id = CAST(:t AS uuid)",
        t=tid,
    )
    assert n.n == 1  # the tenant’s notes stay for a colleague


@pytest.mark.asyncio
async def test_wrong_confirmation_and_paid_subscription_change_nothing(sm, sup):
    eng = sup[0]
    email = _email()
    uid, tid = await _make_account(sm, sup, email)
    async with sm() as s:
        bad = await svc.delete_own_account(
            s, tenant_id=tid, user_id=uid, confirm_email="not-me@example.rs"
        )
    assert bad.outcome == "email_mismatch"
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO billing_subscriptions (tenant_id, provider, status) "
                "VALUES (CAST(:t AS uuid), 'paddle', 'active')"
            ),
            {"t": tid},
        )
    async with sm() as s:
        paid = await svc.delete_own_account(
            s, tenant_id=tid, user_id=uid, confirm_email=email
        )
    assert paid.outcome == "active_subscription"
    u = await _one(
        eng, "SELECT deleted_at, email FROM users WHERE id = CAST(:u AS uuid)", u=uid
    )
    assert u.deleted_at is None and u.email == email
    a = await _one(
        eng,
        "SELECT count(*) AS n FROM admin_audit_log "
        "WHERE target_user_id = CAST(:u AS uuid)",
        u=uid,
    )
    assert a.n == 0


@pytest.mark.asyncio
async def test_resignup_same_email_blocked_by_antiabuse_hash_kept(sm, sup):
    """anti-abuse is NOT reset: the hash stays, a repeated trial on the same
    canonical address (incl. +alias) is refused; another address — passes."""
    eng = sup[0]
    base = f"re{uuid.uuid4().hex[:8]}"
    email = f"{base}@gmail.com"
    uid, tid = await _make_account(sm, sup, email)
    async with sm() as s:
        assert (
            await svc.delete_own_account(
                s, tenant_id=tid, user_id=uid, confirm_email=email
            )
        ).outcome == "ok"
    h = await _one(
        eng,
        "SELECT value_hash, value_last4 FROM anti_abuse_identifiers "
        "WHERE tenant_id = CAST(:t AS uuid)",
        t=tid,
    )
    assert h.value_hash and h.value_last4 is None
    again = await _signup(sm, email)
    assert again.outcome == auth_service.REJECTED_DUPLICATE
    alias = await _signup(sm, f"{base}+x@gmail.com")
    assert alias.outcome == auth_service.REJECTED_DUPLICATE
    fresh = await _signup(sm, _email())
    assert fresh.outcome == auth_service.TRIAL_STARTED
    row = await _one(
        eng,
        "SELECT tenant_id FROM users WHERE email LIKE 'del-%' "
        "ORDER BY created_at DESC LIMIT 1",
    )
    sup[1].append(str(row.tenant_id))
