"""The map of the models 5.5 (ai-models-upgrade, owner 14.10): slugs, effort, ceilings, errors.

The expectations — literals from the owner’s decision, not through the code constants.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.ai import AnalysisRequest
from src.ai.client import (
    AiClient,
    AiRefusalError,
    AiTruncatedError,
    _extract_text,
    load_model_config,
)

_L1 = json.dumps({"relevant": True, "score": 4, "reason": "ok"})


def _resp(text=_L1, stop="end_turn", content=None, model=None, to=100):
    return SimpleNamespace(
        content=content or [SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        model=model,
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=to,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class _Msgs:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.resp


def _client(resp):
    m = _Msgs(resp)
    sdk = SimpleNamespace(messages=m, beta=SimpleNamespace(messages=m))
    return AiClient(config=load_model_config(), sdk_client=sdk), m


@pytest.mark.parametrize(
    "level,tier_override,slug,effort",
    [
        ("quick", None, "claude-haiku-5-5", "low"),
        ("translate", None, "claude-haiku-5-5", "low"),
        ("standard", None, "claude-haiku-5-5", "medium"),
        ("bid", None, "claude-haiku-5-5", "medium"),
        ("checklist", None, "claude-haiku-5-5", "medium"),
        ("draft", "balanced", "claude-haiku-5-5", "medium"),
        ("draft", "premium", "claude-opus-5-5", "high"),
        ("deep", None, "claude-opus-5-5", "high"),
    ],
)
def test_slug_and_effort_per_level(level, tier_override, slug, effort):
    c, m = _client(_resp())
    tier = c.tier_for(level, override_tier=tier_override)
    assert c.slug_for(level, override_tier=tier_override) == slug

    import asyncio

    system, msgs = [{"type": "text", "text": "s"}], [{"role": "user", "content": "u"}]
    asyncio.run(c._call_with_retry(slug, system, msgs, 8000, tier=tier))
    call = m.calls[0]
    assert call["model"] == slug
    assert call["output_config"] == {"effort": effort}
    for banned in ("temperature", "top_p", "top_k", "thinking"):
        assert banned not in call


@pytest.mark.asyncio
async def test_premium_uses_server_side_fallback_beta():
    c, m = _client(_resp())
    await c._call_with_retry("claude-opus-5-5", [], [], 16000, tier="premium")
    call = m.calls[0]
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["extra_body"] == {"fallbacks": "default"}


@pytest.mark.asyncio
async def test_haiku_has_no_fallback():
    c, m = _client(_resp())
    await c._call_with_retry("claude-haiku-5-5", [], [], 8000, tier="balanced")
    assert "betas" not in m.calls[0] and "extra_body" not in m.calls[0]


@pytest.mark.asyncio
async def test_ceilings_literals():
    c, m = _client(_resp())
    await c.analyze(AnalysisRequest(level="quick", title="T", description="D"))
    assert m.calls[0]["max_tokens"] == 8000


@pytest.mark.asyncio
async def test_max_tokens_stop_is_error_not_answer():
    c, _ = _client(_resp(text='{"relevant": tr', stop="max_tokens"))
    with pytest.raises(AiTruncatedError):
        await c.analyze(AnalysisRequest(level="quick", title="T", description="D"))


@pytest.mark.asyncio
async def test_refusal_is_error():
    c, _ = _client(_resp(text="", stop="refusal", content=[]))
    with pytest.raises(AiRefusalError):
        await c.analyze(AnalysisRequest(level="quick", title="T", description="D"))


def test_text_only_from_text_blocks():
    r = _resp(
        content=[
            SimpleNamespace(type="thinking", thinking="", signature="x"),
            SimpleNamespace(type="text", text='{"a":'),
            SimpleNamespace(type="text", text=" 1}"),
        ]
    )
    assert _extract_text(r) == '{"a":\n 1}'


@pytest.mark.asyncio
async def test_thinking_block_first_then_json_parses_and_costs_output():
    r = _resp(
        content=[
            SimpleNamespace(type="thinking", thinking="", signature="x"),
            SimpleNamespace(type="text", text=_L1),
        ],
        to=2_000_000,
    )
    c, _ = _client(r)
    res = await c.analyze(AnalysisRequest(level="quick", title="T", description="D"))
    assert res.model == "claude-haiku-5-5"
    # 1000 in * 0.10/1M + 2M out * 0.50/1M = 1.0001
    assert res.cost_usd == pytest.approx(1.0001, abs=1e-6)


@pytest.mark.asyncio
async def test_opus_cost_and_served_model_on_fallback():
    r = _resp(model="claude-opus-5-5", to=1_000_000)
    c, _ = _client(r)
    res = await c.analyze(AnalysisRequest(level="deep", title="T", description="D"))
    assert res.model == "claude-opus-5-5"
