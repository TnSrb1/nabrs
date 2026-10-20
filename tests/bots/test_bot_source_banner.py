"""The bot: “a new source — enable in all templates” (step 4 of the brief).

The notice — one message on /start and “🗂 Sources”, the button → the same service
as the cabinet banner. The expectations — literals of the bot’s ru catalog and facts of the fixture
(two subscriptions, the source ``ted_balkan``), not via ``bot_t()``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.types import Message

from src.api.cabinet import profile_service
from src.api.cabinet.source_banner import PendingSource
from src.bots import source_choice
from src.bots import telegram as tg

FROZEN = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
OLD = "2026-09-01T10:00:00+00:00"


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return FROZEN


def _row(key, name, countries):
    return SimpleNamespace(
        source_key=key,
        display_name=name,
        country_codes=countries,
        available_for_tiers=None,
        enabled=True,
        enabled_at=None,
    )


JN = _row("jnportal", "Portal javnih nabavki (UJN)", ["RS"])
TED = _row("ted_balkan", "TED — Zapadni Balkan", ["BA", "ME", "MK", "RS"])


class _SourcesSession:
    """The ``sources`` registry on any SELECT from it; counts the commits."""

    def __init__(self):
        self.commits = 0

    async def execute(self, stmt, params=None):
        assert "FROM sources" in str(stmt), str(stmt)
        result = MagicMock()
        result.all.return_value = [JN, TED]
        return result

    async def commit(self):
        self.commits += 1


def _profile(name, keys):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        is_active=True,
        match_all=False,
        enabled_source_keys=list(keys),
        source_opt_in={k: OLD for k in keys},
        deleted_at=None,
    )


@asynccontextmanager
async def _scope(session):
    yield session


def _callback(data, chat_id=42):
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=chat_id)
    msg.edit_reply_markup = AsyncMock()
    cb = MagicMock()
    cb.data = data
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


def _patch_env(monkeypatch, session, profiles, locale="ru"):
    async def _list_svc(sess, tenant_id):
        return list(profiles)

    monkeypatch.setattr(tg, "session_scope", lambda: _scope(session))
    monkeypatch.setattr(tg, "resolve_bot_locale", AsyncMock(return_value=locale))
    monkeypatch.setattr(tg, "resolve_tenant_id", AsyncMock(return_value="t-1"))
    monkeypatch.setattr(profile_service, "list_profiles", _list_svc)
    monkeypatch.setattr(tg, "datetime", _FrozenDatetime)


# --- callback and the notice text (pure functions) --------------------------- #


def test_callback_parses_only_clean_source_keys():
    assert source_choice.parse_enable_all_callback("src:a:nis") == "nis"
    assert source_choice.parse_enable_all_callback("src:a:") is None
    assert source_choice.parse_enable_all_callback("src:a:NIS") is None
    assert source_choice.parse_enable_all_callback("src:a:nis;rm") is None
    assert source_choice.parse_enable_all_callback("src:a:" + "x" * 41) is None
    assert source_choice.parse_enable_all_callback("src:t:1:0:ab12") is None


def test_notice_text_and_button_literals_with_match_all_warning():
    pending = PendingSource(
        source_key="ted_balkan",
        label="TED — Zapadni Balkan",
        countries=("BA", "ME", "MK", "RS"),
        caps_short=(),
        enabled_at=FROZEN,
        missing_in=1,
        is_plan=False,
        has_match_all=True,
    )
    text, markup = source_choice.everywhere_notice(
        pending, profile_total=2, locale="ru"
    )
    assert text.splitlines()[0] == "🗂 <b>Новая площадка: TED — Zapadni Balkan</b>"
    assert "Не включена в 1 из 2 шаблонов." in text
    assert "Шаблон «все тендеры» — до 30 в день, лишнее не придёт." in text
    button = markup.inline_keyboard[0][0]
    assert button.text == "Включить во всех шаблонах"
    assert button.callback_data == "src:a:ted_balkan"
    assert len(button.callback_data.encode()) <= 64


def test_notice_escapes_source_name():
    pending = PendingSource(
        source_key="nis",
        label="<b>NIS</b>",
        countries=("RS",),
        caps_short=(),
        enabled_at=FROZEN,
        missing_in=1,
        is_plan=False,
        has_match_all=False,
    )
    text, _ = source_choice.everywhere_notice(pending, profile_total=1, locale="ru")
    assert "&lt;b&gt;NIS&lt;/b&gt;" in text


# --- the button handler: the same service as the cabinet --------------------------------- #


@pytest.mark.asyncio
async def test_enable_all_writes_every_template_and_answers_count(monkeypatch):
    p1 = _profile("Gradnja", ["jnportal"])
    p2 = _profile("IT", ["jnportal"])
    session = _SourcesSession()
    _patch_env(monkeypatch, session, [p1, p2])
    cb = _callback("src:a:ted_balkan")

    await tg.on_enable_all_templates(cb)

    assert p1.enabled_source_keys == ["jnportal", "ted_balkan"]
    assert p2.enabled_source_keys == ["jnportal", "ted_balkan"]
    assert p1.source_opt_in["ted_balkan"] == FROZEN.isoformat()
    assert session.commits == 1
    cb.answer.assert_awaited_once_with("✅ Включено в шаблонах: 2")
    cb.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_enable_all_refuses_unknown_source_without_writing(monkeypatch):
    p1 = _profile("Gradnja", ["jnportal"])
    session = _SourcesSession()
    _patch_env(monkeypatch, session, [p1])
    cb = _callback("src:a:nope")

    await tg.on_enable_all_templates(cb)

    assert p1.enabled_source_keys == ["jnportal"]
    assert session.commits == 0
    cb.answer.assert_awaited_once_with(
        "Эта площадка сейчас недоступна для включения.", show_alert=True
    )


@pytest.mark.asyncio
async def test_enable_all_ignores_garbage_callback(monkeypatch):
    p1 = _profile("Gradnja", ["jnportal"])
    session = _SourcesSession()
    _patch_env(monkeypatch, session, [p1])
    cb = _callback("src:a:NIS;drop")

    await tg.on_enable_all_templates(cb)

    assert p1.enabled_source_keys == ["jnportal"]
    assert session.commits == 0


def _domain_source(key, name, countries):
    from src.domain.sources import SourceInfo

    return SourceInfo(key, name, tuple(countries), None, enabled=True)


@pytest.mark.asyncio
async def test_notice_sent_once_when_source_missing_and_silent_when_included(
    monkeypatch,
):
    """/start and “🗂 Sources”: one message with the button; no missing one — silence."""
    from src.api.cabinet import source_banner

    session = _SourcesSession()
    monkeypatch.setattr(tg, "session_scope", lambda: _scope(session))
    monkeypatch.setattr(tg, "resolve_bot_locale", AsyncMock(return_value="ru"))
    monkeypatch.setattr(tg, "resolve_tenant_id", AsyncMock(return_value="t-1"))
    ted = _domain_source("ted_balkan", "TED — Zapadni Balkan", ["BA", "ME", "MK", "RS"])
    monkeypatch.setattr(
        source_banner, "load_enabled_sources", AsyncMock(return_value=[(ted, FROZEN)])
    )

    missing = [_profile("Gradnja", ["jnportal"])]
    message = AsyncMock()
    message.chat = SimpleNamespace(id=42)
    monkeypatch.setattr(tg, "_list_profiles", AsyncMock(return_value=missing))
    await tg._send_everywhere_notice(message)
    message.answer.assert_awaited_once()
    assert (
        message.answer.await_args.kwargs["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data
        == "src:a:ted_balkan"
    )

    message.answer.reset_mock()
    included = [_profile("Gradnja", ["jnportal", "ted_balkan"])]
    monkeypatch.setattr(tg, "_list_profiles", AsyncMock(return_value=included))
    await tg._send_everywhere_notice(message)
    message.answer.assert_not_awaited()
