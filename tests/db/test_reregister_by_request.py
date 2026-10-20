"""The repeated registration after self-deletion — only by request (#658, owner 10.10).

Under ``tendersrb_app`` (FORCE RLS) on a real PG; it skips without
``TEST_RLS_DATABASE_URL``. The run/mutations — ``docs/reports/TESTING_reregister-by-request.md``.
The expectations — literals from the fact, not via ``t()``/the code under the test.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api import anti_abuse as aa
from src.api import deps, security
from src.api.admin import admin_service
from src.api.cabinet import account_delete_service as delete_svc
from src.api.cabinet import auth_service as svc
from src.api.config import CabinetSettings
from src.api.emailer import MockEmailSender
from src.api.main import app

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="TEST_RLS_DATABASE_URL не задан — RLS-интеграция пропускается"
)

_SETTINGS = CabinetSettings(CABINET_BASE_URL="https://nab.rs", CABINET_SECRET_KEY="t")
_OPERATOR = "ops@nab.rs"


@pytest_asyncio.fixture
async def sm():
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


@pytest_asyncio.fixture
async def sup():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    created: list[str] = []
    yield eng, created
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM admin_audit_log WHERE target_tenant_id = ANY("
                "SELECT id FROM tenants WHERE id::text = ANY(CAST(:ids AS text[])))"
            ),
            {"ids": created},
        )
        for tid in created:
            for tbl in ("users", "profiles"):
                await conn.execute(
                    text(f"DELETE FROM {tbl} WHERE tenant_id = CAST(:t AS uuid)"),
                    {"t": tid},
                )
            await conn.execute(
                text("DELETE FROM tenants WHERE id = CAST(:t AS uuid)"), {"t": tid}
            )
    await eng.dispose()


def _email() -> str:
    return f"rr-{uuid.uuid4().hex[:10]}@firma.rs"


async def _signup(sm, email: str, *, sender=None, locale="sr") -> svc.SignupResult:
    async with sm() as s:
        return await svc.signup(
            s,
            email=email,
            turnstile_token="ok",
            turnstile=aa.NoopTurnstileVerifier(True),
            email_validator=aa.NoopEmailValidator(),
            email_sender=sender or MockEmailSender(),
            settings=_SETTINGS,
            locale=locale,
        )


async def _one(eng, sql: str, **p):
    async with eng.connect() as c:
        return (await c.execute(text(sql), p)).first()


async def _deleted_account(sm, sup, email: str) -> str:
    """Registration + self-deletion; returns the tenant_id of the deleted account."""
    eng, created = sup
    assert (await _signup(sm, email)).outcome == svc.TRIAL_STARTED
    row = await _one(eng, "SELECT id, tenant_id FROM users WHERE email = :e", e=email)
    created.append(str(row.tenant_id))
    async with sm() as s:
        res = await delete_svc.delete_own_account(
            s, tenant_id=str(row.tenant_id), user_id=str(row.id), confirm_email=email
        )
    assert res.outcome == "ok"
    return str(row.tenant_id)


async def _request(sm, email: str, locale="sr"):
    async with sm() as s:
        return await svc.request_reregistration(s, email=email, locale=locale)


async def _decide(sm, rid: str, *, approve: bool, sender, comment=None):
    async with sm() as s:
        fn = admin_service.approve_request if approve else admin_service.reject_request
        kw = {"days": 30} if approve else {}
        return await fn(
            s,
            admin_uid="u",
            request_id=rid,
            comment=comment,
            actor_email=_OPERATOR,
            email_sender=sender,
            settings=_SETTINGS,
            **kw,
        )


@pytest.mark.asyncio
async def test_before_approval_signup_stays_blocked_and_request_is_single(sm, sup):
    eng, _ = sup
    email = _email()
    old_tid = await _deleted_account(sm, sup, email)

    # The dead end as before: without a request/approval the registration is refused.
    res = await _signup(sm, email)
    assert res.outcome == "rejected_duplicate"

    rid = await _request(sm, email)
    assert rid is not None
    row = await _one(
        eng,
        "SELECT status, comment, tenant_id::text AS tid FROM trial_requests "
        "WHERE id = CAST(:r AS uuid)",
        r=rid,
    )
    assert row.status == "pending" and row.tid == old_tid
    assert row.comment == f"reregister_request:sr:{email}"

    # A repeat while a request is pending is blocked: a second request is not created.
    assert await _request(sm, email) is None
    cnt = await _one(
        eng,
        "SELECT count(*) AS n FROM trial_requests WHERE tenant_id = CAST(:t AS uuid)",
        t=old_tid,
    )
    assert cnt.n == 1
    # The request itself does NOT open the registration.
    assert (await _signup(sm, email)).outcome == "rejected_duplicate"


@pytest.mark.asyncio
async def test_live_or_unknown_address_creates_no_request(sm, sup):
    eng, created = sup
    live = _email()
    assert (await _signup(sm, live)).outcome == svc.TRIAL_STARTED
    row = await _one(eng, "SELECT tenant_id FROM users WHERE email = :e", e=live)
    created.append(str(row.tenant_id))

    assert await _request(sm, live) is None  # a live trial account
    assert await _request(sm, _email()) is None  # there is no address in the DB
    n = await _one(
        eng,
        "SELECT count(*) AS n FROM trial_requests WHERE comment LIKE 'reregister_request:%' "
        "AND tenant_id = CAST(:t AS uuid)",
        t=str(row.tenant_id),
    )
    assert n.n == 0


@pytest.mark.asyncio
async def test_approve_opens_signup_once_new_account_no_second_trial(sm, sup):
    eng, created = sup
    email = _email()
    old_tid = await _deleted_account(sm, sup, email)
    rid = await _request(sm, email, locale="ru")
    sender = MockEmailSender()

    outcome = await _decide(
        sm, rid, approve=True, sender=sender, comment="Добро пожаловать"
    )
    assert outcome == "reregister_approved"
    # A letter to the person: to his address, in the language of the request (ru), with a link to the registration.
    assert len(sender.outbox) == 1
    mail = sender.outbox[0]
    assert mail.to == email
    assert mail.subject == "Повторная регистрация одобрена — TenderSRB"
    assert "https://nab.rs/signup" in mail.text_body
    assert "Добро пожаловать" in mail.text_body
    # The decision is recorded, the address from the request is wiped, the audit without the email.
    row = await _one(
        eng,
        "SELECT status, comment FROM trial_requests WHERE id = CAST(:r AS uuid)",
        r=rid,
    )
    assert (row.status, row.comment) == ("approved", "reregister_request")
    audit = await _one(
        eng,
        "SELECT actor_email, action, details::text AS d, ip FROM admin_audit_log "
        "WHERE target_tenant_id = CAST(:t AS uuid) AND action = 'approve_reregister'",
        t=old_tid,
    )
    assert audit.action == "approve_reregister"
    assert "@" not in audit.d and audit.ip is None
    # A repeated decision of the same request — «already», no second letter.
    assert await _decide(sm, rid, approve=True, sender=sender) == "already"
    assert len(sender.outbox) == 1

    # The registration passes; the account is NEW.
    res = await _signup(sm, email)
    assert res.outcome == svc.TRIAL_STARTED
    new = await _one(
        eng,
        "SELECT tenant_id::text AS tid, deleted_at FROM users WHERE email = :e",
        e=email,
    )
    created.append(new.tid)
    assert new.tid != old_tid and new.deleted_at is None
    # A trial at this address already existed within 12 months → it is not issued again:
    # the access is created “expired” (the extension is decided by the operator via the request from the cabinet, #657).
    ten = await _one(
        eng,
        "SELECT status, access_until <= now() AS frozen FROM tenants "
        "WHERE id = CAST(:t AS uuid)",
        t=new.tid,
    )
    assert ten.frozen is True

    # The approval is single-use: now the address is taken by the new account.
    assert (await _signup(sm, email)).outcome == "rejected_duplicate"
    # ...and a request for a live account does not create a request.
    assert await _request(sm, email) is None


@pytest.mark.asyncio
async def test_expired_trial_window_gives_trial_again(sm, sup):
    eng, created = sup
    email = _email()
    old_tid = await _deleted_account(sm, sup, email)
    async with eng.begin() as c:  # the 12-month window has passed
        await c.execute(
            text(
                "UPDATE anti_abuse_identifiers SET expires_at = now() - interval '1 day' "
                "WHERE tenant_id = CAST(:t AS uuid)"
            ),
            {"t": old_tid},
        )
    rid = await _request(sm, email)
    await _decide(sm, rid, approve=True, sender=MockEmailSender())
    assert (await _signup(sm, email)).outcome == svc.TRIAL_STARTED
    new = await _one(
        eng, "SELECT tenant_id::text AS tid FROM users WHERE email = :e", e=email
    )
    created.append(new.tid)
    ten = await _one(
        eng,
        "SELECT access_until > now() AS live FROM tenants WHERE id = CAST(:t AS uuid)",
        t=new.tid,
    )
    assert ten.live is True


@pytest.mark.asyncio
async def test_reject_sends_refusal_and_keeps_block(sm, sup):
    email = _email()
    await _deleted_account(sm, sup, email)
    rid = await _request(sm, email)
    sender = MockEmailSender()
    assert await _decide(sm, rid, approve=False, sender=sender) == "reregister_rejected"
    assert len(sender.outbox) == 1 and sender.outbox[0].to == email
    assert sender.outbox[0].subject == "Ponovna registracija — odluka — TenderSRB"
    assert (await _signup(sm, email)).outcome == "rejected_duplicate"


@pytest.mark.asyncio
async def test_generic_decide_cannot_resolve_reregister_request(sm, sup):
    """The bot/the common path must not “eat” the request: it is decided only by its own function."""
    eng, _ = sup
    email = _email()
    await _deleted_account(sm, sup, email)
    rid = await _request(sm, email)
    async with sm() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT * FROM app_admin_decide_trial_request("
                    "CAST(:r AS uuid), 'approved', 30, NULL)"
                ),
                {"r": rid},
            )
        ).all()
        await s.commit()
    assert rows == []
    st = await _one(
        eng, "SELECT status FROM trial_requests WHERE id = CAST(:r AS uuid)", r=rid
    )
    assert st.status == "pending"


@pytest.mark.asyncio
async def test_admin_list_shows_reregister_next_to_other_requests(sm, sup):
    email = _email()
    await _deleted_account(sm, sup, email)
    rid = await _request(sm, email)
    async with sm() as s:
        rows, _total = await admin_service.list_trial_requests(s, page=1, page_size=500)
    mine = [r for r in rows if r.id == rid]
    assert len(mine) == 1
    assert (mine[0].plan, mine[0].email) == ("reregister", email)


def _page_text(html: str) -> str:
    sec = re.search(r"<section.*?</section>", html, re.S).group(0)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", sec)).strip()


@pytest.mark.asyncio
async def test_http_screen_button_and_uniform_answer(sm, sup):
    """The screen with a button on ANY “address taken”; the answer to the request is the same for a deleted,
    a live and an unknown address (anti-enumeration)."""
    eng, created = sup
    deleted, live, unknown = _email(), _email(), _email()
    await _deleted_account(sm, sup, deleted)
    assert (await _signup(sm, live)).outcome == svc.TRIAL_STARTED
    row = await _one(eng, "SELECT tenant_id FROM users WHERE email = :e", e=live)
    created.append(str(row.tenant_id))

    sender = MockEmailSender()
    app.dependency_overrides[deps.get_session_factory] = lambda: sm
    app.dependency_overrides[deps.get_turnstile] = lambda: aa.NoopTurnstileVerifier(
        True
    )
    app.dependency_overrides[deps.get_email_validator] = lambda: aa.NoopEmailValidator()
    app.dependency_overrides[deps.get_email_sender] = lambda: sender
    try:
        client = TestClient(app)
        client.cookies.set("lang", "sr")
        pages = {}
        for name, em in (("deleted", deleted), ("live", live)):
            r = client.post(
                "/signup",
                data={"email": em, "cf-turnstile-response": "x", "consent": "yes"},
            )
            assert r.status_code == 200
            assert 'action="/signup/reregister"' in r.text
            assert "Zatraži ponovnu registraciju" in r.text
            pages[name] = _page_text(r.text)
        # The “taken” screen for a deleted and a live one — the same text.
        assert pages["deleted"] == pages["live"]
        assert "Ako je nalog sa ovom adresom ranije obrisan" in pages["deleted"]

        answers = []
        for em in (deleted, live, unknown):
            r = client.post(
                "/signup/reregister",
                data={"email": em, "cf-turnstile-response": "x"},
            )
            assert r.status_code == 200
            answers.append(r.text)
        assert "Zahtev je primljen" in answers[0]
        assert answers[0] == answers[1] == answers[2]
    finally:
        app.dependency_overrides.clear()

    # Exactly one request is created (for the deleted address), not three.
    n = await _one(
        eng,
        "SELECT count(*) AS n FROM trial_requests "
        "WHERE comment LIKE 'reregister_request:%' AND status = 'pending' "
        "AND comment LIKE '%' || :e",
        e=deleted,
    )
    assert n.n == 1


@pytest.mark.asyncio
async def test_medium_risk_signup_keeps_approval_for_next_signup(sm, sup):
    """The approval is spent only when the registration really completes (is_low).

    The medium risk with the step-up enabled goes to pending_verification without a trial: the claim before this branch burned the approval and rewrote the hash of the old address, and the address stayed taken by the new unconfirmed account. The expectations — literals.
    """
    eng, created = sup
    email = _email()
    old_tid = await _deleted_account(sm, sup, email)
    rid = await _request(sm, email)
    assert await _decide(sm, rid, approve=True, sender=MockEmailSender()) == (
        "reregister_approved"
    )

    step_up = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="t",
        REQUIRE_STEP_UP_FOR_MEDIUM=True,
    )
    async with sm() as s:
        res = await svc.signup(
            s,
            email=email,
            turnstile_token="ok",
            turnstile=aa.NoopTurnstileVerifier(True),
            email_validator=aa.NoopEmailValidator(risky=True),
            email_sender=MockEmailSender(),
            settings=step_up,
            locale="sr",
        )
    # A medium risk under step-up: the registration does NOT complete (neither a new record, nor a trial).
    assert res.outcome == "rejected_duplicate"
    row = await _one(
        eng,
        "SELECT reregister_approved_at IS NOT NULL AS approved, "
        "value_hash = :h AS hash_kept FROM anti_abuse_identifiers "
        "WHERE tenant_id = CAST(:t AS uuid)",
        h=security.hash_value(security.canonical_email_for_abuse(email)),
        t=old_tid,
    )
    assert (row.approved, row.hash_kept) == (True, True)

    # The approval was not burned: a low risk passes the registration.
    res = await _signup(sm, email)
    assert res.outcome == svc.TRIAL_STARTED
    new = await _one(
        eng, "SELECT tenant_id::text AS tid FROM users WHERE email = :e", e=email
    )
    created.append(new.tid)


async def _second_user(sup, tid: str) -> None:
    """A second live user of the tenant: the deletion of the first does not close the tenant."""
    eng, _ = sup
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO users (tenant_id, email, email_status, locale) "
                "VALUES (CAST(:t AS uuid), :e, 'verified', 'sr')"
            ),
            {"t": tid, "e": _email()},
        )


@pytest.mark.asyncio
async def test_request_opens_after_non_last_user_deleted_tenant_alive(sm, sup):
    eng, created = sup
    email = _email()
    assert (await _signup(sm, email)).outcome == svc.TRIAL_STARTED
    row = await _one(eng, "SELECT id, tenant_id FROM users WHERE email = :e", e=email)
    tid = str(row.tenant_id)
    created.append(tid)
    await _second_user(sup, tid)
    async with sm() as s:
        res = await delete_svc.delete_own_account(
            s, tenant_id=tid, user_id=str(row.id), confirm_email=email
        )
    assert (res.outcome, res.tenant_deleted) == ("ok", False)
    st = await _one(
        eng, "SELECT status FROM tenants WHERE id = CAST(:t AS uuid)", t=tid
    )
    assert st.status != "deleted"

    # It was a dead end: with a live tenant the request was silently not created.
    rid = await _request(sm, email)
    assert rid is not None
    r = await _one(
        eng,
        "SELECT status, comment, tenant_id::text AS tid FROM trial_requests "
        "WHERE id = CAST(:r AS uuid)",
        r=rid,
    )
    assert (r.status, r.tid) == ("pending", tid)
    assert r.comment == f"reregister_request:sr:{email}"
    # An address that did not exist — the same “empty” answer (anti-enumeration).
    assert await _request(sm, _email()) is None

    # The approval lifts the block on a LIVE tenant too: the registration passes, the account is new.
    assert await _decide(sm, rid, approve=True, sender=MockEmailSender()) == (
        "reregister_approved"
    )
    res = await _signup(sm, email)
    assert res.outcome == svc.TRIAL_STARTED
    new = await _one(
        eng, "SELECT tenant_id::text AS tid FROM users WHERE email = :e", e=email
    )
    created.append(new.tid)
    assert new.tid != tid


@pytest.mark.asyncio
async def test_self_delete_clears_operator_decision_comment(sm, sup):
    eng, created = sup
    email = _email()
    assert (await _signup(sm, email)).outcome == svc.TRIAL_STARTED
    row = await _one(eng, "SELECT id, tenant_id FROM users WHERE email = :e", e=email)
    tid = str(row.tenant_id)
    created.append(tid)
    await _second_user(sup, tid)  # not the last: the tenant stays alive
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO trial_requests (tenant_id, comment, status, decision_comment) "
                "VALUES (CAST(:t AS uuid), 'upgrade_request:pro', 'rejected', :note)"
            ),
            {"t": tid, "note": f"не подошло, {email}"},
        )
    async with sm() as s:
        res = await delete_svc.delete_own_account(
            s, tenant_id=tid, user_id=str(row.id), confirm_email=email
        )
    assert res.outcome == "ok"
    r = await _one(
        eng,
        "SELECT status, decision_comment FROM trial_requests "
        "WHERE tenant_id = CAST(:t AS uuid)",
        t=tid,
    )
    # The operator’s note could contain the address: it is zeroed, the decision stays.
    assert (r.status, r.decision_comment) == ("rejected", None)
