"""Tests of the service for binding the cabinet’s Telegram channel (Chat 26, T3, C-04).

The DB is mocked (FakeSession), the network is not touched. We check the generation of the
``channel_bind`` token (validity, TTL, the hash is stored, not the raw one) and the detection of the
active binding.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.api import security
from src.api.cabinet import channel_service as svc
from src.api.config import CabinetSettings
from src.api.emailer import MockEmailSender
from src.workers.matching import ChannelState
from tests.api.conftest import FakeResult, FakeSession


def _settings():
    return CabinetSettings(CABINET_BASE_URL="https://nab.rs", CABINET_SECRET_KEY="t")


@pytest.mark.asyncio
async def test_create_bind_token_persists_hash_not_raw():
    session = FakeSession()
    raw = await svc.create_bind_token(session, user_id="user-1", settings=_settings())
    # The raw token is returned; in the DB — an AuthToken with purpose channel_bind.
    added = [o for o in session.added if type(o).__name__ == "AuthToken"]
    assert len(added) == 1
    token = added[0]
    assert token.purpose == "channel_bind"
    assert token.user_id == "user-1"
    # Only the SHA-256 is stored, not the token itself.
    assert token.token_hash == security.hash_token(raw)
    assert token.token_hash != raw
    # The TTL is in the future (single-use is burned by the bot).
    assert token.expires_at > datetime.now(timezone.utc)
    assert token.consumed_at is None
    assert session.committed == 1


@pytest.mark.asyncio
async def test_create_bind_token_ttl_matches_settings():
    settings = CabinetSettings(CABINET_SECRET_KEY="t", CHANNEL_BIND_TTL=900)
    before = datetime.now(timezone.utc)
    session = FakeSession()
    await svc.create_bind_token(session, user_id="u", settings=settings)
    token = [o for o in session.added if type(o).__name__ == "AuthToken"][0]
    delta = (token.expires_at - before).total_seconds()
    # ~15 min (a small drift for execution is allowed).
    assert 890 <= delta <= 905


@pytest.mark.asyncio
async def test_has_active_telegram_true_when_channel_present():
    channel = SimpleNamespace(channel="telegram", status="active")
    session = FakeSession(results=[FakeResult(items=[channel])])
    assert await svc.has_active_telegram(session, "user-1") is True


@pytest.mark.asyncio
async def test_has_active_telegram_false_when_absent():
    session = FakeSession(results=[FakeResult(items=[])])
    assert await svc.has_active_telegram(session, "user-1") is False


def test_build_deep_link_format():
    link = svc.build_deep_link("Nabrs_bot", "abc123")
    assert link == "https://t.me/Nabrs_bot?start=abc123"


# --------------------------------------------------------------------------- #
# dead_channels — the predicate of the “channel dropped off” banner (the same as in routing)
# --------------------------------------------------------------------------- #


def _uc(*, channel="telegram", status="active", external_id="555"):
    """A user_channels row, as matching.LIVE_CHANNELS_SQL returns it."""
    return SimpleNamespace(channel=channel, status=status, external_id=external_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,external_id",
    [
        ("blocked", "555"),  # the user has blocked the bot
        ("revoked", "555"),  # unlinking / manual normalization (case 7986f3b0)
        ("active", ""),  # no recipient
        ("active", None),
    ],
)
async def test_dead_channels_reports_broken_channel(status, external_id):
    session = FakeSession(
        results=[FakeResult(items=[_uc(status=status, external_id=external_id)])]
    )
    assert await svc.dead_channels(session, "t-1") == ["Telegram"]


@pytest.mark.asyncio
async def test_dead_channels_empty_when_channel_live():
    session = FakeSession(results=[FakeResult(items=[_uc()])])
    assert await svc.dead_channels(session, "t-1") == []


@pytest.mark.asyncio
async def test_dead_channels_empty_when_never_connected():
    """There are no user_channels rows at all — this is onboarding, not a drop-off: there is no banner."""
    session = FakeSession(results=[FakeResult(items=[])])
    assert await svc.dead_channels(session, "t-1") == []


# --------------------------------------------------------------------------- #
# A2: address validation — malformed input must become a form error, not a 500
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("user@company.rs", "user@company.rs"),
        ("  User@Company.RS  ", "user@company.rs"),  # trim+lowercase as in signup
        ("first.last+tag@mail.example.com", "first.last+tag@mail.example.com"),
    ],
)
def test_parse_email_address_accepts_and_normalizes(raw, expected):
    address, error = svc.parse_email_address(raw)
    assert error is None
    assert address == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "company.rs",  # no @
        "user@@company.rs",  # two @
        "user@company@rs",
        "@company.rs",  # empty local part
        "user@",  # empty domain
        "user@company",  # domain without a dot
        "user@.rs",
        "user@company.",
        "user name@company.rs",  # space inside
        "u" * 250 + "@company.rs",  # longer than 254
    ],
)
def test_parse_email_address_rejects_garbage(raw):
    address, error = svc.parse_email_address(raw)
    assert address is None
    assert error  # non-empty text for the form


@pytest.mark.parametrize(
    "address,expected",
    [
        ("john.doe@example.com", "j***e@example.com"),
        ("ab@example.com", "a***@example.com"),
        ("a@example.com", "a***@example.com"),
        ("", "***"),
        (None, "***"),
        ("not-an-email", "***"),
    ],
)
def test_mask_email_hides_local_part(address, expected):
    assert svc.mask_email(address) == expected


# --------------------------------------------------------------------------- #
# A2: the state of the email channel — the same selection as the delivery uses
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_email_channel_state_returns_row_of_channel():
    session = FakeSession(
        results=[
            FakeResult(
                items=[
                    _uc(channel="email", external_id="user@company.rs"),
                    _uc(channel="telegram"),
                ]
            )
        ]
    )
    state = await svc.email_channel_state(session, "t-1")
    assert state is not None
    assert state.external_id == "user@company.rs"
    assert state.is_live is True


@pytest.mark.asyncio
async def test_email_channel_state_none_when_never_linked():
    """Only telegram → no email was ever set up: None, not a “dead email”."""
    session = FakeSession(results=[FakeResult(items=[_uc(channel="telegram")])])
    assert await svc.email_channel_state(session, "t-1") is None


@pytest.mark.asyncio
async def test_email_channel_state_not_live_when_blocked():
    session = FakeSession(
        results=[
            FakeResult(
                items=[_uc(channel="email", status="blocked", external_id="u@c.rs")]
            )
        ]
    )
    state = await svc.email_channel_state(session, "t-1")
    assert state is not None and state.is_live is False


# --------------------------------------------------------------------------- #
# A2: binding / unbinding
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_link_email_adds_pending_row_never_active():
    """owner: the address could have been someone else’s — delivery is granted only by a click on the link from the email."""
    session = FakeSession()
    await svc.link_email(session, user_id="user-1", address="user@company.rs")

    added = [o for o in session.added if type(o).__name__ == "UserChannel"]
    assert len(added) == 1
    row = added[0]
    assert row.user_id == "user-1"
    assert row.channel == "email"
    assert row.external_id == "user@company.rs"
    assert row.status == "pending"
    assert row.status != "active"
    assert row.linked_at is not None
    assert session.committed == 1


class _TracingSession(FakeSession):
    """FakeSession that remembers the ORDER of execute/add in one list."""

    def __init__(self):
        super().__init__()
        self.trace: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.trace.append("update" if sql.startswith("UPDATE") else "execute")
        return await super().execute(statement, params)

    def add(self, obj):
        self.trace.append("add")
        super().add(obj)


@pytest.mark.asyncio
async def test_link_email_deactivates_previous_active_rows_before_insert():
    """external_id is not unique: without retiring the previous active rows, the winner would be
    chosen by the DISTINCT ON order, not by the user. Retiring must happen BEFORE the insert —
    otherwise the UPDATE (status='active' → 'blocked') would retire the new row too."""
    session = _TracingSession()
    await svc.link_email(session, user_id="user-1", address="new@company.rs")

    assert session.trace == ["update", "add"]
    updates = [sql for sql, _ in session.executed if sql.startswith("UPDATE")]
    assert len(updates) == 1
    assert "user_channels" in updates[0]


@pytest.mark.asyncio
async def test_deactivate_email_targets_pending_too_not_only_active():
    """Changing the address must also kill an unfinished confirmation of the previous address:
    otherwise the link issued for it would stay clickable. We retire by
    ``status IN (active, pending)``, not by ``status = 'active'``."""
    session = FakeSession()
    await svc.link_email(session, user_id="user-1", address="new@company.rs")

    updates = [sql for sql, _ in session.executed if sql.startswith("UPDATE")]
    assert len(updates) == 1
    compiled = updates[0].replace("\n", " ")
    assert "status IN (" in compiled
    assert "status = " not in compiled.split("WHERE", 1)[1]


@pytest.mark.asyncio
async def test_unlink_email_blocks_active_rows_without_insert():
    session = FakeSession()
    await svc.unlink_email(session, user_id="user-1")

    updates = [sql for sql, _ in session.executed if sql.startswith("UPDATE")]
    assert len(updates) == 1
    assert "user_channels" in updates[0]
    # Unbinding creates nothing — it only retires.
    assert [o for o in session.added if type(o).__name__ == "UserChannel"] == []
    assert session.committed == 1


# --------------------------------------------------------------------------- #
# owner: double opt-in — emails only to a confirmed address
# --------------------------------------------------------------------------- #


def _confirm_settings():
    return CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="t",
        EMAIL_VERIFY_TTL=60 * 60 * 24,
    )


def _pending_state(address="user@company.rs"):
    """The results queue for email_channel_state → a pending row."""
    return FakeResult(
        items=[_uc(channel="email", status="pending", external_id=address)]
    )


def _token(*, token_hash, user_id="user-1", consumed_at=None, ttl=timedelta(hours=1)):
    return SimpleNamespace(
        purpose="channel_bind",
        consumed_at=consumed_at,
        expires_at=datetime.now(timezone.utc) + ttl,
        user_id=user_id,
        token_hash=token_hash,
    )


class _TokenStoreSession(FakeSession):
    """FakeSession that ACTUALLY looks up the token by ``token_hash``.

    ‼️ An ordinary FakeSession returns a pre-built results queue without
    looking at the query: on it the test “a foreign token does not match” turns green by itself —
    “not found” is hard-coded in the fixture, not derived from the hash. Such a test does not
    turn red even if the domain prefix is removed entirely, i.e. proves nothing. Here the SELECT on
    ``auth_tokens`` resolves by the REAL bind parameter ``token_hash``, so the separation of the domains
    is checked seriously.
    """

    def __init__(self, *, channel_rows, tokens):
        super().__init__()
        self._channel_rows = list(channel_rows)
        self._tokens = list(tokens)

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params))
        is_select = sql.strip().upper().startswith("SELECT")
        if "user_channels" in sql and is_select:
            return FakeResult(items=self._channel_rows)
        if "auth_tokens" in sql and is_select:
            wanted = {
                v
                for key, v in statement.compile().params.items()
                if "token_hash" in key
            }
            return FakeResult(items=[t for t in self._tokens if t.token_hash in wanted])
        return FakeResult()


@pytest.mark.asyncio
async def test_request_email_confirmation_sends_link_and_leaves_channel_pending():
    session = FakeSession()
    sender = MockEmailSender()
    await svc.request_email_confirmation(
        session,
        user_id="user-1",
        address="user@company.rs",
        settings=_confirm_settings(),
        email_sender=sender,
    )

    row = [o for o in session.added if type(o).__name__ == "UserChannel"][0]
    assert row.status == "pending"

    assert len(sender.outbox) == 1
    letter = sender.outbox[0]
    assert letter.to == "user@company.rs"
    assert "/cabinet/email/confirm?token=" in letter.text_body


@pytest.mark.asyncio
async def test_confirmation_token_stored_as_hash_not_raw():
    """The raw token from the link does not appear in the DB — only the SHA-256 (as with telegram)."""
    session = FakeSession()
    sender = MockEmailSender()
    await svc.request_email_confirmation(
        session,
        user_id="user-1",
        address="user@company.rs",
        settings=_confirm_settings(),
        email_sender=sender,
    )
    raw = sender.outbox[0].text_body.split("token=")[1].split("\n")[0]
    token = [o for o in session.added if type(o).__name__ == "AuthToken"][0]

    assert raw not in token.token_hash
    assert token.token_hash == security.hash_token(
        f"email-confirm:user@company.rs:{raw}"
    )
    # The hash of the “bare” token is the telegram contour; the email token must not match.
    assert token.token_hash != security.hash_token(raw)
    assert token.consumed_at is None


def _confirm_hash(raw, address="user@company.rs"):
    """The hash under which the confirmation token lies in the DB.

    We compute it with the PRODUCTION function, not with a copy of the formula: a copy here would make
    the behavioral tests dummies — substituting the formula in prod would just stop
    finding the test token, and “did not match” would turn green for the wrong reason.
    The formula itself is pinned by a literal in a separate test
    (``test_confirmation_token_stored_as_hash_not_raw``).
    """
    return security.hash_token(svc._email_confirm_namespace(address) + raw)


@pytest.mark.asyncio
async def test_confirm_email_activates_pending_channel_on_valid_token():
    token = _token(token_hash=_confirm_hash("raw-token"))
    session = _TokenStoreSession(
        channel_rows=[
            _uc(channel="email", status="pending", external_id="user@company.rs")
        ],
        tokens=[token],
    )

    ok = await svc.confirm_email(
        session, user_id="user-1", tenant_id="t-1", raw_token="raw-token"
    )

    assert ok is True
    updates = [sql for sql, _ in session.executed if sql.startswith("UPDATE")]
    assert len(updates) == 1 and "user_channels" in updates[0]
    # The token is consumed — a second click on the same link will not pass.
    assert token.consumed_at is not None
    assert session.committed == 1


@pytest.mark.asyncio
async def test_confirm_email_rejects_expired_token():
    token = _token(token_hash=_confirm_hash("raw-token"), ttl=-timedelta(seconds=1))
    session = _TokenStoreSession(
        channel_rows=[
            _uc(channel="email", status="pending", external_id="user@company.rs")
        ],
        tokens=[token],
    )

    assert (
        await svc.confirm_email(
            session, user_id="user-1", tenant_id="t-1", raw_token="raw-token"
        )
        is False
    )
    assert [sql for sql, _ in session.executed if sql.startswith("UPDATE")] == []


@pytest.mark.asyncio
async def test_confirm_email_rejects_reused_token():
    token = _token(
        token_hash=_confirm_hash("raw-token"),
        consumed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    session = _TokenStoreSession(
        channel_rows=[
            _uc(channel="email", status="pending", external_id="user@company.rs")
        ],
        tokens=[token],
    )

    assert (
        await svc.confirm_email(
            session, user_id="user-1", tenant_id="t-1", raw_token="raw-token"
        )
        is False
    )
    assert [sql for sql, _ in session.executed if sql.startswith("UPDATE")] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "unknown-token"])
async def test_confirm_email_rejects_unknown_token(raw):
    session = _TokenStoreSession(
        channel_rows=[
            _uc(channel="email", status="pending", external_id="user@company.rs")
        ],
        tokens=[_token(token_hash=_confirm_hash("some-other-token"))],
    )
    assert (
        await svc.confirm_email(
            session, user_id="user-1", tenant_id="t-1", raw_token=raw
        )
        is False
    )
    assert [sql for sql, _ in session.executed if sql.startswith("UPDATE")] == []


@pytest.mark.asyncio
async def test_confirm_email_rejects_token_issued_for_another_address():
    """‼️ A double opt-in bypass: "register your own address → get the email → register
    someone else’s → click your own link". The token was issued for attacker@evil.rs, and the
    pending one is now victim@company.rs: the hash will not match, the address will not activate."""
    token = _token(token_hash=_confirm_hash("raw-token", "attacker@evil.rs"))
    session = _TokenStoreSession(
        channel_rows=[
            _uc(channel="email", status="pending", external_id="victim@company.rs")
        ],
        tokens=[token],
    )

    ok = await svc.confirm_email(
        session, user_id="user-1", tenant_id="t-1", raw_token="raw-token"
    )

    assert ok is False
    assert token.consumed_at is None
    assert [sql for sql, _ in session.executed if sql.startswith("UPDATE")] == []


@pytest.mark.asyncio
async def test_confirm_email_does_nothing_when_channel_already_active():
    """We do not touch an active channel with a repeated confirmation — and we do not even look for the token."""
    session = _TokenStoreSession(
        channel_rows=[_uc(channel="email", external_id="u@c.rs")],
        tokens=[_token(token_hash=_confirm_hash("raw-token", "u@c.rs"))],
    )
    assert (
        await svc.confirm_email(
            session, user_id="user-1", tenant_id="t-1", raw_token="raw-token"
        )
        is False
    )
    assert [sql for sql, _ in session.executed if sql.startswith("UPDATE")] == []


@pytest.mark.asyncio
async def test_email_confirm_already_true_for_consumed_token_on_active_channel():
    """Idempotency (anti-prefetch): the email scanner opened the link first —
    it confirmed the address (the channel is active) and consumed the token; the person’s click lands on
    the consumed token. The binding fact EXISTS (the channel is live + the token is ours) → success.

    Mutation: ignore the hash (success by the single active fact) → the
    ``..._false_for_foreign_token...`` becomes red; remove the is_live check → the
    ``..._false_when_channel_not_active...`` becomes red.
    """
    token = _token(
        token_hash=_confirm_hash("raw-token", "u@c.rs"),
        consumed_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    session = _TokenStoreSession(
        channel_rows=[_uc(channel="email", status="active", external_id="u@c.rs")],
        tokens=[token],
    )
    assert (
        await svc.email_confirm_already(
            session, user_id="user-1", tenant_id="t-1", raw_token="raw-token"
        )
        is True
    )


@pytest.mark.asyncio
async def test_email_confirm_already_false_for_foreign_token_on_active_channel():
    """An active channel + a foreign token (issued for another address) → NOT a success.

    One must not show “ok” for an arbitrary consumed token: double opt-in does not weaken —
    the hash must match with the domain prefix of the LIVE address.
    """
    token = _token(token_hash=_confirm_hash("raw-token", "attacker@evil.rs"))
    session = _TokenStoreSession(
        channel_rows=[_uc(channel="email", status="active", external_id="u@c.rs")],
        tokens=[token],
    )
    assert (
        await svc.email_confirm_already(
            session, user_id="user-1", tenant_id="t-1", raw_token="raw-token"
        )
        is False
    )


@pytest.mark.asyncio
async def test_email_confirm_already_false_when_channel_not_active():
    """The channel is still pending (not confirmed) → there is no idempotent success."""
    token = _token(token_hash=_confirm_hash("raw-token", "u@c.rs"))
    session = _TokenStoreSession(
        channel_rows=[_uc(channel="email", status="pending", external_id="u@c.rs")],
        tokens=[token],
    )
    assert (
        await svc.email_confirm_already(
            session, user_id="user-1", tenant_id="t-1", raw_token="raw-token"
        )
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "unknown-token"])
async def test_email_confirm_already_false_for_unknown_token(raw):
    """An empty/unknown token on an active channel → we do not give a success."""
    session = _TokenStoreSession(
        channel_rows=[_uc(channel="email", status="active", external_id="u@c.rs")],
        tokens=[_token(token_hash=_confirm_hash("some-other-token", "u@c.rs"))],
    )
    assert (
        await svc.email_confirm_already(
            session, user_id="user-1", tenant_id="t-1", raw_token=raw
        )
        is False
    )


@pytest.mark.asyncio
async def test_resend_uses_address_from_db_not_from_request():
    session = FakeSession(results=[_pending_state("stored@company.rs")])
    sender = MockEmailSender()

    ok = await svc.resend_email_confirmation(
        session,
        user_id="user-1",
        tenant_id="t-1",
        settings=_confirm_settings(),
        email_sender=sender,
    )

    assert ok is True
    assert [m.to for m in sender.outbox] == ["stored@company.rs"]
    # A repeat does not create a new channel row — only a new token.
    assert [o for o in session.added if type(o).__name__ == "UserChannel"] == []


@pytest.mark.asyncio
async def test_resend_refuses_when_nothing_pending():
    """The channel is active (or absent) → there is nothing to confirm, we do not send emails."""
    session = FakeSession(
        results=[FakeResult(items=[_uc(channel="email", external_id="u@c.rs")])]
    )
    sender = MockEmailSender()
    ok = await svc.resend_email_confirmation(
        session,
        user_id="user-1",
        tenant_id="t-1",
        settings=_confirm_settings(),
        email_sender=sender,
    )
    assert ok is False
    assert sender.outbox == []


@pytest.mark.asyncio
async def test_telegram_bind_token_cannot_confirm_email():
    """‼️ A regression on the separation of domains: the purpose is one (CHECK does not allow
    a second one), and without the hash prefix a token from the telegram link — which the cabinet
    shows to the user himself — would confirm any address that was set up."""
    issuing = FakeSession()
    raw_tg = await svc.create_bind_token(
        issuing, user_id="user-1", settings=_confirm_settings()
    )
    tg_token = [o for o in issuing.added if type(o).__name__ == "AuthToken"][0]
    assert tg_token.token_hash == security.hash_token(raw_tg)

    # The same live telegram token lies in the DB; the confirmation looks for it by
    # hash("email-confirm:<address>:" + raw) and must not find it.
    session = _TokenStoreSession(
        channel_rows=[
            _uc(channel="email", status="pending", external_id="victim@company.rs")
        ],
        tokens=[_token(token_hash=tg_token.token_hash)],
    )

    assert (
        await svc.confirm_email(
            session, user_id="user-1", tenant_id="t-1", raw_token=raw_tg
        )
        is False
    )
    assert [sql for sql, _ in session.executed if sql.startswith("UPDATE")] == []


@pytest.mark.asyncio
async def test_pending_email_is_not_reported_as_dead_channel():
    """The expectation of the confirmation is not “the channel dropped off”: the dashboard banner about
    a blocked bot would lie about it."""
    session = FakeSession(results=[_pending_state()])
    assert await svc.dead_channels(session, "t-1") == []


# --------------------------------------------------------------------------- #
# Onboarding of the email channel: the gap “channel setup ↔ live recipient”
# “Setting ≠ fact” (CLAUDE.md): delivery_channels — WHERE to send, states — whether
# there is a live recipient. Their gap = silently lost delivery (case #340).
# --------------------------------------------------------------------------- #


def _state(channel, *, status="active", external_id="x"):
    """ChannelState, as matching.channel_states returns it."""
    return ChannelState(channel=channel, status=status, external_id=external_id)


def _prof(channels, *, is_active=True):
    return SimpleNamespace(delivery_channels=channels, is_active=is_active)


def test_gap_when_channel_requested_but_never_connected():
    """The profile asks for an email, no binding → a quiet “connect” nudge."""
    gaps = svc._delivery_gaps([_prof(["email"])], {})
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["channel"] == "email"
    assert gap["label"] == "Email"
    assert gap["pending"] is False
    assert gap["link"] == "/cabinet/account#kanali"


def test_gap_when_channel_pending_confirmation():
    """The address is set up but awaits confirmation → a “confirm” nudge (pending)."""
    states = {"email": _state("email", status="pending", external_id="u@c.rs")}
    gaps = svc._delivery_gaps([_prof(["email"])], states)
    assert [g["pending"] for g in gaps] == [True]


def test_no_gap_when_channel_live():
    """A live recipient exists → the delivery will arrive, there is nothing to nudge about."""
    states = {"email": _state("email", external_id="u@c.rs")}
    assert svc._delivery_gaps([_prof(["email"])], states) == []


def test_no_gap_when_channel_dead_that_is_dead_channels_job():
    """‼️ Mutation anchor: “dropped off” (active→blocked) is NOT duplicated — the loud dead_channels
    banner already shouts about it. Remove the guard `never or pending` — this channel would leak
    into the quiet nudge AND the banner at once."""
    states = {"email": _state("email", status="blocked", external_id="u@c.rs")}
    assert svc._delivery_gaps([_prof(["email"])], states) == []


def test_no_gap_from_inactive_profile():
    """A disabled profile does not send → there is no gap, even if the channel is not connected."""
    assert svc._delivery_gaps([_prof(["email"], is_active=False)], {}) == []


def test_gap_only_for_missing_channel_when_one_is_live():
    """They ask for telegram+email, telegram is live, no email → the nudge is only about the email."""
    states = {"telegram": _state("telegram", external_id="555")}
    gaps = svc._delivery_gaps([_prof(["telegram", "email"])], states)
    assert [g["channel"] for g in gaps] == ["email"]


@pytest.mark.asyncio
async def test_delivery_channel_status_flags_live_channel_and_gaps():
    """has_live_channel (the onboarding step) and gaps (the nudge) — from ONE SELECT of the states."""
    session = FakeSession(
        results=[FakeResult(items=[_uc(channel="telegram", external_id="555")])]
    )
    status = await svc.delivery_channel_status(session, "t-1", [_prof(["email"])])
    assert status["has_live_channel"] is True  # telegram live
    assert [g["channel"] for g in status["gaps"]] == ["email"]  # email requested, but absent


@pytest.mark.asyncio
async def test_delivery_channel_status_no_live_channel_when_all_dead():
    session = FakeSession(
        results=[FakeResult(items=[_uc(status="blocked", external_id="555")])]
    )
    status = await svc.delivery_channel_status(session, "t-1", [])
    assert status["has_live_channel"] is False
