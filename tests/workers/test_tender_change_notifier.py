"""TenderChangeNotifier: language of EACH channel, freeze, limit, sent write, fail-closed.

Without DB and Telegram (mocks). The SQL predicates (day, "delivered before the change") are not checked on mocks —
they are checked by tests/db/test_0058_tender_changes.py on a real PG. Text expectations are literals.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api.emailer import MockEmailSender
from src.workers import tender_change_notifier as tcn
from src.workers.matching import ChannelState

TENDER = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TENANT = "dddd4444-dddd-dddd-dddd-dddddddddddd"
FUTURE = datetime.now(timezone.utc) + timedelta(days=30)
PAST = datetime.now(timezone.utc) - timedelta(days=1)

_DIFF = {
    "deadline": {
        "old": "2026-10-20T09:00:00+00:00",
        "new": "2026-10-27T09:00:00+00:00",
    },
    "files": {"added": [], "changed": ["TZ.docx"], "removed": []},
}


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakeDb:
    """Session: answers by a keyword of the query; records the calls."""

    def __init__(self, *, pending, diffs=None, access_until=FUTURE, sent_last_day=0):
        self.pending = pending
        self.diffs = [
            SimpleNamespace(diff=d) for d in (diffs if diffs is not None else [_DIFF])
        ]
        self.access_until = access_until
        self.sent_last_day = sent_last_day
        self.recorded: list[dict] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM tender_changes c" in sql:
            return _Res(self.pending)
        if "SELECT diff FROM tender_changes" in sql:
            return _Res(self.diffs)
        if "FROM tenants" in sql:
            return _Res([SimpleNamespace(access_until=self.access_until)])
        if "count(*)" in sql:
            return _Res([SimpleNamespace(n=self.sent_last_day)])
        if "INSERT INTO tender_change_sent" in sql:
            self.recorded.append(params)
            return _Res([])
        raise AssertionError(sql)

    async def commit(self):
        pass

    def factory(self):
        @asynccontextmanager
        async def _cm():
            yield self

        return _cm


def _row(prev_id=None):
    return SimpleNamespace(
        tender_id=TENDER,
        tenant_id=TENANT,
        last_id=7,
        first_sent_at=PAST,
        title="Контролна испитивања",
        prev_id=prev_id,
    )


def _states(monkeypatch, **channels):
    states = {
        ch: ChannelState(
            channel=ch,
            status="active",
            external_id=spec[0],
            locale=spec[1],
            # the third element — users.locale; by default it matches the channel language
            user_locale=spec[2] if len(spec) > 2 else spec[1],
        )
        for ch, spec in channels.items()
    }
    monkeypatch.setattr(tcn, "channel_states", AsyncMock(return_value=states))


def _notifier(db, *, bot=None, email=None, cap=30):
    return tcn.TenderChangeNotifier(
        bot=bot,
        email_sender=email,
        session_factory=db.factory(),
        cabinet_base_url="https://nab.rs",
        daily_cap=cap,
    )


@pytest.mark.asyncio
async def test_each_channel_gets_its_own_language(monkeypatch):
    """Telegram reads sr, email — ru: each channel has ITS OWN language (i18n.md)."""
    db = _FakeDb(pending=[_row()])
    _states(monkeypatch, telegram=("123456789", "sr"), email=("a@example.com", "ru"))
    bot = AsyncMock()
    mail = MockEmailSender()
    sent = await _notifier(db, bot=bot, email=mail).poll_once()

    assert sent == 1
    tg = bot.send_message.await_args.kwargs["text"]
    assert tg == (
        "🔔 <b>Tender je izmenjen</b>\n"
        '📋 <a href="https://nab.rs/tender/aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa">'
        "Контролна испитивања</a>\n"
        "• Rok za podnošenje ponuda: 20.10.2026 11:00 → 27.10.2026 10:00\n"
        "• Izmenjeni fajlovi: TZ.docx\n"
        "• Prethodna AI analiza je zastarela — zatražite novu na kartici."
    )
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.subject == "Тендер изменён: Контролна испитивања"
    assert "Срок подачи: 20.10.2026 11:00 → 27.10.2026 10:00" in msg.text_body
    assert "Изменены файлы: TZ.docx" in msg.text_body
    assert db.recorded == [{"tender_id": TENDER, "tenant_id": TENANT, "last_id": 7}]


@pytest.mark.asyncio
async def test_email_uses_user_locale_not_channel_override(monkeypatch):
    """Email — users.locale, even if the channel has its own override."""
    db = _FakeDb(pending=[_row()])
    _states(monkeypatch, email=("a@example.com", "sr", "ru"))
    mail = MockEmailSender()
    assert await _notifier(db, email=mail).poll_once() == 1
    assert mail.outbox[0].subject == "Тендер изменён: Контролна испитивања"


@pytest.mark.asyncio
async def test_english_telegram_title(monkeypatch):
    db = _FakeDb(pending=[_row()])
    _states(monkeypatch, telegram=("123456789", "en"))
    bot = AsyncMock()
    await _notifier(db, bot=bot).poll_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert text.startswith("🔔 <b>Tender updated</b>")
    assert "• Bid deadline: 20.10.2026 11:00 → 27.10.2026 10:00" in text


@pytest.mark.asyncio
async def test_frozen_tenant_gets_nothing_and_is_not_recorded(monkeypatch):
    db = _FakeDb(pending=[_row()], access_until=PAST)
    _states(monkeypatch, telegram=("123456789", "ru"))
    bot = AsyncMock()
    assert await _notifier(db, bot=bot).poll_once() == 0
    bot.send_message.assert_not_awaited()
    assert db.recorded == []  # will arrive after renewal


@pytest.mark.asyncio
async def test_daily_cap_defers_without_recording(monkeypatch):
    db = _FakeDb(pending=[_row()], sent_last_day=2)
    _states(monkeypatch, telegram=("123456789", "ru"))
    bot = AsyncMock()
    assert await _notifier(db, bot=bot, cap=2).poll_once() == 0
    bot.send_message.assert_not_awaited()
    assert db.recorded == []


@pytest.mark.asyncio
async def test_email_without_transport_is_fail_closed(monkeypatch):
    db = _FakeDb(pending=[_row()])
    _states(monkeypatch, email=("a@example.com", "ru"))
    assert await _notifier(db, email=None).poll_once() == 0
    assert (
        db.recorded == []
    )  # no silent loss: it will go out when the mail is configured


@pytest.mark.asyncio
async def test_dead_channel_is_skipped(monkeypatch):
    db = _FakeDb(pending=[_row()])
    monkeypatch.setattr(
        tcn,
        "channel_states",
        AsyncMock(
            return_value={
                "telegram": ChannelState("telegram", "disabled", "123456789", "ru")
            }
        ),
    )
    bot = AsyncMock()
    assert await _notifier(db, bot=bot).poll_once() == 0
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_collapsed_change_is_marked_without_message(monkeypatch):
    # the deadline was changed and restored: "was → became" is empty — we do not write to the subscriber, but we do not loop forever
    flip = [
        {"deadline": {"old": "A", "new": "B"}},
        {"deadline": {"old": "B", "new": "A"}},
    ]
    db = _FakeDb(pending=[_row()], diffs=flip)
    _states(monkeypatch, telegram=("123456789", "ru"))
    bot = AsyncMock()
    assert await _notifier(db, bot=bot).poll_once() == 0
    bot.send_message.assert_not_awaited()
    assert len(db.recorded) == 1


def test_subject_is_single_line_and_capped():
    msg = tcn.build_email(
        _DIFF,
        title="Строка1\r\nBcc: x@example.com " + "я" * 300,
        tender_id=TENDER,
        cabinet_base_url="https://nab.rs",
        locale="en",
    )
    assert "\n" not in msg.subject and "\r" not in msg.subject
    assert len(msg.subject) <= 120
    assert msg.subject.startswith("Tender updated: Строка1 Bcc: x@example.com")


def test_pii_not_logged_on_telegram_failure(monkeypatch, caplog):
    import asyncio

    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("chat 123456789 blocked")
    n = tcn.TenderChangeNotifier(bot=bot, session_factory=lambda: None, daily_cap=0)
    with caplog.at_level("ERROR"):
        assert asyncio.run(n._send_telegram("123456789", "x")) is False
    assert "123456789" not in caplog.text
    assert "RuntimeError" in caplog.text
