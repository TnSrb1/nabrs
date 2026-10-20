"""The language of the BACKGROUND notices = the language of the recipient (stage 2c, pack C).

The packs A (#481) and B (#482) taught the delivery card, the digest and the bot to speak in the recipient’s language. Four background workers remained Russian for everyone — not one of them read the ``locale``. Here are the guards that each of them ASKS for the language and that the asked value reaches the text.

‼️ The honesty boundary is the same as in ``test_delivery_channel_locale``: the session here is fake, it does not execute the SQL. Therefore the file proves TWO things checkable in the CI always: (1) the selection asks the DB exactly for ``COALESCE(uc.locale, u.locale)`` — “the language of the channel, otherwise the language of the cabinet” (migration 0038), and not the bare ``u.locale``; (2) the value of the column reaches the chrome of the message. The semantics of the COALESCE itself is given only by a live PostgreSQL — the run is attached to the REPORT/PR.

The main risk of the pack is ``subscription_reminders``: ONE worker, TWO channels, TWO languages (the letter — the language of the cabinet, Telegram — the language of the channel). The divergence is normal and is checked separately, on one row of the selection.

The backward compatibility (“the language is not set anywhere → the texts as now, down to the character”) is checked by comparison with literals, not with the catalog: a comparison with the catalog would pass also on an accidental edit of the ru string.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.api.emailer import MockEmailSender
from src.workers.adapters.base import DeliveryResult
from src.workers.award_watch_notifier import (
    AlertTarget,
    build_alert_card,
    build_alert_email,
    build_alert_subject,
)
from src.workers.matching import LIVE_CHANNELS_SQL, ChannelState
from src.workers.reminders import _CANDIDATES_SQL, ReminderWorker, build_reminder_text
from src.workers.subscription_reminders import (
    _TENANTS_SQL as _SUB_TENANTS_SQL,
)
from src.workers.subscription_reminders import (
    SubscriptionReminderWorker,
    build_email_body,
    build_email_subject,
    build_tg_text,
)
from src.workers.summary import _TENANTS_SQL as _SUMMARY_TENANTS_SQL
from src.workers.summary import SummaryWorker, build_summary_text

NOW = datetime(2026, 6, 14, 8, 0, tzinfo=timezone.utc)  # 10:00 Belgrade
BG_TZ = ZoneInfo("Europe/Belgrade")


def _now() -> datetime:
    return NOW


def _flat(sql) -> str:
    return " ".join(str(sql).split())


# --------------------------------------------------------------------------- #
#  Mocks of the DB and the channels (as in test_summary.py / test_subscription_reminders.py)
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return self._results.pop(0) if self._results else _FakeResult([])

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class _FakeAdapter:
    """An adapter stub; ``**kwargs`` — the extension of the contract does not break the mock."""

    channel = "telegram"

    def __init__(self, **_kwargs):
        self.sent: list[tuple[str, str]] = []

    def supports_buttons(self) -> bool:
        return True

    async def send_text(self, target, text, **_kwargs) -> DeliveryResult:
        self.sent.append((target, text))
        return DeliveryResult(ok=True, response_code=200)


# --------------------------------------------------------------------------- #
#  1. summary — “📊 Daily summary”
# --------------------------------------------------------------------------- #


def test_summary_sql_asks_channel_locale_with_cabinet_fallback():
    assert "COALESCE(uc.locale, u.locale) AS locale" in _flat(_SUMMARY_TENANTS_SQL)


def test_summary_sql_does_not_read_user_locale_alone():
    """The guard of the regression: the bare ``u.locale`` would return the language of the cabinet, not of the channel."""
    assert "u.locale AS locale" not in _flat(_SUMMARY_TENANTS_SQL)


def _summary(locale: str | None) -> str:
    return build_summary_text(
        active=2,
        watching=1,
        submitted=3,
        won=0,
        deadlines=[
            SimpleNamespace(
                title="Nabavka kancelarijskog materijala",
                deadline_at=datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc),
            )
        ],
        locale=locale,
    )


def test_summary_text_sr():
    msg = _summary("sr")
    assert "📊 Dnevni pregled" in msg
    assert "✅ Učestvujemo: 2" in msg
    assert "👀 Pratimo: 1" in msg
    assert "📨 Podneto: 3" in msg
    assert "🏆 Pobede: 0" in msg
    assert "⏳ Najbliži rokovi:" in msg
    assert "Ежедневная сводка" not in msg
    # the data are not translated
    assert "Nabavka kancelarijskog materijala" in msg


def test_summary_text_ru():
    msg = _summary("ru")
    assert "📊 Ежедневная сводка" in msg
    assert "✅ Участвуем: 2" in msg
    assert "Dnevni pregled" not in msg


def test_summary_no_deadlines_branch_is_localized():
    sr = build_summary_text(
        active=1, watching=0, submitted=0, won=0, deadlines=[], locale="sr"
    )
    assert "nema najbližih rokova" in sr
    assert "нет ближайших дедлайнов" not in sr


def test_summary_no_title_fallback_is_localized():
    sr = build_summary_text(
        active=1,
        watching=0,
        submitted=0,
        won=0,
        deadlines=[SimpleNamespace(title=None, deadline_at=None)],
        locale="sr",
    )
    assert "Bez naziva" in sr
    assert "Без названия" not in sr


@pytest.mark.asyncio
async def test_summary_worker_passes_row_locale_to_text():
    """The column ``locale`` of the selection row reaches the sent message."""
    row = SimpleNamespace(tenant_id="ten-1", tz="UTC", target="555", locale="sr")
    session = _FakeSession(
        [
            _FakeResult([row]),
            _FakeResult([SimpleNamespace(active=1, watching=0, submitted=0, won=0)]),
            _FakeResult([]),
            _FakeResult([SimpleNamespace(id="s-1")]),  # claim
        ]
    )
    adapter = _FakeAdapter()
    worker = SummaryWorker(
        adapters={"telegram": adapter},
        session_factory=_session_factory(session),
        now_factory=lambda: datetime(2026, 6, 14, 9, 0, tzinfo=timezone.utc),
    )
    assert await worker.poll_once() == 1
    _, text = adapter.sent[0]
    assert "Dnevni pregled" in text


# --------------------------------------------------------------------------- #
#  2. reminders — “⏰ Reminder: the deadline is TODAY”
# --------------------------------------------------------------------------- #


def test_reminders_sql_asks_channel_locale_with_cabinet_fallback():
    assert "COALESCE(uc.locale, u.locale) AS locale" in _flat(_CANDIDATES_SQL)


def test_reminders_sql_does_not_read_user_locale_alone():
    assert "u.locale AS locale" not in _flat(_CANDIDATES_SQL)


def _reminder(threshold: int, locale: str | None) -> str:
    return build_reminder_text(
        threshold,
        title="Nabavka kancelarijskog materijala",
        deadline_at=datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc),
        source_url="https://jnportal.rs/tender-eo/1",
        locale=locale,
    )


def test_reminder_text_sr():
    msg = _reminder(5, "sr")
    assert "⏰ Podsetnik: do roka 5 dana" in msg
    assert "🗓 Rok do" in msg
    assert "Напоминание" not in msg
    assert "Nabavka kancelarijskog materijala" in msg


def test_reminder_text_sr_singular_day_form():
    """1 day in Serbian — «1 dan», not «1 dana»: the unit is inflected by the number."""
    assert "do roka 1 dan" in _reminder(1, "sr")
    assert "1 dana" not in _reminder(1, "sr")


def test_reminder_text_today_sr():
    assert "⏰ Podsetnik: rok je DANAS" in _reminder(0, "sr")


def test_reminder_text_ru():
    msg = _reminder(5, "ru")
    assert "⏰ Напоминание: до дедлайна 5 дн." in msg
    assert "🗓 Приём до" in msg


def test_reminder_no_title_fallback_is_localized():
    msg = build_reminder_text(
        3, title=None, deadline_at=None, source_url=None, locale="sr"
    )
    assert "<b>Bez naziva</b>" in msg


@pytest.mark.asyncio
async def test_reminders_worker_passes_row_locale_to_text():
    row = SimpleNamespace(
        tenant_id="ten-1",
        tender_id="t-1",
        user_status="active",
        title="Nabavka",
        source_url="https://jnportal.rs/t/1",
        deadline_at=NOW + timedelta(days=5),
        tz="Europe/Belgrade",
        user_channel_id="uc-1",
        target="555",
        locale="sr",
    )
    session = _FakeSession(
        [_FakeResult([row]), _FakeResult([SimpleNamespace(id="r-1")])]
    )
    adapter = _FakeAdapter()
    worker = ReminderWorker(
        adapters={"telegram": adapter},
        session_factory=_session_factory(session),
        now_factory=_now,
    )
    assert await worker.poll_once() == 1
    _, text = adapter.sent[0]
    assert "Podsetnik" in text


# --------------------------------------------------------------------------- #
#  3. subscription_reminders — ONE worker, TWO channels, TWO languages
# --------------------------------------------------------------------------- #


def test_subscription_sql_asks_two_languages():
    """The letter — the language of the cabinet, Telegram — the language of the channel: two DIFFERENT columns."""
    flat = _flat(_SUB_TENANTS_SQL)
    assert "u.locale AS email_locale" in flat
    assert "COALESCE(uc.locale, u.locale) AS tg_locale" in flat


def test_subscription_tg_text_sr():
    msg = build_tg_text(7, NOW + timedelta(days=7), BG_TZ, "sr")
    assert "⏰ Pristup TenderSRB-u ističe za 7 dana" in msg
    assert "Za produženje se obratite na support@nab.rs." in msg
    assert "Доступ" not in msg


def test_subscription_email_sr():
    access = NOW + timedelta(days=7)
    assert build_email_subject(7, "sr") == "TenderSRB — pristup ističe za 7 dana"
    body = build_email_body(7, access, BG_TZ, "sr")
    assert "Pristup TenderSRB-u ističe za 7 dana" in body
    assert "Доступ" not in body


def _sub_tenant(**over) -> SimpleNamespace:
    row = SimpleNamespace(
        tenant_id="ten-1",
        access_until=NOW + timedelta(days=7),
        tz="Europe/Belgrade",
        email="user@test.rs",
        email_status="active",
        email_locale="ru",
        tg_chat_id="555",
        tg_locale="sr",
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row


@pytest.mark.asyncio
async def test_subscription_cabinet_ru_and_channel_sr_diverge():
    """The MAIN risk of the pack: the cabinet ru + the channel sr → the letter ru, Telegram sr.

    One row of the selection, two addressees, two languages. Mixing the axes would give either a Serbian letter to a person who reads the cabinet in Russian, or Russian text in the bot that he specifically switched to Serbian (``/language``).
    """
    session = _FakeSession(
        [_FakeResult([_sub_tenant()]), _FakeResult([SimpleNamespace(id="r-1")])]
    )
    adapter = _FakeAdapter()
    email_sender = MockEmailSender()
    worker = SubscriptionReminderWorker(
        tg_adapter=adapter,
        email_sender=email_sender,
        session_factory=_session_factory(session),
        now_factory=_now,
    )
    assert await worker.poll_once() == 1

    _, tg_text = adapter.sent[0]
    assert "Pristup TenderSRB-u ističe" in tg_text
    assert "Доступ к TenderSRB" not in tg_text

    letter = email_sender.outbox[0]
    assert "доступ заканчивается" in letter.subject
    assert "Доступ к TenderSRB заканчивается" in letter.text_body
    assert "Pristup" not in letter.text_body


@pytest.mark.asyncio
async def test_subscription_cabinet_sr_and_channel_ru_diverge_too():
    """The reverse divergence — so that the test does not pass on “always everything by the cabinet”."""
    session = _FakeSession(
        [
            _FakeResult([_sub_tenant(email_locale="sr", tg_locale="ru")]),
            _FakeResult([SimpleNamespace(id="r-1")]),
        ]
    )
    adapter = _FakeAdapter()
    email_sender = MockEmailSender()
    worker = SubscriptionReminderWorker(
        tg_adapter=adapter,
        email_sender=email_sender,
        session_factory=_session_factory(session),
        now_factory=_now,
    )
    assert await worker.poll_once() == 1
    assert "Доступ к TenderSRB" in adapter.sent[0][1]
    assert "Pristup TenderSRB-u" in email_sender.outbox[0].text_body


# --------------------------------------------------------------------------- #
#  4. award_watch_notifier — the language from the COMMON predicate of the live channels
# --------------------------------------------------------------------------- #


def test_live_channels_sql_carries_locale():
    assert "COALESCE(uc.locale, u.locale) AS locale" in _flat(LIVE_CHANNELS_SQL)


def test_live_channels_sql_still_ignores_deleted_at():
    """The predicate “where the delivery will reach” was not changed by the task — only the language.

    The absence of the filter by ``users.deleted_at`` is deliberate (see ``award_watch_notifier._check_tenant``): it is needed by ALL three selections at once, or by none.
    """
    assert "deleted_at" not in _flat(LIVE_CHANNELS_SQL)


@pytest.mark.asyncio
async def test_channel_states_maps_locale_column_to_state():
    """The column ``locale`` of the selection reaches :class:`ChannelState`.

    A guard of the mapping itself: the contract of the SQL is checked by the test above, and here — that the read value is not lost on the way to the state of the channel.
    """
    from src.workers.matching import channel_states

    session = _FakeSession(
        [
            _FakeResult(
                [
                    SimpleNamespace(
                        channel="telegram",
                        status="active",
                        external_id="555",
                        locale="sr",
                    )
                ]
            )
        ]
    )
    states = await channel_states(session, "ten-1")
    assert states["telegram"].locale == "sr"


@pytest.mark.asyncio
async def test_channel_states_tolerates_row_without_locale():
    """A row without the column (the fakes of the old consumers' tests) → locale=None."""
    from src.workers.matching import channel_states

    session = _FakeSession(
        [
            _FakeResult(
                [
                    SimpleNamespace(
                        channel="email", status="active", external_id="a@b.rs"
                    )
                ]
            )
        ]
    )
    states = await channel_states(session, "ten-1")
    assert states["email"].locale is None
    assert states["email"].is_live is True


def test_channel_state_locale_is_optional_for_other_consumers():
    """The contract of the second consumer is not broken: the field with a default, is_live is as before.

    ``src/api/cabinet/channel_service.py`` and ``channel_dead_notify`` build a ``ChannelState`` without a language — it has no relation to the “liveness”.
    """
    state = ChannelState(channel="telegram", status="active", external_id="42")
    assert state.locale is None
    assert state.is_live is True
    assert (
        ChannelState(
            channel="telegram", status="active", external_id="42", locale="sr"
        ).is_live
        is True
    )
    assert (
        ChannelState(
            channel="telegram", status="disabled", external_id="42", locale="sr"
        ).is_live
        is False
    )


def _award_row() -> SimpleNamespace:
    return SimpleNamespace(
        sub_id=1,
        tenant_id="ten-1",
        winner_name="DOO Gradnja",
        award_id=10,
        tender_id="11111111-1111-1111-1111-111111111111",
        winner_pib="100200300",
        decision_date=date(2026, 6, 20),
        award_amount_eur=Decimal("50000"),
        customer_name="Ministarstvo",
        title="Nabavka opreme",
    )


def test_award_card_sr():
    text, markup = build_alert_card(_award_row(), "sr")
    assert "— nova pobeda" in text
    assert "🏛 Naručilac: Ministarstvo" in text
    assert "новая победа" not in text
    assert markup.inline_keyboard[0][0].text == "🏛 Ko pobeđuje?"
    # callback_data does not depend on the language
    assert markup.inline_keyboard[0][0].callback_data.startswith("an:cust:")


def test_award_card_ru():
    text, markup = build_alert_card(_award_row(), "ru")
    assert "— новая победа" in text
    assert "🏛 Заказчик: Ministarstvo" in text
    assert markup.inline_keyboard[0][0].text == "🏛 Кто выигрывает?"


def test_award_email_sr():
    letter = build_alert_email(
        _award_row(), cabinet_base_url="https://nab.rs", locale="sr"
    )
    assert letter.subject == "Nova pobeda: DOO Gradnja (PIB 100200300)"
    assert "Zdravo!" in letter.text_body
    assert "Kompanija koju pratite pobedila je na tenderu:" in letter.text_body
    # ALL the labels of the facts, not the first one: the letter is assembled as a list, and the invisible
    # would remain exactly the line that the test did not name.
    assert "Naručilac: Ministarstvo" in letter.text_body
    assert "Tender: Nabavka opreme" in letter.text_body
    assert "Iznos: 50 000 EUR" in letter.text_body
    assert "Datum odluke: 20.06.2026" in letter.text_body
    assert "Otvori karticu tendera" in letter.text_body
    assert "Ko još pobeđuje kod ovog naručioca" in letter.text_body
    for ru in ("Заказчик", "Тендер", "Сумма", "Дата решения", "Здравствуйте"):
        assert ru not in letter.text_body
    assert "Zdravo!" in letter.html_body
    assert "Datum odluke" in letter.html_body


def test_award_subject_sr_truncation_keeps_length_guard():
    row = _award_row()
    row.winner_name = "A" * 300
    assert len(build_alert_subject(row, "sr")) <= 120


@pytest.mark.asyncio
async def test_award_send_all_uses_per_channel_locale():
    """Telegram in sr, the letter in ru — each channel has ITS OWN language."""

    class _Bot:
        def __init__(self):
            self.calls: list[dict] = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)

    from src.workers.award_watch_notifier import AwardWatchNotifier

    bot = _Bot()
    email_sender = MockEmailSender()
    worker = AwardWatchNotifier(
        bot=bot, email_sender=email_sender, cabinet_base_url="https://nab.rs"
    )
    delivered = await worker._send_all(
        _award_row(),
        {
            "telegram": AlertTarget(address="555", locale="sr"),
            "email": AlertTarget(address="user@test.rs", locale="ru"),
        },
    )
    assert delivered == ["telegram", "email"]
    assert "nova pobeda" in bot.calls[0]["text"]
    assert "Новая победа" in email_sender.outbox[0].subject


