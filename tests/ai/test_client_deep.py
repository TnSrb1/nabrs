"""AiClient tests for the level 'deep' (STAGE 3 L3).

Without a network: the SDK is injected as a fake. We cover:
- analyze(level='deep') → AnalysisResult with slug=claude-opus-4-7.
- the _build_prompt deep branch: system with build_l3_system, user with build_l3_user.
- max_tokens for deep = deep_max_tokens (4000), for standard = max_tokens (1500).
- deep NO LONGER raises NotImplementedError (the stub is removed).
- validate_output('deep') is called and returns L3Output.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.ai import AnalysisRequest
from src.ai.client import AiClient, _DEFAULT_DEEP_MAX_TOKENS, _DEFAULT_MAX_TOKENS

_CONFIG = {
    "cheap": "claude-haiku-4-5-20251001",
    "balanced": "claude-sonnet-4-6",
    "premium": "claude-opus-4-7",
}

_L3_JSON = json.dumps(
    {
        "summary": "Глубокий разбор тендера",
        "match_score": 5,
        "match_reasons": ["Полное совпадение"],
        "red_flags": [],
        "required_qualifications": ["Лицензия А"],
        "estimated_effort_days": 60,
        "competition_estimate": "low",
        "work_breakdown": ["Этап 1", "Этап 2"],
        "participation_checklist": ["Шаг 1", "Шаг 2"],
        "focus_answers": [{"question": "Q1", "answer": "A1"}],
    },
    ensure_ascii=False,
)


def _resp(json_text: str, ti: int = 200, to: int = 100, cached: int = 50):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json_text)],
        usage=SimpleNamespace(
            input_tokens=ti,
            output_tokens=to,
            cache_read_input_tokens=cached,
            cache_creation_input_tokens=0,
        ),
    )


class _FakeMessages:
    def __init__(self, side_effects):
        self._side = list(side_effects)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        nxt = self._side.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class _FakeSDK:
    def __init__(self, side_effects):
        self.messages = _FakeMessages(side_effects)
        self.beta = SimpleNamespace(messages=self.messages)


def _client(side_effects, **kw) -> AiClient:
    sleeps: list[float] = []

    async def _fake_sleep(d):
        sleeps.append(d)

    c = AiClient(
        config=dict(_CONFIG),
        sdk_client=_FakeSDK(side_effects),
        sleep=_fake_sleep,
        backoff=kw.pop("backoff", (1, 2, 3)),
        **kw,
    )
    c._sleeps = sleeps
    return c


# --------------------------------------------------------------------------- #
#  The main deep tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_analyze_deep_uses_opus_slug():
    """deep → slug=claude-opus-4-7."""
    c = _client([_resp(_L3_JSON)])
    res = await c.analyze(
        AnalysisRequest(level="deep", title="T", description="D", country_name="Сербия")
    )
    assert res.model == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_analyze_deep_returns_l3_output():
    """deep → L3Output with work_breakdown and participation_checklist."""
    c = _client([_resp(_L3_JSON)])
    res = await c.analyze(
        AnalysisRequest(level="deep", title="T", description="D", country_name="Сербия")
    )
    assert res.output["match_score"] == 5
    assert res.output["work_breakdown"] == ["Этап 1", "Этап 2"]
    assert res.output["participation_checklist"] == ["Шаг 1", "Шаг 2"]


@pytest.mark.asyncio
async def test_analyze_deep_no_longer_raises_not_implemented():
    """The stub is removed: deep NO LONGER raises NotImplementedError."""
    c = _client([_resp(_L3_JSON)])
    res = await c.analyze(AnalysisRequest(level="deep", title="T", description="D"))
    assert res is not None


@pytest.mark.asyncio
async def test_analyze_deep_uses_deep_max_tokens():
    """deep calls the SDK with max_tokens=deep_max_tokens (4000 by default)."""
    c = _client([_resp(_L3_JSON)])
    await c.analyze(AnalysisRequest(level="deep", title="T", description="D"))
    call = c._sdk.messages.calls[0]
    assert call["max_tokens"] == _DEFAULT_DEEP_MAX_TOKENS


@pytest.mark.asyncio
async def test_analyze_standard_uses_standard_max_tokens():
    """standard still uses max_tokens (1500), not deep_max_tokens."""
    std_json = json.dumps(
        {
            "summary": "s",
            "match_score": 3,
            "match_reasons": [],
            "red_flags": [],
            "required_qualifications": [],
            "estimated_effort_days": None,
            "competition_estimate": None,
            "focus_answers": [],
        }
    )
    c = _client([_resp(std_json)])
    await c.analyze(
        AnalysisRequest(
            level="standard",
            title="T",
            description="D",
            country_name="Сербия",
            target_language="ru",
        )
    )
    call = c._sdk.messages.calls[0]
    assert call["max_tokens"] == _DEFAULT_MAX_TOKENS


@pytest.mark.asyncio
async def test_build_prompt_deep_uses_l3_system():
    """_build_prompt for deep includes build_l3_system (checked via a fragment)."""
    c = _client([_resp(_L3_JSON)])
    await c.analyze(
        AnalysisRequest(
            level="deep",
            title="T",
            description="D",
            country_name="Сербия",
            target_language="ru",
        )
    )
    call = c._sdk.messages.calls[0]
    system_text = call["system"][0]["text"]
    # A phrase from SYSTEM_L3_TEMPLATE
    assert (
        "ГЛУБОКИЙ" in system_text
        or "deep" in system_text.lower()
        or "Глубокий" in system_text
    )


@pytest.mark.asyncio
async def test_build_prompt_deep_caches_system():
    """The deep system prompt is marked cache_control: ephemeral."""
    c = _client([_resp(_L3_JSON)])
    await c.analyze(AnalysisRequest(level="deep", title="T", description="D"))
    call = c._sdk.messages.calls[0]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_analyze_deep_custom_deep_max_tokens():
    """the deep_max_tokens constructor parameter overrides the default."""
    c = _client([_resp(_L3_JSON)], deep_max_tokens=8000)
    await c.analyze(AnalysisRequest(level="deep", title="T", description="D"))
    call = c._sdk.messages.calls[0]
    assert call["max_tokens"] == 8000


def test_default_deep_max_tokens_value():
    """The default deep_max_tokens = 16000 (includes the thinking tokens)."""
    assert _DEFAULT_DEEP_MAX_TOKENS == 16000


def test_default_max_tokens_unchanged():
    """The default max_tokens (quick/standard) = 8000 (includes the thinking tokens)."""
    assert _DEFAULT_MAX_TOKENS == 8000
