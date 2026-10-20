"""Unit tests of RoutingWorker (Sprint-16).

The DB and asyncpg are mocked: we check the parsing of the payload, the orchestration of the matching, the idempotent write into the outbox, the advance of the checkpoint and the reconnect loop — without a real PostgreSQL/Redis.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.workers.routing import RoutingWorker, _asyncpg_dsn

# --------------------------------------------------------------------------- #
#  Mocks
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple] = []
        self.committed = False

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return self._results.pop(0) if self._results else _FakeResult([])

    async def commit(self):
        self.committed = True


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class _FakeCheckpoint:
    def __init__(self, value=None):
        self.value = value
        self.sets: list = []

    async def get(self):
        return self.value

    async def set(self, value):
        self.value = value
        self.sets.append(value)


_TENDER_ROW = SimpleNamespace(
    id="t-1",
    country_code="RS",
    source_key="jnportal",
    regions=["RS-00"],
    classifications=[{"system_code": "eu_cpv", "code": "32200000"}],
    procedure_type="open",
    contract_type="goods",
    customer_name="ЕМС АД Београд",
    language="sr-Cyrl",
    amount_eur=10000.0,
    deadline_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    status="active",
    title_normalized="nabavka video opreme",
    description_normalized="montaza sistema video nadzora",
    first_seen_at=datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
)

_CAND_ROW = SimpleNamespace(
    id="p-1",
    tenant_id="te-1",
    keywords_include=[{"all_of": ["video"]}],
    keywords_exclude=None,
    ai_analysis_level="standard",
    delivery_channels=["telegram"],
    keyword_dict=False,
)

# The live telegram channel of the tenant — the output of LIVE_CHANNELS_SQL inside write_outbox.
# The order of the executes in route_tender: load tender → stage A → live_channels → INSERT.
_LIVE_CHANNEL_ROW = SimpleNamespace(
    channel="telegram", status="active", external_id="123456"
)
_REVOKED_CHANNEL_ROW = SimpleNamespace(
    channel="telegram", status="revoked", external_id="123456"
)


# --------------------------------------------------------------------------- #
#  DSN
# --------------------------------------------------------------------------- #


def test_asyncpg_dsn_strips_sqlalchemy_prefix():
    assert _asyncpg_dsn("postgresql+asyncpg://u:p@h/db") == "postgresql://u:p@h/db"
    assert _asyncpg_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


# --------------------------------------------------------------------------- #
#  route_tender
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_route_tender_empty_payload_returns_zero():
    worker = RoutingWorker(session_factory=_session_factory(_FakeSession([])))
    assert await worker.route_tender(json.dumps({})) == 0


@pytest.mark.asyncio
async def test_route_tender_tender_not_found():
    session = _FakeSession([_FakeResult([])])  # _load_tender → first() None
    worker = RoutingWorker(session_factory=_session_factory(session))
    assert await worker.route_tender(json.dumps({"tender_id": "missing"})) == 0
    assert session.committed is False


@pytest.mark.asyncio
async def test_route_tender_happy_path_writes_outbox_idempotently():
    # the order of the executes: load tender → stage A → live_channels → write_outbox
    session = _FakeSession(
        [
            _FakeResult([_TENDER_ROW]),
            _FakeResult([_CAND_ROW]),
            _FakeResult([_LIVE_CHANNEL_ROW]),
            _FakeResult([]),
        ]
    )
    checkpoint = _FakeCheckpoint()
    worker = RoutingWorker(
        session_factory=_session_factory(session), checkpoint=checkpoint
    )

    matched = await worker.route_tender(
        json.dumps({"tender_id": "t-1", "change_kind": "INSERT"})
    )

    assert matched == 1
    assert session.committed is True
    # the last execute — the INSERT into the outbox with the idempotent ON CONFLICT
    stmt, params = session.calls[-1]
    assert "delivery_outbox" in stmt and "DO NOTHING" in stmt
    assert params["match_kind"] == "new"
    # the checkpoint advanced to the first_seen_at of the tender
    assert checkpoint.value == _TENDER_ROW.first_seen_at


@pytest.mark.asyncio
async def test_route_tender_stage_b_drops_nonmatching_profile():
    cand_nomatch = SimpleNamespace(
        id="p-2",
        tenant_id="te-2",
        keywords_include=[{"all_of": ["skud"]}],  # not in the text of the tender
        keywords_exclude=None,
        ai_analysis_level="none",
        delivery_channels=["telegram"],
        keyword_dict=False,
    )
    session = _FakeSession([_FakeResult([_TENDER_ROW]), _FakeResult([cand_nomatch])])
    worker = RoutingWorker(session_factory=_session_factory(session))

    matched = await worker.route_tender(json.dumps({"tender_id": "t-1"}))
    assert matched == 0
    # the outbox INSERT was not called (only load + stage A)
    assert all("delivery_outbox" not in stmt for stmt, _ in session.calls)


@pytest.mark.asyncio
async def test_route_tender_update_kind_maps_to_update():
    session = _FakeSession(
        [
            _FakeResult([_TENDER_ROW]),
            _FakeResult([_CAND_ROW]),
            _FakeResult([_LIVE_CHANNEL_ROW]),
            _FakeResult([]),
        ]
    )
    worker = RoutingWorker(session_factory=_session_factory(session))
    await worker.route_tender(json.dumps({"tender_id": "t-1", "change_kind": "UPDATE"}))
    _, params = session.calls[-1]
    assert params["match_kind"] == "update"


@pytest.mark.asyncio
async def test_route_tender_matches_profile_but_skips_outbox_for_dead_channel():
    """A production case: the profile matches, but its channel is revoked.

    The match remains a match (the profile count does not change — this is not "not matched"),
    but there is no row in the send queue: nowhere to send (in the cabinet — only the
    ``web_only`` row, third channel owner 18.10). Otherwise delivery would fail it with
    "channel inactive or no target" — that is how 2540 dead records accumulated.
    """
    session = _FakeSession(
        [
            _FakeResult([_TENDER_ROW]),
            _FakeResult([_CAND_ROW]),
            _FakeResult([_REVOKED_CHANNEL_ROW]),
        ]
    )
    worker = RoutingWorker(session_factory=_session_factory(session))

    matched = await worker.route_tender(json.dumps({"tender_id": "t-1"}))

    assert matched == 1  # the profile matched
    outbox = [
        (stmt, params) for stmt, params in session.calls if "delivery_outbox" in stmt
    ]
    assert len(outbox) == 1  # exactly the cabinet row, not the send queue
    assert "'web_only'" in outbox[0][0] and "channels" not in outbox[0][1]
    assert session.committed is True


@pytest.mark.asyncio
async def test_route_tender_accepts_dict_payload():
    session = _FakeSession([_FakeResult([])])
    worker = RoutingWorker(session_factory=_session_factory(session))
    assert await worker.route_tender({"tender_id": "missing"}) == 0


# --------------------------------------------------------------------------- #
#  The LISTEN queue
# --------------------------------------------------------------------------- #


def test_on_notify_enqueues_payload():
    worker = RoutingWorker()
    worker._on_notify(None, 1, "tender_changed", '{"tender_id":"t-1"}')
    assert worker._queue.get_nowait() == '{"tender_id":"t-1"}'


# --------------------------------------------------------------------------- #
#  Reconnect
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_reconnects_on_connection_error_without_raising():
    attempts = {"n": 0}

    async def failing_connect(_dsn):
        attempts["n"] += 1
        raise OSError("connection refused")

    worker = RoutingWorker(connect=failing_connect)

    # the backoff-sleep instead of waiting asks the worker to stop → the loop exits
    async def fake_sleep(_delay):
        worker.stop()

    worker._sleep = fake_sleep  # type: ignore[assignment]

    await asyncio.wait_for(worker.run(), timeout=2.0)
    assert attempts["n"] == 1  # one attempt, then stop in the backoff


@pytest.mark.asyncio
async def test_replay_since_checkpoint_routes_missed_tenders(monkeypatch):
    # the checkpoint is set → the replay must select the missed ids and run route_tender
    session = _FakeSession([_FakeResult([SimpleNamespace(id="t-9")])])
    checkpoint = _FakeCheckpoint(value=datetime(2026, 5, 28, tzinfo=timezone.utc))
    worker = RoutingWorker(
        session_factory=_session_factory(session), checkpoint=checkpoint
    )

    routed: list = []

    async def fake_route(payload):
        routed.append(payload)
        return 0

    monkeypatch.setattr(worker, "route_tender", fake_route)
    await worker._replay_since_checkpoint()

    assert routed == [json.dumps({"tender_id": "t-9"})]


@pytest.mark.asyncio
async def test_replay_noop_without_checkpoint_value():
    session = _FakeSession([])
    checkpoint = _FakeCheckpoint(value=None)
    worker = RoutingWorker(
        session_factory=_session_factory(session), checkpoint=checkpoint
    )
    await worker._replay_since_checkpoint()
    assert session.calls == []  # there is no checkpoint → there is nothing to replay
