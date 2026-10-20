"""Approving a request by ANY path removes ``pending_verification`` (the tail of #700).

The defect (review #700, 14.10.2026): the activation ``pending_verification`` → ``trial`` lived only in ``grant_access`` (the admin button “Extend”). The approval of a request from the cabinet (the bot ``trial_service.decide`` → ``extend_access``) and the web approval ``admin_service.approve_request`` extended ``access_until``, but the status stayed ``pending_verification`` → ``matching.STAGE_A_SQL`` (only trial/active) did not deliver the tenders, and the tenant was “approved”, but silent.

The fix: ``extend_access`` (the single point of extension) calls ``activate_pending_verification`` itself in the same transaction.

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG under the role ``tendersrb_app``, FORCE RLS — the real SQL, not mocks). NOT the deploy production. The expectations — literals from the fact (the status, the term), not through the product code.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.admin import admin_service
from src.bots.trial_service import decide

APP_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", APP_URL)
needs_app = pytest.mark.skipif(not APP_URL, reason="нет TEST_RLS_DATABASE_URL")

_DAYS = 14
_TOLERANCE = timedelta(minutes=1)


async def _seed(status: str) -> tuple[str, str]:
    """A tenant in ``status`` with a live term + a pending request. Returns (tid, rid)."""
    tid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    until = datetime.now(timezone.utc) + timedelta(days=2)
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    try:
        async with eng.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status, access_until) "
                    "VALUES (:id, 'RS', 'lite', :st, :au)"
                ),
                {"id": tid, "st": status, "au": until},
            )
            await c.execute(
                text(
                    "INSERT INTO trial_requests (id, tenant_id, status, comment) "
                    "VALUES (:rid, :tid, 'pending', '')"
                ),
                {"rid": rid, "tid": tid},
            )
    finally:
        await eng.dispose()
    return tid, rid


async def _row(tid: str) -> tuple[str, datetime, datetime]:
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    try:
        async with eng.connect() as c:
            r = (
                await c.execute(
                    text(
                        "SELECT status, access_until, trial_until FROM tenants "
                        "WHERE id = :id"
                    ),
                    {"id": tid},
                )
            ).one()
            return r.status, r.access_until, r.trial_until
    finally:
        await eng.dispose()


def _sessions():
    eng = create_async_engine(APP_URL, poolclass=NullPool, future=True)
    return eng, async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)


async def _approve_via_bot(rid: str) -> None:
    eng, sessions = _sessions()
    try:
        async with sessions() as s:
            await decide(s, request_id=rid, approve=True, days=_DAYS)
            await s.commit()
    finally:
        await eng.dispose()


async def _approve_via_web(rid: str) -> None:
    eng, sessions = _sessions()
    try:
        async with sessions() as s:
            await admin_service.approve_request(
                s,
                admin_uid=str(uuid.uuid4()),
                request_id=rid,
                days=_DAYS,
            )
    finally:
        await eng.dispose()


@needs_app
@pytest.mark.asyncio
async def test_bot_approve_pending_becomes_trial():
    tid, rid = await _seed("pending_verification")
    _, before_until, _ = await _row(tid)

    await _approve_via_bot(rid)

    status, access_until, trial_until = await _row(tid)
    assert status == "trial"
    assert abs(access_until - (before_until + timedelta(days=_DAYS))) <= _TOLERANCE
    assert trial_until == access_until


@needs_app
@pytest.mark.asyncio
async def test_web_approve_pending_becomes_trial():
    tid, rid = await _seed("pending_verification")
    _, before_until, _ = await _row(tid)

    await _approve_via_web(rid)

    status, access_until, trial_until = await _row(tid)
    assert status == "trial"
    assert abs(access_until - (before_until + timedelta(days=_DAYS))) <= _TOLERANCE
    assert trial_until == access_until


@needs_app
@pytest.mark.asyncio
async def test_bot_approve_trial_stays_trial_and_extends():
    tid, rid = await _seed("trial")
    _, before_until, _ = await _row(tid)

    await _approve_via_bot(rid)

    status, access_until, _ = await _row(tid)
    assert status == "trial"
    assert abs(access_until - (before_until + timedelta(days=_DAYS))) <= _TOLERANCE
