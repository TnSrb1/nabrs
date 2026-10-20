"""Cabinet channel-binding service (Chat 26, T3 — cabinet↔bot linkage, C-04).

A subscriber from the web cabinet (signup) has a ``tenant``+``user``, but does NOT have
``user_channels(telegram)`` — therefore delivery does not find a target and the delivery goes
to ``failed``. Here the cabinet generates a one-time bind token for ITS ``user_id`` and
hands out the deep link ``https://t.me/<bot>?start=<token>``; the bot itself revokes the token and creates
``user_channels`` in ``/start <token>`` (see :mod:`src.bots.telegram`).

The token is the same mechanism as the magic link/email-verify: ``AuthToken`` with
``purpose='channel_bind'`` (migration 0007), only the SHA-256 is stored
(:func:`src.api.security.hash_token`), single-use (``consumed_at``), TTL
``settings.channel_bind_ttl`` (~15 min).

owner (double opt-in email): the email address is entered in status ``pending`` and
becomes ``active`` only through the link from the letter to THIS address — until then
delivery ignores it (the predicate ``status = 'active'``, see
:func:`src.workers.matching.channel_states`). The confirmation token is the SAME
contour (:func:`create_bind_token`), but the hash is computed with the domain prefix
``email-confirm:<address>:`` (see :func:`_email_confirm_namespace`).

The functions take ``AsyncSession`` and collaborators — they are tested directly on a
mock session (ADR-013). With VG-01 (0021) ``api`` runs under the role ``tendersrb_app``
(NOSUPERUSER, FORCE RLS on tenant tables) — ``user_channels`` is isolated by the RLS
policy (0022), therefore the calling route MUST set the tenant context
(``repo.set_tenant``) before any access here; the queries are also explicitly
filtered by ``user_id`` from the signed session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from src.api import security
from src.api.config import CabinetSettings
from src.api.emailer import EmailSender
from src.i18n_email import email_t
from src.db.models import AuthToken, UserChannel
from src.db.session import commit_keep_tenant
from src.domain.profile_channels import adopt_channel_into_empty_profiles
from src.workers.matching import ChannelState, channel_states

CHANNEL_BIND_PURPOSE = "channel_bind"

# Channel names for the cabinet banner (ru-only, like the whole cabinet).
CHANNEL_LABELS = {"telegram": "Telegram", "email": "Email"}

EMAIL_CHANNEL = "email"
TELEGRAM_CHANNEL = "telegram"

# Where the “connect a channel” nudge from the dashboard leads (ru-only cabinet). Both channels — a section
# “Kanali” of the “Moj kabinet” hub (owner 10.10): there is no Telegram card on the dashboard (owner
# 22.09 — channels are not duplicated on the dashboard), the anchor ``#telegram-card`` would lead to nowhere.
CHANNEL_BIND_LINKS = {
    TELEGRAM_CHANNEL: "/cabinet/account#kanali",
    EMAIL_CHANNEL: "/cabinet/account#kanali",
}

# The dictionary of ``user_channels`` statuses (DB_SCHEMA §2.5). Both the routing
# (``ChannelState.is_live``) and the delivery (``_LOAD_JOBS_SQL``) ask exactly
# ``status = 'active'``, therefore everything that is not ``active`` is the same for them — "do not
# send". This is what ``pending`` (owner) rests on: an unconfirmed address gets no
# delivery without a single edit in the workers. The column is a free TEXT (0001), a new
# status does not require a migration.
CHANNEL_STATUS_ACTIVE = "active"
CHANNEL_STATUS_BLOCKED = "blocked"
CHANNEL_STATUS_PENDING = "pending"

# RFC 5321: the whole address — no longer than 254 characters.
_MAX_EMAIL_LENGTH = 254


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def build_deep_link(bot_username: str, raw_token: str) -> str:
    """Telegram deep link for binding: ``https://t.me/<bot>?start=<token>``."""
    return f"https://t.me/{bot_username}?start={raw_token}"


async def has_active_telegram(session, user_id: str) -> bool:
    """Whether the user has an active Telegram channel binding."""
    result = await session.execute(
        select(UserChannel).where(
            UserChannel.user_id == user_id,
            UserChannel.channel == "telegram",
            UserChannel.status == "active",
        )
    )
    return result.scalars().first() is not None


