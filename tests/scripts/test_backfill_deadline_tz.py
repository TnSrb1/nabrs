"""Tests of the backfill of the deadline timezone: a dry-run without a write, idempotency
(the marker), the correctness boundary (`first_seen_at`), pagination, the SQL invariants.

The fake session in memory emulates the semantics of the marker/boundary in Python — a DB is not needed; the SQL invariants (the reinterpretation, `NOT EXISTS`, the inviolability of `last_changed_at`) are checked IN THE TEXT of the query (the trick from the RLS-force test).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from scripts import backfill_deadline_tz
from scripts.backfill_deadline_tz import (
    _CREATE_MARKER,
    _MARK_BATCH,
    _SELECT_BATCH,
    _SELECT_FIRST_BATCH,
    _UPDATE_BATCH,
    _parse_before_ts,
    run,
)

_CUTOFF_DT = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
_CUTOFF = (
    _CUTOFF_DT  # the boundary goes into the bind as an aware datetime, not a string
)


class _Row:
    def __init__(self, id: str, first_seen: datetime | None):
        self.id = id
        self.first_seen = first_seen
        # the values of the deadline for the preview “was → will become” (in the test they do not matter exactly)
        self.old_deadline = datetime(2026, 8, 6, 11, 0, tzinfo=timezone.utc)
        self.new_deadline = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
        self.old_opening = None


class _Result:
    def __init__(self, rows: list[_Row], rowcount: int = 0):
        self._rows = rows
        self.rowcount = rowcount

    def fetchall(self) -> list[_Row]:
        return self._rows


class _FakeSession:
    """Emulates tenders_normalized + the marker: the shifted rows disappear from the selection.

    `shifted` — the set of ids from the marker; `updates` — the journal of the UPDATE batches; `forbid_writes` — the dry-run must not write and not commit.
    """

    def __init__(self, rows: list[_Row], *, forbid_writes: bool = False):
        self.rows = rows
        self.shifted: set[str] = set()
        self.updates: list[list[str]] = []
        self.commits = 0
        self.forbid_writes = forbid_writes

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        params = params or {}
        if stmt is _CREATE_MARKER:
            return _Result([])

        if stmt is _SELECT_FIRST_BATCH or stmt is _SELECT_BATCH:
            # ‼️ before_ts must be an aware datetime (the asyncpg contract for
            # CAST(:before_ts AS timestamptz)) — a string must not be let in here.
            before = params["before_ts"]
            assert isinstance(
                before, datetime
            ), f"before_ts must be a datetime, not {type(before).__name__}"
            assert before.tzinfo is not None, "before_ts must be aware (UTC)"
            after = params.get("after_id")
            pending = [
                r
                for r in self.rows
                if r.id not in self.shifted
                and (r.first_seen is None or r.first_seen < before)
            ]
            if after is not None:
                pending = [r for r in pending if r.id > after]
            pending.sort(key=lambda r: r.id)
            return _Result(pending[: params["batch_size"]])

        if stmt is _UPDATE_BATCH:
            if self.forbid_writes:
                raise AssertionError(f"the dry-run tried an UPDATE: {params}")
            ids = [i for i in params["ids"] if i not in self.shifted]
            self.updates.append(list(params["ids"]))
            return _Result([], rowcount=len(ids))

        if stmt is _MARK_BATCH:
            if self.forbid_writes:
                raise AssertionError("the dry-run tried to write the marker")
            self.shifted.update(params["ids"])
            return _Result([])

        raise AssertionError(f"an unexpected query: {stmt}")

    async def commit(self) -> None:
        if self.forbid_writes:
            raise AssertionError("the dry-run called commit")
        self.commits += 1


@pytest.fixture
def patch_session(monkeypatch):
    def _install(session: _FakeSession) -> _FakeSession:
        @asynccontextmanager
        async def _scope():
            yield session

        monkeypatch.setattr(backfill_deadline_tz, "session_scope", _scope)
        return session

    return _install


def _uid(n: int) -> str:
    return str(uuid.UUID(int=n))


# --------------------------------------------------------------------------- #
#  1. The SQL invariants (the textual guards)
# --------------------------------------------------------------------------- #
_REINTERPRET_SNIPPET = "AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Belgrade'"


@pytest.mark.parametrize("stmt", [_SELECT_FIRST_BATCH, _SELECT_BATCH, _UPDATE_BATCH])
def test_sql_reinterprets_via_postgres_timezones(stmt):
    """The shift — the reinterpretation via AT TIME ZONE (DST — Postgres, not a hard-code)."""
    assert _REINTERPRET_SNIPPET in str(stmt)


@pytest.mark.parametrize("stmt", [_SELECT_FIRST_BATCH, _SELECT_BATCH, _UPDATE_BATCH])
def test_sql_guards_already_shifted_rows(stmt):
    """Idempotency: a marked row is not touched a second time (NOT EXISTS)."""
    assert "NOT EXISTS" in str(stmt)
    assert "deadline_tz_backfill" in str(stmt)


@pytest.mark.parametrize("stmt", [_SELECT_FIRST_BATCH, _SELECT_BATCH])
def test_select_applies_correctness_cutoff(stmt):
    """The boundary: only the rows of the old ingest (first_seen_at < before_ts)."""
    assert "first_seen_at" in str(stmt)
    assert "ORDER BY tn.id" in str(stmt)


def test_update_touches_only_deadline_and_opening():
    """‼️ last_changed_at/last_seen_at/published_at are NOT in the UPDATE — otherwise we would wake
    the routing replay (a real incident: a mass UPDATE → 765 extra messages)."""
    sql = str(_UPDATE_BATCH)
    assert "deadline_at" in sql and "opening_at" in sql
    assert "last_changed_at" not in sql
    assert "last_seen_at" not in sql
    assert "published_at" not in sql


def test_batch_select_advances_cursor():
    assert "tn.id > CAST(:after_id AS uuid)" in str(_SELECT_BATCH)


# --------------------------------------------------------------------------- #
#  2. The dry-run writes NOTHING
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dry_run_writes_nothing(patch_session):
    session = patch_session(
        _FakeSession(
            [_Row(_uid(1), None), _Row(_uid(2), _CUTOFF_DT.replace(hour=1))],
            forbid_writes=True,
        )
    )
    await run(apply=False, batch_size=10, before_ts=_CUTOFF)
    assert session.updates == []
    assert session.commits == 0
    assert session.shifted == set()


# --------------------------------------------------------------------------- #
#  3. --apply shifts and marks
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_apply_shifts_and_marks(patch_session):
    session = patch_session(
        _FakeSession([_Row(_uid(1), None), _Row(_uid(2), _CUTOFF_DT.replace(hour=1))])
    )
    await run(apply=True, batch_size=10, before_ts=_CUTOFF)
    assert session.shifted == {_uid(1), _uid(2)}
    assert session.commits >= 1


# --------------------------------------------------------------------------- #
#  4. Idempotency: a second run does not change the row
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rerun_is_idempotent(patch_session):
    session = patch_session(_FakeSession([_Row(_uid(1), None), _Row(_uid(2), None)]))
    await run(apply=True, batch_size=10, before_ts=_CUTOFF)
    first_shifted = set(session.shifted)
    session.updates.clear()

    await run(apply=True, batch_size=10, before_ts=_CUTOFF)  # the repeat does not fall

    assert session.shifted == first_shifted  # the same set
    assert session.updates == []  # the second run did not touch a single batch


# --------------------------------------------------------------------------- #
#  5. The correctness boundary: the rows AFTER the deployment of the fix are not shifted
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rows_after_cutoff_not_shifted(patch_session):
    """first_seen_at >= before_ts — already the true UTC, they must not be touched."""
    after_fix = _CUTOFF_DT.replace(hour=23)  # later than the boundary
    session = patch_session(
        _FakeSession([_Row(_uid(1), None), _Row(_uid(2), after_fix)])
    )
    await run(apply=True, batch_size=10, before_ts=_CUTOFF)
    assert session.shifted == {_uid(1)}  # _uid(2) is not touched


# --------------------------------------------------------------------------- #
#  6. Pagination by batches (the cursor by id)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_paginates_all_rows_in_small_batches(patch_session):
    rows = [_Row(_uid(i), None) for i in range(1, 8)]
    session = patch_session(_FakeSession(rows))
    await run(apply=True, batch_size=2, before_ts=_CUTOFF)
    assert session.shifted == {_uid(i) for i in range(1, 8)}


@pytest.mark.asyncio
async def test_dry_run_scans_all_batches_without_hanging(patch_session):
    """the dry-run marks nothing → the pagination holds only on the cursor by id."""
    rows = [_Row(_uid(i), None) for i in range(1, 8)]
    session = patch_session(_FakeSession(rows, forbid_writes=True))
    await run(
        apply=False, batch_size=2, before_ts=_CUTOFF
    )  # it would loop → it will fall
    assert session.updates == []


# --------------------------------------------------------------------------- #
#  7. The parsing of the boundary
# --------------------------------------------------------------------------- #
def test_parse_before_ts_accepts_z_suffix():
    assert _parse_before_ts("2026-07-22T12:00:00Z") == datetime(
        2026, 7, 22, 12, 0, tzinfo=timezone.utc
    )


def test_parse_before_ts_naive_treated_as_utc():
    assert _parse_before_ts("2026-07-22T12:00:00") == datetime(
        2026, 7, 22, 12, 0, tzinfo=timezone.utc
    )


def test_parse_before_ts_returns_aware_datetime():
    """‼️ The return — an aware datetime (not a string): asyncpg binds it into timestamptz.

    The guard for the CLASS of the refusal from production (DataError 'got str'): if
    _parse_before_ts returns a string, this assert turns red.
    """
    result = _parse_before_ts("2026-07-22T14:00:00+02:00")
    assert isinstance(result, datetime)
    assert result.tzinfo is not None
    assert result == datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def test_parse_before_ts_rejects_garbage():
    with pytest.raises(ValueError):
        _parse_before_ts("не-дата")


# --------------------------------------------------------------------------- #
#  8. The guard of the driver’s contract: the bind :before_ts carries a datetime, not a string
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_bind_before_ts_is_datetime_not_str(patch_session):
    """‼️ The asyncpg contract: CAST(:before_ts AS timestamptz) requires a datetime.

    We check the TYPE of what really got into session.execute (and not the result of _parse_before_ts in a vacuum). Production fell exactly here: a string → DataError
    'expected a datetime.date or datetime.datetime instance, got str'.
    Mutation: return a string from _parse_before_ts → this test turns red.
    """
    captured: list[Any] = []

    class _CapturingSession(_FakeSession):
        async def execute(
            self, stmt: Any, params: dict[str, Any] | None = None
        ) -> _Result:
            if params is not None and "before_ts" in params:
                captured.append(params["before_ts"])
            return await super().execute(stmt, params)

    before = _parse_before_ts("2026-07-22T12:00:00Z")
    patch_session(_CapturingSession([_Row(_uid(1), None)]))
    await run(apply=True, batch_size=10, before_ts=before)

    assert captured, "before_ts did not reach session.execute"
    for value in captured:
        assert isinstance(
            value, datetime
        ), f"the bind :before_ts carries {type(value).__name__}, we expect a datetime"
        assert value.tzinfo is not None, "before_ts in the bind must be aware (UTC)"
