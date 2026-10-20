"""Tests of the service of the cabinet trial access (STAGE T).

Without network and a real DB (ADR-013): the mock session gives pre-set results from the queue; ``set_config(...)`` (RLS) does NOT consume the queue. We check the parsing of the access status and the anti-duplication of creating an application (pending / active access).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.api.cabinet.trial_service import create_trial_request, get_access_status

_TID = "11111111-1111-1111-1111-111111111111"
_UID = "22222222-2222-2222-2222-222222222222"


class _Result:
    def __init__(self, *, items=None, scalar=None):
        self._items = list(items) if items is not None else []
        self._scalar = scalar

    def first(self):
        return self._items[0] if self._items else None

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _Session:
    """Mock AsyncSession: set_config does not touch the results queue."""

    def __init__(self, results):
        self._results = list(results)
        self.committed = 0

    async def execute(self, statement, params=None):
        text = str(statement)
        if "set_config" in text or "app.current_tenant_id" in text:
            return _Result()
        return self._results.pop(0) if self._results else _Result()

    async def commit(self):
        self.committed += 1


def _past():
    return datetime.now(timezone.utc) - timedelta(days=1)


def _future():
    return datetime.now(timezone.utc) + timedelta(days=5)


# --------------------------------------------------------------------------- #
# get_access_status
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_status_null_access_is_unlimited_active():
    s = _Session(
        [_Result(items=[SimpleNamespace(access_until=None, last_status=None)])]
    )
    st = await get_access_status(s, _TID)
    assert st.state == "active"
    assert st.active is True
    assert st.can_request is False


@pytest.mark.asyncio
async def test_status_future_access_shows_until_date():
    s = _Session(
        [
            _Result(
                items=[SimpleNamespace(access_until=_future(), last_status="approved")]
            )
        ]
    )
    st = await get_access_status(s, _TID)
    assert st.state == "active"
    assert st.active is True
    assert st.until is not None


@pytest.mark.asyncio
async def test_status_pending_waits_for_approval():
    s = _Session(
        [_Result(items=[SimpleNamespace(access_until=None, last_status="pending")])]
    )
    st = await get_access_status(s, _TID)
    assert st.state == "pending"
    assert st.can_request is False


@pytest.mark.asyncio
async def test_status_expired_can_request_again():
    s = _Session(
        [_Result(items=[SimpleNamespace(access_until=_past(), last_status="approved")])]
    )
    st = await get_access_status(s, _TID)
    assert st.state == "expired"
    assert st.active is False
    assert st.can_request is True


@pytest.mark.asyncio
async def test_status_rejected_can_request_again():
    # A NULL access_until would mean active access; to reach the rejected branch,
    # the access must be absent (expired) with the last rejected application.
    s = _Session(
        [_Result(items=[SimpleNamespace(access_until=_past(), last_status="rejected")])]
    )
    st = await get_access_status(s, _TID)
    assert st.state == "rejected"
    assert st.can_request is True


# --------------------------------------------------------------------------- #
# create_trial_request — anti-duplication
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_blocked_when_pending_exists():
    s = _Session(
        [_Result(items=[SimpleNamespace(has_pending=True, access_until=None)])]
    )
    res = await create_trial_request(s, tenant_id=_TID, user_id=_UID, comment=None)
    assert res.ok is False
    assert res.reason == "pending"
    assert s.committed == 0


@pytest.mark.asyncio
async def test_create_blocked_when_access_active():
    s = _Session(
        [_Result(items=[SimpleNamespace(has_pending=False, access_until=None)])]
    )
    res = await create_trial_request(s, tenant_id=_TID, user_id=_UID, comment=None)
    assert res.ok is False
    assert res.reason == "active"


@pytest.mark.asyncio
async def test_upgrade_request_created_despite_active_access():
    """An UPGRADE application (comment 'upgrade_request:…') is created EVEN with an active
    access_until — otherwise a trial user silently loses the request for a subscription."""
    s = _Session(
        [
            _Result(items=[SimpleNamespace(has_pending=False, access_until=_future())]),
            _Result(scalar="44444444-4444-4444-4444-444444444444"),
            _Result(items=["trial@company.rs"]),
        ]
    )
    res = await create_trial_request(
        s, tenant_id=_TID, user_id=_UID, comment="upgrade_request:pro"
    )
    assert res.ok is True
    assert res.request_id == "44444444-4444-4444-4444-444444444444"
    assert s.committed == 1


@pytest.mark.asyncio
async def test_upgrade_request_still_blocked_by_pending():
    """Anti-spam is kept: the upgrade application is blocked by the existing pending."""
    s = _Session(
        [_Result(items=[SimpleNamespace(has_pending=True, access_until=_future())])]
    )
    res = await create_trial_request(
        s, tenant_id=_TID, user_id=_UID, comment="upgrade_request:business"
    )
    assert res.ok is False
    assert res.reason == "pending"
    assert s.committed == 0


@pytest.mark.asyncio
async def test_normal_trial_request_unchanged_blocked_by_active():
    """An ordinary trial-access request (not an upgrade) is still blocked by the active access —
    the behavior of the dashboard does NOT change."""
    s = _Session(
        [_Result(items=[SimpleNamespace(has_pending=False, access_until=_future())])]
    )
    res = await create_trial_request(s, tenant_id=_TID, user_id=_UID, comment=None)
    assert res.ok is False
    assert res.reason == "active"
    assert s.committed == 0


@pytest.mark.asyncio
async def test_create_succeeds_when_expired_and_no_pending():
    s = _Session(
        [
            _Result(items=[SimpleNamespace(has_pending=False, access_until=_past())]),
            _Result(scalar="33333333-3333-3333-3333-333333333333"),
            _Result(items=["user@company.rs"]),
        ]
    )
    res = await create_trial_request(
        s, tenant_id=_TID, user_id=_UID, comment="ООО Ромашка"
    )
    assert res.ok is True
    assert res.request_id == "33333333-3333-3333-3333-333333333333"
    assert res.email == "user@company.rs"
    assert s.committed == 1
