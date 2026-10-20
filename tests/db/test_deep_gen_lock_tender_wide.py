"""The generation lock of the deep analysis — shared per tender: two profiles/tenants do not pay twice.

Two REAL connections with open transactions (the xact lock lives until commit/rollback).
It skips without ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
The expectations are literals: deep → the second query ``False`` (in_progress, the model is not called);
standard → the lock per profile, the second profile ``True``.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.cabinet.tender_ai_generate import _try_generation_lock

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="TEST_RLS_DATABASE_URL не задан — лок генерации пропускается"
)


async def _two_tx_locks(
    level: str, tid_b: str, profile_b: str, tid_a: str, profile_a: str, tender: str
):
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    try:
        async with eng.connect() as c1, eng.connect() as c2:
            t1 = await c1.begin()
            t2 = await c2.begin()
            try:
                got_a = await _try_generation_lock(
                    c1, tid=tid_a, tender_id=tender, profile_id=profile_a, level=level
                )
                got_b = await _try_generation_lock(
                    c2, tid=tid_b, tender_id=tender, profile_id=profile_b, level=level
                )
            finally:
                await t2.rollback()
                await t1.rollback()
        return got_a, got_b
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_deep_two_profiles_same_tenant_one_generation():
    tid, tender = str(uuid.uuid4()), str(uuid.uuid4())
    got_a, got_b = await _two_tx_locks(
        "deep", tid, str(uuid.uuid4()), tid, str(uuid.uuid4()), tender
    )
    assert (got_a, got_b) == (True, False)


@pytest.mark.asyncio
async def test_deep_two_tenants_same_tender_one_generation():
    tender = str(uuid.uuid4())
    got_a, got_b = await _two_tx_locks(
        "deep",
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        tender,
    )
    assert (got_a, got_b) == (True, False)


@pytest.mark.asyncio
async def test_standard_stays_per_profile():
    tid, tender = str(uuid.uuid4()), str(uuid.uuid4())
    got_a, got_b = await _two_tx_locks(
        "standard", tid, str(uuid.uuid4()), tid, str(uuid.uuid4()), tender
    )
    assert (got_a, got_b) == (True, True)
