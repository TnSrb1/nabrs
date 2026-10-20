"""Tests of the buttons of the tender status funnel in the Telegram bot (a port of v6 tracker_bot).

The network and the DB are not touched: the keyboards are pure constructors; the handler :func:`on_tender_status` is run with a mocked ``session_scope`` and mocked service functions (``resolve_tenant_id``/``set_tender_status``). We check the assembly of the 3 keyboards, the mapping :func:`keyboard_for_status` (the terminal → None), the parsing of ``st:{status}:{tender_id}`` (a UUID with hyphens stays whole) and the update of the “Status: …” line.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bots.telegram import (
    STATUS_LABELS,
    _apply_status_line,
    _kb_active,
    _kb_new,
    _kb_submitted,
    build_tender_keyboard,
    keyboard_for_status,
    on_tender_status,
)

_TID = "11111111-1111-1111-1111-111111111111"


def _datas(markup) -> list[str]:
    """A flat list of the callback_data of all the buttons of the keyboard."""
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def _texts(markup) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


# --------------------------------------------------------------------------- #
# Assembly of 3 keyboards by stage.
# --------------------------------------------------------------------------- #


def test_kb_new_three_buttons_one_row():
    kb = _kb_new(_TID)
    # one row, three buttons
    assert len(kb.inline_keyboard) == 1
    assert len(kb.inline_keyboard[0]) == 3
    assert _datas(kb) == [
        f"st:active:{_TID}",
        f"st:watching:{_TID}",
        f"st:skipped:{_TID}",
    ]
    assert _texts(kb) == ["✅ Участвую", "👀 Изучить", "❌ Пропустить"]


def test_kb_active_submit_or_decline():
    kb = _kb_active(_TID)
    assert _datas(kb) == [f"st:submitted:{_TID}", f"st:skipped:{_TID}"]
    assert _texts(kb) == ["📨 Подал заявку", "❌ Отказаться"]


def test_kb_submitted_won_or_lost():
    kb = _kb_submitted(_TID)
    assert _datas(kb) == [f"st:won:{_TID}", f"st:lost:{_TID}"]
    assert _texts(kb) == ["🏆 Выиграли", "😔 Проиграли"]


# --------------------------------------------------------------------------- #
# keyboard_for_status: the mapping stage → keyboard (terminal → None).
# --------------------------------------------------------------------------- #


def test_keyboard_for_status_new_and_watching_same_as_new():
    for status in ("new", "watching"):
        kb = keyboard_for_status(status, _TID)
        assert kb is not None
        assert _datas(kb) == [
            f"st:active:{_TID}",
            f"st:watching:{_TID}",
            f"st:skipped:{_TID}",
        ]


def test_keyboard_for_status_active_and_submitted():
    assert _datas(keyboard_for_status("active", _TID)) == [
        f"st:submitted:{_TID}",
        f"st:skipped:{_TID}",
    ]
    assert _datas(keyboard_for_status("submitted", _TID)) == [
        f"st:won:{_TID}",
        f"st:lost:{_TID}",
    ]


@pytest.mark.parametrize("status", ["won", "lost", "skipped"])
def test_keyboard_for_status_terminal_is_none(status):
    assert keyboard_for_status(status, _TID) is None


def test_build_tender_keyboard_default_new():
    kb = build_tender_keyboard(_TID)
    assert kb is not None
    assert _datas(kb) == [
        f"st:active:{_TID}",
        f"st:watching:{_TID}",
        f"st:skipped:{_TID}",
    ]


def test_build_tender_keyboard_terminal_none():
    assert build_tender_keyboard(_TID, status="won") is None


def test_status_labels_cover_seven_statuses():
    assert set(STATUS_LABELS) == {
        "new",
        "watching",
        "active",
        "submitted",
        "won",
        "lost",
        "skipped",
    }


# --------------------------------------------------------------------------- #
# _apply_status_line: cut out the previous status line, append the new one.
# --------------------------------------------------------------------------- #


def test_apply_status_line_appends_when_absent():
    text = "<b>Тендер</b>\n\n💰 1 000 EUR"
    out = _apply_status_line(text, "👀 Изучаю")
    assert "<b>Тендер</b>" in out
    assert out.rstrip().endswith("Статус: 👀 Изучаю")
    assert out.count("Статус:") == 1


def test_apply_status_line_replaces_existing():
    text = "<b>Тендер</b>\n\n💰 1 000 EUR\n\nСтатус: 👀 Изучаю"
    out = _apply_status_line(text, "✅ Участвую")
    assert out.count("Статус:") == 1
    assert out.rstrip().endswith("Статус: ✅ Участвую")
    assert "👀 Изучаю" not in out


def test_apply_status_line_idempotent_single_line():
    text = "<b>Тендер</b>"
    once = _apply_status_line(text, "✅ Участвую")
    twice = _apply_status_line(once, "✅ Участвую")
    assert twice.count("Статус:") == 1


# --------------------------------------------------------------------------- #
# The handler on_tender_status: parsing + UPSERT call + redraw.
# --------------------------------------------------------------------------- #


def _make_callback(data: str, *, chat_id: int = 123456789, text: str | None = None):
    cb = AsyncMock()
    cb.data = data
    cb.answer = AsyncMock()
    msg = AsyncMock()
    msg.chat = SimpleNamespace(id=chat_id)
    body = text if text is not None else "<b>Тендер</b>\n\n💰 1 000 EUR"
    msg.text = body
    msg.html_text = body
    msg.edit_text = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    cb.message = msg
    return cb


def _patch_scope_and_services(*, tenant_id="tenant-1"):
    """Patches session_scope + the bot’s service functions. Returns (scope, set_mock)."""
    session = AsyncMock()
    # is_tenant_frozen calls (await session.execute(...)).first() — .first() is synchronous.
    # AsyncMock by default makes all the methods async, which gives a coroutine instead of a row.
    # Decision: execute returns a MagicMock with a synchronous .first() → access_until=None (not frozen).
    _result = MagicMock()
    _result.first.return_value = SimpleNamespace(access_until=None)
    session.execute.return_value = _result

    @asynccontextmanager
    async def _scope():
        yield session

    resolve_mock = AsyncMock(return_value=tenant_id)
    set_mock = AsyncMock()
    patches = [
        patch("src.bots.telegram.session_scope", _scope),
        patch("src.bots.telegram.resolve_tenant_id", resolve_mock),
        patch("src.bots.telegram.set_tender_status", set_mock),
        patch("src.bots.telegram.get_tender_status", AsyncMock(return_value=None)),
    ]
    return patches, resolve_mock, set_mock


@pytest.mark.asyncio
async def test_handler_parses_callback_and_upserts():
    cb = _make_callback(f"st:active:{_TID}")
    patches, _resolve, set_mock = _patch_scope_and_services(tenant_id="tenant-1")
    with patches[0], patches[1], patches[2], patches[3]:
        await on_tender_status(cb)
    # UUID with hyphens is not split by split(":", 2)
    set_mock.assert_awaited_once_with(
        # session — the first positional (the mock), we check the named ones
        set_mock.await_args.args[0],
        tenant_id="tenant-1",
        tender_id=_TID,
        status="active",
    )
    # the toast with the label of the new status
    cb.answer.assert_awaited_with("✅ Участвую")
    # the redraw: the text with the status line + the keyboard of the stage 'active'
    edit_text = cb.message.edit_text.await_args
    assert "Статус: ✅ Участвую" in edit_text.args[0]
    markup = edit_text.kwargs["reply_markup"]
    assert _datas(markup) == [f"st:submitted:{_TID}", f"st:skipped:{_TID}"]


@pytest.mark.asyncio
async def test_handler_terminal_status_removes_keyboard():
    cb = _make_callback(f"st:won:{_TID}")
    patches, _resolve, set_mock = _patch_scope_and_services()
    with patches[0], patches[1], patches[2], patches[3]:
        await on_tender_status(cb)
    set_mock.assert_awaited_once()
    assert set_mock.await_args.kwargs["status"] == "won"
    # a terminal status → reply_markup=None (no buttons)
    assert cb.message.edit_text.await_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_handler_no_binding_hints_start():
    cb = _make_callback(f"st:active:{_TID}")
    patches, _resolve, set_mock = _patch_scope_and_services(tenant_id=None)
    with patches[0], patches[1], patches[2], patches[3]:
        await on_tender_status(cb)
    # there is no binding — the binding path is through the cabinet, we write and redraw nothing
    cb.answer.assert_awaited_with(
        "Чат не привязан: кабинет → Каналы → Подключить Telegram", show_alert=True
    )
    set_mock.assert_not_awaited()
    cb.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_malformed_callback_ignored():
    cb = _make_callback("st:active")  # no tender_id
    patches, _resolve, set_mock = _patch_scope_and_services()
    with patches[0], patches[1], patches[2], patches[3]:
        await on_tender_status(cb)
    set_mock.assert_not_awaited()
    cb.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_handler_unknown_status_ignored():
    cb = _make_callback(f"st:bogus:{_TID}")
    patches, _resolve, set_mock = _patch_scope_and_services()
    with patches[0], patches[1], patches[2], patches[3]:
        await on_tender_status(cb)
    set_mock.assert_not_awaited()
    cb.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_handler_edit_failure_falls_back_to_markup_only():
    cb = _make_callback(f"st:active:{_TID}")
    cb.message.edit_text = AsyncMock(
        side_effect=RuntimeError("message is not modified")
    )
    patches, _resolve, set_mock = _patch_scope_and_services()
    with patches[0], patches[1], patches[2], patches[3]:
        await on_tender_status(cb)
    # the text could not be updated → we try at least the keyboard
    set_mock.assert_awaited_once()
    cb.message.edit_reply_markup.assert_awaited_once()
