"""The core apply_billing_event: idempotency, tenant resolution, applying the plan.

Without a DB: a fake session intercepts execute()/commit() and parametrises the results of the «claim» insert of the dedup and the tenant resolution. The live RLS regression (upsert via SECURITY DEFINER under the app role) — in tests/db/test_rls_isolation.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.billing.models import BillingEvent
from src.billing.service import apply_billing_event


class _Result:
    def __init__(self, first_value):
        self._first = first_value

    def first(self):
        return self._first


class _FakeSession:
    """Records the execute calls; answers by a heuristic for claim/resolve."""

    def __init__(self, *, claim=(1,), resolve=None):
        self.calls: list[tuple[str, dict | None]] = []
        self._claim = claim
        self._resolve = resolve
        self.commits = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append((sql, params))
        if "billing_webhook_events" in sql:
            return _Result(self._claim)
        if "app_billing_resolve_tenant_by_customer" in sql:
            return _Result(self._resolve)
        return _Result(None)

    async def commit(self):
        self.commits += 1

    # helpers
    def called(self, needle: str) -> bool:
        return any(needle in sql for sql, _ in self.calls)

    def params_for(self, needle: str) -> dict:
        for sql, params in self.calls:
            if needle in sql:
                return params or {}
        raise AssertionError(f"no call with {needle!r}")


def _event(**over) -> BillingEvent:
    base = dict(
        provider="stripe",
        provider_event_id="evt_x",
        type="activated",
        provider_customer_id="cus_x",
        provider_subscription_id="sub_x",
        plan_key="pro",
        current_period_end=datetime(2030, 1, 1, tzinfo=timezone.utc),
        tenant_id="tenant-1",
    )
    base.update(over)
    return BillingEvent(**base)


@pytest.mark.asyncio
async def test_activated_applies_tier_and_access():
    db = _FakeSession(claim=(1,))
    await apply_billing_event(db, _event())
    assert db.called("app_billing_upsert_subscription")
    assert db.called("app_admin_set_tier")
    assert db.called("app_admin_grant_access")
    # pro plan → tier pro
    assert db.params_for("app_admin_set_tier")["tier"] == "pro"
    assert db.params_for("app_admin_set_tier")["tenant_id"] == "tenant-1"
    assert db.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["activated", "renewed", "past_due", "canceled"])
async def test_enqueues_notification_for_every_event_type(event_type):
    """The letter is queued on EVERY event type (kind = event.type).

    Mutation: remove enqueue from apply_billing_event → these checks turn red.
    """
    db = _FakeSession(claim=(1,))
    await apply_billing_event(db, _event(type=event_type))
    assert db.called("app_billing_enqueue_notification")
    params = db.params_for("app_billing_enqueue_notification")
    assert params["kind"] == event_type
    assert params["tenant_id"] == "tenant-1"
    assert params["provider"] == "stripe"
    assert params["event_id"] == "evt_x"


@pytest.mark.asyncio
async def test_enqueue_uses_resolved_tenant_when_no_metadata():
    """The letter is queued for the tenant resolved by customer_id (not for None)."""
    db = _FakeSession(claim=(1,), resolve=("tenant-resolved",))
    await apply_billing_event(db, _event(tenant_id=None))
    assert db.params_for("app_billing_enqueue_notification")["tenant_id"] == (
        "tenant-resolved"
    )


@pytest.mark.asyncio
async def test_duplicate_event_does_not_enqueue():
    """A repeat of the webhook (claim None) → the letter is NOT queued (a double webhook = one letter)."""
    db = _FakeSession(claim=None)
    await apply_billing_event(db, _event())
    assert not db.called("app_billing_enqueue_notification")


@pytest.mark.asyncio
async def test_unresolved_tenant_does_not_enqueue():
    """The tenant does not resolve → the letter is not queued (nowhere to send)."""
    db = _FakeSession(claim=(1,), resolve=None)
    await apply_billing_event(db, _event(tenant_id=None))
    assert not db.called("app_billing_enqueue_notification")


@pytest.mark.asyncio
async def test_business_plan_maps_to_business_tier():
    db = _FakeSession(claim=(1,))
    await apply_billing_event(db, _event(plan_key="business"))
    assert db.params_for("app_admin_set_tier")["tier"] == "business"


@pytest.mark.asyncio
async def test_duplicate_event_is_noop():
    """A repeat of the event_id (claim returned None) → only commit, without applying."""
    db = _FakeSession(claim=None)
    await apply_billing_event(db, _event())
    assert not db.called("app_admin_set_tier")
    assert not db.called("app_billing_upsert_subscription")
    assert db.commits == 1


@pytest.mark.asyncio
async def test_canceled_does_not_cut_access():
    """canceled: the upsert of the status is there, but tier/access are not touched (it will expire by itself)."""
    db = _FakeSession(claim=(1,))
    await apply_billing_event(db, _event(type="canceled"))
    assert db.called("app_billing_upsert_subscription")
    assert db.params_for("app_billing_upsert_subscription")["status"] == "canceled"
    assert not db.called("app_admin_set_tier")
    assert not db.called("app_admin_grant_access")


@pytest.mark.asyncio
async def test_past_due_does_not_cut_access():
    db = _FakeSession(claim=(1,))
    await apply_billing_event(db, _event(type="past_due"))
    assert db.params_for("app_billing_upsert_subscription")["status"] == "past_due"
    assert not db.called("app_admin_set_tier")


@pytest.mark.asyncio
async def test_tenant_resolved_by_customer_id_when_no_metadata():
    """No tenant_id in the event → resolve by customer_id (SECURITY DEFINER)."""
    db = _FakeSession(claim=(1,), resolve=("tenant-resolved",))
    await apply_billing_event(db, _event(tenant_id=None))
    assert db.called("app_billing_resolve_tenant_by_customer")
    assert db.params_for("app_admin_set_tier")["tenant_id"] == "tenant-resolved"


@pytest.mark.asyncio
async def test_unresolved_tenant_is_acknowledged_without_apply():
    """The tenant does not resolve → the dedup is recorded, the plan is not applied."""
    db = _FakeSession(claim=(1,), resolve=None)
    await apply_billing_event(db, _event(tenant_id=None))
    assert db.called("app_billing_resolve_tenant_by_customer")
    assert not db.called("app_billing_upsert_subscription")
    assert not db.called("app_admin_set_tier")
    assert db.commits == 1


@pytest.mark.asyncio
async def test_unknown_plan_key_skips_tier():
    db = _FakeSession(claim=(1,))
    await apply_billing_event(db, _event(plan_key="mythic"))
    assert not db.called("app_admin_set_tier")


@pytest.mark.asyncio
async def test_no_period_end_skips_grant_access():
    db = _FakeSession(claim=(1,))
    await apply_billing_event(db, _event(current_period_end=None))
    assert db.called("app_admin_set_tier")
    assert not db.called("app_admin_grant_access")


@pytest.mark.asyncio
async def test_renewed_recovers_access_and_status_active():
    """renewed (e.g. past_due→active): applies the tier + extends the access, the status active."""
    db = _FakeSession(claim=(1,))
    await apply_billing_event(db, _event(type="renewed"))
    assert db.called("app_admin_set_tier")
    assert db.called("app_admin_grant_access")
    assert db.params_for("app_billing_upsert_subscription")["status"] == "active"
    assert db.params_for("app_admin_set_tier")["tier"] == "pro"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["activated", "renewed"])
async def test_webhook_access_is_exactly_provider_period_end(event_type):
    """The payment sets the ABSOLUTE date of the provider — the contract is not touched.

    The rule “extension does not reduce” (src.domain.access.extend_until) addresses the translation of “N days” → a date (the admin/bot). Here the term comes from the payment
    gateway and is the SSOT of the payment: neither GREATEST nor now()+N may be here —
    otherwise a plan change to a shorter period would diverge from the billing.
    """
    period_end = datetime(2031, 3, 4, 9, 30, tzinfo=timezone.utc)
    db = _FakeSession(claim=(1,))
    await apply_billing_event(
        db, _event(type=event_type, current_period_end=period_end)
    )
    assert db.params_for("app_admin_grant_access")["until"] == period_end
    assert db.params_for("app_billing_upsert_subscription")["period_end"] == period_end


@pytest.mark.asyncio
async def test_activated_upsert_status_is_active():
    db = _FakeSession(claim=(1,))
    await apply_billing_event(db, _event(type="activated"))
    assert db.params_for("app_billing_upsert_subscription")["status"] == "active"


@pytest.mark.asyncio
async def test_no_subscription_id_skips_upsert_but_applies_tier():
    """An event without provider_subscription_id → the upsert of the subscription is skipped, the tier is applied."""
    db = _FakeSession(claim=(1,))
    await apply_billing_event(db, _event(provider_subscription_id=None))
    assert not db.called("app_billing_upsert_subscription")
    assert db.called("app_admin_set_tier")
    assert db.called("app_admin_grant_access")
