"""Tests of the web service of the generation of AI artifacts (web-ai-2, ADR-013: mock session).

We check all the branches of ``generate_artifact`` / ``precheck_generation`` on the common primitives (usage.py + AiClient) without a live DB and without network:
- success (INSERT + commit, profile_id per-profile — NOT NULL);
- already (a cache hit → without a model call and without a charge);
- gate_pro / frozen / limit_exhausted / no_tender / no_profile;
- model_error (analyze threw) / model_busy (AiRetryableError);
- set_tenant is called FIRST (RLS invariant) in every transaction;
- draft gets model_tier by plan (VG-10), bid/checklist — no.

The live RLS regression under the app role — tests/db/test_rls_isolation.py
(``test_tender_ai_generate_isolation_under_app_role``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.ai import AiRetryableError
from src.api.cabinet import tender_ai_generate as gen

_TID = "11111111-1111-1111-1111-111111111111"
_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"

_FUTURE = datetime.now(timezone.utc) + timedelta(days=30)
_PAST = datetime.now(timezone.utc) - timedelta(days=1)


class _Result:
    def __init__(self, *, rows=None):
        self._rows = list(rows) if rows is not None else []

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Session:
    """The AsyncSession mock: set_config (RLS) and the advisory lock do NOT touch the results queue,
    the fact of the call is recorded in ``executed`` to check the set_tenant invariant. ``lock_granted`` controls the outcome of
    ``pg_try_advisory_xact_lock`` (by default it is taken — as without contention)."""

    def __init__(self, results, *, lock_granted=True):
        self._results = list(results)
        self._lock_granted = lock_granted
        self.executed: list[tuple[str, dict | None]] = []
        self.committed = 0
        self.rolled_back = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params))
        if "set_config" in sql or "app.current_tenant_id" in sql:
            return _Result()
        if "pg_try_advisory_xact_lock" in sql:
            return _Result(rows=[SimpleNamespace(locked=self._lock_granted)])
        # The track record (tender_user_status) — a helper query, does not touch
        # the results queue (empty → there is no track-record block).
        if "tender_user_status" in sql:
            return _Result()
        return self._results.pop(0) if self._results else _Result()

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1


class _FakeAi:
    """A fake AiClient: returns the result or throws the given exception."""

    def __init__(self, *, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.request = None
        self.calls = 0  # the counter of model calls (for the race test)

    async def analyze(self, request):
        self.calls += 1
        self.request = request
        if self._raises is not None:
            raise self._raises
        return self._result


def _tenant(tier="pro", access_until=_FUTURE):
    return _Result(rows=[SimpleNamespace(tier=tier, access_until=access_until)])


def _tender():
    return _Result(
        rows=[
            SimpleNamespace(
                title="Набавка рачунара",
                description="Опис ТЗ",
                country_name="Србија",
            )
        ]
    )


def _profile():
    return _Result(
        rows=[
            SimpleNamespace(
                classifications=None,
                regions=["RS-00"],
                keywords_include=None,
                ai_extra_context=None,
                target_language="sr",
            )
        ]
    )


def _usage(count=0):
    """The row of ai_usage.count for limit_decision.

    ‼️ It is needed by ALL levels with a finite limit. bid/checklist stopped being unlimited on pro/business (VG-SPEND: trial=Pro + self-service = unlimited spend), therefore the counter is read for them too.
    """
    return _Result(rows=[SimpleNamespace(count=count)])


def _insert_id(analysis_id="aaaaaaaa-0000-0000-0000-000000000001"):
    return _Result(rows=[SimpleNamespace(id=analysis_id)])


def _ai_result():
    return SimpleNamespace(
        output={"verdict": "go", "score": 80, "pros": ["a"], "cons": []},
        model="claude-sonnet-4-6",
        tokens_input=100,
        tokens_output=200,
        tokens_cached=0,
        cost_usd=0.0021,
    )


# --------------------------------------------------------------------------- #
# generate_artifact — the branches
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_success_inserts_and_commits():
    s = _Session(
        [_tenant(), _Result(rows=[]), _usage(), _tender(), _profile(), _insert_id()]
    )
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid", ai_client=ai
    )
    assert status == gen.STATUS_SUCCESS
    assert s.committed >= 1
    # set_tenant FIRST action of the transaction (RLS).
    assert "set_config" in s.executed[0][0]
    # INSERT into tender_ai_analyses with profile_id per-profile (NOT NULL).
    inserts = [
        (sql, p) for sql, p in s.executed if "INSERT INTO tender_ai_analyses" in sql
    ]
    assert inserts, "the INSERT of the artifact was expected"
    assert inserts[0][1]["profile_id"] == _PROFILE
    # The UPSERT of ai_usage is charged to its own tenant (tid from the web session).
    upserts = [(sql, p) for sql, p in s.executed if "INSERT INTO ai_usage" in sql]
    assert upserts and upserts[0][1]["tenant_id"] == _TID
    # AnalysisRequest built with the tender body and the bid level.
    assert ai.request.level == "bid"
    assert ai.request.title == "Набавка рачунара"
    assert ai.request.model_tier is None  # bid — without a tier override


@pytest.mark.asyncio
async def test_draft_uses_model_tier_by_plan():
    s = _Session(
        [
            _tenant(tier="business"),
            _Result(rows=[]),  # cache miss
            _Result(rows=[SimpleNamespace(count=0)]),  # usage (draft limit != None)
            _tender(),
            _profile(),
            _insert_id(),
        ]
    )
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s,
        tid=_TID,
        tender_id=_TENDER,
        profile_id=_PROFILE,
        level="draft",
        ai_client=ai,
    )
    assert status == gen.STATUS_SUCCESS
    assert ai.request.level == "draft"
    assert ai.request.model_tier == "premium"  # business → premium (VG-10)


@pytest.mark.asyncio
async def test_already_when_cache_hit_no_model_call():
    s = _Session([_tenant(), _Result(rows=[SimpleNamespace(id="cached-1")])])
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid", ai_client=ai
    )
    assert status == gen.STATUS_ALREADY
    assert ai.request is None  # the model was not called
    assert s.committed == 0  # without a charge


@pytest.mark.asyncio
async def test_gate_pro_when_level_zero_on_tier():
    # lite: bid=0 → the gate before the cache/limit/model.
    s = _Session([_tenant(tier="lite")])
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid", ai_client=ai
    )
    assert status == gen.STATUS_GATE_PRO
    assert ai.request is None


@pytest.mark.asyncio
async def test_frozen_when_access_expired():
    s = _Session([_tenant(access_until=_PAST)])
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid", ai_client=ai
    )
    assert status == gen.STATUS_FROZEN
    assert ai.request is None


@pytest.mark.asyncio
async def test_limit_exhausted_for_draft():
    s = _Session(
        [
            _tenant(tier="pro"),
            _Result(rows=[]),  # cache miss
            _Result(
                rows=[SimpleNamespace(count=5)]
            ),  # usage >= draft limit (5) → exhausted
        ]
    )
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s,
        tid=_TID,
        tender_id=_TENDER,
        profile_id=_PROFILE,
        level="draft",
        ai_client=ai,
    )
    assert status == gen.STATUS_LIMIT_EXHAUSTED
    assert ai.request is None


@pytest.mark.asyncio
async def test_no_tender_when_missing():
    s = _Session([_tenant(), _Result(rows=[]), _usage(), _Result(rows=[])])
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid", ai_client=ai
    )
    assert status == gen.STATUS_NO_TENDER
    assert ai.request is None


@pytest.mark.asyncio
async def test_no_profile_when_foreign_or_missing():
    s = _Session([_tenant(), _Result(rows=[]), _usage(), _tender(), _Result(rows=[])])
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid", ai_client=ai
    )
    assert status == gen.STATUS_NO_PROFILE
    assert ai.request is None


@pytest.mark.asyncio
async def test_model_error_when_analyze_raises():
    s = _Session([_tenant(), _Result(rows=[]), _usage(), _tender(), _profile()])
    ai = _FakeAi(raises=RuntimeError("boom"))
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid", ai_client=ai
    )
    assert status == gen.STATUS_MODEL_ERROR
    assert s.committed == 0  # no INSERT/charge


@pytest.mark.asyncio
async def test_model_busy_on_retryable():
    s = _Session([_tenant(), _Result(rows=[]), _usage(), _tender(), _profile()])
    ai = _FakeAi(raises=AiRetryableError("429"))
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid", ai_client=ai
    )
    assert status == gen.STATUS_MODEL_BUSY


@pytest.mark.asyncio
async def test_invalid_level_rejected():
    s = _Session([])
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="standard"
    )
    assert status == gen.STATUS_NO_TENDER  # protective fuse, the model was not called


# --------------------------------------------------------------------------- #
# deep — the button in the web (owner 13.10, item 2): shared per tender, the gate and the accounting are the same as for the bot
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_deep_gate_lite_refused_literal():
    # lite: deep=0 → refusal BEFORE the cache/limit/model. The expectation — a literal from the fact
    # (_TIER_LIMITS['lite']['deep'] == 0), not via the same constant.
    s = _Session([_tenant(tier="lite")])
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="deep", ai_client=ai
    )
    assert status == "gate_pro"
    assert ai.calls == 0
    assert s.committed == 0


@pytest.mark.asyncio
async def test_deep_limit_exhausted_pro_ten():
    # pro: deep=10/month; usage 10 → refusal, the model is not called.
    s = _Session([_tenant(tier="pro"), _Result(rows=[]), _usage(count=10)])
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="deep", ai_client=ai
    )
    assert status == "limit_exhausted"
    assert ai.calls == 0


@pytest.mark.asyncio
async def test_deep_success_shared_per_tender_and_records_usage():
    s = _Session(
        [_tenant(), _Result(rows=[]), _usage(), _tender(), _profile(), _insert_id()]
    )
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="deep", ai_client=ai
    )
    assert status == "success"
    # As in the bot: shared per tender — the cache and the INSERT with profile_id=NULL.
    inserts = [p for sql, p in s.executed if "INSERT INTO tender_ai_analyses" in sql]
    assert inserts and inserts[0]["profile_id"] is None
    assert inserts[0]["level"] == "deep"
    # The ai_usage record AFTER the model call, to its own tenant and the deep level.
    upserts = [p for sql, p in s.executed if "INSERT INTO ai_usage" in sql]
    assert upserts and upserts[0]["tenant_id"] == _TID
    assert upserts[0]["level"] == "deep"
    assert upserts[0]["calls"] == 1
    # The same model request as the bot’s: without a profile, language ru, without a tier override.
    assert ai.request.level == "deep"
    assert ai.request.target_language == "ru"
    assert not ai.request.extra_context
    assert ai.request.model_tier is None


@pytest.mark.asyncio
async def test_deep_cache_hit_is_shared_across_profiles_no_charge():
    s = _Session([_tenant(), _Result(rows=[SimpleNamespace(id="cached-deep")])])
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="deep", ai_client=ai
    )
    assert status == "already"
    assert ai.calls == 0
    assert s.committed == 0
    lookups = [p for sql, p in s.executed if "FROM tender_ai_analyses" in sql]
    assert lookups and "profile_id" not in lookups[0]  # search by the NULL branch


# --------------------------------------------------------------------------- #
# The race of parallel generation — the advisory lock (web-ai-6)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_in_progress_when_lock_not_acquired():
    # The lock is held by another request (mock try_lock=false) → in_progress, without the model/
    # charge and WITHOUT going through the expensive path (the results queue is not touched).
    s = _Session(
        [_tenant(), _Result(rows=[]), _tender(), _profile(), _insert_id()],
        lock_granted=False,
    )
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid", ai_client=ai
    )
    assert status == gen.STATUS_IN_PROGRESS
    assert ai.calls == 0  # the model was not called
    assert s.committed == 0  # without a charge
    # set_tenant FIRST, then straight away the advisory lock (the expensive path is not started).
    assert "set_config" in s.executed[0][0]
    assert "pg_try_advisory_xact_lock" in s.executed[1][0]


@pytest.mark.asyncio
async def test_lock_acquired_but_cache_now_hit_returns_already():
    # The lock is taken, but while we were taking it the first one had already written the cache → the REPEATED cache_lookup
    # under the lock sees the hit → already, without a second call/charge.
    s = _Session([_tenant(), _Result(rows=[SimpleNamespace(id="cached-1")])])
    ai = _FakeAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid", ai_client=ai
    )
    assert status == gen.STATUS_ALREADY
    assert ai.calls == 0
    assert s.committed == 0
    assert "pg_try_advisory_xact_lock" in s.executed[1][0]


@pytest.mark.asyncio
async def test_two_parallel_generations_charge_once():
    # Two simultaneous generations of one level with a SHARED mock client: the first
    # takes the lock and generates (1 call + 1 charge), the second does not take the lock →
    # in_progress. The result: exactly one AI call and one charge.
    import asyncio

    ai = _FakeAi(result=_ai_result())
    winner = _Session(
        [_tenant(), _Result(rows=[]), _usage(), _tender(), _profile(), _insert_id()],
        lock_granted=True,
    )
    loser = _Session([], lock_granted=False)

    async def _run(sess):
        return await gen.generate_artifact(
            sess,
            tid=_TID,
            tender_id=_TENDER,
            profile_id=_PROFILE,
            level="bid",
            ai_client=ai,
        )

    results = await asyncio.gather(_run(winner), _run(loser))

    assert sorted(results) == sorted([gen.STATUS_SUCCESS, gen.STATUS_IN_PROGRESS])
    assert ai.calls == 1  # exactly one model call
    # Exactly one charge of usage — only for the winner.
    upserts = [(sql, p) for sql, p in winner.executed if "INSERT INTO ai_usage" in sql]
    assert len(upserts) == 1
    assert not [sql for sql, _ in loser.executed if "INSERT INTO ai_usage" in sql]


# --------------------------------------------------------------------------- #
# precheck_generation — a thin wrapper over _prepare (without a model call)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_precheck_ok_when_ready_to_generate():
    s = _Session([_tenant(), _Result(rows=[]), _usage(), _tender(), _profile()])
    status = await gen.precheck_generation(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid"
    )
    assert status == gen.STATUS_OK


@pytest.mark.asyncio
async def test_precheck_already_on_cache_hit():
    s = _Session([_tenant(), _Result(rows=[SimpleNamespace(id="cached-1")])])
    status = await gen.precheck_generation(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid"
    )
    assert status == gen.STATUS_ALREADY


@pytest.mark.asyncio
async def test_precheck_gate_pro():
    s = _Session([_tenant(tier="starter")])  # starter: bid=0
    status = await gen.precheck_generation(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="bid"
    )
    assert status == gen.STATUS_GATE_PRO


@pytest.mark.asyncio
async def test_stepwise_stops_when_lock_lost_after_step(monkeypatch):
    # Step-wise analysis: the commit of the step's write releases the advisory lock. If after it the lock
    # was taken by a parallel request — this analysis is stopped (otherwise we pay twice).
    monkeypatch.setenv("AI_STEPWISE", "on")

    class _LosingLock(_Session):
        def __init__(self, results):
            super().__init__(results)
            self.lock_calls = 0

        async def execute(self, statement, params=None):
            if "pg_try_advisory_xact_lock" in str(statement):
                self.lock_calls += 1
                self.executed.append((str(statement), params))
                return _Result(rows=[SimpleNamespace(locked=self.lock_calls == 1)])
            return await super().execute(statement, params)

    class _StepAi(_FakeAi):
        async def analyze(self, request):
            self.calls += 1
            res = _ai_result()
            res.output = {"requirements": []}
            return res

    big = SimpleNamespace(
        title="Т", description="я" * 70_000, country_name="Србија", documents=None
    )
    s = _LosingLock(
        [_tenant(), _Result(rows=[]), _usage(), _Result(rows=[big]), _profile()]
    )
    ai = _StepAi(result=_ai_result())
    status = await gen.generate_artifact(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE, level="deep", ai_client=ai
    )
    assert status == gen.STATUS_IN_PROGRESS
    assert ai.calls == 1  # after the first step — stop, the other chunks were not called
    assert s.lock_calls == 2  # taken at the start + an attempt after the step's commit
