"""The tier label in the bots — by the EFFECTIVE tier (src.domain.tiers.effective_tier).

The registration writes tier='lite', status='trial'; the Pro rights are given by effective_tier
(trial → pro). The bot showed the raw tenant.tier → a client on the trial saw «lite».
The literals below are from the common lexicon COMMON (plan_trial / plan_free), not from the code.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.bots.telegram as tg
from src.bots.i18n import bot_t
from src.bots.status_service import get_status_summary

_TRIAL = {"ru": "Триал", "sr": "Probni", "en": "Trial"}
_FREE = {"ru": "Free", "sr": "Free", "en": "Free"}


def _profile():
    return SimpleNamespace(
        keywords_include=[{"all_of": ["gradnja"]}],
        regions=[],
        target_language="sr",
        is_active=True,
        name=None,
        match_all=False,
    )


def _tier_line(locale: str, label: str) -> str:
    return f"{bot_t(locale, 'lbl_tier')}: {label}"


@pytest.mark.parametrize("locale", ["ru", "sr", "en"])
def test_profile_trial_label_is_plan_trial_not_raw_lite(locale):
    data = {"tenant": SimpleNamespace(tier="lite", status="trial", access_until=None)}
    text = tg._render_profile(data, [_profile()], locale)
    assert _tier_line(locale, _TRIAL[locale]) in text
    assert _tier_line(locale, "lite") not in text


@pytest.mark.parametrize("locale", ["ru", "sr", "en"])
def test_profile_active_lite_label_is_free_like_site(locale):
    data = {"tenant": SimpleNamespace(tier="lite", status="active", access_until=None)}
    text = tg._render_profile(data, [_profile()], locale)
    assert _tier_line(locale, _FREE[locale]) in text
    assert _tier_line(locale, "lite") not in text


@pytest.mark.asyncio
async def test_status_summary_trial_label_uses_effective_tier():
    row = SimpleNamespace(
        tier="lite",
        status="trial",
        access_until=datetime(2030, 1, 1, tzinfo=timezone.utc),
        email="klient@example.com",
    )
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(first=MagicMock(return_value=row))
    )
    summary = await get_status_summary(session, "t1")
    assert summary["status"] == "trial"
    assert summary["tier_label"] == "Триал"


@pytest.mark.asyncio
async def test_cmd_status_trial_label_is_plan_trial_not_raw_lite():
    summary = {
        "tier": "lite",
        "status": "trial",
        "tier_label": "неважно",
        "access_until": None,
        "frozen": False,
        "days_left": None,
        "email": "klient@example.com",
    }
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=777)
    message.answer = AsyncMock()
    with (
        patch("src.bots.telegram.session_scope", _fake_scope),
        patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value="en")),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="t1")),
        patch("src.bots.telegram.get_status_summary", AsyncMock(return_value=summary)),
    ):
        await tg.cmd_status(message)
    text = message.answer.await_args.args[0]
    assert "<b>Trial</b>" in text
    assert "<b>lite</b>" not in text


class _fake_scope:
    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *exc):
        return False
