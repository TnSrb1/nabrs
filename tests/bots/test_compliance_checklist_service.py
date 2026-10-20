"""Tests of the on-demand checklist “what to prepare” (VG-09).

Without a network and without a real DB: a mock session via AsyncMock. We cover:
- Gate Pro+: starter/lite → gate_pro, the model is not called.
- Frozen: access_until expired → frozen, the model is not called.
- no_account: no link → no_account.
- Success: the model is called, the JSON is parsed, the HTML is non-empty.
- Cache: a repeated call per (tender_id, profile_id) does not hit the model.
- _render_checklist: documents, deadlines, risks.
- Tier limits: pro.checklist=None → passes; starter.checklist=0 → the gate.
- The callback ai:chk:{tender_id} ≤ 64 bytes.
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

_CHECKLIST_OUTPUT = {
    "documents": [
        {
            "title": "Лицензия строительная категории C",
            "category": "квалификация",
            "mandatory": True,
            "note": "Обязательна по закону",
        },
        {
            "title": "Финансовая гарантия 5%",
            "category": "финансы",
            "mandatory": True,
            "note": "Банковская гарантия на срок контракта",
        },
        {
            "title": "Устав и регистрационные документы",
            "category": "юридич.",
            "mandatory": True,
            "note": "Нотариально заверенная копия",
        },
    ],
    "deadlines": [
        {"label": "Подача заявок до", "value": "2026-07-15"},
        {"label": "Вскрытие конвертов", "value": "2026-07-16 10:00"},
    ],
    "disqualification_risks": [
        "Отсутствие строительной лицензии",
        "Неполный комплект учредительных документов",
        "Размер гарантии меньше требуемого",
    ],
    "summary": "Стандартный строительный тендер. 3 обязательных документа.",
}


@dataclass
class _FakeAnalysisResult:
    output: dict
    model: str = "claude-sonnet-4-6"
    tokens_input: int = 500
    tokens_output: int = 200
    tokens_cached: int = 0
    cost_usd: float = 0.005


def _make_ai_client(
    output: dict | None = None, raise_exc: Exception | None = None
) -> AiClient:
    client = MagicMock(spec=AiClient)

    async def _analyze(request):
        if raise_exc:
            raise raise_exc
        return _FakeAnalysisResult(output=output or _CHECKLIST_OUTPUT)

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


_PROFILE_ROW = SimpleNamespace(
    id=_PROFILE_ID_A,
    classifications=[{"scheme": "CPV", "code": "45000000", "label": "Строительство"}],
    regions=["Белград"],
    keywords_include={"keywords": ["строительство"]},
    ai_extra_context=None,
    target_language="ru",
)


# --------------------------------------------------------------------------- #
#  Gate Pro+: starter → gate_pro
# --------------------------------------------------------------------------- #


async def test_gate_pro_starter_rejected():
    """starter: checklist=0 → gate_pro, the model is not called."""
    from src.bots.compliance_checklist_service import run_compliance_checklist

    calls = [
        SimpleNamespace(tier="starter", access_until=_FUTURE),  # 0 tier
        _PROFILE_ROW,  # 1 latest profile
    ]
    session = _make_session_seq(calls)
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.compliance_checklist_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_compliance_checklist(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "gate_pro"
    assert result.html == ""
    client.analyze.assert_not_called()


async def test_gate_pro_lite_rejected():
    """lite: checklist=0 → gate_pro."""
    from src.bots.compliance_checklist_service import run_compliance_checklist

    calls = [
        SimpleNamespace(tier="lite", access_until=None),  # 0 tier
        _PROFILE_ROW,  # 1 latest profile
    ]
    session = _make_session_seq(calls)
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.compliance_checklist_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_compliance_checklist(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "gate_pro"
    client.analyze.assert_not_called()


# --------------------------------------------------------------------------- #
#  Frozen: access_until expired → frozen, the model is not called
# --------------------------------------------------------------------------- #


async def test_frozen_returns_frozen_without_calling_model():
    """access_until expired → status='frozen', the model is NOT called."""
    from src.bots.compliance_checklist_service import run_compliance_checklist

    session = _session_returning(SimpleNamespace(tier="pro", access_until=_PAST))
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.compliance_checklist_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_compliance_checklist(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
        )

    assert result.status == "frozen"
    assert result.html == ""
    client.analyze.assert_not_called()
    # Exactly one SELECT (tier + access_until), before the profile/cache/limit.
    assert session.execute.await_count == 1


# --------------------------------------------------------------------------- #
#  no_account: resolve_tenant_id returns None
# --------------------------------------------------------------------------- #


async def test_no_account_when_no_tenant():
    """No chat_id link → no_account."""
    from src.bots.compliance_checklist_service import run_compliance_checklist

    session = AsyncMock()
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.compliance_checklist_service.resolve_tenant_id", return_value=None
    ):
        result = await run_compliance_checklist(
            session,
            tender_id=_TENDER_ID,
            chat_id=999999,
            ai_client=client,
        )

    assert result.status == "no_account"
    client.analyze.assert_not_called()


# --------------------------------------------------------------------------- #
#  Success: the model is called, the JSON is parsed, the HTML contains the checklist
# --------------------------------------------------------------------------- #


async def test_ok_valid_json_parsed_and_rendered():
    """Pro+, no cache → a model call, the HTML is non-empty, contains the documents and the title."""
    from src.bots.compliance_checklist_service import run_compliance_checklist

    # The sequence of execute:
    # 0. tier/access
    # 1. latest profile → profile_id + the profile data
    # 2. cache_lookup → None (miss)
    # 3. limit_decision: pro/checklist=30 → reads the counter (there is no unlimited any more)
    # 4. tender load → title/description/country
    # 5. INSERT analysis → id
    # 6. UPSERT usage → None
    calls = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),  # 0 tier
        _PROFILE_ROW,  # 1 latest profile
        None,  # 2 cache miss
        SimpleNamespace(count=0),  # 3 the usage counter of the level
        SimpleNamespace(  # 4 tender
            title="Тестовый строительный тендер",
            description="Требуется строительная компания с лицензией категории C.",
            country_name="Сербия",
        ),
        SimpleNamespace(id=_ANALYSIS_ID),  # 5 INSERT
    ]
    session = _make_session_seq(calls)

    with patch(
        "src.bots.compliance_checklist_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_compliance_checklist(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(_CHECKLIST_OUTPUT),
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "success"
    assert len(result.html) > 0
    assert "📋" in result.html
    assert "Тестовый строительный тендер" in result.html
    assert "Лицензия строительная" in result.html  # document title
    assert "⬜" in result.html  # mandatory checkbox


# --------------------------------------------------------------------------- #
#  Cache: a repeated call per (tender_id, profile_id) does not hit the model
# --------------------------------------------------------------------------- #


async def test_cache_hit_no_model_call():
    """A cache hit → the model is NOT called, the HTML is returned from the cache."""
    from src.bots.compliance_checklist_service import run_compliance_checklist

    cached_output = {
        "documents": [],
        "deadlines": [],
        "disqualification_risks": [],
        "summary": "Кешировано",
    }

    calls = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),  # 0 tier
        _PROFILE_ROW,  # 1 latest profile
        SimpleNamespace(id=_ANALYSIS_ID),  # 2 cache HIT (cache_lookup)
        SimpleNamespace(
            title="Тендер из кеша", description="", country_name="Сербия"
        ),  # 3 tender title
        SimpleNamespace(output=cached_output),  # 4 _load_cached_output
    ]
    session = _make_session_seq(calls)
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.compliance_checklist_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_compliance_checklist(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "success"
    client.analyze.assert_not_called()
    assert "Кешировано" in result.html


# --------------------------------------------------------------------------- #
#  The cache key includes profile_id
# --------------------------------------------------------------------------- #


async def test_cache_key_includes_profile_id():
    """Different profile_id → different cache_lookup calls."""
    from src.bots.compliance_checklist_service import run_compliance_checklist

    calls_a: list[str] = []
    calls_b: list[str] = []

    async def _cache_recording_a(session, tender_id, profile_id, level):
        calls_a.append(profile_id)
        return None

    async def _cache_recording_b(session, tender_id, profile_id, level):
        calls_b.append(profile_id)
        return None

    profile_a = SimpleNamespace(
        id=_PROFILE_ID_A,
        classifications=None,
        regions=None,
        keywords_include=None,
        ai_extra_context=None,
        target_language="ru",
    )
    profile_b = SimpleNamespace(
        id=_PROFILE_ID_B,
        classifications=None,
        regions=None,
        keywords_include=None,
        ai_extra_context=None,
        target_language="ru",
    )

    with (
        patch(
            "src.bots.compliance_checklist_service.resolve_tenant_id",
            return_value=_TENANT_ID,
        ),
        patch(
            "src.bots.compliance_checklist_service.cache_lookup",
            side_effect=_cache_recording_a,
        ),
        patch(
            "src.bots.compliance_checklist_service.limit_decision", return_value="ok"
        ),
    ):

        seq_a = [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            profile_a,
            SimpleNamespace(title="T", description="D", country_name="S"),
            SimpleNamespace(id=_ANALYSIS_ID),
        ]
        session_a = _make_session_seq(seq_a)
        await run_compliance_checklist(
            session_a,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(_CHECKLIST_OUTPUT),
            tier_limits=_TIER_LIMITS,
        )

    with (
        patch(
            "src.bots.compliance_checklist_service.resolve_tenant_id",
            return_value=_TENANT_ID,
        ),
        patch(
            "src.bots.compliance_checklist_service.cache_lookup",
            side_effect=_cache_recording_b,
        ),
        patch(
            "src.bots.compliance_checklist_service.limit_decision", return_value="ok"
        ),
    ):

        seq_b = [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            profile_b,
            SimpleNamespace(title="T", description="D", country_name="S"),
            SimpleNamespace(id=_ANALYSIS_ID),
        ]
        session_b = _make_session_seq(seq_b)
        await run_compliance_checklist(
            session_b,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(_CHECKLIST_OUTPUT),
            tier_limits=_TIER_LIMITS,
        )

    assert calls_a == [_PROFILE_ID_A]
    assert calls_b == [_PROFILE_ID_B]
    assert calls_a[0] != calls_b[0]


# --------------------------------------------------------------------------- #
#  _render_checklist: documents, deadlines, risks
# --------------------------------------------------------------------------- #


def test_render_checklist_with_documents():
    """Documents → categories, icons, ⬜ mandatory."""
    from src.bots.compliance_checklist_service import _render_checklist

    result = _render_checklist("Тендер X", _CHECKLIST_OUTPUT)
    assert "📋" in result
    assert "Тендер X" in result
    assert "⬜" in result  # mandatory
    assert "квалификация" in result.lower() or "Квалификация" in result
    assert "Лицензия строительная" in result


def test_render_checklist_with_deadlines():
    """Deadlines → the section ⏰."""
    from src.bots.compliance_checklist_service import _render_checklist

    result = _render_checklist("Тест", _CHECKLIST_OUTPUT)
    assert "⏰" in result
    assert "Подача заявок до" in result
    assert "2026-07-15" in result


def test_render_checklist_with_risks():
    """Risks → the section 🚫."""
    from src.bots.compliance_checklist_service import _render_checklist

    result = _render_checklist("Тест", _CHECKLIST_OUTPUT)
    assert "🚫" in result
    assert "строительной лицензии" in result


def test_render_checklist_empty_sections():
    """Empty sections are not output."""
    from src.bots.compliance_checklist_service import _render_checklist

    empty_output = {
        "documents": [],
        "deadlines": [],
        "disqualification_risks": [],
        "summary": "Нет данных",
    }
    result = _render_checklist("Пустой", empty_output)
    assert "📋" in result
    assert "⏰" not in result  # empty deadlines are not output
    assert "🚫" not in result  # empty risks are not output
    assert "Нет данных" in result


def test_render_checklist_optional_item():
    """mandatory=false → the symbol ◽ instead of ⬜."""
    from src.bots.compliance_checklist_service import _render_checklist

    output = {
        "documents": [
            {
                "title": "Опционально",
                "category": "прочее",
                "mandatory": False,
                "note": "",
            }
        ],
        "deadlines": [],
        "disqualification_risks": [],
        "summary": None,
    }
    result = _render_checklist("Тест", output)
    assert "◽" in result
    assert "⬜" not in result


# --------------------------------------------------------------------------- #
#  Parsing of ChecklistOutput (Pydantic)
# --------------------------------------------------------------------------- #


def test_checklist_output_valid_json():
    """ChecklistOutput parses a correct JSON without errors."""
    from src.ai.schemas import ChecklistOutput

    parsed = ChecklistOutput.model_validate(_CHECKLIST_OUTPUT)
    assert len(parsed.documents) == 3
    assert parsed.documents[0].mandatory is True
    assert parsed.documents[0].category == "квалификация"
    assert len(parsed.deadlines) == 2
    assert len(parsed.disqualification_risks) == 3
    assert parsed.summary is not None


def test_checklist_output_fallback_on_invalid_schema():
    """An invalid JSON → a soft fallback (ChecklistOutput with the defaults)."""
    from src.ai.schemas import ChecklistOutput

    # A document without the mandatory field title — pydantic must raise an error.
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ChecklistOutput.model_validate(
            {
                "documents": [{"category": "прочее"}],  # no title — a mandatory field
            }
        )


# --------------------------------------------------------------------------- #
#  The callback ai:chk:{tender_id} ≤ 64 bytes
# --------------------------------------------------------------------------- #


def test_callback_ai_chk_within_64_bytes():
    """ai:chk:{tender_id} ≤ 64 bytes (the TG limit of callback_data)."""
    cb = f"ai:chk:{_TENDER_ID}"
    assert len(cb.encode()) <= 64, f"callback '{cb}' = {len(cb.encode())} bytes > 64"


# --------------------------------------------------------------------------- #
#  Tier limits: pro.checklist=None → passes; starter.checklist=0 → the gate
# --------------------------------------------------------------------------- #


def test_tier_limits_checklist_pro_passes_gate():
    """_TIER_LIMITS: pro.checklist > 0 → the gate passes; starter=0 → cut off.

    The ceiling on pro/business is finite (VG-SPEND) — see the same test for bid: unlimited on self-service = unlimited spend. Unlimited only for enterprise.
    """
    assert _TIER_LIMITS["pro"]["checklist"] == 30
    assert _TIER_LIMITS["starter"]["checklist"] == 0
    assert _TIER_LIMITS["lite"]["checklist"] == 0
    assert _TIER_LIMITS["business"]["checklist"] == 150
    assert _TIER_LIMITS["enterprise"]["checklist"] is None
