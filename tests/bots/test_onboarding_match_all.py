"""The bot onboarding declares the mode “All tenders”, and does not deduce it from emptiness.

The keyword step in the bot can be skipped — ``_keywords_to_groups`` on empty
input gives ``[]``, and before 0043 such a profile got the whole flow “by itself”.
With an explicit mode (``profiles.match_all``) the emptiness no longer means anything,
therefore a subscriber who skipped the step would fall silent: tenders do not arrive,
and there is no reason in the interface.

The regression closes exactly this gap — and in both directions: a set of words → the mode is
removed (otherwise the filter would narrow nothing), the words erased via ``/reset`` → the mode
returned.

The DB is not touched: the mock session intercepts add()/flush()/commit(), as in
``test_create_subscriber_tier``.
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
    session.execute = AsyncMock()
    return session


def _profile(session):
    return next(
        o
        for o in (c.args[0] for c in session.add.call_args_list)
        if type(o).__name__ == "Profile"
    )


@pytest.mark.asyncio
async def test_onboarding_without_keywords_declares_match_all():
    session = _fake_session()
    await create_subscriber(
        session,
        telegram_id=424243,
        language="ru",
        keywords=[],
        region_codes=["RS-00"],
    )
    profile = _profile(session)
    assert profile.keywords_include == []
    assert profile.match_all is True, (
        "a subscriber who skipped the keywords will get NOT A SINGLE tender: "
        "since 0043 the whole flow goes only by the explicit mode"
    )


@pytest.mark.asyncio
async def test_onboarding_with_keywords_keeps_mode_off():
    """Counterfact: the words are set → the mode is not needed, otherwise the filter narrows nothing."""
    session = _fake_session()
    await create_subscriber(
        session,
        telegram_id=424244,
        language="ru",
        keywords=["дорога"],
        region_codes=["RS-00"],
    )
    profile = _profile(session)
    assert profile.keywords_include == [{"all_of": ["дорога"]}]
    assert profile.match_all is False
