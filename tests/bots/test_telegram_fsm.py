"""Unit tests of the FSM onboarding of the Telegram bot (Sprint-15).

The network and the DB are not touched: FSMContext is substituted by a light in-memory fake,
``session_scope`` is patched to a mock session. We check the run through all 4 states,
the write to the DB (create for a new one / in-place update after /reset), the duplicates and /reset.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bots.telegram import (
    OnboardingFSM,
    cmd_reset,
    cmd_start,
    on_confirm,
    on_keywords,
    on_language,
    on_region,
)

# --------------------------------------------------------------------------- #
# Fakes and mock factories.
# --------------------------------------------------------------------------- #


class FakeState:
    """An in-memory replacement of the aiogram ``FSMContext`` (data + the current state)."""

    def __init__(self) -> None:
        self._data: dict = {}
        self._state = None

    async def update_data(self, **kwargs) -> None:
        self._data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self._data)

    async def set_state(self, state) -> None:
        self._state = state

    async def get_state(self):
        return self._state

    async def clear(self) -> None:
        self._data = {}
        self._state = None


def _make_message(text: str = "", uid: int = 12345) -> AsyncMock:
    message = AsyncMock()
    message.text = text
    message.from_user = SimpleNamespace(id=uid)
    message.answer = AsyncMock()
    return message


def _make_callback(data: str = "", uid: int = 12345) -> AsyncMock:
    callback = AsyncMock()
    callback.data = data
    callback.from_user = SimpleNamespace(id=uid)
    callback.answer = AsyncMock()
    callback.message = AsyncMock()
    callback.message.edit_text = AsyncMock()
    return callback


def _make_db_session(existing_channel=None, existing_profile=None) -> AsyncMock:
    """A mock AsyncSession: ``add`` is synchronous, ``flush``/``commit`` are async.

    ``existing_channel`` — what ``_find_channel`` returns (None → a new subscriber → the create branch). ``existing_profile`` — what the selection of the last profile returns (for the update branch).

    VG-01 / 0022: save_subscriber makes execute calls in the order:
    - the update path: app_resolve_tenant_id → set_config → _find_channel → profile SELECT.
    - the create path: app_resolve_tenant_id (None) → set_config in create_subscriber.
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()

    find_result = MagicMock()
    find_result.scalars.return_value.first.return_value = existing_channel

    profile_result = MagicMock()
    profile_result.scalars.return_value.first.return_value = existing_profile

    noop_result = MagicMock()  # answers to set_config (the result is not used)

    if existing_channel is not None:
        # the update path (0022): resolve_tenant → set_config → find_channel → profile
        resolve_result = MagicMock()
        resolve_result.scalar.return_value = "tenant-1"
        session.execute = AsyncMock(
            side_effect=[resolve_result, noop_result, find_result, profile_result]
        )
    else:
        # the create path (0022): resolve_tenant (None) → set_config in create_subscriber
        resolve_none = MagicMock()
        resolve_none.scalar.return_value = None
        session.execute = AsyncMock(side_effect=[resolve_none, noop_result])

    session.get = AsyncMock(
        return_value=SimpleNamespace(id="user-1", tenant_id="tenant-1", locale="ru")
    )
    return session


def _patch_scope(session):
    """Substitutes ``session_scope`` with an async CM that returns the mock session."""

    @asynccontextmanager
    async def _scope():
        yield session

    return patch("src.bots.telegram.session_scope", _scope)


def _added(session, type_name: str):
    """Returns the first object added to the session, by the class name."""
    for call in session.add.call_args_list:
        obj = call.args[0]
        if type(obj).__name__ == type_name:
            return obj
    return None


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_onboarding_takes_sources_from_registry(monkeypatch) -> None:
    """The sources of a new profile — from the ``sources`` registry, not a literal (SRC-0a, R1).

    The registry returns two keys → the profile contains exactly them, and each has the moment of subscription
    (``source_opt_in``, R5) — otherwise the tenders of the source would count as “before the subscription”.
    """
    import src.bots.telegram as telegram

    async def _two(session):
        return ["jnportal", "rs_other"]

    monkeypatch.setattr(telegram, "default_source_keys_for_new_profile", _two)
    state = FakeState()
    await on_language(_make_callback("ob:lang:ru"), state)
    await on_keywords(_make_message("ремонт"), state)
    await on_region(_make_callback("ob:region:RS-00"), state)
    session = _make_db_session()
    with _patch_scope(session):
        await on_confirm(_make_callback("ob:confirm", uid=999), state)

    profile = _added(session, "Profile")
    assert profile.enabled_source_keys == ["jnportal", "rs_other"]
    assert set(profile.source_opt_in) == {"jnportal", "rs_other"}


