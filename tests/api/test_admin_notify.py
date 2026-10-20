"""Email notification of platform operators about requests and registrations.

Three events (an access request from the cabinet, an upgrade from the showcase, a new registration) must
reach the operator not only in Telegram/admin, but also by email — a SEPARATE email to EACH
address from ``CABINET_ADMIN_EMAILS`` (best-effort).

Expectations are taken from the FACT: request routes are hit via ``TestClient`` (response code +
the contents of ``MockEmailSender.outbox``), registration — by a direct call to
``auth_service.signup`` (like all of ``test_auth``), network/DB are mocked (ADR-013).
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import auth_service as svc
from src.api.config import CabinetSettings
from src.api.emailer import EmailSender, MockEmailSender, SentEmail
from src.api.main import app
from tests.api.conftest import (
    FakeResult,
    FakeSession,
    good_email,
    make_session_factory,
    ok_turnstile,
)

_TID = "11111111-1111-1111-1111-111111111111"
_UID = "22222222-2222-2222-2222-222222222222"
_SESSION = {"uid": _UID, "tid": _TID}
_ADMINS = {"op1@example.com", "op2@example.com"}
# The operator’s mailbox (ENTERPRISE_CONTACT_EMAIL) — receives emails ALWAYS, but in
# CABINET_ADMIN_EMAILS (admin login) it is NOT included.
_INFO = "info@nab.rs"
_RECIPIENTS = _ADMINS | {_INFO}


def _settings(admins: str = "op1@example.com,op2@example.com") -> CabinetSettings:
    return CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="t",
        CABINET_ADMIN_EMAILS=admins,
    )


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


class _FailFirstSender(EmailSender):
    """MockEmailSender, but for the address ``fail_to`` it raises like a real SMTP refusal.

    ``SMTPRecipientsRefused`` puts the recipient address into the exception arguments —
    exactly the PII case (#347), the reason the module logs only the class.
    ``**kwargs`` — so that extending the contract does not break the stub (#454).
    """

    def __init__(self, fail_to: str) -> None:
        self.outbox: list[SentEmail] = []
        self._fail_to = fail_to

    async def send(self, *, to, subject, text_body, html_body=None, **kwargs) -> None:
        if to == self._fail_to:
            raise smtplib.SMTPRecipientsRefused({to: (550, b"nope")})
        self.outbox.append(SentEmail(to, subject, text_body, html_body))


def _trial_success_session() -> FakeSession:
    """FakeSession for a successful request: guard (no pending, access expired) →
    insert id → the applicant’s email."""
    return FakeSession(
        results=[
            FakeResult(
                items=[SimpleNamespace(has_pending=False, access_until=_past())]
            ),
            FakeResult(scalar="33333333-3333-3333-3333-333333333333"),
            FakeResult(items=["applicant@company.rs"]),
        ]
    )


def _wire(session: FakeSession, sender: EmailSender, settings: CabinetSettings):
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        session
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _SESSION
    app.dependency_overrides[deps.get_settings] = lambda: settings
    app.dependency_overrides[deps.get_email_sender] = lambda: sender
    # ‼️ The cabinet chrome resolves the operator flag (deps.resolve_platform_admin_flag
    # → admin_access._ADMIN_USER_SQL) on EVERY request, and this SELECT consumes the shared
    # queue FakeSession.results — the guard prepared for the route went to it instead,
    # the request was silently not created and the emails were not sent (caught by a run, not by reading:
    # session.executed showed an extra `SELECT email FROM users` before the guard).
    # We substitute the dependency itself: the operator flag has nothing to do with this task.
    app.dependency_overrides[deps.resolve_platform_admin_flag] = lambda: False
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Access request from the cabinet (POST /cabinet/trial/request)
# --------------------------------------------------------------------------- #


def test_trial_request_emails_every_admin():
    """Two addresses in the allowlist → two separate emails, the recipients are the whole allowlist."""
    sender = MockEmailSender()
    client = _wire(_trial_success_session(), sender, _settings())
    resp = client.post("/cabinet/trial/request", data={"comment": "ООО Ромашка"})
    assert resp.status_code == 303
    admin_mails = [
        m for m in sender.outbox if m.subject == "Новая заявка на тестовый доступ"
    ]
    assert {m.to for m in admin_mails} == _RECIPIENTS
    assert "https://nab.rs/admin/requests" in admin_mails[0].text_body


def test_trial_request_empty_allowlist_still_mails_info_inbox():
    """CABINET_ADMIN_EMAILS='' → the email still goes to info@nab.rs (route 303)."""
    sender = MockEmailSender()
    client = _wire(_trial_success_session(), sender, _settings(admins=""))
    resp = client.post("/cabinet/trial/request", data={"comment": "x"})
    assert resp.status_code == 303
    assert [m.to for m in sender.outbox] == [_INFO]


def test_info_inbox_not_duplicated_when_already_in_allowlist():
    """info@nab.rs is already in the allowlist (in another case) → one email, not two."""
    sender = MockEmailSender()
    settings = _settings(admins="INFO@nab.rs, op1@example.com")
    client = _wire(_trial_success_session(), sender, settings)
    resp = client.post("/cabinet/trial/request", data={"comment": "x"})
    assert resp.status_code == 303
    assert sorted(m.to for m in sender.outbox) == ["info@nab.rs", "op1@example.com"]


def test_trial_request_best_effort_first_recipient_fails():
    """A failure on the first address does not cancel the email to the second; the route still returns 303."""
    sender = _FailFirstSender(fail_to="op1@example.com")  # among the sorted recipients
    client = _wire(_trial_success_session(), sender, _settings())
    resp = client.post("/cabinet/trial/request", data={"comment": "x"})
    assert resp.status_code == 303
    assert [m.to for m in sender.outbox] == ["info@nab.rs", "op2@example.com"]


# --------------------------------------------------------------------------- #
# Upgrade request from the showcase (POST /plans/upgrade)
# --------------------------------------------------------------------------- #


def test_upgrade_request_emails_admins_with_plan():
    """An upgrade request (upgrade is created even with active access) → email
    to the operators with the plan name."""
    session = FakeSession(
        results=[
            # guard: active access does not prevent the upgrade request
            FakeResult(items=[SimpleNamespace(has_pending=False, access_until=None)]),
            FakeResult(scalar="44444444-4444-4444-4444-444444444444"),
            FakeResult(items=["applicant@company.rs"]),
        ]
    )
    sender = MockEmailSender()
    client = _wire(session, sender, _settings())
    resp = client.post("/plans/upgrade", data={"plan": "pro"})
    assert resp.status_code == 303
    admin_mails = [
        m for m in sender.outbox if m.subject == "Новая заявка на апгрейд плана"
    ]
    assert {m.to for m in admin_mails} == _RECIPIENTS
    assert "pro" in admin_mails[0].text_body


# --------------------------------------------------------------------------- #
# New registration (auth_service.signup)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_signup_trial_started_notifies_admins_separately():
    """Successful registration: email to the client (verify) AND separate emails to the admins."""
    session = FakeSession()
    sender = MockEmailSender()
    result = await svc.signup(
        session,
        email="newclient@company.rs",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=good_email(),
        email_sender=sender,
        settings=_settings(),
    )
    assert result.outcome == svc.TRIAL_STARTED
    # the email to the client — separate from the admin emails
    client_mails = [m for m in sender.outbox if m.to == "newclient@company.rs"]
    assert len(client_mails) == 1
    assert "/auth/verify?token=" in client_mails[0].text_body
    # the verification email is NOT a welcome: the sender is the configured one (noreply)
    assert client_mails[0].from_email is None
    assert client_mails[0].reply_to is None
    admin_mails = [m for m in sender.outbox if m.subject == "Новая регистрация клиента"]
    assert {m.to for m in admin_mails} == _RECIPIENTS
    assert "https://nab.rs/admin/users" in admin_mails[0].text_body
    assert "trial запущен" in admin_mails[0].text_body
    assert len(sender.outbox) == 4  # client + two operators + info@nab.rs


@pytest.mark.asyncio
async def test_signup_rejected_turnstile_no_admin_email():
    """Refused registration (turnstile fail) → we do not write to the operators."""
    from tests.api.conftest import bad_turnstile

    session = FakeSession()
    sender = MockEmailSender()
    result = await svc.signup(
        session,
        email="spam@company.rs",
        turnstile_token="bad",
        turnstile=bad_turnstile(),
        email_validator=good_email(),
        email_sender=sender,
        settings=_settings(),
    )
    assert result.outcome == svc.REJECTED_TURNSTILE
    assert sender.outbox == []
