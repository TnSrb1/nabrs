"""Cabinet authentication service (ARCHITECTURE §4, ADR-018 revised).

Passwordless flow without business logic in the routers — the functions take
``AsyncSession`` and collaborators (Turnstile/Kickbox/email sender/settings),
therefore they are tested both directly and through TestClient (DI override).

signup → anti-abuse (Turnstile + Kickbox + risk scoring) → tenants/users/
anti_abuse_identifiers → email-verify link (low risk, cardless trial) or
verification_required (medium/high; step-up €1 — Sprint-21, NotImplementedError).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from src.api import admin_notify
from src.api import anti_abuse as aa
from src.api import security
from src.api.config import CabinetSettings
from src.api.emailer import EmailSender
from src.db.models import AntiAbuseIdentifier, AuthToken, Tenant, User
from src.db.session import set_current_tenant
from src.api.public.i18n import auth_message
from src.i18n_email import email_t, norm_locale, welcome_parts
from src.plans.catalog import (
    ENTERPRISE_CONTACT_EMAIL,
    TG_CHANNEL_URL,
    WELCOME_FROM_EMAIL,
)

_log = logging.getLogger(__name__)
# The link “Prijava u servis” from the owner’s text (verbatim). Not cabinet_base_url: that one
# is the base of confirmation links, which defaults to nab.rs in the code, not app.nab.rs.
WELCOME_APP_URL = "https://app.nab.rs"

# SECURITY DEFINER resolvers (0022/0024) — bypass RLS, read users BEFORE
# setting the GUC ``app.current_tenant_id`` (VG-01 FORCE RLS, 0021).
_RESOLVE_USER_FOR_AUTH_SQL = text(
    "SELECT id, tenant_id, email_verified_at "
    "FROM app_resolve_user_for_auth(CAST(:email AS text))"
)
_RESOLVE_TENANT_BY_USER_ID_SQL = text(
    "SELECT app_resolve_tenant_id_by_user_id(CAST(:uid AS uuid)) AS tenant_id"
)

# Re-registration after self-deletion (0049, #658): SECURITY DEFINER —
# the hash of the deleted tenant is not visible under the app role (FORCE RLS).
_REREGISTER_CLAIM_SQL = text(
    "SELECT tenant_id, trial_used FROM app_reregister_claim(CAST(:h AS text))"
)
_REREGISTER_REQUEST_SQL = text(
    "SELECT request_id FROM app_reregister_request("
    "CAST(:h AS text), CAST(:lang AS text), CAST(:email AS text))"
)

# Registration outcomes.
TRIAL_STARTED = "trial_started"
VERIFICATION_REQUIRED = "verification_required"
REJECTED_TURNSTILE = "rejected_turnstile"
REJECTED_EMAIL = "rejected_email"
REJECTED_DUPLICATE = "rejected_duplicate"
REJECTED_INVALID = "rejected_invalid"


@dataclass(frozen=True)
class SignupResult:
    outcome: str
    message: str
    risk: str | None = None


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


_SIGNUP_OUTCOME_RU = {
    "trial_started": "trial запущен",
    "verification_required": "ждёт решения оператора (доступ не выдан)",
}


async def _notify_admins_new_signup(
    *,
    email_sender: EmailSender,
    settings: CabinetSettings,
    email: str,
    tenant_id,
    outcome: str,
    now: datetime | None,
    user_id=None,
) -> None:
    """Best-effort email to the operators about a new registration (ru, outside i18n scope).

    Sent ONLY after ``session.commit()`` — we notify about what is already in the DB.
    ``notify_admins_email`` raises no exceptions (the registration route must not fall over
    because of the mail), so no wrapper is needed here.

    ‼️ ``outcome`` is the outcome CODE (``trial_started``/``verification_required``), not a ready
    Russian text: the guard ``test_no_russian_literals_returned_from_signup`` walks the ast of
    ``signup`` itself and forbids any Cyrillic constants there, so that client messages come
    only from the catalog. The text for the operator is assembled here — the guard stays strict,
    and the email stays in Russian.
    """
    outcome_ru = _SIGNUP_OUTCOME_RU.get(outcome, outcome)
    when = _now(now).strftime("%d.%m.%Y %H:%M UTC")
    base = settings.cabinet_base_url.rstrip("/")
    admin_url = base + "/admin/users"
    lines = [
        "Новая регистрация клиента.",
        f"Email: {email}",
        f"tenant_id: {tenant_id}",
        f"Исход: {outcome_ru}",
        f"Время: {when}",
        f"Клиенты: {admin_url}",
    ]
    if outcome == "verification_required":
        # The tenant has NO access (frozen, no delivery) until the operator decides.
        lines.append("‼️ Нужно решение: выдать доступ / отклонить.")
        if user_id is not None:
            lines.append(f"Карточка клиента: {admin_url}/{user_id}")
    subject = "Новая регистрация клиента"
    if outcome == "verification_required":
        subject = "Нужно решение: регистрация клиента на проверке"
    await admin_notify.notify_admins_email(
        email_sender=email_sender,
        settings=settings,
        subject=subject,
        text_body="\n".join(lines),
    )


async def signup(
    session,
    *,
    email: str,
    turnstile_token: str | None,
    turnstile: aa.TurnstileVerifier,
    email_validator: aa.EmailValidator,
    email_sender: EmailSender,
    settings: CabinetSettings,
    remoteip: str | None = None,
    locale: str | None = None,
    now: datetime | None = None,
) -> SignupResult:
    """Registration with anti-abuse (ADR-018 §4). One tenant+user+identifier in
    one transaction; a race on UNIQUE(value_hash WHERE active) → refusal.

    ``locale`` — the interface language chosen on the site (the router resolves it from the
    request). It sets the language of the confirmation email AND the saved locale of the new
    account (``tenants.locale`` / ``users.locale``). NULL/unknown → ``ru``.
    """
    lang = norm_locale(locale)
    norm_email = security.normalize_email(email)
    if not norm_email or "@" not in norm_email:
        return SignupResult(REJECTED_INVALID, auth_message(lang, "invalid_email"))

    # 1. Turnstile (server-side verify).
    if not await turnstile.verify(turnstile_token, remoteip=remoteip):
        return SignupResult(REJECTED_TURNSTILE, auth_message(lang, "turnstile_failed"))

    # 2. Kickbox — blocks only explicit junk: disposable OR explicitly undeliverable.
    # unknown/accept_all/risky/kickbox_error — NOT a block (fail-open).
    verdict = await email_validator.validate(norm_email)
    if verdict.disposable or verdict.undeliverable:
        return SignupResult(
            REJECTED_EMAIL,
            auth_message(lang, "email_rejected"),
        )

    # 3. Risk scoring.
    risk = aa.score_risk(True, verdict)
    moment = _now(now)
    # ‼️ The identifier hash is from the CANONICAL address, not from norm_email:
    # the rule “one trial per address in 12 months” (UNIQUE(value_hash) WHERE
    # active) would otherwise be bypassed with plus-addressing and dots in gmail. In users.email
    # norm_email still goes — it is the login key, and it must not be changed.
    value_hash = security.hash_value(security.canonical_email_for_abuse(norm_email))

    # 4. We create tenant + user + anti_abuse_identifier in one transaction.
    #    On trial the rights are those of Pro (ARCHITECTURE §4); the tier is stored in
    #    subscriptions (Sprint-21), here — only the trial status.
    # MEDIUM is treated as low until step-up is enabled (REQUIRE_STEP_UP_FOR_MEDIUM).
    # This lets Gmail/unknown/accept_all get a trial without a block.
    is_low = risk == aa.RISK_LOW or (
        risk == aa.RISK_MEDIUM and not settings.require_step_up_for_medium
    )
    # The trial gets Pro rights, but NOT unlimited: the period = trial_until. The same date
    # goes into access_until (the freeze axis), otherwise NULL = “active forever” and the whole
    # trial-expiry axis is dead — the freeze (domain/access), the stop-mailing
    # (matching STAGE_A_SQL), the reminder (subscription_reminders), the cabinet banner
    # (trial_service) read exactly access_until. One date for both axes.
    trial_until = (moment + timedelta(days=settings.trial_days)) if is_low else None
    tenant_id = uuid4()
    # GUC BEFORE add/flush: WITH CHECK on tenants (id=GUC) and users/anti_abuse_identifiers
    # (tenant_id=GUC) — FORCE RLS (0021) checks them on INSERT as well.
    await set_current_tenant(session, tenant_id)
    # The address of a deleted account for which the operator APPROVED re-registration:
    # the block is lifted here, in the SAME transaction as the account creation (a rollback
    # of the registration also restores the block), once (0049). The account is NEW, but
    # the trial follows the rule “one per address per 12 months”: if it has already been used,
    # access is granted as “expired” — the extension is decided by the operator (a request from the cabinet, #657).
    # ‼️ The claim is ONLY for a completing registration (is_low): a medium/high risk
    # goes to pending_verification without a trial, and the approval cannot be spent.
    claim = None
    if is_low:
        claim = (
            await session.execute(_REREGISTER_CLAIM_SQL, {"h": value_hash})
        ).first()
    if claim is not None and claim.trial_used:
        trial_until = moment
    # ‼️ pending_verification: access “not granted” = FROZEN (access_until = now),
    # not NULL. NULL in access_active = “unlimited”: a tenant without an operator decision
    # would otherwise enter the cabinet (resend_verification → /auth/verify → magic-link) with an
    # open access axis. Delivery and Pro rights are closed by the status (matching
    # STAGE_A_SQL, effective_tier) — the freeze axis closes the cabinet and the button AI.
    # The operator’s approval — extend_until (max(access_until, now) + N).
    access_until = trial_until if is_low else moment
    tenant = Tenant(
        id=tenant_id,
        country_code="RS",
        tax_id=None,
        tier="lite",  # the internal canon of tiers (R8); the trial rights are given by effective_tier per status
        status="trial" if is_low else "pending_verification",
        trial_until=trial_until,
        access_until=access_until,  # trial: the trial period; otherwise frozen (see above)
        locale=lang,
        tz="Europe/Belgrade",
    )
    session.add(tenant)
    await session.flush()

    user = User(
        id=uuid4(),  # explicitly: the id is needed for the operator link and without a DB call
        tenant_id=tenant_id,
        email=norm_email,
        email_verified_at=None,
        email_status="active",
        role="admin",
        locale=lang,
    )
    session.add(user)
    await session.flush()

    identifier = AntiAbuseIdentifier(
        tenant_id=tenant_id,
        identifier_type="email_domain",
        value_hash=value_hash,
        value_last4=security.email_last4(norm_email),
        risk_score_at_signup=aa.RISK_SCORE[risk],
        verification_method=("email_link" if is_low else "card_setup_intent"),
        verified_at=None,
        expires_at=moment + timedelta(days=365),  # one trial / 12 months
        region="RS",
    )
    session.add(identifier)
    try:
        await session.flush()  # here UNIQUE(value_hash) active triggers
    except IntegrityError:
        await session.rollback()
        return SignupResult(
            REJECTED_DUPLICATE,
            auth_message(lang, "trial_used"),
        )

    if not is_low:
        # medium/high: we record verification_required, we do NOT give a cardless trial.
        # Step-up €1 (Paddle SetupIntent) — Sprint-21 (see
        # anti_abuse.perform_setup_intent_step_up → NotImplementedError).
        await session.commit()
        await _notify_admins_new_signup(
            email_sender=email_sender,
            settings=settings,
            email=norm_email,
            tenant_id=tenant_id,
            outcome="verification_required",
            now=now,
            user_id=user.id,
        )
        return SignupResult(
            VERIFICATION_REQUIRED,
            auth_message(lang, "card_verification"),
            risk=risk,
        )

    # 5. low risk → email-verify link (TTL 24h), cardless trial.
    raw = security.generate_token()
    session.add(
        AuthToken(
            user_id=user.id,
            purpose="email_verify",
            token_hash=security.hash_token(raw),
            expires_at=moment + timedelta(seconds=settings.email_verify_ttl),
        )
    )
    await session.commit()

    link = f"{settings.cabinet_base_url.rstrip('/')}/auth/verify?token={raw}"
    await email_sender.send(
        to=norm_email,
        subject=email_t(lang, "verify_subject"),
        text_body=email_t(lang, "verify_body").format(link=link),
    )
    await _notify_admins_new_signup(
        email_sender=email_sender,
        settings=settings,
        email=norm_email,
        tenant_id=tenant_id,
        outcome="trial_started",
        now=now,
    )
    return SignupResult(
        TRIAL_STARTED,
        auth_message(lang, "check_mail"),
        risk=risk,
    )


async def request_reregistration(
    session, *, email: str, locale: str | None
) -> str | None:
    """A “re-registration” request for the address of a deleted account (#658).

    Returns the id of the created request or ``None`` — when no request was filed:
    the address does not belong to a deleted account, a request already awaits a decision, or
    the approval has not been used yet. The caller shows the SAME response in both
    cases — one cannot find out by brute force which addresses are deleted from the response.
    """
    norm_email = security.normalize_email(email)
    if not norm_email or "@" not in norm_email:
        return None
    value_hash = security.hash_value(security.canonical_email_for_abuse(norm_email))
    row = (
        await session.execute(
            _REREGISTER_REQUEST_SQL,
            {"h": value_hash, "lang": norm_locale(locale), "email": norm_email},
        )
    ).first()
    await session.commit()
    return str(row.request_id) if row is not None else None


async def resend_verification(
    session,
    *,
    email: str,
    email_sender: EmailSender,
    settings: CabinetSettings,
    locale: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Resends the email confirmation letter.

    Returns True if the email was sent. For a non-existent or already
    confirmed address it returns False (anti-enumeration: the router answers
    the same way in both cases).

    ``locale`` — the language of the email, resolved by the router from the request (the browser UI language).
    The saved ``users.locale`` is not read here: the resolver (SECURITY DEFINER,
    RED) returns only id/tenant/verified — extending it is out of scope; NULL → ru.

    Idempotency: the previous live ``email_verify`` tokens are revoked (consumed_at)
    before a new one is created — old links stop being valid.
    """
    lang = norm_locale(locale)
    norm_email = security.normalize_email(email)
    result = await session.execute(_RESOLVE_USER_FOR_AUTH_SQL, {"email": norm_email})
    row = result.first()

    if row is None or row.email_verified_at is not None:
        return False

    moment = _now(now)

    # Revoke the previous live email_verify tokens (parallel invalidation).
    await session.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == row.id,
            AuthToken.purpose == "email_verify",
            AuthToken.consumed_at.is_(None),
        )
        .values(consumed_at=moment)
    )

    raw = security.generate_token()
    session.add(
        AuthToken(
            user_id=row.id,
            purpose="email_verify",
            token_hash=security.hash_token(raw),
            expires_at=moment + timedelta(seconds=settings.email_verify_ttl),
        )
    )
    await session.commit()

    link = f"{settings.cabinet_base_url.rstrip('/')}/auth/verify?token={raw}"
    await email_sender.send(
        to=norm_email,
        subject=email_t(lang, "verify_subject"),
        text_body=email_t(lang, "verify_body").format(link=link),
    )
    return True


