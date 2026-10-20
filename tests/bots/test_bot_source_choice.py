"""SRC-0b (R6): ``/izvori`` — the subscriber chooses the sources in the bot.

(iv) pressing the toggle changes ``enabled_source_keys`` and ``source_opt_in`` EXACTLY
for the chosen profile (the tenant has two of them — the production fact of 30.09: 1 tenant out of 6).
The clock is frozen by substituting ``datetime`` in the bot module; the expectations are literals.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.types import Message

from src.bots import source_choice
from src.bots import telegram as tg
from src.bots.i18n import BOT

FROZEN = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
OLD = "2026-09-01T10:00:00+00:00"


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return FROZEN


def _row(key, name, countries, enabled=True):
    return SimpleNamespace(
        source_key=key,
        display_name=name,
        country_codes=countries,
        available_for_tiers=None,
        enabled=enabled,
    )


JN = _row("jnportal", "Portal javnih nabavki (UJN)", ["RS"])
TED = _row("ted_balkan", "TED (EU)", ["RS", "ME"])


class _SourcesSession:
    """Session: the ``sources`` registry on any SELECT from it; counts the commits."""

    def __init__(self, rows):
        self.rows = rows
        self.commits = 0

    async def execute(self, stmt, params=None):
        text = str(stmt)
        assert "FROM sources" in text, text
        if "ANY(" in text:  # the choice: the enabled ones + the ones already in the profile
            keys = set((params or {}).get("keys") or [])
            rows = [r for r in self.rows if r.enabled or r.source_key in keys]
        else:  # list_enabled_sources
            rows = [r for r in self.rows if r.enabled]
        result = MagicMock()
        result.all.return_value = rows
        return result

    async def commit(self):
        self.commits += 1


def _profile(name, keys, opt_in):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        is_active=True,
        enabled_source_keys=list(keys),
        source_opt_in=dict(opt_in),
    )


@asynccontextmanager
async def _scope(session):
    yield session


def _callback(data, chat_id=42):
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=chat_id)
    msg.edit_text = AsyncMock()
    cb = MagicMock()
    cb.data = data
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


def _patch_handler_env(monkeypatch, session, profiles, locale="sr"):
    by_id = {str(p.id): p for p in profiles}

    async def _load(sess, tenant_id, profile_id):
        return by_id.get(str(profile_id))

    async def _list(sess, tenant_id):
        return list(profiles)

    monkeypatch.setattr(tg, "session_scope", lambda: _scope(session))
    monkeypatch.setattr(tg, "resolve_bot_locale", AsyncMock(return_value=locale))
    monkeypatch.setattr(tg, "resolve_tenant_id", AsyncMock(return_value="t-1"))
    monkeypatch.setattr(tg, "_list_profiles", _list)
    monkeypatch.setattr(source_choice, "load_profile", _load)
    # The real registry (conftest substitutes it with the production fact — here two portals).
    from src.domain import sources as domain_sources

    monkeypatch.setattr(
        tg, "list_sources_for_selection", domain_sources.list_sources_for_selection
    )
    monkeypatch.setattr(tg, "datetime", _FrozenDatetime)


# --------------------------------------------------------------------------- #
# (iv) the toggle changes exactly the chosen profile
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_toggle_changes_only_chosen_profile(monkeypatch):
    p1 = _profile("Gradnja", ["jnportal"], {"jnportal": OLD})
    p2 = _profile("IT", ["jnportal"], {"jnportal": OLD})
    session = _SourcesSession([JN, TED])
    _patch_handler_env(monkeypatch, session, [p1, p2])

    # Index 1 = ted_balkan (the order by source_key: jnportal, ted_balkan).
    data = source_choice.toggle_callback(p1.id, 1, "ted_balkan")
    cb = _callback(data)
    await tg.on_izvori_toggle(cb)

    assert p1.enabled_source_keys == ["jnportal", "ted_balkan"]
    assert p1.source_opt_in == {
        "jnportal": "2026-09-01T10:00:00+00:00",
        "ted_balkan": "2026-10-01T09:30:00+00:00",
    }
    assert p2.enabled_source_keys == ["jnportal"]
    assert p2.source_opt_in == {"jnportal": "2026-09-01T10:00:00+00:00"}
    assert session.commits == 1
    assert cb.answer.await_args.args[0] == "✅ Uključeno: TED (EU)"
    # Redraw: ted_balkan is now marked.
    text = cb.message.edit_text.await_args.args[0]
    assert "☑ TED (EU) — Srbija, Crna Gora" in text


@pytest.mark.asyncio
async def test_toggle_off_removes_opt_in(monkeypatch):
    p1 = _profile(
        "Gradnja", ["jnportal", "ted_balkan"], {"jnportal": OLD, "ted_balkan": OLD}
    )
    session = _SourcesSession([JN, TED])
    _patch_handler_env(monkeypatch, session, [p1])
    cb = _callback(source_choice.toggle_callback(p1.id, 0, "jnportal"))
    await tg.on_izvori_toggle(cb)
    assert p1.enabled_source_keys == ["ted_balkan"]
    assert p1.source_opt_in == {"ted_balkan": "2026-09-01T10:00:00+00:00"}
    assert cb.answer.await_args.args[0] == "Isključeno: Portal javnih nabavki (UJN)"


@pytest.mark.asyncio
async def test_toggle_last_source_refused(monkeypatch):
    p1 = _profile("Gradnja", ["jnportal"], {"jnportal": OLD})
    session = _SourcesSession([JN, TED])
    _patch_handler_env(monkeypatch, session, [p1], locale="ru")
    cb = _callback(source_choice.toggle_callback(p1.id, 0, "jnportal"))
    await tg.on_izvori_toggle(cb)
    assert p1.enabled_source_keys == ["jnportal"]
    assert p1.source_opt_in == {"jnportal": OLD}
    assert session.commits == 0
    assert cb.answer.await_args.args[0].startswith(
        "Нельзя выключить последний источник"
    )
    assert cb.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_stale_button_does_not_toggle_other_source(monkeypatch):
    """The button was drawn when another key was under index 1 (the list had shifted):
    the tag does not match → nothing is written, we ask for /izvori again."""
    p1 = _profile("Gradnja", ["jnportal"], {"jnportal": OLD})
    session = _SourcesSession([JN, TED])
    _patch_handler_env(monkeypatch, session, [p1], locale="en")
    cb = _callback(source_choice.toggle_callback(p1.id, 1, "other_portal"))
    await tg.on_izvori_toggle(cb)
    assert p1.enabled_source_keys == ["jnportal"]
    assert session.commits == 0
    assert cb.answer.await_args.args[0] == (
        "The source list has changed — send /izvori again."
    )


@pytest.mark.asyncio
async def test_foreign_profile_id_not_found(monkeypatch):
    """A foreign/non-existent UUID in the callback — the profile is not found, no record."""
    p1 = _profile("Gradnja", ["jnportal"], {"jnportal": OLD})
    session = _SourcesSession([JN, TED])
    _patch_handler_env(monkeypatch, session, [p1])
    cb = _callback(source_choice.toggle_callback(uuid4(), 1, "ted_balkan"))
    await tg.on_izvori_toggle(cb)
    assert p1.enabled_source_keys == ["jnportal"]
    assert session.commits == 0
    assert cb.answer.await_args.kwargs["show_alert"] is True


def test_callback_data_fits_telegram_limit():
    pid = uuid4()
    for data in (
        source_choice.toggle_callback(pid, 99, "some_very_long_source_key_name"),
        source_choice.profile_callback(pid),
    ):
        assert len(data.encode("utf-8")) <= 64, data


# --------------------------------------------------------------------------- #
# /izvori: one profile → toggles at once; several → a choice of the profile
# --------------------------------------------------------------------------- #
def _message(uid=42):
    m = AsyncMock()
    m.from_user = SimpleNamespace(id=uid)
    m.answer = AsyncMock()
    return m


@pytest.mark.asyncio
async def test_cmd_izvori_single_profile_shows_toggles(monkeypatch):
    paused = _row("old_portal", "Stari portal", ["ME"], enabled=False)
    p1 = _profile("Gradnja", ["jnportal", "old_portal"], {})
    _patch_handler_env(monkeypatch, _SourcesSession([JN, TED, paused]), [p1])
    msg = _message()
    await tg.cmd_izvori(msg)
    text = msg.answer.await_args.args[0]
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert "Izvori šablona „Gradnja“" in text
    assert "☑ Portal javnih nabavki (UJN) — Srbija" in text
    assert "☐ TED (EU) — Srbija, Crna Gora" in text
    assert "☑ Stari portal (privremeno isključen) — Crna Gora" in text
    labels = [row[0].text for row in kb.inline_keyboard]
    assert labels == [
        "☑ Portal javnih nabavki (UJN)",
        "☑ Stari portal (privremeno isključen)",
        "☐ TED (EU)",
    ]


@pytest.mark.asyncio
async def test_cmd_izvori_several_profiles_asks_which(monkeypatch):
    p1 = _profile("Gradnja", ["jnportal"], {})
    p2 = _profile("IT", ["jnportal"], {})
    _patch_handler_env(monkeypatch, _SourcesSession([JN]), [p1, p2], locale="ru")
    msg = _message()
    await tg.cmd_izvori(msg)
    assert msg.answer.await_args.args[0] == "🗂 <b>Источники</b>\n\nВыберите шаблон:"
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert [row[0].text for row in kb.inline_keyboard] == ["Gradnja", "IT"]
    assert kb.inline_keyboard[0][0].callback_data == f"src:p:{p1.id}"


def test_izvori_in_menu_all_languages():
    names = {n for n, _ in tg._PUBLIC_COMMANDS}
    assert "izvori" in names
    assert (
        tg._COMMAND_DESCRIPTIONS["sr"]["izvori"] == "Izvori tendera (portali) šablona"
    )


def test_onboarding_final_message_points_to_izvori():
    assert "Izvore menjate komandom /izvori." in BOT["sr"]["onb_saved"]
    assert "Источники (порталы) меняются командой /izvori." in BOT["ru"]["onb_saved"]
    assert "Change sources (portals) with /izvori." in BOT["en"]["onb_saved"]


# --------------------------------------------------------------------------- #
# /profile shows the sources by labels
# --------------------------------------------------------------------------- #
def test_render_profile_lists_sources_with_paused_marker():
    from src.domain.sources import SourceInfo

    profile = SimpleNamespace(
        name="Gradnja",
        is_active=True,
        match_all=True,
        keywords_include=[],
        regions=[],
        enabled_source_keys=["jnportal", "old_portal"],
    )
    sources = {
        "jnportal": SourceInfo(
            "jnportal", "Portal javnih nabavki (UJN)", ("RS",), None
        ),
        "old_portal": SourceInfo(
            "old_portal", "Stari portal", ("ME",), None, enabled=False
        ),
    }
    data = {
        "tenant": SimpleNamespace(tier="pro", access_until=None),
        "user": SimpleNamespace(email="a@b.rs"),
    }
    text = tg._render_profile(data, [profile], "sr", sources=sources)
    assert (
        "Izvori: Portal javnih nabavki (UJN), Stari portal (privremeno isključen)"
        in text
    )
