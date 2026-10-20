"""Issue #634: a long bot reply (verdict/checklist/draft) must not go to Telegram above 4096.

Assertions — literals: the 4096 limit, the footer prefix, the pairing of the <b>/<i> tags.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.bots.bid_verdict_service import _render_bid_verdict
from src.bots.telegram import on_bid_verdict

_TENDER_ID = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_PROFILE_ID = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_CHAT_ID = 123456789
_TG_LIMIT = 4096


def _long_output() -> dict:
    """The model reply ~9000 characters: 90 items of “pros” of ~100 characters each."""
    pros = [f"Пункт {i:03d}: " + "а" * 88 for i in range(90)]
    return {"verdict": "caution", "score": 55, "pros": pros, "cons": ["риск"]}


def _count_tag(text: str, name: str) -> tuple[int, int]:
    opened = len(re.findall(rf"<{name}>", text))
    closed = len(re.findall(rf"</{name}>", text))
    return opened, closed


def _make_callback() -> AsyncMock:
    cb = AsyncMock()
    cb.data = f"ai:bid:{_TENDER_ID}"
    cb.answer = AsyncMock()
    cb.message = AsyncMock()
    cb.message.chat = SimpleNamespace(id=_CHAT_ID)
    cb.message.answer = AsyncMock()
    return cb


def _fake_scope(session):
    @asynccontextmanager
    async def _scope():
        yield session

    return _scope


@pytest.mark.asyncio
async def test_long_bid_verdict_is_split_under_telegram_limit():
    """A 9000-character verdict → all the sent messages ≤4096, the markup is paired."""
    from src.bots.ai_gate import GateDecision

    html = _render_bid_verdict("Тендер", _long_output(), "ru")
    assert len(html) > 9000  # the fixture is really long

    session = AsyncMock()
    profile = SimpleNamespace(id=_PROFILE_ID)
    result_obj = SimpleNamespace(status="success", html=html)
    gate_ok = GateDecision(status="ok", tenant_id="t1", tier="pro")

    with (
        patch("src.bots.telegram.session_scope", _fake_scope(session)),
        patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value="ru")),
        patch("src.bots.ai_gate.ai_gate", AsyncMock(return_value=gate_ok)),
        patch(
            "src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="tenant-1")
        ),
        patch("src.bots.telegram._latest_profile", AsyncMock(return_value=profile)),
        patch(
            "src.bots.bid_verdict_service.run_bid_verdict",
            AsyncMock(return_value=result_obj),
        ),
    ):
        cb = _make_callback()
        await on_bid_verdict(cb)

    sent = [c.args[0] for c in cb.message.answer.await_args_list]
    assert len(sent) >= 3
    for chunk in sent:
        assert len(chunk) <= _TG_LIMIT
        assert _count_tag(chunk, "b")[0] == _count_tag(chunk, "b")[1]
        assert _count_tag(chunk, "i")[0] == _count_tag(chunk, "i")[1]
    # the last piece carries the note and the link to the full analysis in the cabinet
    assert "полный разбор в кабинете" in sent[-1]
    assert (
        f"https://app.nab.rs/app/tenders/{_TENDER_ID}?profile_id={_PROFILE_ID}"
        in sent[-1]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "service_path", "data_prefix"),
    [
        (
            "on_compliance_checklist",
            "src.bots.compliance_checklist_service.run_compliance_checklist",
            "ai:chk:",
        ),
        (
            "on_draft_application",
            "src.bots.draft_service.run_draft_application",
            "ai:drf:",
        ),
    ],
)
async def test_checklist_and_draft_footer_link_carries_profile_id(
    handler_name, service_path, data_prefix
):
    """Checklist and draft: the footer link carries the profile_id of the tenant’s last profile."""
    from src.bots import telegram
    from src.bots.ai_gate import GateDecision

    html = _render_bid_verdict("Тендер", _long_output(), "ru")
    session = AsyncMock()
    profile = SimpleNamespace(id=_PROFILE_ID)
    result_obj = SimpleNamespace(status="success", html=html)
    gate_ok = GateDecision(status="ok", tenant_id="t1", tier="pro")

    with (
        patch("src.bots.telegram.session_scope", _fake_scope(session)),
        patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value="ru")),
        patch("src.bots.ai_gate.ai_gate", AsyncMock(return_value=gate_ok)),
        patch(
            "src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="tenant-1")
        ),
        patch("src.bots.telegram._latest_profile", AsyncMock(return_value=profile)),
        patch(service_path, AsyncMock(return_value=result_obj)),
    ):
        cb = _make_callback()
        cb.data = f"{data_prefix}{_TENDER_ID}"
        await getattr(telegram, handler_name)(cb)

    sent = [c.args[0] for c in cb.message.answer.await_args_list]
    assert len(sent) >= 3
    assert (
        f"https://app.nab.rs/app/tenders/{_TENDER_ID}?profile_id={_PROFILE_ID}"
        in sent[-1]
    )


@pytest.mark.asyncio
async def test_send_failure_in_middle_chunk_does_not_drop_tail():
    """A failure to send one piece does not lose the others: the tail with the link goes out anyway."""
    from src.bots.ai_gate import GateDecision

    html = _render_bid_verdict("Тендер", _long_output(), "ru")
    session = AsyncMock()
    profile = SimpleNamespace(id=_PROFILE_ID)
    result_obj = SimpleNamespace(status="success", html=html)
    gate_ok = GateDecision(status="ok", tenant_id="t1", tier="pro")

    attempts: list[str] = []

    async def _flaky_answer(text, **_kwargs):
        attempts.append(text)
        if len(attempts) == 2:
            raise RuntimeError("telegram hiccup")

    with (
        patch("src.bots.telegram.session_scope", _fake_scope(session)),
        patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value="ru")),
        patch("src.bots.ai_gate.ai_gate", AsyncMock(return_value=gate_ok)),
        patch(
            "src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="tenant-1")
        ),
        patch("src.bots.telegram._latest_profile", AsyncMock(return_value=profile)),
        patch(
            "src.bots.bid_verdict_service.run_bid_verdict",
            AsyncMock(return_value=result_obj),
        ),
    ):
        cb = _make_callback()
        cb.message.answer = AsyncMock(side_effect=_flaky_answer)
        await on_bid_verdict(cb)

    assert len(attempts) >= 3
    assert "полный разбор в кабинете" in attempts[-1]
