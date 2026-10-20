"""Unit tests of ChannelDeadNotifyWorker.

The DB and the e-mail sender are mocked (as in test_subscription_reminders.py): we check the predicate “dropped off” (incl. the difference from “never connected”), the idempotency of claim-then-send through an open incident, a repeated letter after the recovery, the privacy of the logs and the assembly of the texts — without a real PostgreSQL/network.
"""

from __future__ import annotations

import logging
import re
import smtplib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.api.emailer import MockEmailSender
from src.workers.channel_dead_notify import (
    RECONNECT_PATH,
    ChannelDeadNotifyWorker,
    _TENANTS_SQL,
    build_email_body,
    build_email_subject,
    build_reconnect_url,
)

TENANT = "11111111-1111-1111-1111-111111111111"
EMAIL = "user@test.rs"


# --------------------------------------------------------------------------- #
#  Mocks of the DB
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """Returns the prepared results in the order of execute()."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return self._results.pop(0) if self._results else _FakeResult([])

    async def commit(self):
        self.commits += 1


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


def _tenant_row(*, channels=("telegram",), email=EMAIL, email_status="active"):
    """The row of the tenant (as from _TENANTS_SQL)."""
    return SimpleNamespace(
        tenant_id=TENANT,
        email=email,
        email_status=email_status,
        configured_channels=list(channels),
    )


def _channel_row(*, channel="telegram", status="active", external_id="555"):
    """The row of user_channels (as from LIVE_CHANNELS_SQL)."""
    return SimpleNamespace(channel=channel, status=status, external_id=external_id)


def _worker(session, email_sender=None, **kw) -> ChannelDeadNotifyWorker:
    return ChannelDeadNotifyWorker(
        email_sender=email_sender or MockEmailSender(),
        session_factory=_session_factory(session),
        **kw,
    )


# --------------------------------------------------------------------------- #
#  Assembly of the texts
# --------------------------------------------------------------------------- #


def test_reconnect_url_points_at_cabinet_page():
    assert (
        build_reconnect_url("https://app.nab.rs")
        == f"https://app.nab.rs{RECONNECT_PATH}"
    )


def test_reconnect_url_tolerates_trailing_slash():
    """A double slash breaks the link — the user lands on a 404 instead of the reconnection."""
    assert (
        build_reconnect_url("https://app.nab.rs/")
        == f"https://app.nab.rs{RECONNECT_PATH}"
    )


def test_email_body_contains_working_reconnect_link():
    url = build_reconnect_url("https://app.nab.rs")
    body = build_email_body("telegram", reconnect_url=url)
    assert url in body
    assert "Telegram" in body


def test_email_tone_is_care_not_error():
    """The tone of the letter is care, not an “error”: the user broke nothing."""
    body = build_email_body("telegram", reconnect_url="https://nab.rs/x")
    lowered = body.lower()
    assert "ошибка" not in lowered
    assert "сбой" not in lowered
    assert "в порядке" in lowered


def test_email_subject_names_the_channel():
    assert "Telegram" in build_email_subject("telegram")


# --------------------------------------------------------------------------- #
#  The predicate “dropped off”
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,external_id",
    [
        ("blocked", "555"),  # the user blocked the bot (delivery.py on 403)
        ("revoked", "555"),  # unlinking / manual normalization (case 7986f3b0)
        ("active", ""),  # no recipient
        ("active", None),
    ],
)
async def test_dead_channel_sends_exactly_one_email(status, external_id):
    mailer = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([_tenant_row()]),
            _FakeResult([_channel_row(status=status, external_id=external_id)]),
            _FakeResult([SimpleNamespace(id="incident-1")]),  # the claim passed
        ]
    )
    sent = await _worker(session, mailer).poll_once()

    assert sent == 1
    assert len(mailer.outbox) == 1
    assert mailer.outbox[0].to == EMAIL


@pytest.mark.asyncio
async def test_live_channel_sends_nothing():
    mailer = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([_tenant_row()]),
            _FakeResult([_channel_row(status="active", external_id="555")]),
            _FakeResult([]),  # _resolve: there are no open incidents
        ]
    )
    sent = await _worker(session, mailer).poll_once()

    assert sent == 0
    assert mailer.outbox == []


@pytest.mark.asyncio
async def test_never_connected_channel_sends_nothing():
    """A channel is absent from user_channels altogether — that is onboarding, not a drop-off."""
    mailer = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([_tenant_row()]),
            _FakeResult([]),  # not a single row of user_channels
        ]
    )
    sent = await _worker(session, mailer).poll_once()

    assert sent == 0
    assert mailer.outbox == []
    # the claim was not even tried — no INSERT into the journal sounded
    assert not any("channel_dead_notice_log" in sql for sql, _ in session.calls)


@pytest.mark.asyncio
async def test_channel_not_configured_in_profile_is_ignored():
    """The profile does not send to the e-mail channel → a dead e-mail channel is not our concern."""
    mailer = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([_tenant_row(channels=["telegram"])]),
            _FakeResult(
                [
                    _channel_row(status="active", external_id="555"),
                    _channel_row(channel="webhook", status="revoked"),
                ]
            ),
            _FakeResult([]),  # _resolve by a live telegram
        ]
    )
    sent = await _worker(session, mailer).poll_once()

    assert sent == 0
    assert mailer.outbox == []


# --------------------------------------------------------------------------- #
#  Idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_second_run_while_still_dead_sends_no_second_email():
    """The incident is already open → the claim does not pass → we do not spam the user."""
    mailer = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([_tenant_row()]),
            _FakeResult([_channel_row(status="revoked")]),
            _FakeResult([]),  # claim: ON CONFLICT DO NOTHING → empty
        ]
    )
    sent = await _worker(session, mailer).poll_once()

    assert sent == 0
    assert mailer.outbox == []


@pytest.mark.asyncio
async def test_restored_channel_resolves_open_incident():
    """The channel is live again → the incident is closed (otherwise the next drop-off would be silent)."""
    session = _FakeSession(
        [
            _FakeResult([_tenant_row()]),
            _FakeResult([_channel_row(status="active", external_id="555")]),
            _FakeResult([SimpleNamespace(id="x")]),  # the UPDATE closed the incident
        ]
    )
    await _worker(session).poll_once()

    update_calls = [
        sql for sql, _ in session.calls if "UPDATE channel_dead_notice_log" in sql
    ]
    assert len(update_calls) == 1
    assert "resolved_at = now()" in update_calls[0]
    assert "resolved_at IS NULL" in update_calls[0]


@pytest.mark.asyncio
async def test_died_again_after_restore_sends_new_email():
    """Restored → dropped off again → the incident is new → the letter goes out again."""
    mailer = MockEmailSender()

    # Cycle 1: live → we close the incident, there is no letter.
    session1 = _FakeSession(
        [
            _FakeResult([_tenant_row()]),
            _FakeResult([_channel_row(status="active", external_id="555")]),
            _FakeResult([SimpleNamespace(id="old-incident")]),
        ]
    )
    assert await _worker(session1, mailer).poll_once() == 0

    # Cycle 2: dead again → there are no open ones → the claim passes → the letter.
    session2 = _FakeSession(
        [
            _FakeResult([_tenant_row()]),
            _FakeResult([_channel_row(status="blocked")]),
            _FakeResult([SimpleNamespace(id="new-incident")]),
        ]
    )
    assert await _worker(session2, mailer).poll_once() == 1
    assert len(mailer.outbox) == 1


@pytest.mark.asyncio
async def test_claim_targets_partial_unique_index():
    """ON CONFLICT must repeat the predicate of the partial index 0031."""
    session = _FakeSession(
        [
            _FakeResult([_tenant_row()]),
            _FakeResult([_channel_row(status="revoked")]),
            _FakeResult([SimpleNamespace(id="i")]),
        ]
    )
    await _worker(session).poll_once()

    claim = next(
        sql for sql, _ in session.calls if "INSERT INTO channel_dead_notice_log" in sql
    )
    assert (
        "ON CONFLICT (tenant_id, channel) WHERE resolved_at IS NULL DO NOTHING" in claim
    )
    assert "RETURNING id" in claim


@pytest.mark.asyncio
async def test_email_failure_does_not_roll_back_claim():
    """The journal is already recorded: a rollback → a repeated claim → spam on every cycle."""

    class _BoomSender(MockEmailSender):
        async def send(self, **kw):
            raise RuntimeError("smtp down")

    session = _FakeSession(
        [
            _FakeResult([_tenant_row()]),
            _FakeResult([_channel_row(status="revoked")]),
            _FakeResult([SimpleNamespace(id="i")]),
        ]
    )
    sent = await _worker(session, _BoomSender()).poll_once()

    assert sent == 0  # the letter did not go out...
    assert session.commits == 1  # ...but the claim is committed and not rolled back


# --------------------------------------------------------------------------- #
#  Other
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "email,status", [("user@test.rs", "bounced"), (None, "active")]
)
async def test_no_valid_email_does_not_claim(email, status):
    """There is nowhere to send → the incident is NOT opened.

    A claim without the ability to send would turn off all the next cycles (the incident is open ⇒ ON CONFLICT), and the user would not get the letter even after the address is fixed. We do not claim → the address is fixed → the letter will go out.
    """
    mailer = MockEmailSender()
    session = _FakeSession(
        [
            _FakeResult([_tenant_row(email=email, email_status=status)]),
            _FakeResult([_channel_row(status="revoked")]),
        ]
    )
    assert await _worker(session, mailer).poll_once() == 0
    assert mailer.outbox == []
    assert not any(
        "INSERT INTO channel_dead_notice_log" in sql for sql, _ in session.calls
    )


@pytest.mark.asyncio
async def test_email_never_leaks_into_logs(caplog):
    """The subscriber’s address — PII: only tenant_id/channel in the logs."""
    session = _FakeSession(
        [
            _FakeResult([_tenant_row()]),
            _FakeResult([_channel_row(status="revoked")]),
            _FakeResult([SimpleNamespace(id="i")]),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="src.workers.channel_dead_notify"):
        await _worker(session).poll_once()

    assert caplog.records, "the worker must log the fact of the send"
    assert EMAIL not in caplog.text


@pytest.mark.asyncio
async def test_smtp_refusal_does_not_leak_email_into_logs(caplog):
    """‼️ SMTP puts the address INTO THE EXCEPTION ITSELF — the traceback would leak the PII.

    `SMTPRecipientsRefused` carries the recipient in its args, and `logger.exception` prints the traceback in full. This is not hypothetical: the drop-off of the channel is sent exactly to the accounts with expired addresses, and `make_email_sender` chooses SMTP FIRST (emailer.py) — on production this is the main path.
    """

    class _RefusingSender(MockEmailSender):
        async def send(self, **kw):
            raise smtplib.SMTPRecipientsRefused({EMAIL: (550, b"User unknown")})

    session = _FakeSession(
        [
            _FakeResult([_tenant_row()]),
            _FakeResult([_channel_row(status="revoked")]),
            _FakeResult([SimpleNamespace(id="i")]),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="src.workers.channel_dead_notify"):
        assert await _worker(session, _RefusingSender()).poll_once() == 0

    assert EMAIL not in caplog.text  # neither in the message, nor in the traceback
    assert "SMTPRecipientsRefused" in caplog.text  # but the diagnostics remain


def test_tenants_sql_returns_one_row_per_tenant():
    """A tenant with several users must not give several rows.

    The incident lies on (tenant, channel): without DISTINCT ON the letter would go to a random user of the tenant (whichever the DB gives first), not to the owner.
    """
    sql = re.sub(r"\s+", " ", str(_TENANTS_SQL))
    assert "DISTINCT ON (ten.id)" in sql
    # A deterministic choice: first the suitable addresses, then the oldest user.
    assert "u.email_status IS DISTINCT FROM 'bounced') DESC" in sql
    assert "u.created_at, u.id" in sql


def test_tenants_sql_prefers_a_mailable_user():
    """A broken address of the owner must not silence the whole tenant.

    _can_email skips the tenant WITHOUT a claim; if the SQL always returned the owner, a tenant with a broken owner address would be silent forever, and its users with working mail would never learn that the delivery had stopped.
    """
    sql = re.sub(r"\s+", " ", str(_TENANTS_SQL))
    order_by = sql.split("ORDER BY", 1)[1]
    # the branch “the address is valid” stands BEFORE the sort by the user’s age
    assert order_by.index("bounced") < order_by.index("u.created_at")


def test_tenants_sql_filters_by_profile_activity():
    """The same conditions of activity as in matching STAGE_A_SQL."""
    sql = re.sub(r"\s+", " ", str(_TENANTS_SQL))
    assert "p.is_active = true" in sql
    assert "ten.status IN ('trial', 'active')" in sql
    assert "ten.access_until IS NULL OR ten.access_until > now()" in sql
    assert "u.deleted_at IS NULL" in sql


@pytest.mark.asyncio
async def test_tenant_without_configured_channels_is_skipped():
    mailer = MockEmailSender()
    session = _FakeSession([_FakeResult([_tenant_row(channels=[])])])
    assert await _worker(session, mailer).poll_once() == 0
    assert mailer.outbox == []
