"""Regression R8: the bot onboarding creates a tenant with the internal canon tier='lite'.

Earlier ``create_subscriber`` wrote ``tier='free'`` (an alias of the showcase, NOT an internal tier) — after ck_tenants_tier/NOT NULL (0027) such an INSERT would fail. The DB is not touched: the mock session intercepts add()/flush()/commit().
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bots.telegram import create_subscriber


def _fake_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()  # set_current_tenant (set_config GUC)
    return session


def _added(session):
    return [call.args[0] for call in session.add.call_args_list]


@pytest.mark.asyncio
async def test_create_subscriber_tenant_tier_is_lite():
    session = _fake_session()
    await create_subscriber(
        session,
        telegram_id=424242,
        language="ru",
        keywords=["дорога"],
        region_codes=["RS-00"],
    )
    tenant = next(o for o in _added(session) if type(o).__name__ == "Tenant")
    assert tenant.tier == "lite", "the bot must write the internal canon, not 'free'"
    assert tenant.status == "trial"  # the trial rights are given by effective_tier by the status
    session.commit.assert_awaited_once()