async def dead_channels(session, tenant_id: str) -> list[str]:
    """The tenant’s channels that were connected but are now not live (ru names).

    The source of truth is :func:`src.workers.matching.channel_states`: THE SAME
    predicate by which routing decides whether to write into ``delivery_outbox``. A second
    predicate must not be introduced here — it would diverge from reality, and the banner would
    lie (as in case ``7986f3b0``, where a user silently did not receive tenders).

    A channel the tenant does not have at all does NOT land here: that is “not connected yet”
    (onboarding is the job of the dashboard checklist), not “dropped out”. For the same
    reason ``pending`` (owner) does not land here either: the address awaits confirmation —
    it is not “no longer accepting messages”, and the banner’s text about a blocked
    bot and a canceled binding would lie about it. The waiting status is shown by
    the “My Email” panel.

    The caller MUST set the tenant context (``repo.set_tenant``) before the call:
    ``user_channels`` under the role ``tendersrb_app`` is isolated by RLS (0022).
    """
    states = await channel_states(session, tenant_id)
    return [
        CHANNEL_LABELS.get(name, name)
        for name, state in sorted(states.items())
        if not state.is_live and state.status != CHANNEL_STATUS_PENDING
    ]


def _delivery_gaps(profiles, states: dict[str, ChannelState]) -> list[dict]:
    """Channels that the ACTIVE profile asks for, but there is no live addressee.

    “Setting ≠ fact” (CLAUDE.md): ``profiles.delivery_channels`` — WHERE to send
    (the setting), ``states`` (the freshest ``user_channels`` row, THE SAME selection
    as delivery’s — :func:`src.workers.matching.channel_states`) — whether there is a
    live addressee. Their gap = silently lost delivery (case #340).

    We return only two SILENT cases: the address awaits confirmation (``pending``)
    and the channel was never set up. The case “the channel dropped out” (it was ``active``, now
    not) is NOT duplicated here — the loud banner :func:`dead_channels` already shouts about it, and it takes
    exactly those channels (not live AND not pending). A disabled profile sends nothing → it does not fall into the gap.
    """
    requested: set[str] = set()
    for profile in profiles:
        if getattr(profile, "is_active", False):
            requested.update(getattr(profile, "delivery_channels", None) or [])
    gaps: list[dict] = []
    for channel in sorted(requested):
        state = states.get(channel)
        if state is not None and state.is_live:
            continue  # there is a live addressee — delivery will arrive
        never = state is None
        pending = state is not None and state.status == CHANNEL_STATUS_PENDING
        if not (never or pending):
            continue  # “dropped out” — that is dead_channels, we do not duplicate it
        gaps.append(
            {
                "channel": channel,
                "label": CHANNEL_LABELS.get(channel, channel),
                "pending": pending,
                "link": CHANNEL_BIND_LINKS.get(channel, "/app"),
            }
        )
    return gaps


def _channelless_profiles(profiles, states: dict[str, ChannelState]) -> list[dict]:
    """Active templates WITHOUT a single channel while the tenant has a live channel.

    The inverse of :func:`_delivery_gaps`: an addressee exists, but the template does not say
    where to send → ``write_outbox`` skips it, tenders do not arrive. Binding a channel adds it to such templates
    itself (:func:`src.domain.profile_channels.adopt_channel_into_empty_profiles`), but
    a template where the client unticked all the boxes while a live channel exists, and Lite with a single
    Telegram, remain — without a notice the client sees only “no deliveries”. No live channel
    at all — we stay silent: that is the onboarding step “connect a channel”.
    """
    if not any(state.is_live for state in states.values()):
        return []
    return [
        {"id": str(profile.id), "name": getattr(profile, "name", None)}
        for profile in profiles
        if getattr(profile, "is_active", False)
        and not (getattr(profile, "delivery_channels", None) or [])
    ]


