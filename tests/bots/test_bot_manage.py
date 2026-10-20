"""The bot: “📋 My tenders” (/list), the template pause (/pause), the buttons under /profile.

The tenant isolation by DATA (a foreign template is not switched, the list — only one’s own
tenders) — in ``tests/db/test_rls_isolation.py::
test_bot_pause_and_list_isolated_under_app_role`` (under ``tendersrb_app``). Here — a unit test on mocks: the branching of the toggle and the limit gate, the markup, the buttons. The expectations — literals from the catalog at the time of writing, not via ``bot_t``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from src.bots import manage
from src.bots import telegram as tg

TID = "11111111-1111-1111-1111-111111111111"
PID = "22222222-2222-2222-2222-222222222222"
TENDER = "33333333-3333-3333-3333-333333333333"


@asynccontextmanager
async def _fake_scope():
    yield AsyncMock()


def _message(uid: int = 777) -> AsyncMock:
    m = AsyncMock()
    m.from_user = SimpleNamespace(id=uid)
    m.answer = AsyncMock()
    return m


def _profile(name: str, active: bool | None, pid: str | None = None):
    return SimpleNamespace(id=pid or str(uuid4()), name=name, is_active=active)


def _patch_svc(*, profile=None, tier="pro", enabled=0, set_ok=True):
    """Stubs of the common cabinet service (profile_service) — the write does not go to the DB."""
    setter = AsyncMock(return_value=set_ok)
    return setter, [
        patch(
            "src.api.cabinet.profile_service.get_profile",
            AsyncMock(return_value=profile),
        ),
        patch(
            "src.api.cabinet.profile_service.get_tenant_tier",
            AsyncMock(return_value=tier),
        ),
        patch(
            "src.api.cabinet.profile_service.count_enabled_profiles",
            AsyncMock(return_value=enabled),
        ),
        patch("src.api.cabinet.profile_service.set_profile_active", setter),
    ]


async def _toggle(profile, **kw):
    setter, patches = _patch_svc(profile=profile, **kw)
    for p in patches:
        p.start()
    try:
        outcome = await manage.toggle_profile(AsyncMock(), TID, PID)
    finally:
        for p in patches:
            p.stop()
    return outcome, setter


# --------------------------------------------------------------------------- #
# The template toggle.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_toggle_active_profile_pauses_via_cabinet_service():
    outcome, setter = await _toggle(_profile("A", True, PID))
    assert outcome == "paused"
    setter.assert_awaited_once()
    assert setter.await_args.kwargs == {
        "tenant_id": TID,
        "profile_id": PID,
        "is_active": False,
    }


@pytest.mark.asyncio
async def test_toggle_paused_profile_resumes_within_limit():
    # lite: a limit of 1 template, 0 working → can be enabled.
    outcome, setter = await _toggle(_profile("A", False, PID), tier="lite", enabled=0)
    assert outcome == "resumed"
    assert setter.await_args.kwargs["is_active"] is True


@pytest.mark.asyncio
async def test_toggle_resume_over_tier_limit_refused_without_write():
    # lite: a limit of 1, 1 already working → a refusal, no record (the gate as in the cabinet /activate).
    outcome, setter = await _toggle(_profile("A", False, PID), tier="lite", enabled=1)
    assert outcome == "limit"
    setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_toggle_foreign_or_missing_profile_is_gone_without_write():
    # get_profile searches by id AND tenant_id: a foreign template is not found.
    outcome, setter = await _toggle(None)
    assert outcome == "gone"
    setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_all_stops_at_tier_limit():
    profiles = [_profile(n, False) for n in ("A", "B", "C")]
    setter, patches = _patch_svc(tier="lite", enabled=0)
    patches.append(
        patch.object(manage, "load_profiles", AsyncMock(return_value=profiles))
    )
    for p in patches:
        p.start()
    try:
        result = await manage.set_all_profiles(AsyncMock(), TID, True)
    finally:
        for p in patches:
            p.stop()
    assert result == manage.BulkResult(changed=1, blocked=2)
    assert setter.await_count == 1
    assert setter.await_args.kwargs["profile_id"] == profiles[0].id
    assert (
        manage.bulk_toast(result, True, "ru")
        == "✅ Включено шаблонов: 1. Ещё 2 не включены — лимит тарифа."
    )


@pytest.mark.asyncio
async def test_pause_all_touches_only_active():
    profiles = [_profile("A", True), _profile("B", False), _profile("C", None)]
    setter, patches = _patch_svc()
    patches.append(
        patch.object(manage, "load_profiles", AsyncMock(return_value=profiles))
    )
    for p in patches:
        p.start()
    try:
        result = await manage.set_all_profiles(AsyncMock(), TID, False)
    finally:
        for p in patches:
            p.stop()
    assert result == manage.BulkResult(changed=1, blocked=0)
    assert setter.await_args.kwargs == {
        "tenant_id": TID,
        "profile_id": profiles[0].id,
        "is_active": False,
    }


# --------------------------------------------------------------------------- #
# The pause screen.
# --------------------------------------------------------------------------- #


def test_pause_screen_marks_and_bulk_buttons():
    profiles = [_profile("Gradnja <b>", True, PID), _profile("IT", False)]
    text = manage.pause_text(profiles, "ru")
    assert "✅ Gradnja &lt;b&gt; — работает" in text
    assert "⏸ IT — на паузе" in text
    kb = manage.pause_keyboard(profiles, "ru", "https://app.nab.rs/app")
    rows = kb.inline_keyboard
    assert rows[0][0].text == "✅ Gradnja <b>"
    assert rows[0][0].callback_data == f"pz:t:{PID}"
    assert [b.text for b in rows[2]] == ["⏸ Пауза всем", "▶ Включить все"]
    assert [b.callback_data for b in rows[2]] == ["pz:a:0", "pz:a:1"]
    assert rows[3][0].url == "https://app.nab.rs/app"
    assert all(len(b.callback_data or "") <= 64 for r in rows for b in r)


def test_callback_parsers_reject_garbage():
    assert manage.parse_toggle_callback(f"pz:t:{PID}") == PID
    assert manage.parse_toggle_callback("pz:t:not-a-uuid") is None
    assert manage.parse_all_callback("pz:a:1") is True
    assert manage.parse_all_callback("pz:a:0") is False
    assert manage.parse_all_callback("pz:a:2") is None
    assert manage.parse_list_callback("ls:20") == (20, ())
    assert manage.parse_list_callback("ls:20:0.7") == (20, (0, 7))
    assert manage.parse_list_callback("ls:-1") is None
    assert manage.parse_list_callback("ls:20:x") is None


@pytest.mark.asyncio
async def test_cmd_pause_renders_tenant_profiles():
    message = _message()
    profiles = [_profile("Gradnja", True, PID)]
    with (
        patch("src.bots.telegram.session_scope", _fake_scope),
        patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value="sr")),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value=TID)),
        patch.object(manage, "load_profiles", AsyncMock(return_value=profiles)),
    ):
        await tg.cmd_pause(message)
    text = message.answer.await_args.args[0]
    assert text.startswith("⏸ <b>Pauza slanja</b>")
    assert "✅ Gradnja — radi" in text
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == f"pz:t:{PID}"


# --------------------------------------------------------------------------- #
# “📋 My tenders”.
# --------------------------------------------------------------------------- #


def _item(n: int = 1, **kw):
    base = dict(
        tender_id=TENDER,
        status=None,
        profile_id=PID,
        title=f"Nabavka {n}",
        source_url="https://jnportal.src-ujn.example.invalid/tender-eo/1?a=1&b=2",
        deadline_at=datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc),
        amount_original=1200000,
        currency_original="RSD",
        amount_eur=10200,
        is_amount_estimated=False,
    )
    base.update(kw)
    return manage.MyTender(**base)


def test_my_tenders_empty_explains_and_links_cabinet():
    text, kb = manage.render_my_tenders(
        [], offset=0, has_more=False, locale="ru", cabinet_base="https://app.nab.rs"
    )
    assert text == (
        "📋 <b>Мои тендеры</b>\n\n"
        "Пока пусто: тендеров в работе нет, и за 30 дн. бот ничего не присылал.\n\n"
        "Тендеры приходят автоматически по вашим шаблонам. Нажмите «⭐ Следить» или "
        "«✅ Участвую» на карточке — и тендер появится в этом списке."
    )
    assert kb.inline_keyboard == [[kb.inline_keyboard[0][0]]] and kb.inline_keyboard[0][
        0
    ].url == ("https://app.nab.rs/app/my")


def test_my_tenders_list_line_buttons_and_more():
    items = [
        _item(1, status="watching", title="<Radovi> & co"),
        _item(2, source_url=None, profile_id=None),
    ]
    text, kb = manage.render_my_tenders(
        items, offset=0, has_more=True, locale="ru", cabinet_base="https://app.nab.rs"
    )
    lines = text.split("\n")
    assert lines[0] == "📋 <b>Мои тендеры</b>"
    assert lines[1] == "В работе и присланные в Telegram по вашим шаблонам за 30 дн."
    assert lines[3] == (
        '1. <a href="https://jnportal.src-ujn.example.invalid/tender-eo/1?a=1&amp;b=2">'
        "&lt;Radovi&gt; &amp; co</a>"
    )
    assert lines[4].startswith("   👀 Изучаю · 🗓 12.10.2026 10:00 · 💰 1")
    assert lines[6] == "2. Nabavka 2"  # without a link to the source — just text
    rows = kb.inline_keyboard
    # «📂 N» only for a tender with a known template (without profile_id the cabinet returns 404).
    assert [(b.text, b.url) for b in rows[0]] == [
        ("📂 1", f"https://app.nab.rs/app/tenders/{TENDER}?profile_id={PID}")
    ]
    assert [(b.text, b.callback_data) for b in rows[1]] == [
        ("Ещё ▸", "ls:2:0")
    ]  # offset + shown
    assert rows[2][0].url == "https://app.nab.rs/app/my"


def test_my_tenders_second_page_has_back_and_numbering():
    text, kb = manage.render_my_tenders(
        [_item(11)],
        offset=10,
        has_more=False,
        locale="en",
        cabinet_base="https://app.nab.rs",
    )
    assert "\n11. <a " in text
    assert [(b.text, b.callback_data) for b in kb.inline_keyboard[1]] == [
        ("◂ Back", "ls:0")
    ]


def test_my_tenders_back_returns_to_previous_page_start():
    """A page cut by the limit is shorter than PAGE_SIZE: “Back” must not repeat the records."""
    # The 1st page showed 4 records (cut) → “More” leads to offset 4 with history [0].
    _, kb = manage.render_my_tenders(
        [_item(i) for i in range(1, 5)],
        offset=0,
        has_more=True,
        locale="ru",
        cabinet_base="https://app.nab.rs",
    )
    assert kb.inline_keyboard[-2][-1].callback_data == "ls:4:0"
    # From the 2nd page (offset 4, history [0]) “Back” = the start of the 1st, not 4 − 10 → 0 by coincidence.
    _, kb2 = manage.render_my_tenders(
        [_item(5)],
        offset=14,
        has_more=True,
        locale="ru",
        cabinet_base="https://app.nab.rs",
        history=(0, 9),
    )
    nav = {b.text: b.callback_data for b in kb2.inline_keyboard[-2]}
    assert nav == {"◂ Назад": "ls:9:0", "Ещё ▸": "ls:15:0.9.14"}


def test_list_callback_stays_within_telegram_64_bytes():
    cb = manage._list_callback(900, tuple(range(100, 200, 5)))
    assert len(cb.encode()) <= 64 and cb.startswith("ls:900:")


def test_my_tenders_empty_sr_en_name_real_card_buttons():
    for loc, expected in (
        (
            "sr",
            "Pritisnite „⭐ Prati“ ili „✅ Učestvujem“ na kartici",
        ),
        ("en", "Tap “⭐ Follow” or “✅ Participate” on a card"),
    ):
        text, _ = manage.render_my_tenders(
            [], offset=0, has_more=False, locale=loc, cabinet_base="https://x.rs"
        )
        assert expected in text and "30" in text


def test_pause_toast_and_hint_name_all_channels():
    from src.bots.i18n import BOT

    assert BOT["ru"]["pause_toast_paused"] == (
        "⏸ Шаблон на паузе — тендеры по нему не придут ни в Telegram, ни на email."
    )
    assert BOT["en"]["pause_toast_paused"].endswith("in Telegram or by email.")
    assert BOT["sr"]["pause_toast_paused"].endswith("ni na Telegram, ni na imejl.")
    assert BOT["ru"]["pause_hint"].endswith(
        "Пауза действует на все каналы — Telegram и email."
    )


def test_my_tenders_respects_telegram_limit():
    items = [
        _item(i, title="X" * 300, source_url="https://e.rs/" + "y" * 2000)
        for i in range(10)
    ]
    text, kb = manage.render_my_tenders(
        items, offset=0, has_more=False, locale="ru", cabinet_base="https://app.nab.rs"
    )
    assert len(text) <= 4096
    shown = sum(1 for ln in text.split("\n") if ln[:1].isdigit())
    assert 0 < shown < 10
    nav = kb.inline_keyboard[-2]
    assert nav[0].callback_data == f"ls:{shown}:0"


@pytest.mark.asyncio
async def test_cmd_list_non_empty_sends_list():
    message = _message()
    with (
        patch("src.bots.telegram.session_scope", _fake_scope),
        patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value="ru")),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value=TID)),
        patch.object(
            manage, "load_my_tenders", AsyncMock(return_value=([_item(1)], False))
        ) as loader,
    ):
        await tg.cmd_list(message)
    assert loader.await_args.kwargs == {"offset": 0}
    text = message.answer.await_args.args[0]
    assert text.startswith("📋 <b>Мои тендеры</b>")
    assert "1. <a href=" in text
    assert message.answer.await_args.kwargs["disable_web_page_preview"] is True


# --------------------------------------------------------------------------- #
# /profile, /status, the menu.
# --------------------------------------------------------------------------- #


def _assert_manage_keyboard(markup):
    rows = markup.inline_keyboard
    assert rows[0][0].text == "⚙️ Шаблоны в кабинете"
    assert rows[0][0].url == "https://app.nab.rs/app"
    assert [(b.text, b.callback_data) for b in rows[1]] == [
        ("⏸ Пауза", "mg:pause"),
        ("📋 Мои тендеры", "mg:list"),
    ]
    assert (rows[2][0].text, rows[2][0].callback_data) == ("🗂 Площадки", "mg:src")


@pytest.mark.asyncio
async def test_cmd_profile_has_manage_buttons():
    message = _message()
    tenant = SimpleNamespace(id=TID, tier="pro", access_until=None)
    data = {"tenant": tenant, "user": SimpleNamespace(email="a@b.rs")}
    profile = SimpleNamespace(
        id=PID,
        name="Gradnja",
        is_active=True,
        match_all=False,
        keywords_include={"keywords": ["beton"]},
        regions=[],
        enabled_source_keys=[],
    )
    with (
        patch("src.bots.telegram.session_scope", _fake_scope),
        patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value="ru")),
        patch("src.bots.telegram.get_subscriber", AsyncMock(return_value=data)),
        patch("src.bots.telegram._list_profiles", AsyncMock(return_value=[profile])),
        patch(
            "src.bots.telegram.list_sources_for_selection", AsyncMock(return_value=[])
        ),
    ):
        await tg.cmd_profile(message)
    _assert_manage_keyboard(message.answer.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_cmd_status_has_manage_buttons():
    message = _message()
    summary = {"tier": "pro", "access_until": None, "email": "a@b.rs"}
    with (
        patch("src.bots.telegram.session_scope", _fake_scope),
        patch("src.bots.telegram.resolve_bot_locale", AsyncMock(return_value="ru")),
        patch("src.bots.telegram.resolve_tenant_id", AsyncMock(return_value=TID)),
        patch("src.bots.telegram.get_status_summary", AsyncMock(return_value=summary)),
    ):
        await tg.cmd_status(message)
    _assert_manage_keyboard(message.answer.await_args.kwargs["reply_markup"])


def test_profile_overflow_link_has_scheme():
    assert tg._PROFILES_CABINET_URL == "https://app.nab.rs/app"


def test_pause_in_menu_and_help_all_languages():
    assert dict(tg._PUBLIC_COMMANDS)["pause"] == "Пауза и включение шаблонов"
    assert tg._COMMAND_DESCRIPTIONS["sr"]["pause"] == "Pauza i uključivanje šablona"
    assert tg._COMMAND_DESCRIPTIONS["en"]["pause"] == "Pause and resume templates"
    assert (
        dict(tg._PUBLIC_COMMANDS)["list"]
        == "Мои тендеры: в работе и последние присланные"
    )
    from src.bots.i18n import BOT

    assert "/pause — пауза и включение шаблонов" in BOT["ru"]["help"]
    assert "/pause — pauza i uključivanje šablona" in BOT["sr"]["help"]
    assert "/list — my tenders: in progress and recently sent" in BOT["en"]["help"]
    assert "в разработке" not in BOT["ru"]["help"]
