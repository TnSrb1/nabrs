"""The owner’s decision of 30.09.2026: the letters of the workers — in the language of users.locale, the default sr.

The coverage: channel_dead_notify and billing_notifications (earlier — only ru). The assertions — literals, not through email_t().
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.api.emailer import MockEmailSender
from src.workers import billing_notifications as bn
from src.workers import channel_dead_notify as cdn

pytestmark = pytest.mark.sr_default

UNTIL = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
TZ = ZoneInfo("Europe/Belgrade")
URL = "https://app.nab.rs/x"


def test_dead_channel_subject_and_body_three_languages():
    assert cdn.build_email_subject("telegram") == (
        "TenderSRB — tenderi ne stižu: Telegram je isključen"
    )
    assert cdn.build_email_subject("telegram", "ru") == (
        "TenderSRB — тендеры не приходят: Telegram отключён"
    )
    assert cdn.build_email_subject("telegram", "en") == (
        "TenderSRB — tenders are not arriving: Telegram disconnected"
    )
    sr = cdn.build_email_body("telegram", reconnect_url=URL)
    assert sr.startswith("Zdravo!") and URL in sr
    assert cdn.build_email_body("telegram", reconnect_url=URL, locale="ru").startswith(
        "Здравствуйте!"
    )
    assert cdn.build_email_body("telegram", reconnect_url=URL, locale="en").startswith(
        "Hello!"
    )


@pytest.mark.parametrize(
    "kind,subject_sr",
    [
        ("activated", "TenderSRB — uplata je prošla, pretplata je aktivna"),
        ("past_due", "TenderSRB — plaćanje nije prošlo"),
        ("manual_rejected", "TenderSRB — doneta je odluka po zahtevu"),
        ("unknown", "TenderSRB — status pretplate"),
    ],
)
def test_billing_subject_default_is_serbian(kind, subject_sr):
    assert bn.build_email_subject(kind) == subject_sr


def test_billing_body_serbian_has_date_and_no_cyrillic():
    body = bn.build_email_body(
        "past_due", access_until=UNTIL, tz=TZ, cabinet_url=URL, locale=None
    )
    assert "pristup se čuva do 15.08.2026" in body
    assert "Vaš lični kabinet: " + URL in body
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body)


def test_billing_body_ru_and_en_keep_their_language():
    ru = bn.build_email_body(
        "canceled", access_until=UNTIL, tz=TZ, cabinet_url=URL, locale="ru"
    )
    assert (
        "Подписка на TenderSRB отменена. Доступ к сервису сохраняется до 15.08.2026"
        in ru
    )
    en = bn.build_email_body(
        "manual_approved",
        access_until=UNTIL,
        tz=TZ,
        cabinet_url=URL,
        plan="pro",
        decision_comment="ok",
        locale="en",
    )
    assert "the Pro subscription is active — access is open until 15.08.2026" in en
    assert "Comment: ok" in en


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _BillingSession:
    def __init__(self, locale):
        self._locale = locale
        self.sql: list[str] = []

    async def execute(self, statement, params=None):
        s = str(statement)
        self.sql.append(s)
        if "app_billing_pending_notifications" in s:
            return _Result(
                [
                    SimpleNamespace(
                        id=1,
                        tenant_id="t-1",
                        kind="renewed",
                        email="a@b.rs",
                        email_status="ok",
                        access_until=UNTIL,
                        tz="Europe/Belgrade",
                    )
                ]
            )
        if "app_billing_mark_notification_sent" in s:
            return _Result([(1,)])
        if "FROM users" in s:
            return _Result([(self._locale,)] if self._locale else [])
        return _Result([])

    async def commit(self):
        pass


def _factory(session):
    @asynccontextmanager
    async def f():
        yield session

    return f


@pytest.mark.parametrize(
    "locale,subject",
    [
        (None, "TenderSRB — pretplata je produžena"),
        ("sr", "TenderSRB — pretplata je produžena"),
        ("ru", "TenderSRB — подписка продлена"),
        ("en", "TenderSRB — subscription renewed"),
    ],
)
async def test_billing_worker_uses_users_locale(locale, subject):
    session = _BillingSession(locale)
    sender = MockEmailSender()
    worker = bn.BillingNotificationsWorker(
        email_sender=sender, session_factory=_factory(session)
    )
    assert await worker.poll_once() == 1
    assert sender.outbox[0].subject == subject
    assert any("FROM users" in s for s in session.sql)


class _DeadSession:
    def __init__(self, tenants, states):
        self._queue = [_Result(tenants), _Result(states), _Result([(1,)])]

    async def execute(self, statement, params=None):
        return self._queue.pop(0) if self._queue else _Result([])

    async def commit(self):
        pass


async def test_dead_channel_worker_passes_row_locale():
    tenant = SimpleNamespace(
        tenant_id="t-1",
        email="a@b.rs",
        email_status="active",
        locale="en",
        configured_channels=["telegram"],
    )
    dead = SimpleNamespace(channel="telegram", status="blocked", external_id="1")
    session = _DeadSession([tenant], [dead])
    sender = MockEmailSender()
    worker = cdn.ChannelDeadNotifyWorker(
        email_sender=sender, session_factory=_factory(session)
    )
    assert await worker.poll_once() == 1
    assert sender.outbox[0].subject == (
        "TenderSRB — tenders are not arriving: Telegram disconnected"
    )