async def delivery_channel_status(session, tenant_id: str, profiles) -> dict:
    """Delivery state for the dashboard: whether there is a live channel + gaps “asks, but nowhere to send”.

    ONE SELECT of states (:func:`src.workers.matching.channel_states` — a mirror of the
    delivery selection) serves both the onboarding checklist predicate
    (``has_live_channel``) and the list of gaps (:func:`_delivery_gaps`). A second
    predicate is not introduced — it would diverge from where the delivery actually goes.

    The caller MUST set the tenant context (``repo.set_tenant``) before the call:
    ``user_channels`` under the role ``tendersrb_app`` is isolated by RLS (0022).
    """
    states = await channel_states(session, tenant_id)
    return {
        "has_live_channel": any(state.is_live for state in states.values()),
        "live_channels": sorted(ch for ch, state in states.items() if state.is_live),
        "gaps": _delivery_gaps(profiles, states),
        "channelless": _channelless_profiles(profiles, states),
    }


def build_delivery_warning(
    profiles, has_live_channel: bool, channelless: list[dict]
) -> dict | None:
    """Context of the partial ``_delivery_warning.html`` — two cases of “the template exists, no delivery”.

    (a) ``no_channel``: there is an active template, but the tenant has no live channel at all
    → tenders go nowhere; (b) ``channelless``: a channel exists, but the active
    template has not selected it (:func:`_channelless_profiles`). The cases are mutually exclusive
    (``_channelless_profiles`` is silent without a live channel). No gap → ``None``.
    A tenant without active templates is not warned: there is nothing to send to them, this step
    is led by the onboarding checklist.
    """
    no_channel = not has_live_channel and any(
        getattr(p, "is_active", False) for p in profiles
    )
    if not no_channel and not channelless:
        return None
    return {"no_channel": no_channel, "channelless": [] if no_channel else channelless}


async def delivery_warning(session, tenant_id: str, profiles) -> dict | None:
    """:func:`build_delivery_warning` for pages where the delivery status is not computed.

    The caller MUST set the tenant context (``repo.set_tenant``) before the call.
    """
    status = await delivery_channel_status(session, tenant_id, profiles)
    return build_delivery_warning(
        profiles, status["has_live_channel"], status["channelless"]
    )


async def live_delivery_channels(session, tenant_id: str) -> list[str]:
    """The tenant’s live channels (email, telegram) — those that actually deliver."""
    email_state = await email_channel_state(session, tenant_id)
    telegram_state = await telegram_channel_state(session, tenant_id)
    live: list[str] = []
    if email_state is not None and email_state.is_live:
        live.append(EMAIL_CHANNEL)
    if telegram_state is not None and telegram_state.is_live:
        live.append(TELEGRAM_CHANNEL)
    return live


def parse_email_address(raw: str) -> tuple[str | None, str | None]:
    """Checks the entered address → ``(normalized address, form error KEY)``.

    The check is deliberately basic (there is ``@``, a domain with a dot, the length): only the letter itself can prove
    deliverability, and a faulty input must come back as a form error, not a 500. The normalization is shared with signup/login
    (:func:`src.api.security.normalize_email`), so that the binding address and the account address do not diverge by case.

    The second element is a KEY of the cabinet chrome catalog (``emp_err_*``), not a ready text:
    the service layer does not know the user’s language, it localizes the template through ``t`` (the two
    i18n axes are not mixed — CLAUDE.md). ``_MAX_EMAIL_LENGTH`` (254, RFC 5321)
    is baked into the text of the key ``emp_err_too_long`` — a constant, not a runtime parameter.
    """
    address = security.normalize_email(raw)
    if not address:
        return None, "emp_err_empty"
    if len(address) > _MAX_EMAIL_LENGTH:
        return None, "emp_err_too_long"
    if any(ch.isspace() for ch in address):
        return None, "emp_err_spaces"
    if address.count("@") != 1:
        return None, "emp_err_at"
    local, _, domain = address.partition("@")
    if not local or not domain:
        return None, "emp_err_form"
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None, "emp_err_domain"
    return address, None


def mask_email(address: str | None) -> str:
    """Address mask for display in the cabinet: ``j***n@example.com``.

    In the cabinet it is enough to know “is this the right address”; showing it in full is pointless —
    the same principle as with PII in logs.
    """
    local, sep, domain = (address or "").partition("@")
    if not sep or not local:
        return "***"
    masked = f"{local[0]}***{local[-1]}" if len(local) > 2 else f"{local[0]}***"
    return f"{masked}@{domain}"