@pytest.mark.asyncio
async def test_onboarding_happy_path() -> None:
    """A run through all 4 states → create for users + user_channels + profiles."""
    state = FakeState()

    # Step 1: language.
    await on_language(_make_callback("ob:lang:ru"), state)
    assert (await state.get_data())["language"] == "ru"
    assert await state.get_state() == OnboardingFSM.waiting_keywords

    # Step 2: keywords (comma-separated, at least one).
    await on_keywords(_make_message("строительство, ремонт дорог"), state)
    assert (await state.get_data())["keywords"] == [
        "строительство",
        "ремонт дорог",
    ]
    assert await state.get_state() == OnboardingFSM.waiting_region

    # Step 3: region.
    await on_region(_make_callback("ob:region:RS-00"), state)
    assert (await state.get_data())["region"] == "RS-00"
    assert await state.get_state() == OnboardingFSM.waiting_confirmation

    # Step 4: confirmation → a new subscriber → the create branch.
    session = _make_db_session()  # existing_channel=None
    with _patch_scope(session):
        await on_confirm(_make_callback("ob:confirm", uid=999), state)

    added_types = [type(c.args[0]).__name__ for c in session.add.call_args_list]
    assert "Tenant" in added_types
    assert "User" in added_types
    assert "UserChannel" in added_types
    assert "Profile" in added_types
    session.commit.assert_awaited()

    # The contents of the records match what was entered.
    channel = _added(session, "UserChannel")
    assert channel.channel == "telegram"
    assert channel.external_id == "999"

    profile = _added(session, "Profile")
    # keywords_include in the matcher’s format: OR between the words (each one — its own group)
    assert profile.keywords_include == [
        {"all_of": ["строительство"]},
        {"all_of": ["ремонт дорог"]},
    ]
    assert profile.regions == ["RS-00"]
    assert profile.target_language == "ru"
    # routable defaults: the profile is visible to routing/delivery (the fix of «a new subscriber
    # does not receive tenders»)
    assert profile.enabled_source_keys == ["jnportal"]
    assert profile.delivery_channels == ["telegram"]
    assert profile.languages == []
    assert profile.procedure_types == []
    assert profile.include_no_amount is True
    assert profile.min_days_until_deadline == 0
    assert profile.classifications == {"include": [], "exclude": []}

    tenant = _added(session, "Tenant")
    assert tenant.country_code == "RS"
    # R8: the internal canon of the tiers — 'lite', not the showcase alias 'free'.
    assert tenant.tier == "lite"

    # The FSM is cleared after the confirmation.
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_onboarding_all_regions_empty_filter() -> None:
    """The choice “All regions” (RS-All) writes an empty regions filter."""
    state = FakeState()
    await on_language(_make_callback("ob:lang:ru"), state)
    await on_keywords(_make_message("IT"), state)
    await on_region(_make_callback("ob:region:RS-All"), state)

    session = _make_db_session()
    with _patch_scope(session):
        await on_confirm(_make_callback("ob:confirm", uid=1), state)

    profile = _added(session, "Profile")
    assert profile.regions == []


