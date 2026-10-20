"""Migration 0052: issuing a paid tier moves tenants.status trial → active.

The defect (production 13.10.2026): ``effective_tier`` with ``trial`` returns Pro regardless of
``tier``, and the auto-analysis on ``trial`` runs in the demo quota of 10. A manually issued
``business`` gave nothing but a label.

* without a DB — the chain of revisions, SECURITY DEFINER, GRANT, downgrade;
* on PG (``TEST_RLS_DATABASE_URL`` = the app role, ``TEST_SUPERUSER_DATABASE_URL`` — the setup and
  the reading): ``activate_plan_manual('business')`` and the webhook ``activated`` → ``status='active'``,
  ``effective_tier='business'``, the auto-analysis is not demo; ``lite``/``suspended`` are not touched.
  The expectations — literals, not through the product code.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.ai.usage import auto_limit_decision
from src.billing.models import BillingEvent
from src.billing.service import activate_plan_manual, apply_billing_event
from src.db.session import set_current_tenant
from src.domain.tiers import effective_tier

_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0052_set_tier_activates_tenant.py"
)
APP_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", APP_URL)
needs_app = pytest.mark.skipif(not APP_URL, reason="нет TEST_RLS_DATABASE_URL")


def _load():
    spec = importlib.util.spec_from_file_location("migration_0052", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(monkeypatch, which) -> str:
    m = _load()
    calls: list[str] = []
    monkeypatch.setattr(m.op, "execute", lambda sql: calls.append(sql))
    getattr(m, which)()
    return " ".join("\n".join(calls).split())


def test_revision_chain():
    m = _load()
    assert (m.revision, m.down_revision) == ("0052", "0051")


def test_function_is_definer_granted_and_activates(monkeypatch):
    sql = _sql(monkeypatch, "upgrade")
    head = sql.split("$$", 1)[0]
    assert "FUNCTION app_admin_set_tier(p_tid uuid, p_tier text)" in head
    assert "SECURITY DEFINER" in head and "SET search_path = public" in head
    assert "WHEN status = 'trial' AND p_tier <> 'lite' THEN 'active'" in sql
    assert (
        "GRANT EXECUTE ON FUNCTION app_admin_set_tier(uuid, text) TO tendersrb_app"
        in sql
    )


def test_downgrade_restores_tier_only_body(monkeypatch):
    sql = _sql(monkeypatch, "downgrade")
    assert "status" not in sql.split("$$", 1)[1]
    assert "SET tier = p_tier" in sql


async def _seed(tier: str, status: str, used_standard: int = 10) -> str:
    tid = str(uuid.uuid4())
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    try:
        async with eng.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status) "
                    "VALUES (:id, 'RS', :tier, :status)"
                ),
                {"id": tid, "tier": tier, "status": status},
            )
            await c.execute(
                text(
                    "INSERT INTO ai_usage (tenant_id, period, level, count) "
                    "VALUES (:id, '2026-10', 'standard', :n)"
                ),
                {"id": tid, "n": used_standard},
            )
    finally:
        await eng.dispose()
    return tid


async def _row(tid: str) -> tuple[str, str]:
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    try:
        async with eng.connect() as c:
            r = (
                await c.execute(
                    text("SELECT status, tier FROM tenants WHERE id = :id"),
                    {"id": tid},
                )
            ).one()
            return r.status, r.tier
    finally:
        await eng.dispose()


async def _auto(tid: str, status: str, tier: str) -> str:
    eng = create_async_engine(APP_URL, poolclass=NullPool, future=True)
    sessions = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    try:
        async with sessions() as s:
            await set_current_tenant(s, tid)
            return await auto_limit_decision(
                s, tenant_id=tid, status=status, tier=tier, level="standard"
            )
    finally:
        await eng.dispose()


@needs_app
@pytest.mark.asyncio
async def test_activate_plan_manual_business_makes_tenant_active():
    tid = await _seed("lite", "trial")  # the demo quota is chosen (10 of 10)
    assert await _auto(tid, "trial", "lite") == "demo_exhausted"

    eng = create_async_engine(APP_URL, poolclass=NullPool, future=True)
    sessions = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    try:
        async with sessions() as s:
            await activate_plan_manual(s, tenant_id=tid, plan_key="business", days=30)
            await s.commit()
    finally:
        await eng.dispose()

    status, tier = await _row(tid)
    assert (status, tier) == ("active", "business")
    assert effective_tier(status, tier) == "business"
    # 10 analyzes with the monthly limit of business 1500 — not demo, but the ordinary gate.
    assert await _auto(tid, status, tier) == "ok"


@needs_app
@pytest.mark.asyncio
async def test_webhook_activated_makes_tenant_active():
    tid = await _seed("lite", "trial")
    eng = create_async_engine(APP_URL, poolclass=NullPool, future=True)
    sessions = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    event = BillingEvent(
        provider="stripe",
        provider_event_id=f"evt-{uuid.uuid4().hex}",
        type="activated",
        provider_customer_id=f"cus-{uuid.uuid4().hex}",
        provider_subscription_id=f"sub-{uuid.uuid4().hex}",
        plan_key="pro",
        current_period_end=datetime(2031, 1, 1, tzinfo=timezone.utc),
        tenant_id=tid,
    )
    try:
        async with sessions() as s:
            await apply_billing_event(s, event)
    finally:
        await eng.dispose()
    assert await _row(tid) == ("active", "pro")


@needs_app
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,tier,new_tier,expected",
    [
        ("trial", "lite", "lite", ("trial", "lite")),  # lite — not paid
        ("suspended", "lite", "business", ("suspended", "business")),  # do not wake
        ("active", "pro", "business", ("active", "business")),
    ],
)
async def test_set_tier_only_promotes_trial_for_paid(status, tier, new_tier, expected):
    tid = await _seed(tier, status, used_standard=0)
    eng = create_async_engine(APP_URL, poolclass=NullPool, future=True)
    try:
        async with eng.begin() as c:
            await c.execute(
                text("SELECT app_admin_set_tier(CAST(:t AS uuid), CAST(:x AS text))"),
                {"t": tid, "x": new_tier},
            )
    finally:
        await eng.dispose()
    assert await _row(tid) == expected