async def verify_email_token(
    session,
    *,
    raw_token: str,
    now: datetime | None = None,
    email_sender: EmailSender | None = None,
):
    """Confirms the email by token. Returns ``{uid,tid}`` or ``None``.

    Idempotent-safe: an expired/used/unknown token → None.

    The welcome email (owner 16.10.2026) is sent ONCE — at the moment of the transition of
    ``email_verified_at`` NULL→NOT NULL, without a migration: a repeated click and a
    repeated verify on an already confirmed account do not send mail. A sending error
    does not cancel the confirmation (best-effort, see _send_welcome).
    """
    token = await _get_live_token(session, raw_token, "email_verify", now)
    if token is None:
        return None
    tid_result = await session.execute(
        _RESOLVE_TENANT_BY_USER_ID_SQL, {"uid": token.user_id}
    )
    tid = tid_result.scalar_one_or_none()
    if tid is None:
        return None
    # GUC BEFORE session.get/UPDATE users — otherwise FORCE RLS (0021) raises
    # UndefinedObjectError for a non-existent GUC.
    await set_current_tenant(session, tid)
    user = await session.get(User, token.user_id)
    if user is None:
        return None
    moment = _now(now)
    first_confirmation = user.email_verified_at is None
    user.email_verified_at = moment
    token.consumed_at = moment
    # activate the tenant (if it is still trial) — it is already trial from signup
    locale = (
        user.locale
    )  # into the signed session: the chrome does not read the DB on render
    email = user.email
    await session.commit()
    if first_confirmation and email_sender is not None and email:
        await _send_welcome(email_sender, to=email, locale=locale)
    return {"uid": str(user.id), "tid": str(tid), "locale": locale}