async def email_channel_state(session, tenant_id: str) -> ChannelState | None:
    """State of the tenant’s email channel, or ``None`` — if it was never set up.

    The row is chosen by :func:`src.workers.matching.channel_states` — THE SAME
    selection (the freshest active) that delivery will take. A separate SELECT must not be
    introduced here: ``external_id`` is not unique, and a second predicate would diverge
    from where the letter actually goes.

    The caller MUST set the tenant context (``repo.set_tenant``) before the call.
    """
    states = await channel_states(session, tenant_id)
    return states.get(EMAIL_CHANNEL)


async def telegram_channel_state(session, tenant_id: str) -> ChannelState | None:
    """State of the tenant’s Telegram channel, or ``None`` — if it was never set up.

    The same selection (:func:`src.workers.matching.channel_states`, the freshest
    ``active``) as the email panel and as delivery. The ``external_id`` of the live row —
    is the Telegram chat_id by which the bot identifies the user; code that takes it
    must take it from here, not from its own SELECT: ``external_id`` is not
    unique, and a second predicate would return someone else’s/a stale chat_id.

    The caller MUST set the tenant context (``repo.set_tenant``) before the call.
    """
    states = await channel_states(session, tenant_id)
    return states.get(TELEGRAM_CHANNEL)


async def _deactivate_email(session, *, user_id: str) -> None:
    """Revokes all live email rows of this user (without commit).

    Live — both ``active`` and ``pending``: an unfinished confirmation of the previous
    address must die together with it, otherwise a link issued for it would
    remain clickable.
    """
    await session.execute(
        update(UserChannel)
        .where(
            UserChannel.user_id == user_id,
            UserChannel.channel == EMAIL_CHANNEL,
            UserChannel.status.in_((CHANNEL_STATUS_ACTIVE, CHANNEL_STATUS_PENDING)),
        )
        .values(status=CHANNEL_STATUS_BLOCKED)
    )


async def link_email(
    session, *, user_id: str, address: str, now: datetime | None = None
) -> None:
    """Registers ``address`` as a ``pending`` addressee — WITHOUT delivery to it.

    An ``active`` row never appears from here: the address could be someone else’s or have a typo, and the right to send
    letters is given only by a click on the link from the letter to the address itself (:func:`confirm_email`). The entry point for the cabinet is
    :func:`request_email_confirmation`, which also sends the letter.

    The previous live rows are revoked BEFORE the insert: ``external_id`` is not unique, and
    two live rows at once would mean that the winner is chosen by the order of
    ``DISTINCT ON``, not by the user.

    The caller MUST set the tenant context (``repo.set_tenant``) before the call:
    ``user_channels`` under the role ``tendersrb_app`` is isolated by RLS (0022).
    """
    await _deactivate_email(session, user_id=user_id)
    session.add(
        UserChannel(
            user_id=user_id,
            channel=EMAIL_CHANNEL,
            external_id=address,
            status=CHANNEL_STATUS_PENDING,
            linked_at=_now(now),
        )
    )
    await commit_keep_tenant(session)


async def unlink_email(session, *, user_id: str) -> None:
    """Unbinds the email: all active rows of the channel → ``blocked``.

    The row is not deleted — ``user_channels`` is also the history of bindings, and
    delivery needs only that no active row remains.
    """
    await _deactivate_email(session, user_id=user_id)
    await commit_keep_tenant(session)


