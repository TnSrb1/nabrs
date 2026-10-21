"""Guards of the i18n of the Telegram bot (Phase 2c): the AI buttons of the card + analytics + the referral.

The scope 2c — the chrome of the on-demand handlers: ``ai:deep/bid/chk/drf`` (the deep analysis, the verdict, the checklist, the draft), ``an:cust/comp/repr`` (the analytics of the customer/competitor/re-procurement), the CSV export, “🔔 Follow”/unsubscribe and the notice to the INVITER about the referral bonus. The data (the body of the AI analysis, the name/PIB of the winner, the CSV) are NOT localised — only the chrome (the progress toast, the Pro+ gates, the error texts, the confirmations).

The method (as in 2a/2b): under en/sr there are NO Russian letters ``U+044B U+044D U+0449 U+044A U+0451 U+0439`` in the chrome (they are absent both in Serbian Latin and in English), under ru the previous Russian canon. The test data are chosen in Latin, so that any Russian letter = untranslated chrome.

Two AXES (the project rules): ``users.locale`` (the chrome, the axis of this test) ≠
``profiles.target_language`` (the body of the AI analysis). The notice to the inviter is a special case: the recipient is ANOTHER user, the chrome by HIS ``users.locale``.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.bots import telegram as tg
from src.bots.i18n import bot_t

RU_ONLY = re.compile(r"[ыэщъёйЫЭЩЪЁЙ]")

_TENDER = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #
def _callback(data: str = "", chat_id: int = 555) -> AsyncMock:
    cb = AsyncMock()
    cb.data = data
    cb.message = AsyncMock()
    cb.message.chat = SimpleNamespace(id=chat_id)
    cb.message.answer = AsyncMock()
    cb.message.answer_document = AsyncMock()
    cb.message.edit_reply_markup = AsyncMock()
    cb.answer = AsyncMock()
    return cb


@asynccontextmanager
async def _fake_scope():
    yield AsyncMock()


def _patch_scope():
    return patch("src.bots.telegram.session_scope", _fake_scope)


def _patch_locale(locale: str):
    return patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value=locale))


def _patch_gate_ok():
    """The gate (#513) rings as the FIRST action in the four AI handlers — these tests

    check the localisation of the SERVICE branches (gate_pro/frozen/limit_exhausted/...), therefore the gate itself must let through (``ok``), and not decide for the service.
    Separate tests for the interception of ``limit_exhausted`` BY the gate itself — in
    ``test_ai_gate_limit_visible.py``.
    """
    from src.bots.ai_gate import GateDecision

    return patch(
        "src.bots.ai_gate.ai_gate",
        AsyncMock(return_value=GateDecision(status="ok", tenant_id="t1", tier="pro")),
    )


def _toast(cb: AsyncMock) -> str:
    return cb.answer.await_args.args[0]


def _msg(cb: AsyncMock) -> str:
    return cb.message.answer.await_args.args[0]


# --------------------------------------------------------------------------- #
# ai:deep — all the status branches are localised (the progress + the gate/limit/error).
# --------------------------------------------------------------------------- #
_DEEP_STATUSES = [
    "gate_pro",
    "frozen",
    "no_account",
    "limit_exhausted",
    "model_busy",
    "x",
]


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize("status", _DEEP_STATUSES)
@pytest.mark.asyncio
async def test_deep_branches_localized(locale, status):
    cb = _callback(f"ai:deep:{_TENDER}")
    result = SimpleNamespace(status=status, html_chunks=None)
    with (
        _patch_scope(),
        _patch_locale(locale),
        _patch_gate_ok(),
        patch(
            "src.bots.deep_analysis_service.run_deep_analysis",
            AsyncMock(return_value=result),
        ),
    ):
        await tg.on_deep_analysis(cb)
    assert not RU_ONLY.search(_toast(cb)), f"progress toast under {locale}"
    assert not RU_ONLY.search(_msg(cb)), f"branch {status} under {locale}: {_msg(cb)!r}"


@pytest.mark.asyncio
async def test_deep_gate_ru_canon_preserved():
    cb = _callback(f"ai:deep:{_TENDER}")
    result = SimpleNamespace(status="gate_pro", html_chunks=None)
    with (
        _patch_scope(),
        _patch_locale("ru"),
        _patch_gate_ok(),
        patch(
            "src.bots.deep_analysis_service.run_deep_analysis",
            AsyncMock(return_value=result),
        ),
    ):
        await tg.on_deep_analysis(cb)
    assert "Глубокий анализ" in _msg(cb)
    assert RU_ONLY.search(_toast(cb))  # “Starting” contains U+0439


# --------------------------------------------------------------------------- #
# ai:bid — no_account/no_profile + gate; the profile is resolved in the handler.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize("status", ["gate_pro", "limit_exhausted", "model_busy", "x"])
@pytest.mark.asyncio
async def test_bid_result_branches_localized(locale, status):
    cb = _callback(f"ai:bid:{_TENDER}")
    result = SimpleNamespace(status=status, html=None)
    with (
        _patch_scope(),
        _patch_locale(locale),
        _patch_gate_ok(),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="t1")),
        patch(
            "src.bots.telegram._latest_profile",
            AsyncMock(return_value=SimpleNamespace(id="p1")),
        ),
        patch(
            "src.bots.bid_verdict_service.run_bid_verdict",
            AsyncMock(return_value=result),
        ),
    ):
        await tg.on_bid_verdict(cb)
    assert not RU_ONLY.search(_toast(cb))
    assert not RU_ONLY.search(_msg(cb)), f"{status} under {locale}: {_msg(cb)!r}"


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_bid_no_profile_localized(locale):
    cb = _callback(f"ai:bid:{_TENDER}")
    with (
        _patch_scope(),
        _patch_locale(locale),
        _patch_gate_ok(),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="t1")),
        patch("src.bots.telegram._latest_profile", AsyncMock(return_value=None)),
    ):
        await tg.on_bid_verdict(cb)
    assert not RU_ONLY.search(_msg(cb))


def test_bid_no_profile_ru_canon():
    # The catalog key directly: the ru canon is kept (the parity test — in 2a).
    assert "Шаблон не найден" in bot_t("ru", "bid_no_profile")


# --------------------------------------------------------------------------- #
# ai:chk / ai:drf — gate + progress.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize(
    "handler,service,data",
    [
        (
            "on_compliance_checklist",
            "src.bots.compliance_checklist_service.run_compliance_checklist",
            f"ai:chk:{_TENDER}",
        ),
        (
            "on_draft_application",
            "src.bots.draft_service.run_draft_application",
            f"ai:drf:{_TENDER}",
        ),
    ],
)
@pytest.mark.asyncio
async def test_chk_drf_gate_localized(locale, handler, service, data):
    cb = _callback(data)
    result = SimpleNamespace(status="gate_pro", html=None)
    with (
        _patch_scope(),
        _patch_locale(locale),
        _patch_gate_ok(),
        patch(service, AsyncMock(return_value=result)),
    ):
        await getattr(tg, handler)(cb)
    assert not RU_ONLY.search(_toast(cb))
    assert not RU_ONLY.search(_msg(cb))


# --------------------------------------------------------------------------- #
# an:cust / an:comp / an:repr — gate + no_customer + no_data.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize(
    "status", ["gate_pro", "no_customer", "no_data", "frozen", "x"]
)
@pytest.mark.parametrize(
    "handler,service,data",
    [
        (
            "on_customer_analytics",
            "src.bots.awards_analytics_service.run_customer_analytics",
            f"an:cust:{_TENDER}",
        ),
        (
            "on_competitor_analytics",
            "src.bots.awards_analytics_service.run_competitor_analytics",
            f"an:comp:{_TENDER}",
        ),
    ],
)
@pytest.mark.asyncio
async def test_analytics_branches_localized(locale, status, handler, service, data):
    cb = _callback(data)
    result = SimpleNamespace(status=status, html=None)
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch(service, AsyncMock(return_value=result)),
    ):
        await getattr(tg, handler)(cb)
    assert not RU_ONLY.search(_toast(cb))
    assert not RU_ONLY.search(_msg(cb)), f"{handler}/{status} under {locale}"


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize("status", ["gate_pro", "no_data", "no_customer"])
@pytest.mark.asyncio
async def test_reprocurement_branches_localized(locale, status):
    cb = _callback(f"an:repr:{_TENDER}")
    result = SimpleNamespace(status=status, html=None)
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch(
            "src.bots.awards_analytics_service.run_reprocurement_forecast",
            AsyncMock(return_value=result),
        ),
    ):
        await tg.on_reprocurement_analytics(cb)
    assert not RU_ONLY.search(_toast(cb))
    assert not RU_ONLY.search(_msg(cb))


# --------------------------------------------------------------------------- #
# CSV export — progress, the document caption and the error branches (_send_export_error).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_customer_export_success_caption_localized(locale):
    cb = _callback(f"an:exc:{_TENDER}")
    result = SimpleNamespace(status="success", content=b"a;b", filename="x.csv")
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch(
            "src.bots.awards_analytics_service.run_customer_export",
            AsyncMock(return_value=result),
        ),
    ):
        await tg.on_customer_export(cb)
    assert not RU_ONLY.search(_toast(cb))
    caption = cb.message.answer_document.await_args.kwargs["caption"]
    assert not RU_ONLY.search(caption)


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize("kind", ["customer", "competitor"])
@pytest.mark.parametrize(
    "status", ["gate_pro", "no_customer", "no_data", "frozen", "x"]
)
@pytest.mark.asyncio
async def test_send_export_error_localized(locale, kind, status):
    cb = _callback()
    await tg._send_export_error(cb, status, kind, locale)
    assert not RU_ONLY.search(_msg(cb)), f"{kind}/{status} under {locale}"


# --------------------------------------------------------------------------- #
# “🔔 Follow” + unsubscribe.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_watch_winner_success_localized(locale):
    cb = _callback(f"an:wch:{_TENDER}")
    # The name/PIB — Latin (data); the chrome around them must not be Russian.
    result = SimpleNamespace(
        status="success", winner_name="Firma DOO", winner_pib="123456789"
    )
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch(
            "src.bots.award_watch_service.run_watch_winner",
            AsyncMock(return_value=result),
        ),
    ):
        await tg.on_watch_winner(cb)
    assert not RU_ONLY.search(_toast(cb))
    assert not RU_ONLY.search(_msg(cb))
    assert "Firma DOO" in _msg(cb)  # data preserved


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize("status", ["gate_pro", "no_data", "frozen", "x"])
@pytest.mark.asyncio
async def test_watch_winner_error_branches_localized(locale, status):
    cb = _callback(f"an:wch:{_TENDER}")
    result = SimpleNamespace(status=status, winner_name=None, winner_pib="")
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch(
            "src.bots.award_watch_service.run_watch_winner",
            AsyncMock(return_value=result),
        ),
    ):
        await tg.on_watch_winner(cb)
    assert not RU_ONLY.search(_msg(cb))


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize("status", ["success", "no_account", "x"])
@pytest.mark.asyncio
async def test_unwatch_toast_localized(locale, status):
    cb = _callback("an:unw:42")
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch(
            "src.bots.award_watch_service.run_unwatch",
            AsyncMock(return_value=SimpleNamespace(status=status)),
        ),
    ):
        await tg.on_unwatch(cb)
    assert not RU_ONLY.search(_toast(cb)), f"{status} under {locale}"