async def _send_welcome(email_sender: EmailSender, *, to: str, locale: str | None):
    """Welcome email: From/Reply-To — the operator’s mailbox.

    Best-effort: the confirmation is already committed.

    Only the exception type goes into the log: we do not write the address or the SMTP error text (PII).
    The address form is an e-mail: there is no separate user name in users.
    """
    try:
        subject, text_body, html_body = welcome_parts(
            locale,
            name=to,
            contact_email=ENTERPRISE_CONTACT_EMAIL,
            tg_url=TG_CHANNEL_URL.removeprefix("https://"),
            app_url=WELCOME_APP_URL,
        )
        await email_sender.send(
            to=to,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            locale=locale,
            from_email=WELCOME_FROM_EMAIL,
            reply_to=ENTERPRISE_CONTACT_EMAIL,
        )
    except Exception as exc:  # noqa: BLE001 — the email must not break the confirmation
        _log.warning("welcome email not sent: %s", type(exc).__name__)


async def email_verify_already_done(
    session, *, raw_token: str, now: datetime | None = None
) -> bool:
    """Idempotency of verify: the token was genuinely issued for an ALREADY confirmed account.

    Link prefetch (anti-phishing/mail preview) opens ``/auth/verify``
    first, revokes the token and sets ``email_verified_at``; the real click
    by the person arrives at the revoked token → :func:`verify_email_token` returns
    ``None``, although the account is already confirmed. Showing “the link is invalid”
    in this case is wrong.

    ``True`` — ONLY when a token with such a hash (``purpose='email_verify'``)
    exists AND the account bound to it is already confirmed
    (``email_verified_at IS NOT NULL``). The token logic is NOT touched: nothing is
    revoked and no session is issued — this is a read-only fact check only for the branch that shows
    the result. An unknown token gives no false success.
    """
    if not raw_token:
        return False
    result = await session.execute(
        select(AuthToken).where(
            AuthToken.token_hash == security.hash_token(raw_token),
            AuthToken.purpose == "email_verify",
        )
    )
    token = result.scalars().first()
    if token is None:
        return False
    tid_result = await session.execute(
        _RESOLVE_TENANT_BY_USER_ID_SQL, {"uid": token.user_id}
    )
    tid = tid_result.scalar_one_or_none()
    if tid is None:
        return False
    # GUC BEFORE session.get(User) — FORCE RLS (0021) otherwise raises UndefinedObjectError.
    await set_current_tenant(session, tid)
    user = await session.get(User, token.user_id)
    return user is not None and user.email_verified_at is not None