@pytest.mark.asyncio
async def test_award_check_tenant_carries_locale_from_states(monkeypatch):
    """The language of the addressee comes from the COMMON selection of the live channels, not from its own SQL."""
    from src.workers import award_watch_notifier as awn

    class _TierResult:
        """The row of _LOAD_TENANT_SQL: Pro and not frozen — the gate is passed.

        ‼️ ``access_until`` is counted from the REAL “now”, not from the fixture NOW: the freeze gate in the worker reads ``datetime.now()``, and a term of the form “NOW + 30 days” would expire as soon as the calendar overtakes the fixture (the same debt that #431 fixed by freezing the clocks in test_award_watch_notifier).
        """

        def first(self):
            return SimpleNamespace(
                tier="pro",
                status="active",
                access_until=datetime.now(timezone.utc) + timedelta(days=30),
            )

    class _TierSession(_FakeSession):
        async def execute(self, statement, params=None):
            self.calls.append((str(statement), params))
            return _TierResult()

    async def fake_states(_session, _tenant_id):
        return {
            "telegram": ChannelState(
                channel="telegram", status="active", external_id="555", locale="sr"
            ),
            "email": ChannelState(
                channel="email",
                status="active",
                external_id="user@test.rs",
                locale="ru",
            ),
        }

    monkeypatch.setattr(awn, "channel_states", fake_states)
    session = _TierSession([])
    worker = awn.AwardWatchNotifier(session_factory=_session_factory(session))
    ok, targets = await worker._check_tenant("ten-1")
    assert ok is True
    assert targets["telegram"] == AlertTarget(address="555", locale="sr")
    assert targets["email"] == AlertTarget(address="user@test.rs", locale="ru")


