"""Unit tests of DeliveryWorker (Sprint-17 + the rich card v6).

The DB and the channel adapter are mocked: we check the idempotent pickup of a batch, the dispatch by channel, the finalisation of the statuses (sent/failed/retry), the handling of 403 (the blocking of the channel), the revival of the hung rows and the assembly of DeliveryJob with the fields of the card v6 — without a real PostgreSQL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.workers.adapters.base import DeliveryJob, DeliveryResult, FormattedMessage
from src.workers.delivery import DeliveryWorker

# --------------------------------------------------------------------------- #
#  Mocks of the DB (as in test_routing.py)
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return self._results.pop(0) if self._results else _FakeResult([])

    async def commit(self):
        self.commits += 1


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class _FakeAdapter:
    """A channel adapter stub: records format/send, returns the set result."""

    channel = "telegram"

    def __init__(
        self, result: DeliveryResult | None = None, raise_exc: Exception | None = None
    ):
        self._result = result or DeliveryResult(ok=True, response_code=200)
        self._raise = raise_exc
        self.formatted: list = []
        self.sent: list = []

    def supports_buttons(self) -> bool:
        return True

    def is_configured(self) -> bool:
        return True

    def format(self, job: DeliveryJob) -> FormattedMessage:
        self.formatted.append(job)
        return FormattedMessage(text="msg")

    async def send(self, job: DeliveryJob, message: FormattedMessage) -> DeliveryResult:
        if self._raise is not None:
            raise self._raise
        self.sent.append(job)
        return self._result


def _job(**over) -> DeliveryJob:
    base = dict(
        outbox_id=1,
        tender_id="t-1",
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc-1",
        target="555",
        channel_status="active",
        title="Видеонаблюдение",
        source_url="https://jnportal.rs/t/1",
        amount_eur=12345.0,
        deadline_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )
    base.update(over)
    return DeliveryJob(**base)


def _worker(session, adapter=None, **kw) -> DeliveryWorker:
    return DeliveryWorker(
        adapters={"telegram": adapter or _FakeAdapter()},
        session_factory=_session_factory(session),
        **kw,
    )


# --------------------------------------------------------------------------- #
#  claim — the idempotent pickup (review focus §1)
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
    assert params["claimed"] == "delivering"
    assert params["ready"] == ["pending", "ai_done"]
    # The default of the worker — the channels we can deliver: Telegram and Email.
    assert params["channels"] == ["telegram", "email"]


@pytest.mark.asyncio
async def test_recover_stale_returns_delivering_rows_to_ready():
    session = _FakeSession([_FakeResult([SimpleNamespace(id=1)])])
    worker = _worker(session)
    await worker.recover_stale()
    stmt, params = session.calls[-1]
    assert "status = :claimed" in stmt
    assert "CASE WHEN requires_ai THEN 'ai_done' ELSE 'pending' END" in stmt
    assert params["claimed"] == "delivering"


# --------------------------------------------------------------------------- #
#  finalize — the status transitions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_finalize_ok_marks_sent_and_logs_attempt():
    session = _FakeSession([])
    worker = _worker(session)
    await worker._finalize(_job(), DeliveryResult(ok=True, response_code=200))
    stmts = [s for s, _ in session.calls]
    assert any("delivery_attempts" in s for s in stmts)
    assert any("status='sent'" in s for s in stmts)
    assert session.commits == 1


@pytest.mark.asyncio
async def test_finalize_blocked_marks_failed_and_blocks_channel():
    session = _FakeSession([])
    worker = _worker(session)
    await worker._finalize(
        _job(), DeliveryResult(ok=False, response_code=403, blocked=True)
    )
    stmts = [s for s, _ in session.calls]
    assert any("delivery_attempts" in s for s in stmts)
    assert any("status='failed'" in s for s in stmts)
    # the channel is marked blocked → further attempts are stopped (review focus §2)
    assert any("user_channels SET status='blocked'" in s for s in stmts)


@pytest.mark.asyncio
async def test_finalize_retryable_reverts_to_ready_status():
    session = _FakeSession([])
    worker = _worker(session)
    await worker._finalize(
        _job(), DeliveryResult(ok=False, response_code=429, retryable=True)
    )
    stmts = [s for s, _ in session.calls]
    assert any("CASE WHEN requires_ai" in s for s in stmts)
    assert all("status='sent'" not in s for s in stmts)
    assert all("status='failed'" not in s for s in stmts)


@pytest.mark.asyncio
async def test_finalize_permanent_error_marks_failed_without_blocking_channel():
    session = _FakeSession([])
    worker = _worker(session)
    await worker._finalize(_job(), DeliveryResult(ok=False, error_text="bad markup"))
    stmts = [s for s, _ in session.calls]
    assert any("status='failed'" in s for s in stmts)
    assert all("user_channels" not in s for s in stmts)


# --------------------------------------------------------------------------- #
#  dispatch — the resolve of the adapter, an inactive channel, NotImplemented
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_happy_path_formats_sends_and_marks_sent():
    session = _FakeSession([])
    adapter = _FakeAdapter(DeliveryResult(ok=True, response_code=200))
    worker = _worker(session, adapter)
    await worker._dispatch_one(_job())
    assert len(adapter.formatted) == 1 and len(adapter.sent) == 1
    assert any("status='sent'" in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_dispatch_pending_channel_status_skips_send():
    """‼️ owner: an unconfirmed address does not receive letters.

    The second line of defense after routing (which does not write on pending outbox): the delivery asks exactly for ``status = 'active'``, therefore pending is cut off by itself — no change in the worker was needed. The test pins this, so that the predicate cannot be weakened to “everything except blocked” without turning red.
    """
    session = _FakeSession([])
    adapter = _FakeAdapter()
    worker = _worker(session, adapter)
    # The channel is a registered telegram: otherwise the send would be stopped by «no
    # adapter», and a weakening of the status predicate would not be caught by the test.
    await worker._dispatch_one(_job(channel_status="pending"))

    assert adapter.sent == []
    assert adapter.formatted == []
    assert any("status='failed'" in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_dispatch_blocked_channel_status_skips_send():
    session = _FakeSession([])
    adapter = _FakeAdapter()
    worker = _worker(session, adapter)
    await worker._dispatch_one(_job(channel_status="blocked"))
    # the adapter was not called; the row is marked failed; the channel is not touched again
    assert adapter.sent == []
    stmts = [s for s, _ in session.calls]
    assert any("status='failed'" in s for s in stmts)
    assert all("user_channels" not in s for s in stmts)


@pytest.mark.asyncio
async def test_dispatch_no_target_skips_send():
    session = _FakeSession([])
    adapter = _FakeAdapter()
    worker = _worker(session, adapter)
    await worker._dispatch_one(_job(target=None))
    assert adapter.sent == []
    assert any("status='failed'" in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_dispatch_unknown_channel_marks_failed():
    session = _FakeSession([])
    worker = _worker(session)
    await worker._dispatch_one(
        _job(channel="viber")
    )  # there is no adapter in the worker's registry
    assert any("status='failed'" in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_dispatch_not_implemented_adapter_marks_failed():
    session = _FakeSession([])
    adapter = _FakeAdapter(raise_exc=NotImplementedError("viber"))
    worker = _worker(session, adapter)
    await worker._dispatch_one(_job())
    assert any("status='failed'" in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_dispatch_unexpected_exception_is_retryable():
    session = _FakeSession([])
    adapter = _FakeAdapter(raise_exc=RuntimeError("network glitch"))
    worker = _worker(session, adapter)
    await worker._dispatch_one(_job())
    # an unexpected error → a repeat (revert), not a terminal fail
    stmts = [s for s, _ in session.calls]
    assert any("CASE WHEN requires_ai" in s for s in stmts)


# --------------------------------------------------------------------------- #
#  poll_once — the orchestration + a row without a target
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_poll_once_empty_claim_returns_zero():
    session = _FakeSession([_FakeResult([])])  # claim → empty
    worker = _worker(session)
    assert await worker.poll_once() == 0


@pytest.mark.asyncio
async def test_poll_once_claims_loads_and_dispatches():
    job_row = SimpleNamespace(
        outbox_id=7,
        tender_id="t-7",
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc-7",
        target="999",
        channel_status="active",
        title="Тендер",
        source_url="https://x",
        amount_eur=1000.0,
        deadline_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    # the order of the execute calls in one session: the digest schedules → claim → load
    # (there are no digest profiles — the first result is empty); finalize opens its own
    # session, but _session_factory gives the same session → its execute calls are logged.
    session = _FakeSession(
        [
            _FakeResult([]),
            _FakeResult([SimpleNamespace(id=7)]),
            _FakeResult([job_row]),
        ]
    )
    adapter = _FakeAdapter(DeliveryResult(ok=True, response_code=200))
    worker = _worker(session, adapter)
    processed = await worker.poll_once()
    assert processed == 1
    assert len(adapter.sent) == 1
    assert any("status='sent'" in s for s, _ in session.calls)


@pytest.mark.asyncio
async def test_poll_once_claimed_without_target_marked_failed():
    # claim returned id=5, but load did not find the channel binding (an empty result);
    # the first result — the digest schedules (there are no digest profiles).
    session = _FakeSession(
        [_FakeResult([]), _FakeResult([SimpleNamespace(id=5)]), _FakeResult([])]
    )
    adapter = _FakeAdapter()
    worker = _worker(session, adapter)
    await worker.poll_once()
    assert adapter.sent == []
    stmts = [s for s, _ in session.calls]
    assert any("delivery_attempts" in s for s in stmts)
    assert any("status='failed'" in s for s in stmts)


# --------------------------------------------------------------------------- #
#  _load_jobs — the assembly of DeliveryJob with the fields of the card v6 (NEXT №1)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_load_jobs_carries_rich_card_fields():
    row = SimpleNamespace(
        outbox_id=11,
        tender_id="t-11",
        channel="telegram",
        requires_ai=True,
        user_channel_id="uc-11",
        target="42",
        channel_status="active",
        title="Богатый тендер",
        source_url="https://jnportal.rs/tender-eo/374138",
        amount_eur=1_234_567.0,
        deadline_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc),
        # the fields of the card v6
        source_uid="374138",
        source_key="jnportal",
        amount_original=144_809_500.0,
        currency_original="RSD",
        is_amount_estimated=True,
        published_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        procedure_type="open",
        contract_type="goods",  # the direct column of tenders_normalized (0011)
        regions=["Београдски округ"],
        classifications=[],
    )
    session = _FakeSession([_FakeResult([row])])
    worker = _worker(session)
    jobs = await worker._load_jobs(session, [11])
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source_uid == "374138"
    assert job.source_key == "jnportal"
    assert job.amount_original == 144_809_500.0
    assert job.currency_original == "RSD"
    assert job.is_amount_estimated is True
    assert job.published_at == datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    assert job.procedure_type == "open"
    # contract_type — a direct column (goods/services/works), not from classifications
    assert job.contract_type == "goods"
    assert job.regions == ["Београдски округ"]
    # classifications=[] (a list) → passes as [] (a list is the main format,
    # the Chat 45 fix: earlier the isinstance(dict) filter dropped the list to None)
    assert job.classifications == []
    # the SQL pulls the new columns (including contract_type)
    stmt, _ = session.calls[-1]
    for col in (
        "source_uid",
        "source_key",
        "amount_original",
        "currency_original",
        "is_amount_estimated",
        "published_at",
        "procedure_type",
        "contract_type",
        "regions",
        "classifications",
    ):
        assert col in stmt


@pytest.mark.asyncio
async def test_load_jobs_classifications_list_survives():
    # a list of records [cpv, plan] (the main format, Chat 44/45) is passed through
    # as it is — this is the root of the visibility of cpv/the plan in the card.
    classifications = [
        {"scheme": "CPV", "code": "45000000", "label": "Грађевински радови"},
        {"scheme": "PLAN", "quarter": "2. квартал", "year": 2026},
    ]
    row = SimpleNamespace(
        outbox_id=14,
        tender_id="t-14",
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc-14",
        target="42",
        channel_status="active",
        title="Тендер",
        source_url=None,
        amount_eur=None,
        deadline_at=None,
        source_uid="374138",
        source_key="jnportal",
        amount_original=None,
        currency_original=None,
        is_amount_estimated=None,
        published_at=None,
        procedure_type=None,
        contract_type=None,
        regions=None,
        classifications=classifications,
    )
    session = _FakeSession([_FakeResult([row])])
    worker = _worker(session)
    jobs = await worker._load_jobs(session, [14])
    assert jobs[0].classifications == classifications


@pytest.mark.asyncio
async def test_load_jobs_contract_type_from_direct_column():
    # contract_type is read from the DIRECT column of tenders_normalized (0011),
    # independently of classifications (which are passed separately).
    row = SimpleNamespace(
        outbox_id=12,
        tender_id="t-12",
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc-12",
        target="42",
        channel_status="active",
        title="Тендер",
        source_url=None,
        amount_eur=None,
        deadline_at=None,
        source_uid="1",
        source_key="jnportal",
        amount_original=None,
        currency_original=None,
        is_amount_estimated=None,
        published_at=None,
        procedure_type=None,
        contract_type="works",
        regions=None,
        classifications={"system_code": "eu_cpv"},
    )
    session = _FakeSession([_FakeResult([row])])
    worker = _worker(session)
    jobs = await worker._load_jobs(session, [12])
    assert jobs[0].contract_type == "works"
    # The classifications-dict is passed as it is, without affecting contract_type
    assert jobs[0].classifications == {"system_code": "eu_cpv"}


@pytest.mark.asyncio
async def test_load_jobs_contract_type_none_when_column_absent():
    # a row without contract_type (getattr → None) does not fall.
    row = SimpleNamespace(
        outbox_id=13,
        tender_id="t-13",
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc-13",
        target="42",
        channel_status="active",
        title="Тендер",
        source_url=None,
        amount_eur=None,
        deadline_at=None,
        source_uid="1",
        source_key="jnportal",
        amount_original=None,
        currency_original=None,
        is_amount_estimated=None,
        published_at=None,
        procedure_type=None,
        contract_type=None,
        regions=None,
        classifications=None,
    )
    session = _FakeSession([_FakeResult([row])])
    worker = _worker(session)
    jobs = await worker._load_jobs(session, [13])
    assert jobs[0].contract_type is None
    assert jobs[0].classifications is None


# --------------------------------------------------------------------------- #
#  The diagnostics at the start: the channel is on, but there is no transport
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_warns_once_when_enabled_channel_has_no_transport(caplog):
    """Production case: email is on, the service has no mail env → it is visible in docker logs."""
    from src.workers.adapters.email import EmailAdapter

    session = _FakeSession([])
    worker = DeliveryWorker(
        adapters={"telegram": _FakeAdapter(), "email": EmailAdapter(None)},
        session_factory=_session_factory(session),
        enabled_channels=("telegram", "email"),
    )
    worker.stop()

    with caplog.at_level("WARNING"):
        await worker.run()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "email" in warnings[0].getMessage()
    assert "транспорт не настроен" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_run_is_quiet_when_transports_configured(caplog):
    session = _FakeSession([])
    worker = DeliveryWorker(
        adapters={"telegram": _FakeAdapter()},
        session_factory=_session_factory(session),
        enabled_channels=("telegram",),
    )
    worker.stop()

    with caplog.at_level("WARNING"):
        await worker.run()

    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