@pytest.mark.asyncio
async def test_onboarding_keywords_empty_reprompts() -> None:
    """Empty keyword input does not move the FSM and asks to repeat."""
    state = FakeState()
    await on_language(_make_callback("ob:lang:ru"), state)

    message = _make_message("   ,  ,")  # separators only
    await on_keywords(message, state)

    # The state stayed at keywords, the data is not written.
    assert await state.get_state() == OnboardingFSM.waiting_keywords
    assert "keywords" not in (await state.get_data())
    message.answer.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_for_linked_account_does_not_touch_profile() -> None:
    """The wizard reached the confirmation only AFTER the chat was linked to the account: the profile
    from the cabinet stays as it is (earlier the words/region/activity and the AI-content
    language were overwritten), and the answer does not lie “saved”."""
    state = FakeState()
    await on_language(_make_callback("ob:lang:en"), state)
    await on_keywords(_make_message("медицина, оборудование"), state)
    await on_region(_make_callback("ob:region:RS-VO"), state)

    existing_profile = SimpleNamespace(
        target_language="ru",
        keywords_include=[{"all_of": ["старое"]}],
        regions=["RS-00"],
        is_active=False,
        match_all=False,
    )
    existing_channel = SimpleNamespace(
        user_id="user-1", channel="telegram", external_id="42", locale=None
    )
    session = _make_db_session(
        existing_channel=existing_channel, existing_profile=existing_profile
    )
    user = session.get.return_value  # users.locale — the cabinet axis

    cb = _make_callback("ob:confirm", uid=42)
    with _patch_scope(session):
        await on_confirm(cb, state)

    # Nothing is created or changed — the profile and user data are the previous ones.
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
    assert existing_profile.keywords_include == [{"all_of": ["старое"]}]
    assert existing_profile.regions == ["RS-00"]
    assert existing_profile.target_language == "ru"
    assert existing_profile.is_active is False
    assert user.locale == "ru"
    text = cb.message.edit_text.await_args.args[0]
    assert text.startswith("Шаблоны поиска настраиваются в кабинете")


@pytest.mark.asyncio
async def test_onboarding_duplicate_user() -> None:
    """/start with an existing binding: the hint about /profile, WITHOUT the /reset advice."""
    state = FakeState()
    session = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = "tenant-1"  # resolve → the binding exists (0022)
    session.execute = AsyncMock(return_value=result)

    message = _make_message(uid=555)
    with _patch_scope(session):
        await cmd_start(message, state)

    text = message.answer.await_args.args[0]
    assert text.startswith(
        "Аккаунт уже подключён. /profile — посмотреть шаблоны; "
        "настраиваются они в кабинете."
    )
    assert "/reset" not in text
    # Onboarding is not started.
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_reset_for_linked_account_points_to_cabinet_and_changes_nothing() -> None:
    """/reset of a linked one: onboarding is NOT started, the DB is not written, the answer is about
    the cabinet + a URL button to the template dashboard."""
    state = FakeState()
    message = _make_message(uid=777)

    session = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalar.return_value = "tenant-1"  # resolve → the binding exists
    session.execute = AsyncMock(return_value=result)
    with _patch_scope(session):
        await cmd_reset(message, state)

    assert await state.get_state() is None
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert text == (
        "Шаблоны поиска настраиваются в кабинете — там их можно изменить, "
        "выключить или добавить новый. Бот их не перезаписывает."
    )
    button = message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "⚙️ Шаблоны в кабинете"
    assert button.url == "https://app.nab.rs/app"


@pytest.mark.asyncio
async def test_onboarding_keywords_match_or_semantics_and_render() -> None:
    """keywords_include from the onboarding = OR by words (matcher.keywords_match
    sees each word as a separate group), and /profile renders the words of the new format as a flat list."""
    from src.bots.telegram import _render_profile
    from src.workers.matching import keywords_match

    state = FakeState()
    await on_language(_make_callback("ob:lang:ru"), state)
    await on_keywords(_make_message("video, skud"), state)
    await on_region(_make_callback("ob:region:RS-All"), state)

    session = _make_db_session()
    with _patch_scope(session):
        await on_confirm(_make_callback("ob:confirm", uid=7), state)

    profile = _added(session, "Profile")
    incl = profile.keywords_include
    assert incl == [{"all_of": ["video"]}, {"all_of": ["skud"]}]
    # OR by words: any of the words in the haystack → a match; none → no
    assert keywords_match(incl, [], "nabavka video opreme") is True
    assert keywords_match(incl, [], "skud kontrola pristupa") is True
    assert keywords_match(incl, [], "nabavka kancelarijskog materijala") is False

    # /profile renders the words of the new format (a list of groups → a flat list)
    rendered = _render_profile({"tenant": None}, [profile])
    assert "video" in rendered
    assert "skud" in rendered