# --------------------------------------------------------------------------- #
#  5. The backward compatibility: the language is not set anywhere → the texts as they were, down to the character
# --------------------------------------------------------------------------- #


def test_backward_compat_summary_text_unchanged():
    msg = build_summary_text(
        active=2,
        watching=1,
        submitted=3,
        won=0,
        deadlines=[SimpleNamespace(title=None, deadline_at=None)],
    )
    assert msg == (
        "📊 Ежедневная сводка\n"
        "✅ Участвуем: 2   👀 Изучаем: 1   📨 Подано: 3   🏆 Выиграно: 0\n"
        "\n"
        "⏳ Ближайшие дедлайны:\n"
        "• <b>Без названия</b> — 🗓 —"
    )


def test_backward_compat_summary_no_deadlines_unchanged():
    msg = build_summary_text(active=1, watching=0, submitted=0, won=0, deadlines=[])
    assert msg.endswith("⏳ Ближайшие дедлайны:\nнет ближайших дедлайнов")


def test_backward_compat_reminder_text_unchanged():
    msg = build_reminder_text(
        5,
        title=None,
        deadline_at=datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc),
        source_url="https://jnportal.rs/t/1",
    )
    assert msg == (
        "⏰ Напоминание: до дедлайна 5 дн.\n"
        "<b>Без названия</b>\n"
        "🗓 Приём до 20.06.2026 12:00\n"
        "🔗 https://jnportal.rs/t/1"
    )


