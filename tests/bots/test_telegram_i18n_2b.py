"""Guards of the i18n of the Telegram bot (Phase 2b): onboarding, command handlers, the funnel.

The scope 2b — the chrome of the onboarding (FSM), the command handlers (/profile, /reset,
/status, /list, /watching), the service gates and the funnel card. The data
(the titles of the tenders/winners/the content language) are NOT localised — only the chrome.

The method (as in 2a): under en/sr there are NO Russian letters ``U+044B U+044D U+0449 U+044A U+0451 U+0439`` in the chrome (they are absent both in Serbian Latin and in English), under ru the previous Russian canon. The data-values are excluded from the check: the test data are chosen in Latin, so that any Russian letter = untranslated chrome.

Two AXES (the project rules): ``users.locale`` (the chrome, the axis of this test) ≠
``profiles.target_language`` (AI content; in the wizard the user chooses exactly
it, and it is NOT the language of the onboarding chrome).
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.bots import telegram as tg

RU_ONLY = re.compile(r"[ыэщъёйЫЭЩЪЁЙ]")


# --------------------------------------------------------------------------- #
# Common fixtures.
# --------------------------------------------------------------------------- #
class _State:
    """A FSM stub with a data store (for ui_locale in the onboarding steps)."""

    def __init__(self, data: dict | None = None) -> None:
        self._data = dict(data or {})
        self.state = None

    async def clear(self) -> None:
        self._data = {}
        self.state = None

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, **kwargs) -> None:
        self._data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self._data)


def _message(uid: int = 777) -> AsyncMock:
    m = AsyncMock()
    m.from_user = SimpleNamespace(id=uid)
    m.answer = AsyncMock()
    return m


def _callback(uid: int = 777, chat_id: int = 555, html: str = "") -> AsyncMock:
    cb = AsyncMock()
    cb.from_user = SimpleNamespace(id=uid)
    cb.message = AsyncMock()
    cb.message.chat = SimpleNamespace(id=chat_id)
    cb.message.html_text = html
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


@asynccontextmanager
async def _fake_scope():
    yield AsyncMock()


def _patch_scope():
    return patch("src.bots.telegram.session_scope", _fake_scope)


def _patch_locale(locale: str):
    return patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value=locale))


def _last(mock: AsyncMock) -> str:
    return mock.await_args.args[0]


# --------------------------------------------------------------------------- #
# Pure summary functions: _build_summary, _render_profile.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("locale", ["en", "sr"])
def test_build_summary_chrome_localized(locale):
    # Data in Latin: the content language sr → «Srpski», the words in Latin.
    data = {"language": "sr", "keywords": ["gradnja", "putevi"], "region": "RS-All"}
    text = tg._build_summary(data, locale)
    assert not RU_ONLY.search(
        text
    ), f"Russian letters in the summary under {locale}: {text!r}"


def test_build_summary_ru_canon_preserved():
    data = {"language": "sr", "keywords": ["gradnja"], "region": "RS-All"}
    text = tg._build_summary(data, "ru")
    assert "настройки шаблона" in text
    assert RU_ONLY.search(text)  # the ru canon contains U+0439 (settings)


def _profile_data(
    target_language: str = "sr",
    tier: str = "pro",
    email: str | None = None,
    access_until=None,
    is_active: bool | None = True,
    name: str | None = None,
    match_all: bool = False,
) -> tuple[dict, list]:
    profile = SimpleNamespace(
        keywords_include=[{"all_of": ["gradnja"]}],
        regions=[],
        target_language=target_language,
        is_active=is_active,
        name=name,
        match_all=match_all,
    )
    data = {
        "tenant": SimpleNamespace(tier=tier, access_until=access_until),
    }
    if email is not None:
        data["user"] = SimpleNamespace(email=email)
    return data, [profile]


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_render_profile_chrome_localized(locale):
    data, profiles = _profile_data()
    text = tg._render_profile(data, profiles, locale)
    assert not RU_ONLY.search(
        text
    ), f"Russian letters in /profile under {locale}: {text!r}"


def test_render_profile_ru_canon_preserved():
    data, profiles = _profile_data()
    text = tg._render_profile(data, profiles, "ru")
    assert "Шаблоны поиска" in text
    assert RU_ONLY.search(text)  # the word for “Keywords” contains U+044B


def test_render_profile_tier_from_common_catalog():
    """The plan name — common lexicon: Pro is the same in all languages."""
    from src.i18n_common import common_t

    for loc in ("ru", "sr", "en"):
        data, profiles = _profile_data(tier="pro")
        assert common_t(loc, "plan_pro") in tg._render_profile(data, profiles, loc)


def test_render_profile_shows_account_email():
    """The card must show WHICH account this chat is linked to (owner 06.09)."""
    data, profiles = _profile_data(email="owner@example.com")
    text = tg._render_profile(data, profiles, "ru")
    assert "owner@example.com" in text


def test_render_profile_no_email_shows_dash():
    data, profiles = _profile_data(email=None)
    text = tg._render_profile(data, profiles, "ru")
    assert "—" in text


def test_render_profile_access_expired():
    from datetime import datetime, timezone

    now = datetime(2026, 9, 6, tzinfo=timezone.utc)
    expired = datetime(2026, 8, 18, 16, 3, tzinfo=timezone.utc)
    data, profiles = _profile_data(access_until=expired)
    text = tg._render_profile(data, profiles, "ru", now=now)
    # The expectation comes from the FACT of the render, not from the template: the client must see the DATE,
    # when the access ended (owner, 06.09.2026), in the display zone Europe/Belgrade.
    assert "истёк 18.08.2026" in text


def test_render_profile_access_active_days():
    from datetime import datetime, timezone

    now = datetime(2026, 9, 6, tzinfo=timezone.utc)
    future = datetime(2026, 9, 11, tzinfo=timezone.utc)
    data, profiles = _profile_data(access_until=future)
    text = tg._render_profile(data, profiles, "ru", now=now)
    assert "до <b>11.09.2026</b>" in text
    assert "осталось 5 дн." in text


def test_render_profile_access_unlimited():
    data, profiles = _profile_data(access_until=None)
    text = tg._render_profile(data, profiles, "ru")
    assert tg.bot_t("ru", "status_access_unlimited") in text


def test_render_profile_no_tenant_skips_access_line():
    """tenant=None (see test_telegram_fsm) — the render does not fall, there is no access line."""
    profile = SimpleNamespace(
        keywords_include=[{"all_of": ["gradnja"]}],
        regions=[],
        target_language="ru",
        is_active=True,
        name=None,
        match_all=False,
    )
    text = tg._render_profile({"tenant": None}, [profile], "ru")
    for key in (
        "status_access_unlimited",
        "status_access_expired",
        "status_access_soon",
    ):
        assert tg.bot_t("ru", key) not in text


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_render_profile_account_and_access_chrome_localized(locale):
    data, profiles = _profile_data(email="test@example.com", access_until=None)
    text = tg._render_profile(data, profiles, locale)
    assert not RU_ONLY.search(
        text
    ), f"Russian letters in /profile under {locale}: {text!r}"


# --------------------------------------------------------------------------- #
# Onboarding: the FSM steps take ui_locale from the state.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_onboarding_start_welcome_localized(locale):
    message = _message()
    state = _State()
    await tg._start_onboarding(message, state, locale)
    assert not RU_ONLY.search(_last(message.answer))
    assert (await state.get_data())["ui_locale"] == locale


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_onboarding_keywords_prompt_localized(locale):
    cb = _callback()
    cb.data = "ob:lang:en"
    state = _State({"ui_locale": locale})
    await tg.on_language(cb, state)
    assert not RU_ONLY.search(_last(cb.message.edit_text))


@pytest.mark.asyncio
async def test_onboarding_keywords_empty_localized_sr():
    message = _message()
    message.text = "   "  # empty after strip
    state = _State({"ui_locale": "sr"})
    await tg.on_keywords(message, state)
    assert not RU_ONLY.search(_last(message.answer))


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_onboarding_summary_step_localized(locale):
    cb = _callback()
    cb.data = "ob:region:RS-All"
    state = _State({"ui_locale": locale, "language": "sr", "keywords": ["put"]})
    await tg.on_region(cb, state)
    assert not RU_ONLY.search(_last(cb.message.edit_text))


@pytest.mark.asyncio
async def test_onboarding_ru_canon_preserved():
    message = _message()
    state = _State()
    await tg._start_onboarding(message, state, "ru")
    text = _last(message.answer)
    assert "Добро пожаловать" in text
    assert RU_ONLY.search(text)  # the word for “language” contains U+044B


# --------------------------------------------------------------------------- #
# /status: chrome + plan from the common lexicon.
# --------------------------------------------------------------------------- #
def _status_summary(
    tier="pro", access=None, frozen=False, days=None, email="klient@example.com"
) -> dict:
    return {
        "tier": tier,
        "tier_label": "неважно",
        "access_until": access,
        "frozen": frozen,
        "days_left": days,
        "email": email,
    }


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_cmd_status_localized(locale):
    from datetime import datetime, timezone

    message = _message()
    summary = _status_summary(
        tier="pro", access=datetime(2030, 1, 1, tzinfo=timezone.utc), days=42
    )
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="t1")),
        patch("src.bots.telegram.get_status_summary", AsyncMock(return_value=summary)),
    ):
        await tg.cmd_status(message)
    text = _last(message.answer)
    assert not RU_ONLY.search(
        text
    ), f"Russian letters in /status under {locale}: {text!r}"


@pytest.mark.asyncio
async def test_cmd_status_ru_canon_preserved():
    from datetime import datetime, timezone

    message = _message()
    summary = _status_summary(
        tier="pro", access=datetime(2030, 1, 1, tzinfo=timezone.utc), days=42
    )
    with (
        _patch_scope(),
        _patch_locale("ru"),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="t1")),
        patch("src.bots.telegram.get_status_summary", AsyncMock(return_value=summary)),
    ):
        await tg.cmd_status(message)
    text = _last(message.answer)
    assert "Статус подписки" in text
    # The expectation comes from the FACT of the output: /status must name the account and the DATE of the end
    # of the access (owner, 06.09.2026). The previous assertion looked for the letter U+0451 in “still active”
    # and would stay silent about the loss of both lines.
    assert "klient@example.com" in text
    assert "01.01.2030" in text


# --------------------------------------------------------------------------- #
# /watching, /list, /profile.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_cmd_watching_list_localized(locale):
    message = _message()
    item = SimpleNamespace(winner_name="Firma DOO", winner_pib="123456789", sub_id=7)
    result = SimpleNamespace(status="ok", items=[item])
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch(
            "src.bots.award_watch_service.run_list_watching",
            AsyncMock(return_value=result),
        ),
    ):
        await tg.cmd_watching(message)
    text = _last(message.answer)
    assert not RU_ONLY.search(
        text
    ), f"Russian letters in /watching under {locale}: {text!r}"


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_cmd_watching_empty_localized(locale):
    message = _message()
    result = SimpleNamespace(status="ok", items=[])
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch(
            "src.bots.award_watch_service.run_list_watching",
            AsyncMock(return_value=result),
        ),
    ):
        await tg.cmd_watching(message)
    assert not RU_ONLY.search(_last(message.answer))


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_cmd_list_localized(locale):
    message = _message()
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="t-1")),
        patch("src.bots.manage.load_my_tenders", AsyncMock(return_value=([], False))),
    ):
        await tg.cmd_list(message)
    assert not RU_ONLY.search(_last(message.answer))


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_cmd_profile_not_found_localized(locale):
    message = _message()
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch("src.bots.telegram.get_subscriber", AsyncMock(return_value=None)),
    ):
        await tg.cmd_profile(message)
    assert not RU_ONLY.search(_last(message.answer))


# --------------------------------------------------------------------------- #
# The funnel card: on_tender_status — a toast, buttons, the “Status: …” line.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.asyncio
async def test_on_tender_status_chrome_localized(locale):
    cb = _callback(html="Тендер №1")
    cb.data = "st:active:11111111-1111-1111-1111-111111111111"
    with (
        _patch_scope(),
        _patch_locale(locale),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="t1")),
        patch("src.bots.telegram.is_tenant_frozen", AsyncMock(return_value=False)),
        patch("src.bots.telegram.set_tender_status", AsyncMock()),
        patch("src.bots.telegram.get_tender_status", AsyncMock(return_value=None)),
    ):
        await tg.on_tender_status(cb)
    toast = cb.answer.await_args.args[0]
    new_text = cb.message.edit_text.await_args.args[0]
    # The toast label and the status line are localised; the data (“Tender №1”) are excluded —
    # we check only the APPENDED chrome part (after the original text).
    appended = new_text.replace("Тендер №1", "")
    assert not RU_ONLY.search(toast), f"Russian toast under {locale}: {toast!r}"
    assert not RU_ONLY.search(appended), f"Russian status line under {locale}"


@pytest.mark.asyncio
async def test_on_tender_status_frozen_gate_localized_sr():
    cb = _callback()
    cb.data = "st:active:11111111-1111-1111-1111-111111111111"
    with (
        _patch_scope(),
        _patch_locale("sr"),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="t1")),
        patch("src.bots.telegram.is_tenant_frozen", AsyncMock(return_value=True)),
    ):
        await tg.on_tender_status(cb)
    assert not RU_ONLY.search(cb.answer.await_args.args[0])


def test_apply_status_line_strips_all_locale_prefixes():
    """The previous status line in ANY language is cut out on redraw."""
    ru = tg._apply_status_line("Тендер", "✅ Участвую", "ru")
    # Locale change to en: the old ru line “Status: …” must disappear.
    en = tg._apply_status_line(ru, "✅ Participating", "en")
    assert "Статус:" not in en
    assert en.count("Status:") == 1
    assert "✅ Participating" in en


@pytest.mark.parametrize("locale", ("ru", "sr", "en"))
def test_access_line_shows_date_in_every_locale(locale):
    """The access date is visible in all three locales and in both branches (active/expired).

    The assertion is on the FACT of the line, not on the template: a test through ``bot_t(...)`` turns green
    for any text and would not notice the loss of the date.
    """
    from datetime import datetime, timezone

    now = datetime(2026, 9, 6, 11, 0, tzinfo=timezone.utc)
    active = tg._access_line(
        datetime(2026, 9, 11, 8, 21, tzinfo=timezone.utc), now, locale
    )
    expired = tg._access_line(
        datetime(2026, 8, 18, 16, 3, tzinfo=timezone.utc), now, locale
    )
    assert "11.09.2026" in active
    assert "18.08.2026" in expired
    assert tg._access_line(None, now, locale) == tg.bot_t(
        locale, "status_access_unlimited"
    )


@pytest.mark.asyncio
async def test_cmd_status_names_account_and_expiry_date():
    """/status names the account and the end date — both lines by one fact."""
    from datetime import datetime, timezone

    message = _message()
    summary = _status_summary(
        tier="pro",
        access=datetime(2026, 9, 11, 8, 21, tzinfo=timezone.utc),
        days=4,
        email="victor@example.com",
    )
    with (
        _patch_scope(),
        _patch_locale("ru"),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value="t1")),
        patch("src.bots.telegram.get_status_summary", AsyncMock(return_value=summary)),
    ):
        await tg.cmd_status(message)
    text = _last(message.answer)
    assert "Аккаунт: victor@example.com" in text
    assert "11.09.2026" in text


@pytest.mark.asyncio
async def test_onboarding_step_sets_bot_locale_from_fsm():
    """The wizard steps do not go to the DB for the language — ``bot_locale`` is taken from the FSM (sr → Latin)."""
    from src.bots.sr_latin import bot_locale

    bot_locale.set(None)
    message = _message()
    message.text = "ремонт"
    await tg.on_keywords(message, _State({"ui_locale": "sr"}))
    assert bot_locale.get() == "sr"
