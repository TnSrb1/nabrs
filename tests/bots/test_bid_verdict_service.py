"""Tests of the on-demand bid/no-bid verdict (VG-08).

Without a network and without a real DB: a mock session via AsyncMock. We cover:
- Gate Pro+: starter/lite → gate_pro, the model is not called.
- Frozen: access_until expired → frozen, the model is not called.
- Success: the model is called, the JSON is parsed, the HTML is non-empty.
- The cache key includes profile_id: different profiles → different keys.
- _render_bid_verdict: correct icons and structure.
- _format_profile_context: CPV/regions/keywords are packed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.client import AiClient
from src.ai.usage import _TIER_LIMITS

_TENDER_ID = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_PROFILE_ID_A = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_PROFILE_ID_B = "cccc3333-cccc-cccc-cccc-cccccccccccc"
_TENANT_ID = "dddd4444-dddd-dddd-dddd-dddddddddddd"
_ANALYSIS_ID = "eeee5555-eeee-eeee-eeee-eeeeeeeeeeee"
_CHAT_ID = 123456789

_NOW = datetime.now(timezone.utc)
_PAST = _NOW - timedelta(days=1)
_FUTURE = _NOW + timedelta(days=365)

_BID_OUTPUT = {
    "verdict": "go",
    "score": 82,
    "pros": ["CPV совпадает с профилем", "Дедлайн реалистичный"],
    "cons": ["Требуется лицензия C категории"],
}


@dataclass
class _FakeAnalysisResult:
    output: dict
    model: str = "claude-sonnet-4-6"
    tokens_input: int = 400
    tokens_output: int = 150
    tokens_cached: int = 0
    cost_usd: float = 0.003


def _make_ai_client(
    output: dict | None = None, raise_exc: Exception | None = None
) -> AiClient:
    client = MagicMock(spec=AiClient)

    async def _analyze(request):
        if raise_exc:
            raise raise_exc
        return _FakeAnalysisResult(output=output or _BID_OUTPUT)

    client.analyze = _analyze
    return client


def _session_returning(row) -> AsyncMock:
    result = MagicMock()
    result.first.return_value = row
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _make_session_seq(calls: list) -> AsyncMock:
    """Mock session with sequential responses to execute."""
    call_idx = [0]
    session = AsyncMock()

    async def _exec(sql, params=None):
        # The track record (tender_user_status) — an auxiliary query, reads .all();
        # does not take a position in the sequence, so as not to shift the calls.
        if "tender_user_status" in str(sql):
            r = MagicMock()
            r.all.return_value = []
            r.first.return_value = None
            return r
        idx = call_idx[0]
        call_idx[0] += 1
        row = calls[idx] if idx < len(calls) else None
        r = MagicMock()
        r.first.return_value = row
        return r

    session.execute = _exec
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


# --------------------------------------------------------------------------- #
#  Gate Pro+: starter → gate_pro
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gate_pro_starter_rejected():
    """starter: bid=0 → gate_pro, the model is not called."""
    from src.bots.bid_verdict_service import run_bid_verdict

    session = _session_returning(SimpleNamespace(tier="starter", access_until=_FUTURE))
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.bid_verdict_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_bid_verdict(
            session,
            tender_id=_TENDER_ID,
            profile_id=_PROFILE_ID_A,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "gate_pro"
    assert result.html == ""
    client.analyze.assert_not_called()


@pytest.mark.asyncio
async def test_gate_pro_lite_rejected():
    """lite: bid=0 → gate_pro."""
    from src.bots.bid_verdict_service import run_bid_verdict

    session = _session_returning(SimpleNamespace(tier="lite", access_until=None))
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.bid_verdict_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_bid_verdict(
            session,
            tender_id=_TENDER_ID,
            profile_id=_PROFILE_ID_A,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "gate_pro"
    client.analyze.assert_not_called()


# --------------------------------------------------------------------------- #
#  Frozen: access_until expired → frozen, the model is not called
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_frozen_returns_frozen_without_calling_model():
    """access_until expired → status='frozen', the model is NOT called, one execute."""
    from src.bots.bid_verdict_service import run_bid_verdict

    session = _session_returning(SimpleNamespace(tier="pro", access_until=_PAST))
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.bid_verdict_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_bid_verdict(
            session,
            tender_id=_TENDER_ID,
            profile_id=_PROFILE_ID_A,
            chat_id=_CHAT_ID,
            ai_client=client,
        )

    assert result.status == "frozen"
    assert result.html == ""
    client.analyze.assert_not_called()
    # Exactly one SELECT (tier + access_until), before the cache/limit/tender.
    assert session.execute.await_count == 1


# --------------------------------------------------------------------------- #
#  no_account: resolve_tenant_id returns None
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_account_when_no_tenant():
    """No chat_id link → no_account."""
    from src.bots.bid_verdict_service import run_bid_verdict

    session = AsyncMock()
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch("src.bots.bid_verdict_service.resolve_tenant_id", return_value=None):
        result = await run_bid_verdict(
            session,
            tender_id=_TENDER_ID,
            profile_id=_PROFILE_ID_A,
            chat_id=999999,
            ai_client=client,
        )

    assert result.status == "no_account"
    client.analyze.assert_not_called()


# --------------------------------------------------------------------------- #
#  Success: the model is called, the JSON is parsed, the HTML contains the verdict
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ok_valid_json_parsed_and_rendered():
    """Pro+, no cache → a model call, the HTML is non-empty, contains the verdict and the title."""
    from src.bots.bid_verdict_service import run_bid_verdict

    # The sequence of execute:
    # 0. tier/access
    # 1. cache_lookup → None (miss)
    # 2. limit_decision: pro/bid=30 → reads the counter (there is no unlimited any more)
    # 3. tender load → title/description/country
    # 4. profile load → classifications/regions/keywords
    # 5. INSERT analysis → id
    # 6. UPSERT usage → None (no return needed)
    calls = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),  # 0 tier
        None,  # 1 cache miss
        SimpleNamespace(count=0),  # 2 the usage counter of the level
        SimpleNamespace(  # 3 tender
            title="Тестовый тендер",
            description="Описание ТЗ",
            country_name="Сербия",
        ),
        SimpleNamespace(  # 4 profile
            classifications=[
                {"scheme": "CPV", "code": "45000000", "label": "Строительство"}
            ],
            regions=["Белград"],
            keywords_include={"keywords": ["строительство"]},
            ai_extra_context=None,
            target_language="ru",
        ),
        SimpleNamespace(id=_ANALYSIS_ID),  # 5 INSERT
    ]
    session = _make_session_seq(calls)

    with patch(
        "src.bots.bid_verdict_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_bid_verdict(
            session,
            tender_id=_TENDER_ID,
            profile_id=_PROFILE_ID_A,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(_BID_OUTPUT),
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "success"
    assert len(result.html) > 0
    assert "🎯" in result.html
    assert "🟢" in result.html  # go → green
    assert "82" in result.html  # score
    assert "Тестовый тендер" in result.html
    assert "CPV совпадает" in result.html  # pros item


# --------------------------------------------------------------------------- #
#  The cache key includes profile_id
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cache_key_includes_profile_id():
    """Different profile_id → different cache_lookup calls (the key per profile)."""
    from src.bots.bid_verdict_service import run_bid_verdict

    async def _cache_always_miss(*args, **kwargs):
        return None

    async def _limit_ok(*args, **kwargs):
        return "ok"

    calls_a: list[str] = []
    calls_b: list[str] = []

    async def _cache_recording_a(session, tender_id, profile_id, level):
        calls_a.append(profile_id)
        return None

    async def _cache_recording_b(session, tender_id, profile_id, level):
        calls_b.append(profile_id)
        return None

    with (
        patch(
            "src.bots.bid_verdict_service.resolve_tenant_id", return_value=_TENANT_ID
        ),
        patch(
            "src.bots.bid_verdict_service.cache_lookup", side_effect=_cache_recording_a
        ),
        patch("src.bots.bid_verdict_service.limit_decision", return_value="ok"),
    ):

        seq_a = [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            SimpleNamespace(title="T", description="D", country_name="S"),
            SimpleNamespace(
                classifications=None,
                regions=None,
                keywords_include=None,
                ai_extra_context=None,
                target_language="ru",
            ),
            SimpleNamespace(id=_ANALYSIS_ID),
        ]
        session_a = _make_session_seq(seq_a)
        await run_bid_verdict(
            session_a,
            tender_id=_TENDER_ID,
            profile_id=_PROFILE_ID_A,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(_BID_OUTPUT),
            tier_limits=_TIER_LIMITS,
        )

    with (
        patch(
            "src.bots.bid_verdict_service.resolve_tenant_id", return_value=_TENANT_ID
        ),
        patch(
            "src.bots.bid_verdict_service.cache_lookup", side_effect=_cache_recording_b
        ),
        patch("src.bots.bid_verdict_service.limit_decision", return_value="ok"),
    ):

        seq_b = [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            SimpleNamespace(title="T", description="D", country_name="S"),
            SimpleNamespace(
                classifications=None,
                regions=None,
                keywords_include=None,
                ai_extra_context=None,
                target_language="ru",
            ),
            SimpleNamespace(id=_ANALYSIS_ID),
        ]
        session_b = _make_session_seq(seq_b)
        await run_bid_verdict(
            session_b,
            tender_id=_TENDER_ID,
            profile_id=_PROFILE_ID_B,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(_BID_OUTPUT),
            tier_limits=_TIER_LIMITS,
        )

    # The cache was called with different profile_id
    assert calls_a == [_PROFILE_ID_A]
    assert calls_b == [_PROFILE_ID_B]
    assert calls_a[0] != calls_b[0]


# --------------------------------------------------------------------------- #
#  _render_bid_verdict: icons and structure
# --------------------------------------------------------------------------- #


def test_render_go_verdict():
    """go → 🟢, Recommended, score is present, pros and cons."""
    from src.bots.bid_verdict_service import _render_bid_verdict

    html = _render_bid_verdict("Тендер X", _BID_OUTPUT)
    assert "🟢" in html
    assert "Рекомендую" in html
    assert "82" in html
    assert "Тендер X" in html
    assert "✅" in html  # pros section
    assert "⚠️" in html  # cons section


def test_render_no_go_verdict():
    """no_go → 🔴, Not recommended."""
    from src.bots.bid_verdict_service import _render_bid_verdict

    output = {"verdict": "no_go", "score": 15, "pros": [], "cons": ["Нет лицензии"]}
    html = _render_bid_verdict("Тест", output)
    assert "🔴" in html
    assert "Не рекомендую" in html
    assert "15" in html


def test_render_caution_verdict():
    """caution → 🟡, With reservations."""
    from src.bots.bid_verdict_service import _render_bid_verdict

    output = {"verdict": "caution", "score": 55, "pros": ["Плюс"], "cons": ["Минус"]}
    html = _render_bid_verdict("Тест", output)
    assert "🟡" in html
    assert "оговорками" in html


# --------------------------------------------------------------------------- #
#  _format_profile_context: CPV/regions/keywords
# --------------------------------------------------------------------------- #


def test_format_profile_context_full():
    """All fields are present — all the rows are formatted."""
    from src.ai.profile_context import format_profile_context

    ctx = format_profile_context(
        classifications=[
            {"scheme": "CPV", "code": "45000000", "label": "Строительство"}
        ],
        regions=["Белград", "Нови Сад"],
        keywords_include={"keywords": ["строительство", "ремонт"]},
        ai_extra_context="Специализируемся на промышленных объектах",
    )
    assert "45000000" in ctx
    assert "Строительство" in ctx
    assert "Белград" in ctx
    assert "строительство" in ctx
    assert "промышленных" in ctx


def test_format_profile_context_empty():
    """An empty profile → a message about the absence of data."""
    from src.ai.profile_context import format_profile_context

    ctx = format_profile_context(
        classifications=None, regions=None, keywords_include=None, ai_extra_context=None
    )
    assert "профиль не заполнен" in ctx


# --------------------------------------------------------------------------- #
#  Parsing of the callback “ai:bid:{tender_id}:{profile_id}”
# --------------------------------------------------------------------------- #


def test_callback_bid_prefix_parsing():
    """The callback ai:bid:{t_id}:{p_id} is parsed correctly."""
    data = f"ai:bid:{_TENDER_ID}:{_PROFILE_ID_A}"
    prefix = "ai:bid:"
    assert data.startswith(prefix)
    rest = data[len(prefix) :]
    parts = rest.split(":", 1)
    assert len(parts) == 2
    assert parts[0] == _TENDER_ID
    assert parts[1] == _PROFILE_ID_A


def test_tier_limits_bid_pro_passes_gate():
    """_TIER_LIMITS: pro.bid > 0 → the gate passes; starter.bid=0 → cut off.

    ‼️ The ceiling on pro/business is finite (VG-SPEND): trial = the Pro rights, and the
    registration is self-service, therefore unlimited here = unlimited Anthropic spend for any registered user. Unlimited remained only for
    enterprise (the contractual tier).
    """
    assert _TIER_LIMITS["pro"]["bid"] == 30  # > 0 → the gate passes
    assert _TIER_LIMITS["starter"]["bid"] == 0  # 0 = no access
    assert _TIER_LIMITS["lite"]["bid"] == 0
    assert _TIER_LIMITS["business"]["bid"] == 150
    assert _TIER_LIMITS["enterprise"]["bid"] is None
