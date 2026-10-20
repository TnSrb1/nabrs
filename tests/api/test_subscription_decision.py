"""Tests of reading the latest decision on an application for the cabinet banner (0035).

Without network/DB (ADR-013): a mock session. A reliable answer independent of Telegram —
the subscriber sees the approval/refusal and the operator’s comment in “My subscription”. RLS
(tenant_isolation FORCE 0012/0032) + an explicit WHERE tenant_id → only their own tenant:
we check structurally (the SQL contains the filter and does NOT read someone else’s).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.cabinet import subscription_service as svc

_TID = "11111111-1111-1111-1111-111111111111"


def _first(obj):
    r = MagicMock()
    r.first.return_value = obj
    return r


@pytest.mark.asyncio
async def test_last_decision_approved_upgrade_maps_plan_and_comment():
    row = SimpleNamespace(
        status="approved",
        days=30,
        decided_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        comment="upgrade_request:pro",
        decision_comment="Добро пожаловать на Pro",
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_first(row))
    view = await svc.get_last_decision(session, _TID)
    assert view is not None
    assert view.approved is True
    assert view.plan == "pro"  # from upgrade_request:<plan>
    assert view.comment == "Добро пожаловать на Pro"

    # RLS defense-in-depth: the query filters by ITS OWN tenant.
    sql = str(session.execute.await_args.args[0])
    params = session.execute.await_args.args[1]
    assert "tenant_id = CAST(:tid AS uuid)" in sql
    assert params["tid"] == _TID
    # only the decided statuses (pending does not leak into the banner).
    assert "status IN ('approved', 'rejected')" in sql


@pytest.mark.asyncio
async def test_last_decision_rejected_plain_trial_no_plan():
    row = SimpleNamespace(
        status="rejected",
        days=None,
        decided_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        comment=None,
        decision_comment="Профиль неполный",
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_first(row))
    view = await svc.get_last_decision(session, _TID)
    assert view.approved is False
    assert view.plan is None
    assert view.comment == "Профиль неполный"


@pytest.mark.asyncio
async def test_last_decision_none_when_no_decided_request():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_first(None))
    assert await svc.get_last_decision(session, _TID) is None