def test_backward_compat_reminder_today_unchanged():
    msg = build_reminder_text(0, title="Тендер", deadline_at=None, source_url=None)
    assert msg == "⏰ Напоминание: дедлайн СЕГОДНЯ\n<b>Тендер</b>"


def test_backward_compat_subscription_texts_unchanged():
    access = datetime(2026, 6, 21, 8, 0, tzinfo=timezone.utc)
    assert build_tg_text(7, access, BG_TZ) == (
        "⏰ Доступ к TenderSRB заканчивается через 7 дн. (21.06.2026).\n"
        "Для продления обратитесь к support@nab.rs."
    )
    assert build_email_subject(7) == "TenderSRB — доступ заканчивается через 7 дн."
    assert build_email_body(1, access, BG_TZ) == (
        "Доступ к TenderSRB заканчивается через 1 дн. (21.06.2026).\n"
        "Для продления обратитесь к support@nab.rs."
    )


def test_backward_compat_award_card_unchanged():
    text, markup = build_alert_card(_award_row())
    assert text == (
        "🔔 <b>DOO Gradnja</b> (PIB 100200300) — новая победа\n"
        "🏛 Заказчик: Ministarstvo\n"
        "📋 Nabavka opreme\n"
        "💶 50 000 EUR\n"
        "🗓 20.06.2026"
    )
    assert markup.inline_keyboard[0][0].text == "🏛 Кто выигрывает?"


