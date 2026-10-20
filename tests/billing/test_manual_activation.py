"""Unit tests of the manual grant of a plan (sub-approval, the billing path).

``activate_plan_manual`` must go by the SAME path as the webhook ``activated``:
app_admin_extend_access(days) + the upsert of the subscription provider='manual' +
app_admin_set_tier(plan.tier). Without a network/DB (ADR-013): a mock session, we check
the SQL parameters and the tier from the catalog, not a rewritten raw UPDATE.

The term is an EXTENSION (0041), not setting ``now()+days``: an approval of N days to a
tenant with a further-away access would otherwise SHORTEN its term (#497). The date is computed by
the DB inside the UPDATE — Python only reads it and must substitute THE SAME one into the subscription.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.billing.service import activate_plan_manual

# The term that the extension “gives back”: the tenant’s access was further than now()+days.
_EXTENDED_UNTIL = datetime(2030, 5, 1, tzinfo=timezone.utc)


def _params(call):
    """the dict parameters of the db.execute(sql, params) call."""
    return call.args[1] if len(call.args) > 1 else {}


def _session(until=_EXTENDED_UNTIL):
    """Mock session: the extension answers ``until``, the other calls are dummies."""
    extend = MagicMock()
    extend.scalar.return_value = until
    session = AsyncMock()
    # extend, then set_tenant+UPDATE of the status (activate_pending_verification inside
    # extend_access), then the upsert of the subscription and set_tier.
    session.execute = AsyncMock(
        side_effect=[extend, MagicMock(), MagicMock(), MagicMock(), MagicMock()]
    )
    return session


@pytest.mark.asyncio
async def test_activate_manual_extends_access_upserts_subscription_and_sets_tier():
    session = _session()
    act = await activate_plan_manual(session, tenant_id="t-1", plan_key="pro", days=30)
    assert act.plan_key == "pro"
    assert act.tier == "pro"  # Plan.tier from the catalog (the axes are not mixed)
    assert act.days == 30
    # The term — the one the DB returned (the extension), not the one recomputed in Python.
    assert act.until == _EXTENDED_UNTIL

    # Exactly three records + the status activation inside the extension (2 queries); all —
    # the existing SECURITY DEFINER / SET LOCAL (not a raw UPDATE of tier).
    assert session.execute.await_count == 5
    calls = session.execute.await_args_list
    extend, _tenant, _activate, upsert, set_tier = (_params(c) for c in calls)

    # 1) the extension of access FIRST — its result goes into the term of the subscription.
    assert "app_admin_extend_access" in str(calls[0].args[0])
    assert extend["days"] == 30
    assert extend["tid"] == "t-1"
    # An absolute date (the webhook’s contract) must not be present on the manual path.
    assert not any("app_admin_grant_access" in str(c.args[0]) for c in calls)

    # 2) the upsert of the subscription provider='manual', idempotent by tenant_id.
    assert upsert["provider"] == "manual"
    assert upsert["plan_key"] == "pro"
    assert upsert["status"] == "active"
    assert upsert["subscription_id"] == "t-1"
    assert upsert["customer_id"] == "t-1"
    assert upsert["period_end"] == act.until

    # 3) set_tier — the tier from the catalog.
    assert set_tier["tier"] == "pro"
    assert set_tier["tenant_id"] == "t-1"

    # It does not commit: the caller writes the decision + the audit in the same transaction.
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_activate_manual_business_tier():
    session = _session()
    act = await activate_plan_manual(
        session, tenant_id="t-2", plan_key="business", days=7
    )
    assert act.tier == "business"


@pytest.mark.asyncio
async def test_activate_manual_missing_tenant_raises_before_subscription():
    """The tenant is absent → the extension returned NULL → we fail, we do NOT write the subscription.

    A silent ``None`` would go into the subscription’s ``period_end`` and into the text of the letter (“access until None”), although the access was in fact not granted.
    """
    session = _session(until=None)
    with pytest.raises(LookupError):
        await activate_plan_manual(session, tenant_id="t-404", plan_key="pro", days=30)
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_activate_manual_unknown_plan_raises_and_writes_nothing():
    session = AsyncMock()
    session.execute = AsyncMock()
    with pytest.raises(ValueError):
        await activate_plan_manual(
            session, tenant_id="t-3", plan_key="nonsense", days=30
        )
    session.execute.assert_not_awaited()
