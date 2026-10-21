"""/profile shows ALL the profiles of the tenant, not only the latest (owner 22.09).

The PRODUCTION FACT of 22.09 (owner): the tenant ``396f699b`` holds 2 profiles, `/profile`
earlier showed the settings of one and was silent about the second (``_latest_profile``
takes exactly the latest by ``created_at``). The red reproducing test —
:func:`test_cmd_profile_shows_all_profiles_not_only_latest` — was failing on the code
BEFORE the fix (``_render_profile`` printed only ``data["profile"]``).

The mock session routes ``execute()`` by the TEXT of the SQL, not by the order of
the calls: ``get_subscriber``/``resolve_bot_locale``/``_list_profiles`` inside make several ``execute()`` calls each, their mutual order is an implementation detail, the test need not know it by heart (otherwise a refactoring of the internals would turn the test red without a change of behavior).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.bots import telegram as tg
from src.db.models import Tenant, User


def _profile(
    name: str | None,
    is_active: bool | None,
    match_all: bool = False,
    keywords: list | None = None,
    regions: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        is_active=is_active,
        match_all=match_all,
        keywords_include=keywords or [],
        regions=regions or [],
    )


def _routed_session(*, tenant_id, channel, user, tenant, profiles) -> AsyncMock:
    session = AsyncMock()

    async def _execute(stmt, params=None):
        s = str(stmt)
        result = MagicMock()
        if "app_resolve_tenant_id" in s:
            result.scalar.return_value = tenant_id
        elif "set_config" in s:
            pass
        elif "FROM user_channels" in s:
            result.scalars.return_value.first.return_value = channel
        elif "FROM profiles" in s:
            result.scalars.return_value.all.return_value = profiles
            result.scalars.return_value.first.return_value = (
                profiles[0] if profiles else None
            )
        else:
            raise AssertionError(f"unrouted execute(): {s}")
        return result

    session.execute = AsyncMock(side_effect=_execute)

    async def _get(model, pk):
        if model is User:
            return user
        if model is Tenant:
            return tenant
        raise AssertionError(f"unrouted session.get({model})")

    session.get = AsyncMock(side_effect=_get)
    return session


@asynccontextmanager
async def _scope(session):
    yield session


def _message(uid: int = 42) -> AsyncMock:
    m = AsyncMock()
    m.from_user = SimpleNamespace(id=uid)
    m.answer = AsyncMock()
    return m


async def _run_cmd_profile(session) -> str:
    message = _message()
    with patch("src.bots.telegram.session_scope", lambda: _scope(session)):
        await tg.cmd_profile(message)
    return message.answer.await_args.args[0]


def _make_env(profiles: list) -> AsyncMock:
    tenant_id = uuid4()
    user_id = uuid4()
    channel = SimpleNamespace(user_id=user_id, locale=None)
    user = SimpleNamespace(
        id=user_id, tenant_id=tenant_id, email="client@example.com", locale="ru"
    )
    tenant = SimpleNamespace(id=tenant_id, tier="pro", access_until=None)
    return _routed_session(
        tenant_id=tenant_id,
        channel=channel,
        user=user,
        tenant=tenant,
        profiles=profiles,
    )


@pytest.mark.asyncio
async def test_cmd_profile_shows_all_profiles_not_only_latest():
    """Red before the fix: exactly 1 profile of 2 was printed (the PRODUCTION FACT 22.09)."""
    p1 = _profile("Строительство", True, keywords=[{"all_of": ["gradnja"]}])
    p2 = _profile("IT-услуги", True, keywords=[{"all_of": ["it"]}])
    session = _make_env([p1, p2])
    text = await _run_cmd_profile(session)
    assert "Строительство" in text
    assert "IT-услуги" in text


@pytest.mark.asyncio
async def test_cmd_profile_single_profile_not_worse_than_before():
    """A counter-fact of the boundaries: for a tenant with ONE profile the output is no worse than before —
    the name/state/keywords/region are still in place."""
    p1 = _profile("Основной", True, keywords=[{"all_of": ["gradnja"]}])
    session = _make_env([p1])
    text = await _run_cmd_profile(session)
    assert "Основной" in text
    assert "gradnja" in text
    assert "client@example.com" in text


@pytest.mark.asyncio
async def test_cmd_profile_null_is_active_shown_as_disabled():
    """‼️ NULL is_active — an oversight of create_profile bypassing the web form; the matching
    (``WHERE p.is_active = true``) on NULL silently returns nothing — the list must show the profile as DISABLED, not as active.
    """
    p_null = _profile("Без формы", None, keywords=[{"all_of": ["x"]}])
    session = _make_env([p_null])
    text = await _run_cmd_profile(session)
    assert tg.bot_t("ru", "profile_state_disabled") in text
    assert tg.bot_t("ru", "profile_state_active") not in text
    assert tg.bot_t("ru", "profile_disabled_section") in text
    assert tg.bot_t("ru", "profile_active_section") not in text


@pytest.mark.asyncio
async def test_cmd_profile_active_first_disabled_separate_block():
    active = _profile("Активный", True)
    disabled = _profile("Отключённый", False)
    # The fake session simply returns the list — the order in production is guaranteed by the SQL
    # ORDER BY (see test_list_profiles_order_by_sql), here it is already correct.
    session = _make_env([active, disabled])
    text = await _run_cmd_profile(session)
    assert text.index("Активный") < text.index("Отключённый")
    assert tg.bot_t("ru", "profile_active_section") in text
    assert tg.bot_t("ru", "profile_disabled_section") in text


@pytest.mark.asyncio
async def test_cmd_profile_match_all_shown_without_jargon():
    """The client need not know the word ``match_all`` — the mode is named in words."""
    p = _profile("Весь поток", True, match_all=True, keywords=[])
    session = _make_env([p])
    text = await _run_cmd_profile(session)
    assert "match_all" not in text
    assert tg.bot_t("ru", "profile_all_tenders") in text


def test_list_profiles_order_by_sql():
    """A guard WITHOUT a DB: the ORDER BY order is is_active DESC NULLS LAST, name, id,
    otherwise the /profile list is not deterministic between runs (the Telegram limit cut will shuffle the profiles between messages).
    """
    import asyncio

    captured: dict[str, str] = {}

    async def fake_execute(stmt, params=None):
        captured["sql"] = str(stmt)
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    session = MagicMock()
    session.execute = AsyncMock(side_effect=fake_execute)
    asyncio.run(tg._list_profiles(session, "tenant-1"))
    assert (
        "ORDER BY profiles.is_active DESC NULLS LAST, profiles.name, profiles.id"
        in captured["sql"]
    )


def test_render_profile_truncates_by_profile_count_with_hidden_note():
    """The Telegram limit of 4096 characters — we cut by WHOLE profiles and explicitly name
    how many are hidden (a small ``max_len`` emulates the limit without inflating the test data to the real 4096 characters).
    """
    profiles = [
        SimpleNamespace(
            id=i,
            name=f"Профиль {i}",
            is_active=True,
            match_all=False,
            keywords_include=[{"all_of": ["слово"]}],
            regions=[],
        )
        for i in range(5)
    ]
    data = {"tenant": SimpleNamespace(tier="pro", access_until=None)}
    text = tg._render_profile(data, profiles, "ru", max_len=420)
    shown = sum(1 for p in profiles if p.name in text)
    assert 0 < shown < 5
    hidden = 5 - shown
    assert (
        tg.bot_t("ru", "profile_hidden_count").format(
            n=hidden, url=tg._PROFILES_CABINET_URL
        )
        in text
    )
    assert len(text) <= 420
