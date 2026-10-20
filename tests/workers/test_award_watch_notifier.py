"""Tests of VG-06 E3b: AwardWatchNotifier + award_watch_service.

Everything is mocked — without a real PostgreSQL/Telegram.

Coverage:
  - the models/migration import, the UNIQUE constraints are present
  - AwardWatchNotifier.poll_once: a new award → send+sent; dedup (0 rows) → 0;
    the frozen/not Pro+/no channel → do not send; the cache of the tenant per tenant_id
  - the e-mail channel: a letter to the subscriber (MockEmailSender), the TG regression, two channels →
    one record in sent, fail-closed without a transport, pending/an empty address are not
    live, no PII of the address in the logs, the header injection into the Subject
  - award_watch_service: run_watch_winner gate (no_account/frozen/gate_pro/no_tender/no_data/success)
  - award_watch_service: run_list_watching (empty / with subscriptions)
  - award_watch_service: run_unwatch (not_found / success)
  - the callback audit an:wch/an:unw/an:cust ≤64 bytes
"""

from __future__ import annotations

import logging
import smtplib
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.emailer import MockEmailSender
from src.db.models.award_watch import AwardWatchSubscription, AwardWatchSent
from src.workers.award_watch_notifier import (
    AwardWatchNotifier,
    _is_pro_plus,
    _is_tenant_frozen,
    build_alert_card,
    build_alert_email,
    build_alert_subject,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
TENANT_ID = "dddd4444-dddd-dddd-dddd-dddddddddddd"
TENDER_UUID = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WINNER_PIB = "123456789"
WINNER_NAME = "ООО Победитель"
CHAT_ID_STR = "123456789"
EMAIL_ADDR = "subscriber@example.com"


# ---------------------------------------------------------------------------
# The determinism of the clocks
# ---------------------------------------------------------------------------
# ‼️ Both award_watch_service and award_watch_notifier read “now” from
# datetime.now(timezone.utc) at the frozen gate. The fixtures build
# access_until = NOW ± days, therefore WITHOUT freezing the clocks the tests “expire” by the
# calendar: as soon as the real date overtakes NOW+30d, any gate returns
# 'frozen' and breaks the assertions (the class “the test depends on the launch time”, CLAUDE.md).
# We freeze the MODULE clocks of both modules at the fixture NOW — the comparison is again
# deterministic relative to NOW and does not depend on the date of the run. The production path does not
# change: only the source of the time under the test is frozen.
#
# The exception is test_is_tenant_frozen: it checks the predicate deliberately on the
# REAL clock (datetime.now), therefore the clock is NOT frozen for it.


class _FrozenDatetime(datetime):
    """datetime, whose ``.now()`` always returns the fixture NOW."""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def _freeze_award_watch_clock(request, monkeypatch):
    if request.node.name == "test_is_tenant_frozen":
        return  # deliberately on the real clock — the freezing is not applied
    monkeypatch.setattr("src.bots.award_watch_service.datetime", _FrozenDatetime)
    monkeypatch.setattr("src.workers.award_watch_notifier.datetime", _FrozenDatetime)


# ---------------------------------------------------------------------------
# Auxiliary mocks
# ---------------------------------------------------------------------------


class _FakeResult:
    """A minimal mock of the result of execute() — only .first() and the iteration."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """The AsyncSession mock: a queue of results, each execute() call → pop(0)."""

    def __init__(self, results=()):
        self._results = list(results)
        self.calls: list[tuple] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return self._results.pop(0) if self._results else _FakeResult([])

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


def _session_seq_mock(rows: list) -> MagicMock:
    """A mock session for the service tests: execute → .first() = rows[i]."""
    idx = {"n": 0}
    session = MagicMock()

    async def _exec(sql, params=None):
        i = idx["n"]
        idx["n"] += 1
        r = MagicMock()
        r.first.return_value = rows[i] if i < len(rows) else None
        return r

    session.execute = _exec
    return session


class _FakeBot:
    def __init__(self, raises=None):
        self.calls: list[dict] = []
        self._raises = raises

    async def send_message(self, **kwargs):
        if self._raises:
            raise self._raises
        self.calls.append(kwargs)


def _pro_tenant(frozen=False):
    delta = -1 if frozen else 30
    return SimpleNamespace(tier="pro", access_until=NOW + timedelta(days=delta))


def _free_tenant():
    return SimpleNamespace(tier="free", access_until=NOW + timedelta(days=30))


def _channel_row(channel="telegram", status="active", external_id=CHAT_ID_STR):
    """The string LIVE_CHANNELS_SQL — it is read by matching.channel_states."""
    return SimpleNamespace(channel=channel, status=status, external_id=external_id)


def _tg_channel():
    return _channel_row()


def _email_channel(address=EMAIL_ADDR):
    return _channel_row(channel="email", external_id=address)


def _award_row(sub_id=1, award_id=10, tenant_id=TENANT_ID):
    return SimpleNamespace(
        sub_id=sub_id,
        tenant_id=tenant_id,
        winner_name=WINNER_NAME,
        award_id=award_id,
        tender_id=TENDER_UUID,
        winner_pib=WINNER_PIB,
        decision_date=date(2026, 6, 20),
        award_amount_eur=Decimal("50000"),
        customer_name="Министерство",
        title="Закупка оборудования",
    )


# ---------------------------------------------------------------------------
# 1. The models — import and UNIQUE constraints
# ---------------------------------------------------------------------------


def test_models_import():
    assert AwardWatchSubscription.__tablename__ == "award_watch_subscriptions"
    assert AwardWatchSent.__tablename__ == "award_watch_sent"


def test_models_have_unique_constraints():
    names_sub = {c.name for c in AwardWatchSubscription.__table_args__}
    assert "award_watch_subscriptions_tenant_pib_uniq" in names_sub

    names_sent = {c.name for c in AwardWatchSent.__table_args__}
    assert "award_watch_sent_sub_award_uniq" in names_sent


# ---------------------------------------------------------------------------
# 2. The auxiliary functions of the notifier
# ---------------------------------------------------------------------------


def test_is_pro_plus_positive():
    for tier in ("pro", "business", "enterprise"):
        assert _is_pro_plus(tier) is True


def test_is_pro_plus_negative():
    for tier in ("free", None, ""):
        assert _is_pro_plus(tier) is False


def test_is_tenant_frozen():
    now = datetime.now(timezone.utc)
    assert _is_tenant_frozen(now - timedelta(days=1)) is True
    assert _is_tenant_frozen(now + timedelta(days=1)) is False
    assert _is_tenant_frozen(None) is False


def test_build_alert_card_fields():
    row = _award_row()
    text, markup = build_alert_card(row)
    assert WINNER_NAME in text
    assert WINNER_PIB in text
    assert "Министерство" in text
    assert "50 000 EUR" in text
    assert "20.06.2026" in text
    assert markup is not None
    btn = markup.inline_keyboard[0][0]
    assert btn.callback_data == f"an:cust:{TENDER_UUID}"


def test_build_alert_card_no_eur_no_line():
    row = SimpleNamespace(
        sub_id=1,
        tenant_id=TENANT_ID,
        winner_name=WINNER_NAME,
        award_id=1,
        tender_id=TENDER_UUID,
        winner_pib=WINNER_PIB,
        decision_date=None,
        award_amount_eur=None,
        customer_name="",
        title="Тест",
    )
    text, _ = build_alert_card(row)
    assert "EUR" not in text


# ---------------------------------------------------------------------------
# 3. poll_once — no rows → 0 sends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_once_no_rows():
    session = _FakeSession([_FakeResult([])])
    notifier = AwardWatchNotifier(
        bot=_FakeBot(), session_factory=_session_factory(session)
    )
    assert await notifier.poll_once() == 0


# ---------------------------------------------------------------------------
# 4. poll_once — a new award → 1 send + a record in sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_once_new_award_sends_and_records():
    bot = _FakeBot()

    # _load_new_pairs: 1 session, 1 execute
    s_pairs = _FakeSession([_FakeResult([_award_row()])])
    # _check_tenant: 1 session, 2 execute (tier + channel)
    s_check = _FakeSession([_FakeResult([_pro_tenant()]), _FakeResult([_tg_channel()])])
    # _record_sent: 1 session, 1 execute
    s_sent = _FakeSession([_FakeResult([SimpleNamespace(id=99)])])

    call_order = [s_pairs, s_check, s_sent]
    idx = 0

    @asynccontextmanager
    async def multi_factory():
        nonlocal idx
        yield call_order[idx]
        idx += 1

    notifier = AwardWatchNotifier(bot=bot, session_factory=multi_factory)
    sent = await notifier.poll_once()

    assert sent == 1
    assert len(bot.calls) == 1
    assert "Победитель" in bot.calls[0]["text"]
    assert bot.calls[0]["parse_mode"] == "HTML"
    assert s_sent.commits == 1


# ---------------------------------------------------------------------------
# 5. poll_once — dedup: a repeated run (0 rows) → 0 sends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_once_dedup_no_rows():
    bot = _FakeBot()
    session = _FakeSession([_FakeResult([])])
    notifier = AwardWatchNotifier(bot=bot, session_factory=_session_factory(session))
    assert await notifier.poll_once() == 0
    assert bot.calls == []


# ---------------------------------------------------------------------------
# 6. poll_once — a frozen tenant → do not send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_once_frozen_tenant_no_send():
    bot = _FakeBot()

    s_pairs = _FakeSession([_FakeResult([_award_row()])])
    s_check = _FakeSession([_FakeResult([_pro_tenant(frozen=True)]), _FakeResult([])])

    idx = 0
    sessions = [s_pairs, s_check]

    @asynccontextmanager
    async def multi_factory():
        nonlocal idx
        yield sessions[idx]
        idx += 1

    notifier = AwardWatchNotifier(bot=bot, session_factory=multi_factory)
    assert await notifier.poll_once() == 0
    assert bot.calls == []


# ---------------------------------------------------------------------------
# 7. poll_once — a non-Pro+ tenant → do not send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_once_non_pro_no_send():
    bot = _FakeBot()

    s_pairs = _FakeSession([_FakeResult([_award_row()])])
    s_check = _FakeSession([_FakeResult([_free_tenant()]), _FakeResult([])])

    idx = 0
    sessions = [s_pairs, s_check]

    @asynccontextmanager
    async def multi_factory():
        nonlocal idx
        yield sessions[idx]
        idx += 1

    notifier = AwardWatchNotifier(bot=bot, session_factory=multi_factory)
    assert await notifier.poll_once() == 0
    assert bot.calls == []


# ---------------------------------------------------------------------------
# 8. poll_once — no TG channel → do not send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_once_no_channel_no_send():
    bot = _FakeBot()

    s_pairs = _FakeSession([_FakeResult([_award_row()])])
    s_check = _FakeSession(
        [_FakeResult([_pro_tenant()]), _FakeResult([])]
    )  # no channel

    idx = 0
    sessions = [s_pairs, s_check]

    @asynccontextmanager
    async def multi_factory():
        nonlocal idx
        yield sessions[idx]
        idx += 1

    notifier = AwardWatchNotifier(bot=bot, session_factory=multi_factory)
    assert await notifier.poll_once() == 0
    assert bot.calls == []


# ---------------------------------------------------------------------------
# 9. poll_once — the tenant cache (2 awards of one tenant → 1 gate check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_once_tenant_cache():
    """Two awards of one tenant → _check_tenant is called once (the cache)."""
    bot = _FakeBot()

    rows = [_award_row(sub_id=1, award_id=10), _award_row(sub_id=1, award_id=11)]
    s_pairs = _FakeSession([_FakeResult(rows)])
    # _check_tenant once — 2 execute
    s_check = _FakeSession([_FakeResult([_pro_tenant()]), _FakeResult([_tg_channel()])])
    s_sent1 = _FakeSession([_FakeResult([SimpleNamespace(id=1)])])
    s_sent2 = _FakeSession([_FakeResult([SimpleNamespace(id=2)])])

    idx = 0
    sessions = [s_pairs, s_check, s_sent1, s_sent2]

    @asynccontextmanager
    async def multi_factory():
        nonlocal idx
        s = sessions[idx]
        idx += 1
        yield s

    notifier = AwardWatchNotifier(bot=bot, session_factory=multi_factory)
    sent = await notifier.poll_once()

    assert sent == 2
    assert len(bot.calls) == 2
    # We make sure that s_check was used only once
    assert idx == 4  # pairs + check + sent1 + sent2


# ---------------------------------------------------------------------------
# 9b. The e-mail channel: the alert letter, the TG regression, idempotency, fail-closed
# ---------------------------------------------------------------------------


def _sessions_factory(sessions):
    """A factory that gives out the prepared sessions in the order of the calls."""
    idx = {"n": 0}

    @asynccontextmanager
    async def factory():
        s = sessions[idx["n"]]
        idx["n"] += 1
        yield s

    return factory


def _check_sessions(*channels):
    """The session of _check_tenant: a Pro+ tenant + the rows of user_channels."""
    return _FakeSession([_FakeResult([_pro_tenant()]), _FakeResult(list(channels))])


def _record_session():
    return _FakeSession([_FakeResult([SimpleNamespace(id=1)])])


@pytest.mark.asyncio
async def test_poll_once_email_subscriber_gets_alert():
    """A Lite subscriber (a live channel of email only) gets the alert by letter."""
    bot = _FakeBot()
    email = MockEmailSender()
    s_sent = _record_session()

    notifier = AwardWatchNotifier(
        bot=bot,
        email_sender=email,
        session_factory=_sessions_factory(
            [
                _FakeSession([_FakeResult([_award_row()])]),
                _check_sessions(_email_channel()),
                s_sent,
            ]
        ),
        cabinet_base_url="https://nab.rs",
    )

    assert await notifier.poll_once() == 1
    assert bot.calls == []  # There is no TG channel — we do not go to Telegram
    assert len(email.outbox) == 1
    letter = email.outbox[0]
    assert letter.to == EMAIL_ADDR
    assert WINNER_NAME in letter.text_body
    assert "Министерство" in letter.text_body
    assert f"https://nab.rs/tender/{TENDER_UUID}" in letter.text_body
    assert s_sent.commits == 1  # the pair is written into award_watch_sent


@pytest.mark.asyncio
async def test_poll_once_both_channels_one_record():
    """Two live channels → two messages, but ONE record in sent and sent==1."""
    bot = _FakeBot()
    email = MockEmailSender()
    s_sent = _record_session()

    notifier = AwardWatchNotifier(
        bot=bot,
        email_sender=email,
        session_factory=_sessions_factory(
            [
                _FakeSession([_FakeResult([_award_row()])]),
                _check_sessions(_tg_channel(), _email_channel()),
                s_sent,
            ]
        ),
    )

    assert await notifier.poll_once() == 1
    assert len(bot.calls) == 1
    assert len(email.outbox) == 1
    assert s_sent.commits == 1


@pytest.mark.asyncio
async def test_poll_once_email_no_transport_fail_closed():
    """There is no transport → we do NOT report success and do NOT write to sent (the alert is not lost)."""
    bot = _FakeBot()
    sessions = [
        _FakeSession([_FakeResult([_award_row()])]),
        _check_sessions(_email_channel()),
    ]

    notifier = AwardWatchNotifier(
        bot=bot,
        email_sender=None,  # the mail env is not configured
        session_factory=_sessions_factory(sessions),
    )

    assert await notifier.poll_once() == 0
    assert bot.calls == []
    # The session of the record in sent was not even queried — the pair will return to the selection.
    assert all(s.commits == 0 for s in sessions)


@pytest.mark.asyncio
async def test_poll_once_email_send_failure_not_recorded():
    """A send failure → the pair is not marked as sent."""

    class _BoomSender(MockEmailSender):
        async def send(self, **kwargs):
            raise smtplib.SMTPRecipientsRefused({EMAIL_ADDR: (550, b"nope")})

    notifier = AwardWatchNotifier(
        bot=_FakeBot(),
        email_sender=_BoomSender(),
        session_factory=_sessions_factory(
            [
                _FakeSession([_FakeResult([_award_row()])]),
                _check_sessions(_email_channel()),
            ]
        ),
    )

    assert await notifier.poll_once() == 0


@pytest.mark.asyncio
async def test_poll_once_email_failure_does_not_log_address(caplog):
    """PII: the subscriber’s address does not get into the log even through the text of an SMTP error."""

    class _BoomSender(MockEmailSender):
        async def send(self, **kwargs):
            raise smtplib.SMTPRecipientsRefused({EMAIL_ADDR: (550, b"nope")})

    notifier = AwardWatchNotifier(
        bot=_FakeBot(),
        email_sender=_BoomSender(),
        session_factory=_sessions_factory(
            [
                _FakeSession([_FakeResult([_award_row()])]),
                _check_sessions(_email_channel()),
            ]
        ),
    )

    with caplog.at_level(logging.ERROR):
        await notifier.poll_once()

    assert caplog.records, "the failure must be logged"
    for record in caplog.records:
        assert EMAIL_ADDR not in record.getMessage()
        assert record.exc_info is None  # the traceback prints the address in full


@pytest.mark.asyncio
async def test_poll_once_pending_email_not_live():
    """An unconfirmed (pending) address is not counted as live — there is no letter."""
    email = MockEmailSender()

    notifier = AwardWatchNotifier(
        bot=_FakeBot(),
        email_sender=email,
        session_factory=_sessions_factory(
            [
                _FakeSession([_FakeResult([_award_row()])]),
                _check_sessions(_channel_row(channel="email", status="pending")),
            ]
        ),
    )

    assert await notifier.poll_once() == 0
    assert email.outbox == []


@pytest.mark.asyncio
async def test_poll_once_email_blank_external_id_not_live():
    """An empty external_id → the channel is not live (the mirror of the delivery predicate)."""
    email = MockEmailSender()

    notifier = AwardWatchNotifier(
        bot=_FakeBot(),
        email_sender=email,
        session_factory=_sessions_factory(
            [
                _FakeSession([_FakeResult([_award_row()])]),
                _check_sessions(_channel_row(channel="email", external_id="   ")),
            ]
        ),
    )

    assert await notifier.poll_once() == 0
    assert email.outbox == []


def test_build_alert_email_fields():
    letter = build_alert_email(_award_row(), cabinet_base_url="https://nab.rs/")
    assert WINNER_PIB in letter.subject
    assert WINNER_NAME in letter.subject
    assert "50 000 EUR" in letter.text_body
    assert "20.06.2026" in letter.text_body
    assert f"https://nab.rs/tender/{TENDER_UUID}" in letter.html_body


def test_build_alert_subject_no_header_injection():
    """A line break in the name from an external source = a header injection of the letter."""
    row = _award_row()
    row.winner_name = "ООО\r\nBcc: victim@example.com"
    subject = build_alert_subject(row)
    assert "\n" not in subject and "\r" not in subject


def test_build_alert_subject_truncated():
    row = _award_row()
    row.winner_name = "Я" * 300
    assert len(build_alert_subject(row)) <= 120


# ---------------------------------------------------------------------------
# 10. award_watch_service: run_watch_winner — no_account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_watch_winner_no_account():
    from src.bots.award_watch_service import run_watch_winner

    session = MagicMock()
    with patch(
        "src.bots.award_watch_service.resolve_tenant_id",
        new=AsyncMock(return_value=None),
    ):
        result = await run_watch_winner(session, tender_id=TENDER_UUID, chat_id=999)
    assert result.status == "no_account"


# ---------------------------------------------------------------------------
# 11. award_watch_service: run_watch_winner — frozen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_watch_winner_frozen():
    from src.bots.award_watch_service import run_watch_winner

    session = _session_seq_mock(
        [
            SimpleNamespace(tier="pro", access_until=NOW - timedelta(days=1)),
        ]
    )
    with patch(
        "src.bots.award_watch_service.resolve_tenant_id",
        new=AsyncMock(return_value=TENANT_ID),
    ):
        result = await run_watch_winner(session, tender_id=TENDER_UUID, chat_id=999)
    assert result.status == "frozen"


# ---------------------------------------------------------------------------
# 12. award_watch_service: run_watch_winner — gate_pro
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_watch_winner_gate_pro():
    from src.bots.award_watch_service import run_watch_winner

    session = _session_seq_mock(
        [
            SimpleNamespace(tier="free", access_until=NOW + timedelta(days=30)),
        ]
    )
    with patch(
        "src.bots.award_watch_service.resolve_tenant_id",
        new=AsyncMock(return_value=TENANT_ID),
    ):
        result = await run_watch_winner(session, tender_id=TENDER_UUID, chat_id=999)
    assert result.status == "gate_pro"


# ---------------------------------------------------------------------------
# 13. award_watch_service: run_watch_winner — no_tender
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_watch_winner_no_tender():
    from src.bots.award_watch_service import run_watch_winner

    session = _session_seq_mock(
        [
            SimpleNamespace(tier="pro", access_until=NOW + timedelta(days=30)),
            None,  # LOAD_TENDER_CUSTOMER_SQL → empty
        ]
    )
    with patch(
        "src.bots.award_watch_service.resolve_tenant_id",
        new=AsyncMock(return_value=TENANT_ID),
    ):
        result = await run_watch_winner(session, tender_id=TENDER_UUID, chat_id=999)
    assert result.status == "no_tender"


# ---------------------------------------------------------------------------
# 14. award_watch_service: run_watch_winner — no_data (there is no top winner with a PIB)
# ---------------------------------------------------------------------------

from src.analytics.awards_analytics import CustomerStats, WinnerRow  # noqa: E402
from decimal import Decimal as D  # noqa: E402

_EMPTY_STATS = CustomerStats(
    customer_pib="111",
    total_awards=0,
    distinct_winners=0,
    with_amount=0,
    total_rsd=D("0"),
    total_eur=D("0"),
    top_winners=[],
    months=None,
)

_STATS_NO_PIB = CustomerStats(
    customer_pib="111",
    total_awards=5,
    distinct_winners=1,
    with_amount=0,
    total_rsd=D("0"),
    total_eur=D("0"),
    top_winners=[WinnerRow(name="Без PIB", pib="", wins=5, sum_rsd=D("0"))],
    months=None,
)


@pytest.mark.asyncio
async def test_run_watch_winner_no_data_empty():
    from src.bots.award_watch_service import run_watch_winner

    session = _session_seq_mock(
        [
            SimpleNamespace(tier="pro", access_until=NOW + timedelta(days=30)),
            SimpleNamespace(customer_tax_id="111", customer_name="Заказчик", title="T"),
        ]
    )
    with (
        patch(
            "src.bots.award_watch_service.resolve_tenant_id",
            new=AsyncMock(return_value=TENANT_ID),
        ),
        patch(
            "src.bots.award_watch_service.customer_award_stats",
            new=AsyncMock(return_value=_EMPTY_STATS),
        ),
    ):
        result = await run_watch_winner(session, tender_id=TENDER_UUID, chat_id=999)
    assert result.status == "no_data"


@pytest.mark.asyncio
async def test_run_watch_winner_no_data_no_pib():
    """The top winner exists, but the PIB is empty → no_data."""
    from src.bots.award_watch_service import run_watch_winner

    session = _session_seq_mock(
        [
            SimpleNamespace(tier="pro", access_until=NOW + timedelta(days=30)),
            SimpleNamespace(customer_tax_id="111", customer_name="Заказчик", title="T"),
        ]
    )
    with (
        patch(
            "src.bots.award_watch_service.resolve_tenant_id",
            new=AsyncMock(return_value=TENANT_ID),
        ),
        patch(
            "src.bots.award_watch_service.customer_award_stats",
            new=AsyncMock(return_value=_STATS_NO_PIB),
        ),
    ):
        result = await run_watch_winner(session, tender_id=TENDER_UUID, chat_id=999)
    assert result.status == "no_data"


# ---------------------------------------------------------------------------
# 15. award_watch_service: run_watch_winner — success + the watermark (backfill)
# ---------------------------------------------------------------------------

from src.analytics.awards_analytics import WinnerRow as WR  # noqa: E402

_SUCCESS_STATS = CustomerStats(
    customer_pib="111",
    total_awards=3,
    distinct_winners=1,
    with_amount=0,
    total_rsd=D("0"),
    total_eur=D("0"),
    top_winners=[WR(name=WINNER_NAME, pib=WINNER_PIB, wins=3, sum_rsd=D("0"))],
    months=None,
)


@pytest.mark.asyncio
async def test_run_watch_winner_success_and_watermark():
    from src.bots.award_watch_service import run_watch_winner

    # All the execute calls in one sequence:
    # 0 → tier/access_until, 1 → tender customer, 2 → UPSERT sub, 3 → BACKFILL sent
    _responses = [
        SimpleNamespace(tier="pro", access_until=NOW + timedelta(days=30)),
        SimpleNamespace(customer_tax_id="111", customer_name="Заказчик", title="T"),
        SimpleNamespace(id=42),  # UPSERT RETURNING
        None,  # BACKFILL DO NOTHING (first = None)
    ]
    execute_sqls: list[str] = []
    idx = {"n": 0}

    def _make_result(val):
        r = MagicMock()
        r.first.return_value = val
        return r

    async def _exec(sql, params=None):
        execute_sqls.append(str(sql))
        i = idx["n"]
        idx["n"] += 1
        return _make_result(_responses[i] if i < len(_responses) else None)

    session = MagicMock()
    session.execute = _exec
    session.commit = AsyncMock()

    with (
        patch(
            "src.bots.award_watch_service.resolve_tenant_id",
            new=AsyncMock(return_value=TENANT_ID),
        ),
        patch(
            "src.bots.award_watch_service.customer_award_stats",
            new=AsyncMock(return_value=_SUCCESS_STATS),
        ),
    ):
        result = await run_watch_winner(session, tender_id=TENDER_UUID, chat_id=999)

    assert result.status == "success"
    assert result.winner_pib == WINNER_PIB
    assert result.winner_name == WINNER_NAME
    # commit was called twice: after the UPSERT and after the BACKFILL
    assert session.commit.call_count == 2
    # Backfill SQL: INSERT INTO award_watch_sent ... SELECT
    backfill_sqls = [
        s for s in execute_sqls if "award_watch_sent" in s and "SELECT" in s.upper()
    ]
    assert backfill_sqls, "The backfill watermark SQL was not called"


# ---------------------------------------------------------------------------
# 16. award_watch_service: run_list_watching — empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_list_watching_empty():
    from src.bots.award_watch_service import run_list_watching

    session = _session_seq_mock([None])  # LIST_WATCHING_SQL → will be iterated
    # list_watching uses list(await session.execute(...)), an iterable result is needed
    execute_r = MagicMock()
    execute_r.__iter__ = MagicMock(return_value=iter([]))

    async def _exec(sql, params=None):
        return execute_r

    session.execute = _exec

    with patch(
        "src.bots.award_watch_service.resolve_tenant_id",
        new=AsyncMock(return_value=TENANT_ID),
    ):
        result = await run_list_watching(session, chat_id=999)

    assert result.status == "success"
    assert result.items == []


# ---------------------------------------------------------------------------
# 17. award_watch_service: run_list_watching — several subscriptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_list_watching_with_items():
    from src.bots.award_watch_service import run_list_watching

    subs = [
        SimpleNamespace(id=1, winner_name="ООО Победитель", winner_pib="111"),
        SimpleNamespace(id=2, winner_name="ИП Второй", winner_pib="222"),
    ]
    execute_r = MagicMock()
    execute_r.__iter__ = MagicMock(return_value=iter(subs))

    async def _exec(sql, params=None):
        return execute_r

    session = MagicMock()
    session.execute = _exec

    with patch(
        "src.bots.award_watch_service.resolve_tenant_id",
        new=AsyncMock(return_value=TENANT_ID),
    ):
        result = await run_list_watching(session, chat_id=999)

    assert result.status == "success"
    assert len(result.items) == 2
    assert result.items[0].sub_id == 1
    assert result.items[1].winner_pib == "222"


# ---------------------------------------------------------------------------
# 18. award_watch_service: run_unwatch — one cannot unsubscribe a foreign subscription
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_unwatch_wrong_tenant():
    from src.bots.award_watch_service import run_unwatch

    update_r = MagicMock()
    update_r.__iter__ = MagicMock(return_value=iter([]))  # 0 rows → not_found

    async def _exec(sql, params=None):
        return update_r

    session = MagicMock()
    session.execute = _exec
    session.commit = AsyncMock()

    with patch(
        "src.bots.award_watch_service.resolve_tenant_id",
        new=AsyncMock(return_value=TENANT_ID),
    ):
        result = await run_unwatch(session, sub_id=9999, chat_id=999)

    assert result.status == "not_found"


# ---------------------------------------------------------------------------
# 19. award_watch_service: run_unwatch — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_unwatch_success():
    from src.bots.award_watch_service import run_unwatch

    update_r = MagicMock()
    update_r.__iter__ = MagicMock(return_value=iter([SimpleNamespace(id=5)]))

    async def _exec(sql, params=None):
        return update_r

    session = MagicMock()
    session.execute = _exec
    session.commit = AsyncMock()

    with patch(
        "src.bots.award_watch_service.resolve_tenant_id",
        new=AsyncMock(return_value=TENANT_ID),
    ):
        result = await run_unwatch(session, sub_id=5, chat_id=999)

    assert result.status == "success"
    session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# 20. award_watch_service: run_list_watching — no_account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_list_watching_no_account():
    from src.bots.award_watch_service import run_list_watching

    session = MagicMock()
    with patch(
        "src.bots.award_watch_service.resolve_tenant_id",
        new=AsyncMock(return_value=None),
    ):
        result = await run_list_watching(session, chat_id=999)
    assert result.status == "no_account"


# ---------------------------------------------------------------------------
# 21. The callback audit ≤64 bytes
# ---------------------------------------------------------------------------


def test_callback_an_wch_within_64():
    tender_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"  # 36 characters
    cb = f"an:wch:{tender_uuid}"
    size = len(cb.encode())
    assert size <= 64, f"an:wch is too long: {size}b"


def test_callback_an_unw_within_64():
    cb = f"an:unw:{10 ** 18}"  # BIGINT maximum ~19 digits
    size = len(cb.encode())
    assert size <= 64, f"an:unw is too long: {size}b"


def test_callback_an_cust_in_card_within_64():
    tender_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    cb = f"an:cust:{tender_uuid}"
    size = len(cb.encode())
    assert size <= 64, f"an:cust is too long: {size}b"