async def create_bind_token(
    session,
    *,
    user_id: str,
    settings: CabinetSettings,
    now: datetime | None = None,
    namespace: str = "",
    ttl_sec: int | None = None,
) -> str:
    """Creates a ``channel_bind`` token for ``user_id`` and returns the RAW token.

    Only the SHA-256 (``token_hash``) goes into the DB; the raw token goes into the
    deep link/letter and is not stored anywhere else.

    ``namespace`` — the domain prefix of the hash: the DB gets
    ``sha256(namespace + raw)``, and the verifying side must know the same
    prefix. Thus one ``purpose`` serves two independent purposes:
    the Telegram binding (the prefix is empty) and the email confirmation
    (:func:`_email_confirm_namespace`). Without the separation, a token from the Telegram link
    — which the cabinet shows to the user themselves — would fit the email confirmation and
    would activate someone else’s address, i.e. double opt-in would be bypassed in two clicks.
    A separate ``purpose`` would be more honest, but ``auth_tokens_purpose_check``
    (0006/0007) enumerates the values → a migration would be needed (RED).
    """
    raw = security.generate_token()
    moment = _now(now)
    ttl = settings.channel_bind_ttl if ttl_sec is None else ttl_sec
    session.add(
        AuthToken(
            user_id=user_id,
            purpose=CHANNEL_BIND_PURPOSE,
            token_hash=security.hash_token(namespace + raw),
            expires_at=moment + timedelta(seconds=ttl),
        )
    )
    await commit_keep_tenant(session)
    return raw


def _email_confirm_namespace(address: str) -> str:
    """The domain hash prefix of the confirmation token — binds the token to the address.

    The address enters the prefix, therefore a token issued for ``a@x`` will not fit
    a ``pending`` row with another address: otherwise "register my address → get the
    letter → register someone else’s → click my own link" would activate someone else’s
    address.
    """
    return f"email-confirm:{address}:"


def _confirm_link(settings: CabinetSettings, raw_token: str) -> str:
    base = settings.cabinet_base_url.rstrip("/")
    return f"{base}/cabinet/email/confirm?token={raw_token}"


