"""Guards of the i18n of the Telegram bot (Phase 2a): the chrome of the INPUT handlers by users.locale.

The scope 2a — only the input points: ``cmd_start`` (the hint “the profile already exists” and the polite refusal of the signup gate) and ``cmd_help``. The onboarding/statuses/AI — that is 2b.

We check: under en/sr the chrome of the input handlers is localised (there are no RUSSIAN letters
``U+044B U+044D U+0449 U+044A U+0451 U+0439`` — they are absent both in Serbian Latin and in English), and under ru the previous Russian text is kept (the backward compatibility of the canon). The parity of the catalog keys is held by a separate test — otherwise a new key in one language would silently drop a KeyError only under this language in production.

Two AXES (CLAUDE.md): ``users.locale`` (the interface/chrome of the bot) ≠
``profiles.target_language`` (AI content). Here only the interface chrome.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.bots.i18n import BOT
from src.bots.telegram import cmd_help, cmd_start

# Russian letters that are absent in Serbian Latin/Cyrillic and in English:
# their presence under en/sr = untranslated Russian chrome.
RU_ONLY = re.compile(r"[ыэщъёйЫЭЩЪЁЙ]")


# --------------------------------------------------------------------------- #
# The catalog: parity of the keys of all three languages.
# --------------------------------------------------------------------------- #


def test_bot_catalog_key_parity():
    ru_keys = set(BOT["ru"])
    for lang in ("sr", "en"):
        assert set(BOT[lang]) == ru_keys, f"a divergence of the keys ru↔{lang}"


# --------------------------------------------------------------------------- #
# Fixtures of the mock handlers.
# --------------------------------------------------------------------------- #


class _FakeState:
    def __init__(self) -> None:
        self._state = None

    async def clear(self) -> None:
        self._state = None

    async def set_state(self, state) -> None:
        self._state = state

    async def get_state(self):
        return self._state

    async def update_data(self, **kwargs) -> None:
        pass


def _make_message(uid: int = 777) -> AsyncMock:
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=uid)
    message.answer = AsyncMock()
    return message


@asynccontextmanager
async def _fake_scope():
    yield AsyncMock()


def _patch_locale(locale: str):
    return patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value=locale))


def _last_text(message: AsyncMock) -> str:
    return message.answer.await_args.args[0]


# --------------------------------------------------------------------------- #
# cmd_help: the help is localised.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_cmd_help_localized_no_russian(locale):
    message = _make_message()
    with patch("src.bots.telegram.session_scope", _fake_scope), _patch_locale(locale):
        await cmd_help(message)
    text = _last_text(message)
    assert not RU_ONLY.search(text), f"Russian letters in the help under {locale}: {text!r}"
    # the anchor commands are in place — the help stayed meaningful
    assert "/start" in text
    assert "/help" in text


@pytest.mark.asyncio
async def test_cmd_help_ru_preserved():
    message = _make_message()
    with patch("src.bots.telegram.session_scope", _fake_scope), _patch_locale("ru"):
        await cmd_help(message)
    text = _last_text(message)
    # the previous Russian canon is kept
    assert "Глубокий анализ" in text
    assert "Кто выигрывает" in text
    assert RU_ONLY.search(text)


# --------------------------------------------------------------------------- #
# cmd_start: the hint “the profile already exists” is localised.
# --------------------------------------------------------------------------- #


def _existing_channel_session() -> AsyncMock:
    """Mock session: app_resolve_tenant_id → the tenant exists (existing subscriber)."""
    session = AsyncMock()
    result = SimpleNamespace(scalar=lambda: "tenant-1")
    session.execute = AsyncMock(return_value=result)
    return session


def _patch_scope(session):
    @asynccontextmanager
    async def _scope():
        yield session

    return patch("src.bots.telegram.session_scope", _scope)


def _patch_no_notice():
    """The notice “new source” is a separate DB path; here we check the text of /start."""
    return patch("src.bots.telegram._send_everywhere_notice", AsyncMock())


def _patch_mode(mode: str = "token_only"):
    return patch(
        "src.bots.telegram.get_bot_settings",
        lambda: SimpleNamespace(bot_signup_mode=mode),
    )


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_cmd_start_existing_hint_localized_no_russian(locale):
    message = _make_message(uid=42)
    state = _FakeState()
    with (
        _patch_mode(),
        _patch_scope(_existing_channel_session()),
        _patch_no_notice(),
        _patch_locale(locale),
    ):
        await cmd_start(message, state, None)
    text = _last_text(message)
    assert not RU_ONLY.search(text), f"Russian letters in /start under {locale}: {text!r}"
    # stayed meaningful: /profile is kept, there is no /reset advice any more
    assert "/profile" in text
    assert "/reset" not in text


@pytest.mark.asyncio
async def test_cmd_start_existing_hint_ru_preserved():
    message = _make_message(uid=42)
    state = _FakeState()
    with (
        _patch_mode(),
        _patch_scope(_existing_channel_session()),
        _patch_no_notice(),
        _patch_locale("ru"),
    ):
        await cmd_start(message, state, None)
    text = _last_text(message)
    assert text.startswith("Аккаунт уже подключён.")
    assert RU_ONLY.search(text)


@pytest.mark.asyncio
async def test_cmd_start_does_not_offer_new_source_banner():
    """The offer “new source” — only on “🗂 Sources”, not on /start (16.10)."""
    message = _make_message(uid=42)
    state = _FakeState()
    notice = AsyncMock()
    with (
        _patch_mode(),
        _patch_scope(_existing_channel_session()),
        patch("src.bots.telegram._send_everywhere_notice", notice),
        _patch_locale("ru"),
    ):
        await cmd_start(message, state, None)
    notice.assert_not_awaited()
