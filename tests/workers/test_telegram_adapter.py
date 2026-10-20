"""Unit tests of TelegramAdapter (the rich card v6, NEXT №1).

We check the rendering of the card (the priority stars, the number of the tender, the region, the type/procedure, the CPV, the amount ORIG+EUR, the publication date, the planned period, the submission deadline, the source, the link, the buttons of the funnel) and the mapping of the aiogram errors to DeliveryResult (403 → blocked, 429 → retryable, chat not found → blocked, 5xx → retryable). The Bot is mocked, the network is not touched. The button “Open tender” is removed (Chat 45) — we check that it is absent, and that the buttons of the funnel are in place.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup

from src.workers.adapters.base import DeliveryJob
from src.workers.adapters.telegram import TelegramAdapter


class _DummyMethod:
    """A stub of TelegramMethod for the constructors of the aiogram exceptions."""


class _FakeBot:
    """A mock of aiogram.Bot: records send_message or raises the given exception."""

    def __init__(self, raise_exc: Exception | None = None):
        self._raise = raise_exc
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return object()


def _job(**over) -> DeliveryJob:
    base = dict(
        outbox_id=1,
        tender_id="abc-123",
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc-1",
        target="555",
        channel_status="active",
        title="Видеонаблюдение & монтаж",
        source_url="https://jnportal.rs/tender/1?x=1",
        amount_eur=1234567.0,
        deadline_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc),
        # the rich card v6
        source_uid="374138",
        source_key="jnportal",
        amount_original=144809500.0,
        currency_original="RSD",
        is_amount_estimated=True,
        published_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        procedure_type="open",
        regions=["Београдски округ"],
        contract_type=None,
        classifications=None,
    )
    base.update(over)
    return DeliveryJob(**base)


def _buttons(markup) -> list:
    """A flat list of all the inline buttons of the markup."""
    return [b for row in markup.inline_keyboard for b in row]


# --------------------------------------------------------------------------- #
#  format — the rich card
# --------------------------------------------------------------------------- #


def test_format_full_card_has_all_rows_stars_and_funnel_buttons():
    adapter = TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs")
    msg = adapter.format(_job())
    text = msg.text
    # the title (escaped) + 4 stars (the sum ≥1M EUR)
    assert "⭐⭐⭐⭐ <b>Видеонаблюдение &amp; монтаж</b>" in text
    # the number of the tender, the region, the amount ORIG+EUR (estimated), the dates, the source, the link
    assert "🔢 № 374138" in text
    assert "📍 Регион: Београдски округ" in text
    assert "💰 ~144 809 500 RSD (~1 234 567 EUR) (оценочно)" in text
    assert "📅 Опубликован: 20.05.2026" in text
    # 14:30Z of June (CEST +2) → 16:30 Belgrade time (the boundary of the presentation).
    assert "🗓 Приём до: 01.06.2026 16:30" in text
    assert "🏛 Площадка: jnportal" in text
    assert "jnportal.rs/tender/1" in text
    # contract_type is absent → the procedure as a temporary substitute
    assert "📦 Процедура: Открытая процедура" in text
    # The buttons of the funnel are in place; the old deep-link “Open tender” (/tender/<id>) is absent.
    assert isinstance(msg.reply_markup, InlineKeyboardMarkup)
    buttons = _buttons(msg.reply_markup)
    assert buttons
    assert all(b.text != "Открыть тендер" for b in buttons)
    assert all("/tender/" not in (getattr(b, "url", None) or "") for b in buttons)


def test_format_shows_cpv_and_plan_lines_from_classifications():
    adapter = TelegramAdapter(_FakeBot())
    classifications = [
        {"scheme": "CPV", "code": "45000000", "label": "Грађевински радови"},
        {"scheme": "PLAN", "quarter": "2. квартал", "year": 2026},
    ]
    msg = adapter.format(_job(classifications=classifications))
    assert "🏷 CPV: 45000000 — Грађевински радови" in msg.text
    assert "📅 План: 2. квартал 2026" in msg.text


def test_format_no_cpv_plan_lines_without_classifications():
    adapter = TelegramAdapter(_FakeBot())
    msg = adapter.format(_job(classifications=None))
    assert "🏷 CPV:" not in msg.text
    assert "📅 План:" not in msg.text


def test_format_uses_contract_type_when_present():
    adapter = TelegramAdapter(_FakeBot())
    msg = adapter.format(_job(contract_type="works"))
    # the explicit subject type displaces the fallback “Procedure: …”
    assert "📦 Тип: Работы" in msg.text
    assert "Процедура:" not in msg.text


def test_format_amount_eur_only_no_original():
    adapter = TelegramAdapter(_FakeBot())
    msg = adapter.format(
        _job(amount_original=None, currency_original=None, is_amount_estimated=None)
    )
    assert "💰 1 234 567 EUR" in msg.text
    assert "RSD" not in msg.text
    assert "оценочно" not in msg.text


def test_format_priority_stars_scale():
    adapter = TelegramAdapter(_FakeBot())
    assert adapter.format(_job(amount_eur=10_000)).text.startswith("⭐ ")
    assert adapter.format(_job(amount_eur=100_000)).text.startswith("⭐⭐ ")
    assert adapter.format(_job(amount_eur=500_000)).text.startswith("⭐⭐⭐ ")
    assert adapter.format(_job(amount_eur=2_000_000)).text.startswith("⭐⭐⭐⭐ ")


def test_format_minimum_card_only_title_no_extra_lines():
    adapter = TelegramAdapter(_FakeBot())
    msg = adapter.format(
        _job(
            title="Минимальный",
            amount_eur=None,
            deadline_at=None,
            source_url=None,
            source_uid=None,
            source_key=None,
            amount_original=None,
            currency_original=None,
            is_amount_estimated=None,
            published_at=None,
            procedure_type=None,
            regions=None,
            contract_type=None,
            classifications=None,
        )
    )
    # only the title + the label of the profile (always present), without stars
    assert msg.text.startswith("<b>Минимальный</b>")
    assert "🔎 Найдено по шаблону:" in msg.text


def test_format_escapes_html_in_dynamic_fields():
    adapter = TelegramAdapter(_FakeBot())
    msg = adapter.format(_job(title="<b>x</b> & y", regions=["A&B"]))
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in msg.text
    assert "📍 Регион: A&amp;B" in msg.text


def test_cabinet_base_url_trailing_slash_stripped():
    adapter = TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs/")
    assert adapter._tender_url("x") == "https://nab.rs/tender/x"


# --------------------------------------------------------------------------- #
#  send — success and the mapping of errors (without changes since Sprint-17)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_send_success_returns_ok():
    bot = _FakeBot()
    adapter = TelegramAdapter(bot)
    result = await adapter.send(_job(), adapter.format(_job()))
    assert result.ok is True and result.response_code == 200
    assert bot.calls[0]["chat_id"] == 555


@pytest.mark.asyncio
async def test_send_forbidden_maps_to_blocked():
    bot = _FakeBot(
        TelegramForbiddenError(
            method=_DummyMethod(),
            message="Forbidden: bot was blocked by the user",
        )
    )
    adapter = TelegramAdapter(bot)
    result = await adapter.send(_job(), adapter.format(_job()))
    assert result.ok is False and result.blocked is True
    assert result.response_code == 403


@pytest.mark.asyncio
async def test_send_chat_not_found_maps_to_blocked():
    bot = _FakeBot(
        TelegramBadRequest(method=_DummyMethod(), message="Bad Request: chat not found")
    )
    adapter = TelegramAdapter(bot)
    result = await adapter.send(_job(), adapter.format(_job()))
    assert result.blocked is True and result.response_code == 400


@pytest.mark.asyncio
async def test_send_retry_after_maps_to_retryable():
    bot = _FakeBot(
        TelegramRetryAfter(
            method=_DummyMethod(),
            message="Too Many Requests: retry after 5",
            retry_after=5,
        )
    )
    adapter = TelegramAdapter(bot)
    result = await adapter.send(_job(), adapter.format(_job()))
    assert result.retryable is True and result.response_code == 429


@pytest.mark.asyncio
async def test_send_generic_bad_request_is_permanent_failure():
    bot = _FakeBot(
        TelegramBadRequest(
            method=_DummyMethod(), message="Bad Request: message text is empty"
        )
    )
    adapter = TelegramAdapter(bot)
    result = await adapter.send(_job(), adapter.format(_job()))
    assert result.ok is False and result.blocked is False and result.retryable is False


@pytest.mark.asyncio
async def test_send_no_target_returns_blocked_without_calling_bot():
    bot = _FakeBot()
    adapter = TelegramAdapter(bot)
    result = await adapter.send(_job(target=None), adapter.format(_job()))
    assert result.blocked is True and bot.calls == []


# --------------------------------------------------------------------------- #
#  The P0-fix BUTTON_DATA_INVALID: callback_data ≤ 64 bytes (the TG limit)
# --------------------------------------------------------------------------- #

_UUID_A = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def test_ai_menu_callback_carries_profile_flag_not_profile_id():
    """“🧠 AI analysis ▾” = 'ai:menu:{tender_id}:1' with the profile — the profile_id itself does not travel in the callback, the length ≤ 64. 🎯 expands already inside the menu (the bot handler)."""
    adapter = TelegramAdapter(_FakeBot())
    msg = adapter.format(_job(tender_id=_UUID_A, profile_id=_UUID_B))
    buttons = _buttons(msg.reply_markup)
    menu = [b for b in buttons if b.text == "🧠 AI-разбор ▾"]
    assert len(menu) == 1
    cb = menu[0].callback_data
    assert cb == f"ai:menu:{_UUID_A}:1"
    assert _UUID_B not in cb
    assert len(cb.encode()) <= 64
    # The AI buttons are not shown in the collapsed card
    assert all(b.text != "🎯 Стоит ли?" for b in buttons)


def test_ai_menu_without_profile_id_is_short_variant():
    """Without profile_id the flag is 0 (only 🧠 and 🏛 in the menu) and there is no button “Open in the cabinet” — the page of the tender in the cabinet without profile_id gives 404."""
    adapter = TelegramAdapter(_FakeBot())
    msg = adapter.format(_job(tender_id=_UUID_A, profile_id=None))
    buttons = _buttons(msg.reply_markup)
    assert [b.callback_data for b in buttons if b.text == "🧠 AI-разбор ▾"] == [
        f"ai:menu:{_UUID_A}:0"
    ]
    assert all(not b.url for b in buttons)


def test_card_keyboard_is_two_rows_of_up_to_three_with_cabinet_link():
    """The collapsed card: row 1 — [Open in the cabinet][Follow][Hide], row 2 — [AI ▾]."""
    adapter = TelegramAdapter(_FakeBot(), cabinet_base_url="https://app.nab.rs/")
    msg = adapter.format(_job(tender_id=_UUID_A, profile_id=_UUID_B))
    rows = msg.reply_markup.inline_keyboard
    assert [len(r) for r in rows] == [3, 1]
    assert [b.text for b in rows[0]] == ["📂 Открыть в ЛК", "⭐ Следить", "✖ Скрыть"]
    assert rows[0][0].url == (
        "https://app.nab.rs/app/tenders/"
        "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        "?profile_id=bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    )
    assert [b.callback_data for b in rows[0][1:]] == [
        f"st:watching:{_UUID_A}",
        f"st:skipped:{_UUID_A}",
    ]


@pytest.mark.parametrize(
    "status", ["new", "watching", "active", "submitted", "won", "lost", "skipped"]
)
def test_all_callbacks_within_64_bytes(status):
    """All callback_data of the card ≤ 64 bytes at any status of the funnel."""
    adapter = TelegramAdapter(_FakeBot())
    msg = adapter.format(_job(tender_id=_UUID_A, profile_id=_UUID_B))
    buttons = _buttons(msg.reply_markup)
    for btn in buttons:
        if btn.callback_data:
            length = len(btn.callback_data.encode())
            assert length <= 64, f"callback '{btn.callback_data}' = {length} bytes > 64"


# --------------------------------------------------------------------------- #
#  The digest: one list message instead of N cards
# --------------------------------------------------------------------------- #


def test_format_digest_lists_all_tenders_without_buttons():
    """The digest: a header with a counter, all the tenders with links, no buttons."""
    adapter = TelegramAdapter(_FakeBot())
    jobs = [_job(outbox_id=1, title="Первый"), _job(outbox_id=2, title="Второй")]

    msg = adapter.format_digest(jobs)

    assert "Дайджест: 2 новых тендера" in msg.text
    assert "1. " in msg.text and "2. " in msg.text
    assert "Первый" in msg.text and "Второй" in msg.text
    assert msg.text.count("https://jnportal.rs/tender/1?x=1") == 2  # a link for each
    assert msg.reply_markup is None  # the buttons are channel-specific for ONE tender


def test_format_digest_escapes_dynamic_text():
    """The title from the source is escaped: the messages of the bot go with parse_mode=HTML."""
    adapter = TelegramAdapter(_FakeBot())
    msg = adapter.format_digest([_job(title="Видеонаблюдение & <b>монтаж</b>")])

    assert "&amp;" in msg.text
    assert "<b>монтаж</b>" not in msg.text
    assert "&lt;b&gt;" in msg.text
