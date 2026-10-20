"""Tests of the authentication service: signup (anti-abuse), magic-link, email-verify,
resend_verification.

The DB and external APIs are mocked (FakeSession + Noop collaborators), the network is not touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import auth_service as svc
from src.api.cabinet import routes_auth
from src.api.cabinet.i18n import CABINET
from src.api.config import CabinetSettings
from src.api.emailer import MockEmailSender
from src.domain.access import is_frozen
from src.api.main import app
from tests.api.conftest import (
    FakeResult,
    FakeSession,
    bad_turnstile,
    disposable_email,
    good_email,
    make_token,
    make_user,
    no_api_key_email,
    ok_turnstile,
    risky_email,
    undeliverable_email,
    unknown_email,
)


def _settings():
    return CabinetSettings(CABINET_BASE_URL="https://nab.rs", CABINET_SECRET_KEY="t")


@pytest.mark.asyncio
async def test_signup_low_risk_starts_trial_and_sends_email():
    session = FakeSession()
    sender = MockEmailSender()
    result = await svc.signup(
        session,
        email="user@company.rs",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=good_email(),
        email_sender=sender,
        settings=_settings(),
    )
    assert result.outcome == svc.TRIAL_STARTED
    # tenant + user + identifier + auth_token added
    added = [type(o).__name__ for o in session.added]
    assert "Tenant" in added and "User" in added
    assert "AntiAbuseIdentifier" in added and "AuthToken" in added
    assert session.committed == 1
    # client (verify) + operator at info@nab.rs; the client — first
    assert [m.to for m in sender.outbox] == ["user@company.rs", "info@nab.rs"]
    # FIX-AUTH-RLS: the GUC is set BEFORE add(tenant) — otherwise WITH CHECK (id=GUC)
    # on tenants and (tenant_id=GUC) on users/anti_abuse_identifiers fails under
    # tendersrb_app (FORCE RLS, 0021).
    assert session.executed, "set_current_tenant must call execute()"
    assert "app.current_tenant_id" in session.executed[0][0]
    assert "/auth/verify?token=" in sender.outbox[0].text_body
    # R8: the web signup sets the internal canon tier='lite' explicitly (not NULL) —
    # otherwise after ck_tenants_tier/NOT NULL (0027) the INSERT would fail.
    tenant = next(o for o in session.added if type(o).__name__ == "Tenant")
    assert tenant.tier == "lite"
    # The trial cycle: access_until is set = trial_until (not NULL), otherwise the axis
    # of freezing (domain/access) is dead and the trial = an unlimited Pro (LAUNCH_AUDIT §4b).
    assert tenant.trial_until is not None
    assert tenant.access_until is not None
    assert tenant.access_until == tenant.trial_until


@pytest.mark.asyncio
async def test_signup_sets_access_until_in_future_for_trial_days():
    """The trial’s access_until — trial_days ahead from the moment of registration (the freezing axis:
    while > now access exists, after — is_frozen)."""
    session = FakeSession()
    settings = _settings()
    before = datetime.now(timezone.utc)
    await svc.signup(
        session,
        email="user@company.rs",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=good_email(),
        email_sender=MockEmailSender(),
        settings=settings,
    )
    after = datetime.now(timezone.utc)
    tenant = next(o for o in session.added if type(o).__name__ == "Tenant")
    lo = before + timedelta(days=settings.trial_days)
    hi = after + timedelta(days=settings.trial_days)
    assert lo <= tenant.access_until <= hi


@pytest.mark.asyncio
async def test_signup_rejects_failed_turnstile():
    session = FakeSession()
    sender = MockEmailSender()
    result = await svc.signup(
        session,
        email="user@company.rs",
        turnstile_token="bad",
        turnstile=bad_turnstile(),
        email_validator=good_email(),
        email_sender=sender,
        settings=_settings(),
    )
    assert result.outcome == svc.REJECTED_TURNSTILE
    assert session.added == [] and sender.outbox == []


@pytest.mark.asyncio
async def test_signup_rejects_disposable_email():
    session = FakeSession()
    result = await svc.signup(
        session,
        email="x@mailinator.com",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=disposable_email(),
        email_sender=MockEmailSender(),
        settings=_settings(),
    )
    assert result.outcome == svc.REJECTED_EMAIL
    assert session.added == []


@pytest.mark.asyncio
async def test_signup_risky_email_starts_trial_by_default():
    """medium risk (risky=True) with the default require_step_up_for_medium=False → trial."""
    session = FakeSession()
    sender = MockEmailSender()
    result = await svc.signup(
        session,
        email="info@corp.rs",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=risky_email(),
        email_sender=sender,
        settings=_settings(),
    )
    assert result.outcome == svc.TRIAL_STARTED
    assert result.risk == "medium"
    assert [m.to for m in sender.outbox] == ["info@corp.rs", "info@nab.rs"]
    assert "AuthToken" in [type(o).__name__ for o in session.added]


@pytest.mark.asyncio
async def test_signup_medium_risk_requires_verification_when_step_up_enabled():
    """require_step_up_for_medium=True → VERIFICATION_REQUIRED for medium risk."""
    session = FakeSession()
    sender = MockEmailSender()
    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="t",
        REQUIRE_STEP_UP_FOR_MEDIUM="true",
    )
    result = await svc.signup(
        session,
        email="info@corp.rs",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=risky_email(),
        email_sender=sender,
        settings=settings,
    )
    assert result.outcome == svc.VERIFICATION_REQUIRED
    assert result.risk == "medium"
    # we do not send verify to the client, the operator gets a decision: only info@nab.rs
    assert [m.to for m in sender.outbox] == ["info@nab.rs"]
    assert "AuthToken" not in [type(o).__name__ for o in session.added]
    # pending_verification is NOT yet a trial: there is no trial (trial_until NULL), and the access
    # is FROZEN (access_until = the moment of registration). NULL in access_active =
    # “unlimited” — exactly by it the tenant would get an open cabinet without the operator’s decision.
    tenant = next(o for o in session.added if type(o).__name__ == "Tenant")
    assert tenant.status == "pending_verification"
    assert tenant.trial_until is None
    assert tenant.access_until is not None
    assert is_frozen(tenant.access_until, datetime.now(timezone.utc))


_PENDING_TEXT = {
    "sr": "Zahtev je prosleđen operateru, javićemo vam se e-poštom u roku od 1 radnog dana.",
    "ru": "Заявка передана оператору, мы свяжемся с вами по e-mail в течение 1 рабочего дня.",
    "en": "Your request has been passed to an operator; we will contact you by e-mail within 1 business day.",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
async def test_pending_verification_message_is_honest_no_card_promise(lang):
    """The high-risk answer — the literal “passed to the operator”, without a promise of a card/€1."""
    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="t",
        REQUIRE_STEP_UP_FOR_MEDIUM="true",
    )
    result = await svc.signup(
        FakeSession(),
        email="info@corp.rs",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=risky_email(),
        email_sender=MockEmailSender(),
        settings=settings,
        locale=lang,
    )
    assert result.outcome == svc.VERIFICATION_REQUIRED
    assert result.message == _PENDING_TEXT[lang]
    for bad in ("€", "kartic", "карт", "card"):
        assert bad not in result.message
    # the result page takes its text from the cabinet catalog — it is the same
    assert CABINET[lang]["sgr_msg_verification"] == _PENDING_TEXT[lang]


@pytest.mark.asyncio
async def test_pending_verification_operator_email_asks_for_decision():
    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="t",
        REQUIRE_STEP_UP_FOR_MEDIUM="true",
        CABINET_ADMIN_EMAILS="op@nab.rs",
    )
    sender = MockEmailSender()
    session = FakeSession()
    await svc.signup(
        session,
        email="info@corp.rs",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=risky_email(),
        email_sender=sender,
        settings=settings,
    )
    # op@nab.rs from the allowlist + info@nab.rs; sorted → info first
    assert [m.to for m in sender.outbox] == ["info@nab.rs", "op@nab.rs"]
    mail = sender.outbox[0]
    user = next(o for o in session.added if type(o).__name__ == "User")
    assert "Нужно решение: выдать доступ / отклонить" in mail.text_body
    assert f"https://nab.rs/admin/users/{user.id}" in mail.text_body


@pytest.mark.asyncio
async def test_signup_unknown_email_starts_trial():
    """Gmail / result=unknown → risky, not undeliverable → TRIAL_STARTED (the main case)."""
    session = FakeSession()
    sender = MockEmailSender()
    result = await svc.signup(
        session,
        email="owner@example.com",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=unknown_email(),
        email_sender=sender,
        settings=_settings(),
    )
    assert result.outcome == svc.TRIAL_STARTED
    assert [m.to for m in sender.outbox] == [
        "owner@example.com",
        "info@nab.rs",
    ]


@pytest.mark.asyncio
async def test_signup_undeliverable_email_is_rejected():
    """result=undeliverable → a block (obvious garbage)."""
    session = FakeSession()
    result = await svc.signup(
        session,
        email="dead@nowhere.example",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=undeliverable_email(),
        email_sender=MockEmailSender(),
        settings=_settings(),
    )
    assert result.outcome == svc.REJECTED_EMAIL
    assert session.added == []


@pytest.mark.asyncio
async def test_signup_no_api_key_fail_open_starts_trial():
    """No KICKBOX_API_KEY (fail-open) → risky → TRIAL_STARTED with the default settings."""
    session = FakeSession()
    sender = MockEmailSender()
    result = await svc.signup(
        session,
        email="user@gmail.com",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=no_api_key_email(),
        email_sender=sender,
        settings=_settings(),
    )
    assert result.outcome == svc.TRIAL_STARTED
    assert [m.to for m in sender.outbox] == ["user@gmail.com", "info@nab.rs"]


@pytest.mark.asyncio
async def test_signup_duplicate_identifier_is_rejected():
    # UNIQUE(value_hash WHERE active) → IntegrityError on the identifier’s flush
    session = FakeSession(flush_raises_for="AntiAbuseIdentifier")
    result = await svc.signup(
        session,
        email="user@company.rs",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=good_email(),
        email_sender=MockEmailSender(),
        settings=_settings(),
    )
    assert result.outcome == svc.REJECTED_DUPLICATE
    assert session.rolled_back == 1
    assert session.committed == 0


@pytest.mark.asyncio
async def test_verify_email_token_marks_verified():
    user = make_user(email_verified_at=None)
    token = make_token(purpose="email_verify", user_id=user.id)
    session = FakeSession(
        results=[_one(token), FakeResult(items=[user.tenant_id])],
        get_map={"User": user},
    )
    sess = await svc.verify_email_token(session, raw_token="raw")
    # locale travels in the signed session (the chrome does not read the DB on each render).
    assert sess == {
        "uid": str(user.id),
        "tid": str(user.tenant_id),
        "locale": user.locale,
    }
    assert user.email_verified_at is not None
    assert token.consumed_at is not None
    # FIX-AUTH-RLS: the tenant is resolved via SECURITY DEFINER (0022), not via the ORM.
    executed = [t for t, _ in session.executed]
    assert any("app_resolve_tenant_id_by_user_id" in t for t in executed)


@pytest.mark.asyncio
async def test_verify_email_token_no_tenant_returns_none():
    """The resolver returned NULL (no such user_id) → None, not 500."""
    token = make_token(purpose="email_verify")
    session = FakeSession(results=[_one(token), FakeResult(items=[None])])
    assert await svc.verify_email_token(session, raw_token="raw") is None


@pytest.mark.asyncio
async def test_magic_link_request_sends_for_verified_user():
    user = make_user(email_verified_at=datetime.now(timezone.utc))
    session = FakeSession(results=[_one(user)])
    sender = MockEmailSender()
    sent = await svc.request_magic_link(
        session, email=user.email, email_sender=sender, settings=_settings()
    )
    assert sent is True
    assert len(sender.outbox) == 1
    assert "/auth/magic?token=" in sender.outbox[0].text_body
    assert "AuthToken" in [type(o).__name__ for o in session.added]
    # FIX-AUTH-RLS: the resolver app_resolve_user_for_auth (0024), not select(User).
    executed = [t for t, _ in session.executed]
    assert any("app_resolve_user_for_auth" in t for t in executed)
    assert not any("FROM users" in t for t in executed)


@pytest.mark.asyncio
async def test_magic_link_request_silent_for_unknown_email():
    session = FakeSession(results=[_one(None)])
    sender = MockEmailSender()
    sent = await svc.request_magic_link(
        session, email="ghost@nowhere.rs", email_sender=sender, settings=_settings()
    )
    assert sent is False
    assert sender.outbox == []


@pytest.mark.asyncio
async def test_magic_link_consume_valid():
    user = make_user()
    token = make_token(purpose="magic_link", user_id=user.id)
    session = FakeSession(
        results=[_one(token), FakeResult(items=[user.tenant_id])],
        get_map={"User": user},
    )
    sess = await svc.consume_magic_link(session, raw_token="raw")
    # locale travels in the signed session (the chrome does not read the DB on each render).
    assert sess == {
        "uid": str(user.id),
        "tid": str(user.tenant_id),
        "locale": user.locale,
    }
    assert token.consumed_at is not None
    # FIX-AUTH-RLS: the tenant is resolved via SECURITY DEFINER; users.locale is read
    # under the GUC of its own tenant (set_tenant FIRST) via session.get(User).
    executed = [t for t, _ in session.executed]
    assert any("app_resolve_tenant_id_by_user_id" in t for t in executed)
    assert any("set_config" in t or "app.current_tenant_id" in t for t in executed)


@pytest.mark.asyncio
async def test_magic_link_consume_no_tenant_returns_none():
    """The resolver returned NULL → None, not 500."""
    token = make_token(purpose="magic_link")
    session = FakeSession(results=[_one(token), FakeResult(items=[None])])
    assert await svc.consume_magic_link(session, raw_token="raw") is None


@pytest.mark.asyncio
async def test_magic_link_consume_expired():
    token = make_token(purpose="magic_link", expired=True)
    session = FakeSession(results=[_one(token)])
    assert await svc.consume_magic_link(session, raw_token="raw") is None


@pytest.mark.asyncio
async def test_magic_link_consume_reused():
    token = make_token(
        purpose="magic_link",
        consumed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    session = FakeSession(results=[_one(token)])
    assert await svc.consume_magic_link(session, raw_token="raw") is None


# --- resend_verification service tests ------------------------------------ #


@pytest.mark.asyncio
async def test_resend_sends_email_for_unverified_user():
    user = make_user(email_verified_at=None)
    session = FakeSession(results=[_one(user)])
    sender = MockEmailSender()
    sent = await svc.resend_verification(
        session, email=user.email, email_sender=sender, settings=_settings()
    )
    assert sent is True
    assert len(sender.outbox) == 1
    assert "/auth/verify?token=" in sender.outbox[0].text_body
    assert "AuthToken" in [type(o).__name__ for o in session.added]
    assert session.committed == 1
    # FIX-AUTH-RLS: the resolver app_resolve_user_for_auth (0024), not select(User).
    executed = [t for t, _ in session.executed]
    assert any("app_resolve_user_for_auth" in t for t in executed)
    assert not any("FROM users" in t for t in executed)


@pytest.mark.asyncio
async def test_resend_silent_for_nonexistent_user():
    session = FakeSession(results=[_one(None)])
    sender = MockEmailSender()
    sent = await svc.resend_verification(
        session, email="ghost@nowhere.rs", email_sender=sender, settings=_settings()
    )
    assert sent is False
    assert sender.outbox == []
    assert session.committed == 0


@pytest.mark.asyncio
async def test_resend_silent_for_already_verified_user():
    user = make_user(email_verified_at=datetime.now(timezone.utc))
    session = FakeSession(results=[_one(user)])
    sender = MockEmailSender()
    sent = await svc.resend_verification(
        session, email=user.email, email_sender=sender, settings=_settings()
    )
    assert sent is False
    assert sender.outbox == []
    assert session.committed == 0


@pytest.mark.asyncio
async def test_resend_old_token_consumed_new_token_added():
    """After resend the old token is invalid, the new one is added."""
    user = make_user(email_verified_at=None)
    session = FakeSession(results=[_one(user)])
    sender = MockEmailSender()
    await svc.resend_verification(
        session, email=user.email, email_sender=sender, settings=_settings()
    )
    # The new AuthToken is added to the session.
    tokens = [o for o in session.added if type(o).__name__ == "AuthToken"]
    assert len(tokens) == 1
    # The UPDATE of the old tokens was called (the second execute after the SELECT user).
    executed_texts = [text for text, _ in session.executed]
    assert any("UPDATE" in t.upper() for t in executed_texts)


@pytest.mark.asyncio
async def test_resend_old_token_rejected_by_verify():
    """The old token (consumed_at set) is rejected by verify_email_token."""
    old_token = make_token(
        purpose="email_verify",
        consumed_at=datetime.now(timezone.utc),
    )
    session = FakeSession(results=[_one(old_token)])
    result = await svc.verify_email_token(session, raw_token="old-raw")
    assert result is None


@pytest.mark.asyncio
async def test_email_verify_already_done_true_for_verified_account():
    """Idempotency (anti-prefetch): the token was consumed by a scanner, but the account is already
    confirmed → the fact check is True (we do NOT issue a session, we do NOT touch the token).

    Mutation: return True for any existing token → ``..._false_when_account_not_verified``
    becomes red.
    """
    user = make_user(email_verified_at=datetime.now(timezone.utc))
    token = make_token(
        purpose="email_verify",
        consumed_at=datetime.now(timezone.utc),
        user_id=user.id,
    )
    session = FakeSession(
        results=[_one(token), FakeResult(items=[user.tenant_id])],
        get_map={"User": user},
    )
    assert await svc.email_verify_already_done(session, raw_token="raw") is True
    # read-only fact check: the token is not consumed again, no commit.
    assert session.committed == 0


@pytest.mark.asyncio
async def test_email_verify_already_done_false_when_account_not_verified():
    """The token exists, but the account is NOT yet confirmed → this is not “already confirmed”."""
    user = make_user(email_verified_at=None)
    token = make_token(purpose="email_verify", user_id=user.id)
    session = FakeSession(
        results=[_one(token), FakeResult(items=[user.tenant_id])],
        get_map={"User": user},
    )
    assert await svc.email_verify_already_done(session, raw_token="raw") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "unknown"])
async def test_email_verify_already_done_false_for_unknown_token(raw):
    """An empty/unknown token → we do not give a false success."""
    session = FakeSession(results=[_one(None)])
    assert await svc.email_verify_already_done(session, raw_token=raw) is False


# --- resend route tests ---------------------------------------------------- #


def _resend_client(session: FakeSession, sender: MockEmailSender):
    def factory():
        return session

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_email_sender] = lambda: sender
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()
    routes_auth._RESEND_VERIFY_PER_EMAIL.reset()
    routes_auth._RESEND_VERIFY_PER_IP.reset()


def test_resend_get_form_renders():
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/auth/resend")
    assert resp.status_code == 200
    # The marker is taken FROM THE CATALOG: the default language of anonymous login screens is sr, as
    # on the showcase (audit batch 2, HIGH-2). Previously a Russian literal stood here.
    assert CABINET["sr"]["rsn_title"] in resp.text


def test_resend_post_always_returns_same_message():
    """A single response regardless of the service outcome (anti-enumeration)."""
    user = make_user(email_verified_at=None)
    session = FakeSession(results=[_one(user)])
    sender = MockEmailSender()
    client = _resend_client(session, sender)
    resp = client.post("/auth/resend", data={"email": user.email})
    assert resp.status_code == 200
    assert CABINET["sr"]["authm_check_mail_title"] in resp.text
    assert len(sender.outbox) == 1


def test_resend_post_same_message_for_unknown_email():
    session = FakeSession(results=[_one(None)])
    sender = MockEmailSender()
    client = _resend_client(session, sender)
    resp = client.post("/auth/resend", data={"email": "ghost@nowhere.rs"})
    assert resp.status_code == 200
    assert CABINET["sr"]["authm_check_mail_title"] in resp.text
    assert sender.outbox == []


def test_resend_post_rate_limit_stops_email_on_4th_request():
    """The 4th request with the same email exceeds the limit — the email is not sent."""
    routes_auth._RESEND_VERIFY_PER_EMAIL.reset()
    routes_auth._RESEND_VERIFY_PER_IP.reset()
    email = "ratelimit@test.rs"
    sender = MockEmailSender()
    for i in range(4):
        user = make_user(email=email, email_verified_at=None)
        session = FakeSession(results=[_one(user)])
        client = _resend_client(session, sender)
        resp = client.post("/auth/resend", data={"email": email})
        assert resp.status_code == 200
        assert CABINET["sr"]["authm_check_mail_title"] in resp.text
    # The first 3 — sent, the 4th — not.
    assert len(sender.outbox) == 3


# --- verify route: idempotency (anti-prefetch) -------------------------- #


def _averify(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def test_verify_route_prefetched_token_shows_already_confirmed(monkeypatch):
    """A mail scanner consumed the verify token and confirmed the account →
    ``verify_email_token``=None, but ``email_verify_already_done``=True →
    “e-mail already confirmed” (success), NOT “the link is one-time”.

    Mutation: remove the ``already`` branch in the route (return “None → error at once”) →
    the test turns red. The markers are taken FROM THE CATALOG: the anonymous user on this screen has the default
    language sr, as on the showcase (see test_auth_link_used.py).
    """
    monkeypatch.setattr(routes_auth.svc, "verify_email_token", _averify(None))
    monkeypatch.setattr(routes_auth.svc, "email_verify_already_done", _averify(True))
    client = _resend_client(FakeSession(), MockEmailSender())
    resp = client.get("/auth/verify?token=prefetched")
    assert resp.status_code == 200
    assert CABINET["sr"]["authm_verify_already_title"] in resp.text
    assert CABINET["sr"]["authm_link_used_title"] not in resp.text


def test_verify_route_foreign_token_shows_single_use_notice(monkeypatch):
    """A genuinely unknown token without a confirmed account → “the link is one-time”
    (we do not substitute success for everything)."""
    monkeypatch.setattr(routes_auth.svc, "verify_email_token", _averify(None))
    monkeypatch.setattr(routes_auth.svc, "email_verify_already_done", _averify(False))
    client = _resend_client(FakeSession(), MockEmailSender())
    resp = client.get("/auth/verify?token=foreign")
    assert resp.status_code == 200
    assert CABINET["sr"]["authm_link_used_title"] in resp.text
    assert CABINET["sr"]["authm_verify_already_title"] not in resp.text


# --- helpers -------------------------------------------------------------- #


def _one(item):
    return FakeResult(items=[item] if item is not None else [])