def test_backward_compat_award_email_unchanged():
    letter = build_alert_email(_award_row(), cabinet_base_url="https://nab.rs")
    assert letter.subject == "Новая победа: DOO Gradnja (PIB 100200300)"
    assert letter.text_body == (
        "Здравствуйте!\n"
        "\n"
        "Компания, за которой вы следите, выиграла тендер: "
        "DOO Gradnja (PIB 100200300) — новая победа\n"
        "\n"
        "Заказчик: Ministarstvo\n"
        "Тендер: Nabavka opreme\n"
        "Сумма: 50 000 EUR\n"
        "Дата решения: 20.06.2026\n"
        "\n"
        "Открыть карточку тендера: "
        "https://nab.rs/tender/11111111-1111-1111-1111-111111111111\n"
        "\n"
        "Кто ещё выигрывает у этого заказчика — на карточке.\n"
    )
    assert "<p>Здравствуйте!</p>" in letter.html_body
    assert (
        "<p>Компания, за которой вы следите, выиграла тендер:</p>" in letter.html_body
    )


def _reminder_with_source(locale: str | None, label: str | None) -> str:
    return build_reminder_text(
        3,
        title="Nabavka",
        deadline_at=None,
        source_url=None,
        locale=locale,
        source_label=label,
    )


def test_reminder_has_source_line_in_three_languages():
    label = "Portal javnih nabavki (UJN)"
    assert f"🏛 Platforma: {label}" in _reminder_with_source("sr", label)
    assert f"🏛 Площадка: {label}" in _reminder_with_source("ru", label)
    assert f"🏛 Platform: {label}" in _reminder_with_source("en", label)
    # an explicit fallback of an unknown locale — ru
    assert f"🏛 Площадка: {label}" in _reminder_with_source(None, label)


def test_reminder_without_source_label_has_no_source_line():
    assert "🏛" not in _reminder_with_source("sr", None)
    assert "🏛" not in _reminder_with_source("sr", "")


def test_reminder_source_label_is_html_escaped():
    assert "A &amp; B &lt;x&gt;" in _reminder_with_source("sr", "A & B <x>")


def test_reminders_candidates_sql_selects_source_label_via_left_join():
    sql = _flat(_CANDIDATES_SQL)
    assert "NULLIF(s.display_name, '') AS source_label" in sql
    assert "LEFT JOIN sources s ON s.source_key = t.source_key" in sql
