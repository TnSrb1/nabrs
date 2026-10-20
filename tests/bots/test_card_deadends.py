"""The tender card in the bot and the dead ends (the audit of owner 01.10): the keyboard ≤2 rows,
the AI menu, “Hide/Restore”, the texts of an unlinked chat, escaping of the user fields.
The expectations are LITERALS (not via ``bot_t``/``common_t``: an assertion with the same
catalog as the code is a tautology).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bots import telegram as tg

_TID = "11111111-1111-1111-1111-111111111111"
_PID = "22222222-2222-2222-2222-222222222222"
_URL = f"https://app.nab.rs/app/tenders/{_TID}?profile_id={_PID}"


def _rows(markup):
    return [
        [(b.text, b.callback_data or b.url) for b in row]
        for row in markup.inline_keyboard
    ]


# --------------------------------------------------------------------------- #
# Card keyboard
# --------------------------------------------------------------------------- #


def test_card_keyboard_new_is_two_rows_literal():
    kb = tg.build_card_keyboard(
        _TID, status="new", locale="ru", cabinet_url=_URL, ai_full=True
    )
    assert _rows(kb) == [
        [
            ("📂 Открыть в ЛК", _URL),
            ("⭐ Следить", f"st:watching:{_TID}"),
            ("✖ Скрыть", f"st:skipped:{_TID}"),
        ],
        [("🧠 AI-разбор ▾", f"ai:menu:{_TID}:1")],
    ]


@pytest.mark.parametrize("status", ["new", "watching", "active", "submitted"])
def test_card_keyboard_never_exceeds_two_rows_three_buttons(status):
    kb = tg.build_card_keyboard(
        _TID, status=status, locale="sr", cabinet_url=_URL, ai_full=True
    )
    assert len(kb.inline_keyboard) <= 2
    assert all(len(r) <= 3 for r in kb.inline_keyboard)
    for row in kb.inline_keyboard:
        for b in row:
            assert len((b.callback_data or "").encode()) <= 64


def test_card_keyboard_watching_offers_participation():
    kb = tg.build_card_keyboard(_TID, status="watching", locale="en", ai_full=False)
    assert _rows(kb) == [
        [
            ("✅ Participate", f"st:active:{_TID}"),
            ("✖ Hide", f"st:skipped:{_TID}"),
        ],
        [("🧠 AI tools ▾", f"ai:menu:{_TID}:0")],
    ]


# --------------------------------------------------------------------------- #
# AI menu: expand / collapse, callback_data unchanged
# --------------------------------------------------------------------------- #


def _msg(markup, text="<b>⭐ Тендер</b>\n\n💰 100 EUR", chat_id=7, message_id=42):
    m = MagicMock(spec=Message)
    m.chat = SimpleNamespace(id=chat_id)
    m.message_id = message_id
    m.html_text = text
    m.reply_markup = markup
    m.edit_text = AsyncMock()
    m.edit_reply_markup = AsyncMock()
    m.answer = AsyncMock()
    return m


def _cb(data, message):
    cb = AsyncMock()
    cb.data = data
    cb.message = message
    cb.from_user = SimpleNamespace(id=7, language_code=None)
    return cb


@asynccontextmanager
async def _scope():
    yield AsyncMock()


@pytest.mark.asyncio
async def test_ai_menu_expands_with_original_callbacks_and_back():
    card = tg.build_card_keyboard(
        _TID, status="new", locale="ru", cabinet_url=_URL, ai_full=True
    )
    message = _msg(card)
    with (
        patch.object(tg, "session_scope", _scope),
        patch.object(tg, "resolve_bot_locale", AsyncMock(return_value="ru")),
    ):
        await tg.on_ai_menu(_cb(f"ai:menu:{_TID}:1", message))
    expanded = message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert _rows(expanded) == [
        [
            ("📂 Открыть в ЛК", _URL),
            ("⭐ Следить", f"st:watching:{_TID}"),
            ("✖ Скрыть", f"st:skipped:{_TID}"),
        ],
        [
            ("🧠 Глубокий анализ", f"ai:deep:{_TID}"),
            ("🎯 Стоит ли?", f"ai:bid:{_TID}"),
        ],
        [
            ("📋 Что подготовить?", f"ai:chk:{_TID}"),
            ("📝 Черновик заявки", f"ai:drf:{_TID}"),
        ],
        [("🏛 Кто выигрывает?", f"an:cust:{_TID}")],
        [("↩ Назад", f"ai:back:{_TID}:1")],
    ]

    # “↩ Back” returns exactly the original collapsed keyboard.
    message.reply_markup = expanded
    with (
        patch.object(tg, "session_scope", _scope),
        patch.object(tg, "resolve_bot_locale", AsyncMock(return_value="ru")),
    ):
        await tg.on_ai_back(_cb(f"ai:back:{_TID}:1", message))
    back = message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert _rows(back) == _rows(card)


@pytest.mark.asyncio
async def test_ai_menu_without_profile_has_no_profile_buttons():
    card = tg.build_card_keyboard(_TID, status="new", locale="ru", ai_full=False)
    message = _msg(card)
    with (
        patch.object(tg, "session_scope", _scope),
        patch.object(tg, "resolve_bot_locale", AsyncMock(return_value="ru")),
    ):
        await tg.on_ai_menu(_cb(f"ai:menu:{_TID}:0", message))
    datas = [
        d
        for row in _rows(message.edit_reply_markup.await_args.kwargs["reply_markup"])
        for _, d in row
    ]
    assert f"ai:deep:{_TID}" in datas
    assert not any(d.startswith(("ai:bid:", "ai:chk:", "ai:drf:")) for d in datas)


# --------------------------------------------------------------------------- #
# “Hide” → one row + “↩ Restore”
# --------------------------------------------------------------------------- #


class _FakeStash:
    """An in-memory cache instead of Redis (patched in the telegram.py namespace)."""

    def __init__(self, ok=True):
        self.ok = ok
        self.data = {}

    async def stash(self, chat_id, message_id, payload):
        if self.ok:
            self.data[(chat_id, message_id)] = payload
        return self.ok

    async def load(self, chat_id, message_id):
        return self.data.get((chat_id, message_id))

    async def drop(self, chat_id, message_id):
        self.data.pop((chat_id, message_id), None)


def _patch_status(stash, prior="watching"):
    set_mock = AsyncMock()
    patches = [
        patch.object(tg, "get_tender_status", AsyncMock(return_value=prior)),
        patch.object(tg, "clear_tender_status", AsyncMock()),
        patch.object(tg, "session_scope", _scope),
        patch.object(tg, "resolve_tenant_id", AsyncMock(return_value="tenant-1")),
        patch.object(tg, "resolve_bot_locale", AsyncMock(return_value="ru")),
        patch.object(tg, "is_tenant_frozen", AsyncMock(return_value=False)),
        patch.object(tg, "set_tender_status", set_mock),
        patch.object(tg, "stash_card", stash.stash),
        patch.object(tg, "load_card", stash.load),
        patch.object(tg, "drop_card", stash.drop),
    ]
    return patches[2:] + patches[:2], set_mock


@pytest.mark.asyncio
async def test_skip_collapses_to_one_line_and_restore_brings_card_back():
    stash = _FakeStash()
    card = tg.build_card_keyboard(
        _TID, status="watching", locale="ru", cabinet_url=_URL, ai_full=True
    )
    original = "<b>⭐ Тендер &amp; Ко</b>\n\n💰 100 EUR\n\nСтатус: 👀 Изучаю"
    message = _msg(card, text=original)
    patches, set_mock = _patch_status(stash)

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
    ):
        await tg.on_tender_status(_cb(f"st:skipped:{_TID}", message))

        # the funnel status is recorded as before
        assert set_mock.await_args.kwargs["status"] == "skipped"
        # the message is collapsed to one line + “↩ Restore”
        collapsed = message.edit_text.await_args
        assert collapsed.args[0] == "<b>⭐ Тендер &amp; Ко</b> — <i>скрыто</i>"
        assert _rows(collapsed.kwargs["reply_markup"]) == [
            [("↩ Вернуть", f"cd:show:{_TID}")]
        ]

        # “↩ Restore”: the same text and keyboard, the status — the previous one (watching)
        message.edit_text.reset_mock()
        await tg.on_card_restore(_cb(f"cd:show:{_TID}", message))
        restored = message.edit_text.await_args
        assert restored.args[0] == original
        assert _rows(restored.kwargs["reply_markup"]) == _rows(card)
        assert set_mock.await_args.kwargs["status"] == "watching"
        assert stash.data == {}  # the cache is removed


@pytest.mark.asyncio
async def test_skip_without_stash_keeps_card_and_only_updates_status_line():
    """The cache is unavailable (Redis is down) → do NOT collapse: a hidden item cannot be restored."""
    stash = _FakeStash(ok=False)
    card = tg.build_card_keyboard(_TID, status="new", locale="ru", ai_full=True)
    message = _msg(card, text="<b>Тендер</b>\n\n💰 100 EUR")
    patches, set_mock = _patch_status(stash)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
    ):
        await tg.on_tender_status(_cb(f"st:skipped:{_TID}", message))
    assert set_mock.await_args.kwargs["status"] == "skipped"
    text = message.edit_text.await_args.args[0]
    assert "скрыто" not in text
    assert text.endswith("Статус: ❌ Пропущен")


@pytest.mark.asyncio
async def test_follow_keeps_cabinet_link_and_ai_menu_on_redraw():
    stash = _FakeStash()
    card = tg.build_card_keyboard(
        _TID, status="new", locale="ru", cabinet_url=_URL, ai_full=True
    )
    message = _msg(card)
    patches, _ = _patch_status(stash)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
    ):
        await tg.on_tender_status(_cb(f"st:watching:{_TID}", message))
    kb = message.edit_text.await_args.kwargs["reply_markup"]
    assert _rows(kb) == [
        [
            ("📂 Открыть в ЛК", _URL),
            ("✅ Участвую", f"st:active:{_TID}"),
            ("✖ Скрыть", f"st:skipped:{_TID}"),
        ],
        [("🧠 AI-разбор ▾", f"ai:menu:{_TID}:1")],
    ]


@pytest.mark.asyncio
async def test_restore_after_stash_expired_says_so():
    stash = _FakeStash()
    message = _msg(InlineKeyboardMarkup(inline_keyboard=[]), text="x — скрыто")
    patches, set_mock = _patch_status(stash)
    cb = _cb(f"cd:show:{_TID}", message)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
    ):
        await tg.on_card_restore(cb)
    set_mock.assert_not_awaited()
    assert cb.answer.await_args.args[0] == (
        "Карточку уже не вернуть — откройте тендер в кабинете."
    )


# --------------------------------------------------------------------------- #
# Unlinked chat: the linking path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unlinked_watching_gives_link_path_with_cabinet_button():
    message = MagicMock()
    message.from_user = SimpleNamespace(id=9)
    message.answer = AsyncMock()
    result = SimpleNamespace(status="no_account", items=[])
    with (
        patch.object(tg, "session_scope", _scope),
        patch.object(tg, "resolve_bot_locale", AsyncMock(return_value="ru")),
        patch(
            "src.bots.award_watch_service.run_list_watching",
            AsyncMock(return_value=result),
        ),
    ):
        await tg.cmd_watching(message)
    text = message.answer.await_args.args[0]
    assert text == (
        "Чат не привязан к аккаунту. Подключить: кабинет → «Каналы» → "
        "«Подключить Telegram» (кнопка ниже)."
    )
    button = message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert (button.text, button.url) == (
        "🔗 Подключить Telegram",
        "https://app.nab.rs/cabinet/telegram",
    )


# --------------------------------------------------------------------------- #
# The locale of an unlinked chat by the Telegram client
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "code,expected",
    [("sr", "sr"), ("en-US", "en"), ("ru", "ru"), ("de", "ru"), (None, "ru")],
)
def test_tg_locale_uses_client_language_code(code, expected):
    event = SimpleNamespace(from_user=SimpleNamespace(language_code=code))
    assert tg._tg_locale(event) == expected


# --------------------------------------------------------------------------- #
# html.escape of the user fields
# --------------------------------------------------------------------------- #


def test_render_profile_escapes_name_keywords_and_email():
    profile = SimpleNamespace(
        id="p1",
        name="D&D <doo>",
        is_active=True,
        match_all=False,
        keywords_include=[{"all_of": ["a<b"]}],
        regions=[],
        enabled_source_keys=[],
    )
    data = {
        "tenant": SimpleNamespace(tier="lite", access_until=None),
        "user": SimpleNamespace(email="x<y>@z.rs"),
    }
    text = tg._render_profile(data, [profile], "ru")
    assert "<b>D&amp;D &lt;doo&gt;</b>" in text
    assert "a&lt;b" in text
    assert "x&lt;y&gt;@z.rs" in text
    assert "<doo>" not in text


def test_build_summary_escapes_keywords():
    text = tg._build_summary(
        {"language": "ru", "keywords": ["D&D <doo>"], "region": "RS-00"}, "ru"
    )
    assert "D&amp;D &lt;doo&gt;" in text
    assert "<doo>" not in text


@pytest.mark.asyncio
async def test_watching_list_escapes_winner_name():
    message = MagicMock()
    message.from_user = SimpleNamespace(id=9)
    message.answer = AsyncMock()
    item = SimpleNamespace(sub_id=5, winner_name="D&D <doo>", winner_pib="123456789")
    result = SimpleNamespace(status="ok", items=[item])
    with (
        patch.object(tg, "session_scope", _scope),
        patch.object(tg, "resolve_bot_locale", AsyncMock(return_value="ru")),
        patch(
            "src.bots.award_watch_service.run_list_watching",
            AsyncMock(return_value=result),
        ),
    ):
        await tg.cmd_watching(message)
    text = message.answer.await_args.args[0]
    assert "• D&amp;D &lt;doo&gt; (PIB 123456789)" in text
    assert "<doo>" not in text


# --------------------------------------------------------------------------- #
# Analytics: button labels by language, not a hard-coded Russian
# --------------------------------------------------------------------------- #


def test_analytics_keyboard_is_localized():
    kb = tg._analytics_period_keyboard(
        _TID,
        12,
        cb_prefix="cust",
        show_competitor=True,
        show_reprocurement=True,
        locale="en",
    )
    assert [b.text for row in kb.inline_keyboard for b in row] == [
        "✓ 📅 12 mo",
        "📅 36 mo",
        "📅 All time",
        "🥊 Top winner",
        "🔮 Expected re-procurements",
        "📊 Export CSV",
    ]


# --------------------------------------------------------------------------- #
# PR #569 review: “Restore” — the status from the DB; the debug card; the Redis timeout
# --------------------------------------------------------------------------- #


class _FakeFunnelDb:
    """Funnel statuses in memory: get/set/clear instead of SQL."""

    def __init__(self, status=None):
        self.status = status

    def patches(self):
        async def _get(session, *, tenant_id, tender_id):
            return self.status

        async def _set(session, *, tenant_id, tender_id, status):
            self.status = status

        async def _clear(session, *, tenant_id, tender_id):
            self.status = None

        return [
            patch.object(tg, "get_tender_status", _get),
            patch.object(tg, "set_tender_status", _set),
            patch.object(tg, "clear_tender_status", _clear),
        ]


async def _hide_then_restore(db, card):
    stash = _FakeStash()
    message = _msg(card, text="<b>Тендер</b>\n\n💰 100 EUR")
    with (
        patch.object(tg, "session_scope", _scope),
        patch.object(tg, "resolve_tenant_id", AsyncMock(return_value="tenant-1")),
        patch.object(tg, "resolve_bot_locale", AsyncMock(return_value="ru")),
        patch.object(tg, "is_tenant_frozen", AsyncMock(return_value=False)),
        patch.object(tg, "stash_card", stash.stash),
        patch.object(tg, "load_card", stash.load),
        patch.object(tg, "drop_card", stash.drop),
        db.patches()[0],
        db.patches()[1],
        db.patches()[2],
    ):
        await tg.on_tender_status(_cb(f"st:skipped:{_TID}", message))
        hidden = db.status
        await tg.on_card_restore(_cb(f"cd:show:{_TID}", message))
    return hidden


@pytest.mark.asyncio
async def test_restore_brings_back_watching_from_db_not_from_buttons():
    """new and watching have the same buttons on main: we take the status from the DB."""
    db = _FakeFunnelDb(status="watching")
    # the card was drawn as «new» (the Follow/Hide buttons), the DB already had watching
    card = tg.build_card_keyboard(_TID, status="new", locale="ru", ai_full=True)
    hidden = await _hide_then_restore(db, card)
    assert hidden == "skipped"
    assert db.status == "watching"


@pytest.mark.asyncio
async def test_restore_without_prior_status_removes_the_row():
    db = _FakeFunnelDb(status=None)
    card = tg.build_card_keyboard(_TID, status="new", locale="ru", ai_full=True)
    hidden = await _hide_then_restore(db, card)
    assert hidden == "skipped"
    assert db.status is None


def test_card_context_finds_cabinet_link_by_url_not_position():
    debug_url = "https://x.test/profiles/p1/edit"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚙️", url=debug_url),
            ],
            [InlineKeyboardButton(text="📂", url=_URL)],
        ]
    )
    cabinet_url, _ = tg._card_context(markup)
    assert cabinet_url == _URL


@pytest.mark.asyncio
async def test_follow_keeps_debug_row_on_redraw():
    debug_url = "https://x.test/profiles/p1/edit"
    card = tg.build_card_keyboard(
        _TID, status="new", locale="ru", cabinet_url=_URL, ai_full=True
    )
    card = InlineKeyboardMarkup(
        inline_keyboard=[
            *card.inline_keyboard,
            [InlineKeyboardButton(text="⚙️ Настроить шаблон", url=debug_url)],
        ]
    )
    message = _msg(card)
    patches, _ = _patch_status(_FakeStash(), prior="new")
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
    ):
        await tg.on_tender_status(_cb(f"st:watching:{_TID}", message))
    rows = message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert rows[0][0].url == _URL  # the cabinet link, not “Configure template”
    assert [b.url for b in rows[-1]] == [debug_url]


@pytest.mark.asyncio
async def test_card_stash_redis_client_has_timeouts_and_timeout_means_unavailable():
    import asyncio

    from src.bots import card_stash

    captured = {}

    class _Hanging:
        async def set(self, *a, **k):
            raise asyncio.TimeoutError()

    def _from_url(url, **kwargs):
        captured.update(kwargs)
        return _Hanging()

    card_stash._client = None
    try:
        with patch("redis.asyncio.from_url", _from_url):
            ok = await card_stash.stash_card(1, 2, {"text": "x"})
    finally:
        card_stash._client = None
    assert captured["socket_timeout"] == 2
    assert captured["socket_connect_timeout"] == 2
    assert ok is False