async def _send_confirmation(
    email_sender: EmailSender,
    *,
    settings: CabinetSettings,
    address: str,
    raw_token: str,
    locale: str | None = None,
) -> None:
    """The letter with the confirmation link. The address never gets into the logs.

    ``locale`` — the interface language of the address owner (``users.locale``): the letter
    arrives in the same language as the cabinet it was requested from.
    Unknown/None → ru (the contract of :func:`src.i18n_email.norm_locale`).
    """
    hours = max(1, settings.email_verify_ttl // 3600)
    await email_sender.send(
        to=address,
        subject=email_t(locale, "channel_verify_subject"),
        text_body=email_t(locale, "channel_verify_body").format(
            link=_confirm_link(settings, raw_token), hours=hours
        ),
    )


async def request_email_confirmation(
    session,
    *,
    user_id: str,
    address: str,
    settings: CabinetSettings,
    email_sender: EmailSender,
    now: datetime | None = None,
    locale: str | None = None,
) -> None:
    """Registers the address as ``pending`` and sends the confirmation link to it.

    There will be no delivery before confirmation: ``pending`` does not pass the live-channel predicate.
    The previous rows (including ``active``) are revoked in :func:`link_email` —
    changing the address means that we no longer write to the old one.

    The caller MUST set the tenant context (``repo.set_tenant``) before the call.
    """
    await link_email(session, user_id=user_id, address=address, now=now)
    raw = await create_bind_token(
        session,
        user_id=user_id,
        settings=settings,
        now=now,
        namespace=_email_confirm_namespace(address),
        ttl_sec=settings.email_verify_ttl,
    )
    await _send_confirmation(
        email_sender,
        settings=settings,
        address=address,
        raw_token=raw,
        locale=locale,
    )


async def resend_email_confirmation(
    session,
    *,
    user_id: str,
    tenant_id: str,
    settings: CabinetSettings,
    email_sender: EmailSender,
    now: datetime | None = None,
    locale: str | None = None,
) -> bool:
    """Resends the link for an already registered ``pending`` address.

    ``False`` — if there is nothing to confirm (the address was not registered / it is already active).
    We do not create a new row: the address is taken from the existing ``pending`` row,
    so a repeat cannot substitute the addressee.
    """
    state = await email_channel_state(session, tenant_id)
    if state is None or state.status != CHANNEL_STATUS_PENDING:
        return False
    address = (state.external_id or "").strip()
    if not address:
        return False
    raw = await create_bind_token(
        session,
        user_id=user_id,
        settings=settings,
        now=now,
        namespace=_email_confirm_namespace(address),
        ttl_sec=settings.email_verify_ttl,
    )
    await _send_confirmation(
        email_sender,
        settings=settings,
        address=address,
        raw_token=raw,
        locale=locale,
    )
    return True


async def _live_email_confirm_token(
    session, *, user_id: str, address: str, raw_token: str, now: datetime | None
):
    """A valid (unused, unexpired) confirmation token or None.

    We search by ``sha256(namespace + raw)`` — the raw token from the link is not compared with the stored
    hash and does not appear in the DB.
    """
    if not raw_token:
        return None
    result = await session.execute(
        select(AuthToken).where(
            AuthToken.user_id == user_id,
            AuthToken.purpose == CHANNEL_BIND_PURPOSE,
            AuthToken.token_hash
            == security.hash_token(_email_confirm_namespace(address) + raw_token),
        )
    )
    token = result.scalars().first()
    if token is None or token.consumed_at is not None:
        return None
    expires = token.expires_at
    if expires is not None and expires <= _now(now):
        return None
    return token


async def confirm_email(
    session,
    *,
    user_id: str,
    tenant_id: str,
    raw_token: str,
    now: datetime | None = None,
) -> bool:
    """Confirms the ``pending`` address by token: ``pending`` → ``active``.

    ``False`` (rather than an exception) on any invalid input — an unknown/expired
    /reused token, there is nothing to confirm: the route must show
    a page, not a 500. The token is revoked (``consumed_at``) — a repeat will not pass.

    The caller MUST set the tenant context (``repo.set_tenant``) before the call:
    the ``pending`` row is searched and updated within the tenant (RLS 0022).
    """
    state = await email_channel_state(session, tenant_id)
    if state is None or state.status != CHANNEL_STATUS_PENDING:
        return False
    address = (state.external_id or "").strip()
    if not address:
        return False
    token = await _live_email_confirm_token(
        session, user_id=user_id, address=address, raw_token=raw_token, now=now
    )
    if token is None:
        return False
    await session.execute(
        update(UserChannel)
        .where(
            UserChannel.channel == EMAIL_CHANNEL,
            UserChannel.status == CHANNEL_STATUS_PENDING,
            UserChannel.external_id == address,
        )
        .values(status=CHANNEL_STATUS_ACTIVE)
    )
    # A template created BEFORE the channel was connected, otherwise it would remain without delivery.
    await adopt_channel_into_empty_profiles(
        session, tenant_id=tenant_id, channel=EMAIL_CHANNEL
    )
    token.consumed_at = _now(now)
    # The route reads the channel state in THIS SAME session — the tenant is needed after the commit as well.
    await commit_keep_tenant(session)
    return True


async def email_confirm_already(
    session, *, user_id: str, tenant_id: str, raw_token: str
) -> bool:
    """Idempotency of confirmation: the address is ALREADY confirmed by THIS token.

    Mail scanners/anti-phishing/mail previews may open the link
    FIRST — confirm the address (``pending`` → ``active``) and revoke the token, while the real
    click of the user arrives at the already revoked token. Then
    :func:`confirm_email` returns ``False``, although the binding in fact took place, and
    showing an error is wrong (“error, but the mail got bound”).

    ``True`` — ONLY when TWO facts coincide: (1) the tenant’s email channel is live
    (``active``) and (2) the passed raw token was genuinely issued to THIS
    user for the address of the live channel — the hash with the domain prefix
    (:func:`_email_confirm_namespace`) matches, even if the token is already revoked.
    A foreign/unknown token gives no false success: the double opt-in does not weaken,
    “ok” cannot be shown for an arbitrary revoked token (see the TTL/single-use
    of the token itself for a NEW binding — it is not affected, this is read-only).

    The caller MUST set the tenant context (``repo.set_tenant``) before the call.
    """
    if not raw_token:
        return False
    state = await email_channel_state(session, tenant_id)
    if state is None or not state.is_live:
        return False
    address = (state.external_id or "").strip()
    if not address:
        return False
    result = await session.execute(
        select(AuthToken).where(
            AuthToken.user_id == user_id,
            AuthToken.purpose == CHANNEL_BIND_PURPOSE,
            AuthToken.token_hash
            == security.hash_token(_email_confirm_namespace(address) + raw_token),
        )
    )
    return result.scalars().first() is not None