async def request_magic_link(
    session,
    *,
    email: str,
    email_sender: EmailSender,
    settings: CabinetSettings,
    next_path: str | None = None,
    locale: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Creates a magic link for an existing confirmed user.

    Returns True if the email was sent. For a non-existent/
    unconfirmed email it returns False, but the router shows the same
    response (anti-enumeration).

    ``locale`` — the language of the email, resolved by the router from the request (the browser UI language);
    the saved ``users.locale`` is not read (see ``resend_verification``). NULL → ru.

    ``next_path`` — an already validated (``security.safe_next_path``) return path:
    it travels in the email link, so that the login from /admin/login leads into the
    admin. The recipient of the link validates it again.
    """
    lang = norm_locale(locale)
    norm_email = security.normalize_email(email)
    result = await session.execute(_RESOLVE_USER_FOR_AUTH_SQL, {"email": norm_email})
    row = result.first()
    if row is None or row.email_verified_at is None:
        return False

    raw = security.generate_token()
    moment = _now(now)
    session.add(
        AuthToken(
            user_id=row.id,
            purpose="magic_link",
            token_hash=security.hash_token(raw),
            expires_at=moment + timedelta(seconds=settings.magic_link_ttl),
        )
    )
    await session.commit()

    link = f"{settings.cabinet_base_url.rstrip('/')}/auth/magic?token={raw}"
    if next_path:
        link += f"&next={quote(next_path, safe='')}"
    await email_sender.send(
        to=norm_email,
        subject=email_t(lang, "magic_subject"),
        text_body=email_t(lang, "magic_body").format(link=link),
    )
    return True


async def consume_magic_link(session, *, raw_token: str, now: datetime | None = None):
    """Revokes the magic link and returns ``{uid,tid}`` for the session, or ``None``."""
    token = await _get_live_token(session, raw_token, "magic_link", now)
    if token is None:
        return None
    tid_result = await session.execute(
        _RESOLVE_TENANT_BY_USER_ID_SQL, {"uid": token.user_id}
    )
    tid = tid_result.scalar_one_or_none()
    if tid is None:
        return None
    # GUC BEFORE reading users.locale (FORCE RLS 0021), then the token is revoked. The locale travels
    # into the signed session, so that the i18n chrome does not do a SELECT of locale on EVERY
    # render of the cabinet (including on every poll tick of the generation status).
    await set_current_tenant(session, tid)
    user = await session.get(User, token.user_id)
    locale = getattr(user, "locale", None) if user is not None else None
    token.consumed_at = _now(now)
    await session.commit()
    return {"uid": str(token.user_id), "tid": str(tid), "locale": locale}


async def _get_live_token(session, raw_token: str, purpose: str, now):
    """Returns the valid (unused, unexpired) token or None."""
    if not raw_token:
        return None
    result = await session.execute(
        select(AuthToken).where(
            AuthToken.token_hash == security.hash_token(raw_token),
            AuthToken.purpose == purpose,
        )
    )
    token = result.scalars().first()
    if token is None or token.consumed_at is not None:
        return None
    moment = _now(now)
    expires = token.expires_at
    # tz-aware comparison; expires_at is stored tz-aware
    if expires is not None and expires <= moment:
        return None
    return token
