"""Unit tests of AiWorker (Sprint-18).

The DB and the AI client are mocked: we check the idempotent pickup of a batch (SKIP LOCKED + the transit ``analyzing``), the revival of the hung ones, the cache hit without calling the model, the enforcement of the limits of the plan (§14.5: hard / soft / Lite-without-AI), the failover (``AiRetryableError`` leaves the row ``ai_pending``), the terminalisation of the L3 stub and of an invalid level, the race on UNIQUE(tender_id,profile_id,level) — without a real PostgreSQL and without the installed ``anthropic`` SDK.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from src.ai import AiRetryableError, AnalysisResult
from src.workers.ai import AiWorker

# --------------------------------------------------------------------------- #
#  DB mocks (as in test_delivery.py)
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        if self._results:
            nxt = self._results.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return _FakeResult([])

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class _FakeAiClient:
    """An AI client stub: returns the set result or raises an exception."""

    def __init__(self, result: AnalysisResult | None = None, raise_exc=None):
        self._result = result or AnalysisResult(
            output={"summary": "ok"},
            model="claude-sonnet-4-6",
            tokens_input=100,
            tokens_output=50,
            tokens_cached=0,
            cost_usd=0.01,
        )
        self._raise = raise_exc
        self.calls: list = []

    async def analyze(self, request):
        self.calls.append(request)
        if self._raise is not None:
            raise self._raise
        return self._result


def _job_row(**over):
    base = dict(
        outbox_id=1,
        tender_id="11111111-1111-1111-1111-111111111111",
        profile_id="22222222-2222-2222-2222-222222222222",
        tenant_id="33333333-3333-3333-3333-333333333333",
        ai_level="standard",
        target_language="ru",
        extra_context="мы ставим видеонаблюдение",
        focus_questions=["какой срок?"],
        tier="pro",
        status="active",
        access_until=None,
        country_name="Сербия",
        title="Видеонаблюдение",
        description="Поставка и монтаж камер",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _worker(session, ai=None, **kw) -> AiWorker:
    return AiWorker(
        ai_client=ai or _FakeAiClient(),
        session_factory=_session_factory(session),
        **kw,
    )


# --------------------------------------------------------------------------- #
#  claim / recover — the idempotent pickup (the review focus §1)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_claim_batch_uses_skip_locked_and_transient_status():
    session = _FakeSession(
        [_FakeResult([SimpleNamespace(id=1), SimpleNamespace(id=2)])]
    )
    worker = _worker(session)
    ids = await worker._claim_batch(session)
    assert ids == [1, 2]
    stmt, params = session.calls[-1]
    assert "FOR UPDATE SKIP LOCKED" in stmt
    assert params["claimed"] == "analyzing"
    assert params["ready"] == "ai_pending"


@pytest.mark.asyncio
async def test_recover_stale_returns_analyzing_rows_to_ai_pending():
    session = _FakeSession([_FakeResult([SimpleNamespace(id=1)])])
    worker = _worker(session)
    await worker.recover_stale()
    stmt, params = session.calls[-1]
    assert params["claimed"] == "analyzing"
    assert params["ready"] == "ai_pending"


@pytest.mark.asyncio
async def test_poll_once_empty_claim_returns_zero():
    session = _FakeSession([_FakeResult([])])
    worker = _worker(session)
    assert await worker.poll_once() == 0


# --------------------------------------------------------------------------- #
#  the level mapping / the validity of the level
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalid_level_is_rejected_without_calling_model():
    session = _FakeSession([])
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(ai_level="none")))
    assert ai.calls == []
    assert any(
        "status='pending'" in s and "requires_ai=false" in s for s, _ in session.calls
    )


@pytest.mark.asyncio
async def test_quick_level_degrades_to_plain_without_model_call():
    """L1 quick on the auto path is no longer executed (the owner’s decision of 15.08.2026).

    The level remained in ``_TIER_LIMITS`` (the historical axis of accounting), but the product does not show it: the output of L1 is service metadata, which no template renders. A profile with the old value ``quick`` must neither call the model, nor lose the tender — the card goes out without analysis.
    """
    session = _FakeSession([])
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(ai_level="quick", tier="pro")))
    assert ai.calls == []
    assert all("FROM ai_usage" not in s for s, _ in session.calls)
    assert any(
        "status='pending'" in s and "requires_ai=false" in s for s, _ in session.calls
    )


@pytest.mark.asyncio
async def test_standard_uses_profile_scoped_cache_and_insert():
    """L2 standard is keyed by the profile: the cache and the INSERT carry the ``profile_id``."""
    session = _FakeSession(
        [
            _FakeResult([SimpleNamespace(count=1)]),  # usage count (pro=400)
            _FakeResult([]),  # cache lookup → miss
            _FakeResult([SimpleNamespace(id="analysis-l2")]),  # INSERT RETURNING
        ]
    )
    ai = _FakeAiClient(
        AnalysisResult(
            output={"summary": "ok"},
            model="claude-sonnet-4-6",
            tokens_input=10,
            tokens_output=5,
            tokens_cached=0,
            cost_usd=0.0001,
        )
    )
    worker = _worker(session, ai)
    row = _job_row(ai_level="standard", tier="pro")
    await worker._process_one(_to_job(row))
    insert_call = next(
        p for s, p in session.calls if "INSERT INTO tender_ai_analyses" in s
    )
    assert insert_call["profile_id"] == row.profile_id
    assert insert_call["level"] == "standard"


# --------------------------------------------------------------------------- #
#  cache hit (the review focus §1)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cache_hit_reuses_analysis_without_model_call():
    session = _FakeSession(
        [
            _FakeResult([SimpleNamespace(count=0)]),  # usage count (pro=400)
            _FakeResult([SimpleNamespace(id="cached-1")]),  # cache lookup → hit
        ]
    )
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row()))
    assert ai.calls == []  # the model not called
    stmts = [s for s, _ in session.calls]
    assert any("status='ai_done'" in s for s in stmts)
    # the usage is NOT incremented on a cache hit (no call — no spend)
    assert all("INSERT INTO ai_usage" not in s for s in stmts)


# --------------------------------------------------------------------------- #
#  enforcement of the limits by plan (§14.5)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_lite_tier_has_no_ai_degrades_to_plain_delivery():
    """A level unavailable on the plan → the tender goes WITHOUT analysis, rather than disappearing.

    The expectation was corrected by the production fact (15.08.2026): at the terminalisation the profiles with AI had **100%** ``ai_rejected`` and zero delivered — switching on the paid feature zeroed the main product. A paid call is still absent: the decision is made before the call to the model.
    """
    session = _FakeSession([])
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(tier="lite", ai_level="standard")))
    assert ai.calls == []
    assert any(
        "status='pending'" in s and "requires_ai=false" in s for s, _ in session.calls
    )
    assert all("status='ai_rejected'" not in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_hard_limit_exhausted_degrades_before_model_call():
    """The limit is exhausted → the card without analysis, the model is not called.

    The exhaustion of the limit is a reason not to spend money, not a reason to deprive the subscriber of the tender: the delivery and the AI are different promises.
    """
    session = _FakeSession([_FakeResult([SimpleNamespace(count=50)])])
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(tier="starter", ai_level="standard")))
    assert ai.calls == []
    assert any(
        "status='pending'" in s and "requires_ai=false" in s for s, _ in session.calls
    )


@pytest.mark.asyncio
async def test_soft_limit_logs_but_proceeds(caplog):
    # starter standard limit 50; usage 40 (=80%) → a soft warning, but it passes.
    session = _FakeSession(
        [
            _FakeResult([SimpleNamespace(count=40)]),  # usage count (soft)
            _FakeResult([]),  # cache lookup → miss
            _FakeResult([SimpleNamespace(id="a-1")]),  # INSERT RETURNING
        ]
    )
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    with caplog.at_level("WARNING"):
        await worker._process_one(
            _to_job(_job_row(tier="starter", ai_level="standard"))
        )
    assert len(ai.calls) == 1  # the model was called despite the soft limit
    assert any("soft" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_unlimited_tier_skips_usage_count_query():
    """Without a limit (``None``) the spend query is not made — but this is ONLY enterprise.

    ‼️ Earlier the test stood on ``pro``: for pro/business ``standard`` was ``None``, that is, unlimited spend on the self-service registration. Since 15.08.2026 a finite limit exists for all, except the contractual enterprise.
    """
    session = _FakeSession(
        [
            _FakeResult([]),  # cache lookup → miss
            _FakeResult([SimpleNamespace(id="a-1")]),  # INSERT RETURNING
        ]
    )
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(tier="enterprise", ai_level="standard")))
    assert all("FROM ai_usage" not in s for s, _ in session.calls)
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_pro_standard_is_limited_and_counts_usage():
    """For pro the auto-analysis is NO LONGER unlimited: the spend is read before the call."""
    session = _FakeSession(
        [
            _FakeResult([SimpleNamespace(count=399)]),  # 399 of 400 — still passes
            _FakeResult([]),  # cache lookup → miss
            _FakeResult([SimpleNamespace(id="a-1")]),  # INSERT RETURNING
        ]
    )
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(tier="pro", ai_level="standard")))
    assert any("FROM ai_usage" in s for s, _ in session.calls)
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_pro_standard_exhausted_degrades_without_model_call():
    session = _FakeSession([_FakeResult([SimpleNamespace(count=400)])])
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(tier="pro", ai_level="standard")))
    assert ai.calls == []
    assert any(
        "status='pending'" in s and "requires_ai=false" in s for s, _ in session.calls
    )


# --------------------------------------------------------------------------- #
#  the gate of the auto-analysis: the freeze / the tenant’s status / the demo of the trial
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_frozen_access_degrades_before_any_db_read():
    """The freeze — the FIRST axis: no spend, no cache, no model.

    ``access_until`` in the past = the access expired (``src.domain.access.is_frozen``).
    """
    session = _FakeSession([])
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await worker._process_one(_to_job(_job_row(tier="pro", access_until=past)))
    assert ai.calls == []
    assert all("FROM ai_usage" not in s for s, _ in session.calls)
    assert any(
        "status='pending'" in s and "requires_ai=false" in s for s, _ in session.calls
    )


@pytest.mark.asyncio
async def test_trial_demo_allows_first_analyses():
    """The trial gets a DEMO: up to TRIAL_AUTO_DEMO_LIMIT analyzes over the whole trial."""
    session = _FakeSession(
        [
            _FakeResult([SimpleNamespace(count=9)]),  # 9 of 10 — still passes
            _FakeResult([]),  # cache lookup → miss
            _FakeResult([SimpleNamespace(id="a-1")]),  # INSERT RETURNING
        ]
    )
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(status="trial", tier="lite")))
    assert len(ai.calls) == 1
    # the demo is counted for the WHOLE trial → there is no period parameter in the spend query
    usage_call = next(p for s, p in session.calls if "FROM ai_usage" in s)
    assert "period" not in usage_call


@pytest.mark.asyncio
async def test_trial_demo_exhausted_degrades_to_plain():
    session = _FakeSession([_FakeResult([SimpleNamespace(count=10)])])
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(status="trial", tier="pro")))
    assert ai.calls == []
    assert any(
        "status='pending'" in s and "requires_ai=false" in s for s, _ in session.calls
    )


@pytest.mark.asyncio
async def test_non_paying_status_never_calls_model():
    """The auto-analysis is switched on ONLY on a paid plan (the owner’s decision).

    A status outside ``trial``/``active`` (incl. ``NULL``) — a refusal without reading the spend.
    """
    for status in (None, "suspended", "cancelled", "pending_verification"):
        session = _FakeSession([])
        ai = _FakeAiClient()
        worker = _worker(session, ai)
        await worker._process_one(_to_job(_job_row(status=status, tier="pro")))
        assert ai.calls == [], status
        assert all("FROM ai_usage" not in s for s, _ in session.calls), status
        assert any(
            "status='pending'" in s and "requires_ai=false" in s
            for s, _ in session.calls
        ), status


# --------------------------------------------------------------------------- #
#  failover — AiRetryableError leaves ai_pending (§8 / §14.7)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_retryable_error_reverts_to_ai_pending():
    session = _FakeSession(
        [
            _FakeResult([SimpleNamespace(count=0)]),  # usage count (pro=400)
            _FakeResult([]),  # cache lookup → miss
        ]
    )
    ai = _FakeAiClient(raise_exc=AiRetryableError("429 исчерпаны"))
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(tier="pro")))
    stmts = [s for s, _ in session.calls]
    assert any("status='ai_pending'" in s for s in stmts)
    assert all("status='ai_done'" not in s for s in stmts)
    assert all("status='ai_rejected'" not in s for s in stmts)


@pytest.mark.asyncio
async def test_deterministic_failure_is_terminal_ai_rejected():
    # A deterministic failure (not retryable) → terminal, NOT a revert (against a paid cycle).
    session = _FakeSession(
        [
            _FakeResult([SimpleNamespace(count=0)]),  # usage count (pro=400)
            _FakeResult([]),  # cache miss
        ]
    )
    ai = _FakeAiClient(raise_exc=ValueError("невалидный JSON"))
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(tier="pro")))
    stmts = [s for s, _ in session.calls]
    assert any("status='ai_rejected'" in s for s in stmts)
    assert all("status='ai_pending'" not in s for s in stmts)


@pytest.mark.asyncio
async def test_deep_level_not_implemented_degrades_to_plain():
    session = _FakeSession([_FakeResult([])])  # cache miss
    ai = _FakeAiClient(raise_exc=NotImplementedError("L3 deep"))
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(tier="pro", ai_level="deep")))
    assert any(
        "status='pending'" in s and "requires_ai=false" in s for s, _ in session.calls
    )


# --------------------------------------------------------------------------- #
#  the happy path + usage + the parametrisation of the prompt
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_success_inserts_analysis_upserts_usage_and_marks_done():
    session = _FakeSession(
        [
            _FakeResult([SimpleNamespace(count=0)]),  # usage count (pro=400)
            _FakeResult([]),  # cache lookup → miss
            _FakeResult([SimpleNamespace(id="analysis-99")]),  # INSERT RETURNING
        ]
    )
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(tier="pro")))
    stmts = [s for s, _ in session.calls]
    assert any("INSERT INTO tender_ai_analyses" in s for s in stmts)
    assert any("INSERT INTO ai_usage" in s for s in stmts)
    assert any("status='ai_done'" in s for s in stmts)
    # the model slug is written at the moment of the call (ADR-029)
    insert_call = next(
        p for s, p in session.calls if "INSERT INTO tender_ai_analyses" in s
    )
    assert insert_call["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_request_carries_country_and_language_params():
    # the parametrisation of the prompt: country_name / target_language reach the client.
    session = _FakeSession(
        [
            _FakeResult([SimpleNamespace(count=0)]),  # usage count (pro=400)
            _FakeResult([]),  # cache lookup → miss
            _FakeResult([SimpleNamespace(id="a-1")]),  # INSERT RETURNING
        ]
    )
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(
        _to_job(_job_row(tier="pro", country_name="Сербия", target_language="ru"))
    )
    req = ai.calls[0]
    assert req.country_name == "Сербия"
    assert req.target_language == "ru"
    assert req.level == "standard"


# --------------------------------------------------------------------------- #
#  the race on UNIQUE(tender_id,profile_id,level) (idempotency)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unique_race_on_insert_finalizes_with_existing():
    # the INSERT falls with IntegrityError → the repeated cache lookup finds a foreign record →
    # the outbox ai_done with the found id (the work is not lost).
    session = _FakeSession(
        [
            _FakeResult([SimpleNamespace(count=0)]),  # usage count (pro=400)
            _FakeResult([]),  # cache lookup (before the call) → miss
            IntegrityError("dup", None, Exception()),  # INSERT → conflict
            _FakeResult([]),  # update of a stale row: nothing to do (it is not stale)
            _FakeResult([SimpleNamespace(id="winner-1")]),  # the repeated cache lookup
        ]
    )
    ai = _FakeAiClient()
    worker = _worker(session, ai)
    await worker._process_one(_to_job(_job_row(tier="pro")))
    assert session.rollbacks == 1
    stmts = [s for s, _ in session.calls]
    assert any("status='ai_done'" in s for s in stmts)


@pytest.mark.asyncio
async def test_poll_once_claimed_without_job_is_rejected():
    # claim returned id=5, but load did not find the profile/tender → terminal.
    session = _FakeSession(
        [_FakeResult([SimpleNamespace(id=5)]), _FakeResult([])]  # claim, load
    )
    worker = _worker(session)
    await worker.poll_once()
    assert any("status='ai_rejected'" in s for s, _ in session.calls)


# --------------------------------------------------------------------------- #
#  helper: row → AiJob via the private loader of the worker
# --------------------------------------------------------------------------- #


def _to_job(row):
    """Converts the SimpleNamespace row into an AiJob (repeats the _load_jobs mapping)."""
    from src.workers.ai import AiJob

    return AiJob(
        outbox_id=row.outbox_id,
        tender_id=str(row.tender_id),
        profile_id=str(row.profile_id) if row.profile_id is not None else None,
        tenant_id=str(row.tenant_id) if row.tenant_id is not None else None,
        ai_level=row.ai_level,
        target_language=row.target_language,
        extra_context=row.extra_context,
        focus_questions=row.focus_questions,
        tier=row.tier,
        status=row.status,
        access_until=row.access_until,
        country_name=row.country_name,
        title=row.title,
        description=row.description,
    )
