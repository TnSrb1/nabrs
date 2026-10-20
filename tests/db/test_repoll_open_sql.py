"""Integration of the repeated poll of the open tenders (repoll-open, 07.10.2026).

Requires ``TEST_RLS_DATABASE_URL`` / ``TEST_SUPERUSER_DATABASE_URL`` (a throwaway PG,
NEVER the deploy production; gate.sh brings it up itself). A mock does not prove the SQL predicate
(`.claude/rules/tests-known-defects.md`), therefore here the real
``_REPOLL_CANDIDATES_SQL`` / ``_REPOLL_UPDATE_NORM_SQL`` against PostgreSQL:

(a) the selection of the candidates: the window −1…+30 days every 24 h, the other open ones every 72 h,
    a deadline older than a day and the stale ones without a deadline are not taken;
(b) the UPDATE of the deadline/status does NOT change ``first_seen_at``;
(c) an unchanged card does NOT make an UPDATE of ``tenders_normalized`` — and therefore no NOTIFY either (the trigger ``tenders_normalized_notify`` AFTER UPDATE without WHEN). We prove it by ``xmin``: any UPDATE of the row shifts it.

The expectations — literals by the rules of ``src/domain/repoll.py``, not from the code.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from types import SimpleNamespace

from src.domain.repoll import select_due
from src.workers.ingest import (
    _REPOLL_CANDIDATES_SQL,
    IngestWorker,
)

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция repoll-open пропускается",
)

NOW = datetime.now(timezone.utc)
H = timedelta(hours=1)
D = timedelta(days=1)

# name: (deadline_at, published_at, first_seen_at, checked_at)
# checked_at — tenders_raw.fetched_at (the mark of the last check); None = not checked.
_CASES = {
    # the deadline in the window −1…+30 days: every 24 h
    "win_checked_25h": (NOW + 5 * D, None, NOW - 9 * D, NOW - 25 * H),
    "win_checked_2h": (NOW + 5 * D, None, NOW - 9 * D, NOW - 2 * H),
    # the deadline passed less than a day ago — still the window
    "win_overdue_12h": (NOW - 12 * H, None, NOW - 9 * D, NOW - 25 * H),
    # the deadline further than +30 days: every 72 h
    "far_checked_73h": (NOW + 60 * D, None, NOW - 9 * D, NOW - 73 * H),
    "far_checked_30h": (NOW + 60 * D, None, NOW - 9 * D, NOW - 30 * H),
    # the deadline passed more than a day ago — not open
    "past_overdue_2d": (NOW - 2 * D - H, None, NOW - 9 * D, NOW - 100 * H),
    # without a deadline: fresh (30 days from the publication/first appearance) — every 72 h
    "nodl_fresh_checked_80h": (None, None, NOW - 5 * D, NOW - 80 * H),
    # without a deadline and stale — not open
    "nodl_stale_40d": (None, None, NOW - 40 * D, NOW - 200 * H),
    # never checked — first
    "win_never_checked": (NOW + 3 * D, None, NOW - 9 * D, None),
}
# What the SQL itself gives (before the periods): the deadline not earlier than “today−1” or fresh without a deadline.
_SQL_CANDIDATES = {
    "win_checked_25h",
    "win_checked_2h",
    "win_overdue_12h",
    "far_checked_73h",
    "far_checked_30h",
    "nodl_fresh_checked_80h",
    "win_never_checked",
}
# What reaches the poll after the periods (select_due): the window — 24 h, the rest — 72 h.
_DUE = {
    "win_checked_25h",
    "win_overdue_12h",
    "far_checked_73h",
    "nodl_fresh_checked_80h",
    "win_never_checked",
}


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    source_key = f"rp-test-{uuid.uuid4().hex[:12]}"
    ids = {name: uuid.uuid4() for name in _CASES}
    async with eng.begin() as c:
        for name, (deadline, published, first_seen, checked) in _CASES.items():
            await c.execute(
                text(
                    "INSERT INTO tenders_raw (id, country_code, source_key, source_uid, "
                    "raw_payload, raw_hash, fetched_at) VALUES "
                    "(:id, 'RS', :k, :uid, CAST('{}' AS jsonb), :h, :fa)"
                ),
                {
                    "id": ids[name],
                    "k": source_key,
                    "uid": name,
                    "h": name,
                    "fa": checked,
                },
            )
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, title, customer_name, status, deadline_at, "
                    "published_at, first_seen_at, last_seen_at, last_changed_at) VALUES "
                    "(:id, :k, :uid, 'RS', :uid, 'Заказчик', 'active', :dl, :pub, "
                    ":fs, :fs, :fs)"
                ),
                {
                    "id": ids[name],
                    "k": source_key,
                    "uid": name,
                    "dl": deadline,
                    "pub": published,
                    "fs": first_seen,
                },
            )
    yield {"source_key": source_key, "ids": ids, "eng": eng}

    async with eng.begin() as c:
        await c.execute(
            text("DELETE FROM tenders_normalized WHERE source_key = :k"),
            {"k": source_key},
        )
        await c.execute(
            text("DELETE FROM tenders_raw WHERE source_key = :k"),
            {"k": source_key},
        )
    await eng.dispose()


async def _candidate_rows(world) -> list[dict]:
    async with world["eng"].connect() as conn:
        result = await conn.execute(
            _REPOLL_CANDIDATES_SQL, {"source_key": world["source_key"], "now": NOW}
        )
        return [dict(r) for r in result.mappings().all()]


# --------------------------------------------------------------------------- #
#  (a) the selection of the candidates — the real SQL + domain.select_due
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_candidates_sql_takes_open_window_and_fresh_no_deadline(world):
    rows = await _candidate_rows(world)
    assert {r["source_uid"] for r in rows} == _SQL_CANDIDATES


@pytest.mark.asyncio
async def test_due_selection_applies_24h_window_and_72h_far_period(world):
    rows = await _candidate_rows(world)
    due = select_due(rows, now=NOW, limit=100)
    assert {r["source_uid"] for r in due} == _DUE


# --------------------------------------------------------------------------- #
#  (b) and (c) — the write through the real _repoll_apply against PostgreSQL
# --------------------------------------------------------------------------- #
def _worker(world):
    factory = async_sessionmaker(
        world["eng"], class_=AsyncSession, expire_on_commit=False
    )
    return SimpleNamespace(_session_factory=factory)


async def _norm_row(world, name) -> dict:
    async with world["eng"].connect() as conn:
        result = await conn.execute(
            text(
                "SELECT deadline_at, status, published_at, first_seen_at, last_changed_at, "
                "xmin::text AS xmin FROM tenders_normalized WHERE id = :id"
            ),
            {"id": world["ids"][name]},
        )
        return dict(result.mappings().one())


@pytest.mark.asyncio
async def test_changed_deadline_updates_row_but_keeps_first_seen_at(world):
    before = await _norm_row(world, "win_checked_25h")
    new_deadline = NOW + 19 * D
    changed = await IngestWorker._repoll_apply(
        _worker(world),
        world["source_key"],
        "win_checked_25h",
        before,
        {"deadline_at": new_deadline, "status": "active"},
        NOW,
    )
    assert changed is True
    after = await _norm_row(world, "win_checked_25h")
    assert after["deadline_at"] == new_deadline
    assert after["last_changed_at"] == NOW
    # (b) the first appearance is not rewritten by the extension of the deadline
    assert after["first_seen_at"] == before["first_seen_at"]
    assert after["first_seen_at"] == _CASES["win_checked_25h"][2]


@pytest.mark.asyncio
async def test_unchanged_card_does_no_update_of_tenders_normalized(world):
    before = await _norm_row(world, "win_checked_25h")
    changed = await IngestWorker._repoll_apply(
        _worker(world),
        world["source_key"],
        "win_checked_25h",
        before,
        {"deadline_at": before["deadline_at"], "status": before["status"]},
        NOW,
    )
    assert changed is False
    after = await _norm_row(world, "win_checked_25h")
    # xmin shifts on EVERY UPDATE of the row; the NOTIFY trigger — AFTER UPDATE without WHEN,
    # so not shifted = neither an UPDATE nor a NOTIFY.
    assert after["xmin"] == before["xmin"]
    assert after["last_changed_at"] == before["last_changed_at"]
