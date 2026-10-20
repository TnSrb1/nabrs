"""Unit tests of the static handlers of the Telegram bot.

They check the stub/help commands that need neither the DB nor the FSM. The network is not touched:
``Message`` is mocked, ``answer`` is intercepted, the real Bot is not created.

The ONBOARDING AND CRUD (/start, /profile, /reset, FSM) are covered separately in
``test_telegram_fsm.py`` — they need FSMContext and a DB session.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.bots.telegram import (
    _ADMIN_COMMANDS,
    _PUBLIC_COMMANDS,
    cmd_help,
    cmd_list,
    cmd_status,
    register_bot_commands,
)


def _make_message() -> AsyncMock:
    """A mock of ``aiogram.types.Message`` with an async ``answer``."""
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=123456)
    message.answer = AsyncMock()
    return message


def _patch_bot_locale(locale: str = "ru"):
    """Mock the bot locale resolve (cmd_help reads users.locale via the DB)."""
    return patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value=locale))


@asynccontextmanager
async def _fake_scope():
    yield AsyncMock()


@pytest.mark.asyncio
async def test_help_returns_nonempty_text() -> None:
    message = _make_message()
    with patch("src.bots.telegram.session_scope", _fake_scope), _patch_bot_locale():
        await cmd_help(message)
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert isinstance(text, str)
    assert text.strip()
    # /help lists the commands — must mention /start and the new commands.
    assert "/start" in text
    assert "/profile" in text
    assert "/reset" in text


@pytest.mark.asyncio
async def test_help_mentions_key_features() -> None:
    """/help must explain not only the commands but also the buttons on the card —
    this is what the client “did not see” (the motivation of VG-31)."""
    message = _make_message()
    with patch("src.bots.telegram.session_scope", _fake_scope), _patch_bot_locale():
        await cmd_help(message)
    text = message.answer.await_args.args[0]
    assert "/watching" in text
    assert "/status" in text
    assert "Глубокий анализ" in text
    assert "Черновик заявки" in text
    assert "Кто выигрывает" in text


@pytest.mark.asyncio
async def test_register_bot_commands_default_scope_matches_real_commands() -> None:
    """The default menu — exactly the real public commands, without /trials (admin-only)."""
    from src.bots.config import BotSettings

    bot = AsyncMock()
    settings = BotSettings(TG_BOT_TOKEN="x", ADMIN_CHAT_ID=0)

    await register_bot_commands(bot, settings)

    # sr by default + the ru/en sets; each — the same real commands.
    assert bot.set_my_commands.await_count == 3
    commands = bot.set_my_commands.await_args_list[0].args[0]
    sent_names = {c.command for c in commands}
    real_names = {name for name, _ in _PUBLIC_COMMANDS}
    assert sent_names == real_names
    assert "trials" not in sent_names


@pytest.mark.asyncio
async def test_register_bot_commands_admin_scope_includes_trials() -> None:
    """With ADMIN_CHAT_ID configured — a second call on the chat scope with /trials."""
    from src.bots.config import BotSettings

    bot = AsyncMock()
    settings = BotSettings(TG_BOT_TOKEN="x", ADMIN_CHAT_ID=42)

    await register_bot_commands(bot, settings)

    assert bot.set_my_commands.await_count == 6  # 3 public languages + 3 admin ones
    admin_commands = bot.set_my_commands.await_args_list[3].args[0]
    admin_names = {c.command for c in admin_commands}
    assert "trials" in admin_names
    assert admin_names == {name for name, _ in (*_PUBLIC_COMMANDS, *_ADMIN_COMMANDS)}


@pytest.mark.asyncio
async def test_cmd_list_unlinked_chat_points_to_cabinet() -> None:
    """/list is no longer a stub: an unlinked chat → a hint + a linking button."""
    message = _make_message()
    with (
        patch("src.bots.telegram.session_scope", _fake_scope),
        _patch_bot_locale(),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value=None)),
    ):
        await cmd_list(message)
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert text.startswith("Чат не привязан к аккаунту.")
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].url == "https://app.nab.rs/cabinet/telegram"


@pytest.mark.asyncio
async def test_cmd_status_no_binding_returns_text() -> None:
    """cmd_status without a TG-channel binding → a hint to go through /start (the DB is mocked)."""
    message = _make_message()
    message.from_user = SimpleNamespace(id=123456)

    @asynccontextmanager
    async def _fake_scope():
        yield AsyncMock()

    with (
        patch("src.bots.telegram.session_scope", _fake_scope),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value=None)),
    ):
        await cmd_status(message)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert isinstance(text, str)
    assert text.strip()


def test_build_bot_without_token_raises() -> None:
    """Without TG_BOT_TOKEN the bot factory must fail explicitly, not reach for the network."""
    from src.bots.config import BotSettings
    from src.bots.telegram import build_bot

    with pytest.raises(RuntimeError):
        build_bot(BotSettings(TG_BOT_TOKEN=""))


def test_build_dispatcher_includes_router() -> None:
    """The Dispatcher must be assembled and contain the commands router."""
    from src.bots.telegram import build_dispatcher, router

    dp = build_dispatcher()
    assert router in dp.sub_routers


@pytest.mark.asyncio
async def test_register_bot_commands_serbian_default_plus_ru_en_sets() -> None:
    """Menu: sr without language_code (by default), then language_code=ru and en."""
    from src.bots.config import BotSettings

    bot = AsyncMock()
    await register_bot_commands(bot, BotSettings(TG_BOT_TOKEN="x", ADMIN_CHAT_ID=0))

    calls = bot.set_my_commands.await_args_list
    assert [c.kwargs["language_code"] for c in calls] == [None, "ru", "en"]
    start = {
        c.kwargs["language_code"]: {b.command: b.description for b in c.args[0]}[
            "start"
        ]
        for c in calls
    }
    assert start[None] == "Registracija i podešavanje šablona pretrage"
    assert start["ru"] == "Регистрация и настройка шаблона поиска"
    assert start["en"] == "Sign up and set up a search template"


def test_command_descriptions_parity_with_ru_menu() -> None:
    from src.bots.telegram import _COMMAND_DESCRIPTIONS

    names = {n for n, _ in (*_PUBLIC_COMMANDS, *_ADMIN_COMMANDS)}
    for lang in ("sr", "en"):
        assert set(_COMMAND_DESCRIPTIONS[lang]) == names
        assert all(3 <= len(d) <= 256 for d in _COMMAND_DESCRIPTIONS[lang].values())
