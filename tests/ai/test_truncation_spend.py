"""Truncation / model refusal: the tokens are paid → they are written to the accounting, the error stays an error.

The expectations — literals from the fixture: input 1000, output 100 → 1100 tokens.
The unit of the limit (calls) is NOT charged on an error: calls=0.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.ai import AnalysisRequest
from src.ai.client import (
    AiError,
    AiRefusalError,
    AiTruncatedError,
    load_model_config,
    AiClient,
)
from src.ai.usage import _UPSERT_USAGE_SQL, record_error_spend

_L1 = json.dumps({"relevant": True, "score": 4, "reason": "ok"})
_REQ = AnalysisRequest(level="quick", title="T", description="D")


def _resp(text=_L1, stop="end_turn", content=None):
    return SimpleNamespace(
        content=content or [SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        model=None,
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=100,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class _Msgs:
    def __init__(self, resp):
        self.resp = resp

    async def create(self, **kw):
        return self.resp


def _client(resp):
    m = _Msgs(resp)
    sdk = SimpleNamespace(messages=m, beta=SimpleNamespace(messages=m))
    return AiClient(config=load_model_config(), sdk_client=sdk)


@pytest.mark.asyncio
async def test_truncated_answer_carries_spend():
    c = _client(_resp(text='{"relevant": tr', stop="max_tokens"))
    with pytest.raises(AiTruncatedError) as ei:
        await c.analyze(_REQ)
    assert ei.value.spend is not None
    assert ei.value.spend.tokens == 1100
    assert ei.value.spend.cost_usd > 0


@pytest.mark.asyncio
async def test_refusal_carries_spend_and_stays_error():
    c = _client(_resp(text="", stop="refusal", content=[]))
    with pytest.raises(AiRefusalError) as ei:
        await c.analyze(_REQ)
    assert ei.value.spend is not None
    assert ei.value.spend.tokens == 1100


class _Session:
    def __init__(self):
        self.calls = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        self.calls.append((stmt, params))

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_truncation_usage_is_recorded_without_limit_unit():
    c = _client(_resp(text='{"relevant": tr', stop="max_tokens"))
    with pytest.raises(AiTruncatedError) as ei:
        await c.analyze(_REQ)
    s = _Session()
    await record_error_spend(s, tenant_id="T1", level="quick", exc=ei.value)
    assert len(s.calls) == 1 and s.commits == 1
    stmt, params = s.calls[0]
    assert stmt is _UPSERT_USAGE_SQL
    assert params["calls"] == 0
    assert params["tokens"] == 1100
    assert params["cost"] > 0
    assert params["level"] == "quick" and params["tenant_id"] == "T1"


@pytest.mark.asyncio
async def test_error_without_spend_records_nothing():
    s = _Session()
    await record_error_spend(s, tenant_id="T1", level="quick", exc=AiError("сбой"))
    assert s.calls == [] and s.commits == 0
