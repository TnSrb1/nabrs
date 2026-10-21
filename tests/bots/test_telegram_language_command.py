"""The /language command: the language of the DELIVERY CHANNEL, not the language of the cabinet (0038, stage 2c).

The owner’s decision: “pass the language on connection according to the interface, but the user must be able to change the language in TG by themselves”. Hence the contract that is pinned here:

* the choice in the bot is written to ``user_channels.locale`` of THAT channel from which
  the command came, and ``users.locale`` does NOT change (the cabinet and the letters are a separate axis; otherwise the language choice in the chat would silently switch the language of the cabinet);
* ‼️ one ``chat_id`` may serve SEVERAL active channels of different tenants
  (the MVP assumption of ``bind_channel_by_token``, in production it is so) — a foreign channel of the same chat stays untouched;
* the read of the channel goes AFTER ``set_current_tenant``: without the GUC the join policy 0022
  is fail-closed and returns 0 rows, and “one’s own” channel would not be found at all;
* the confirmation and the subsequent answers of the bot are already in the new language
  (``resolve_bot_locale`` looks into the channel before the cabinet).

The session is an in-memory mini-DB, not a bare AsyncMock: it REPRODUCES the meaning of the
join policy (without the GUC — an error, with the GUC only the channels of its own tenant are visible).
Otherwise the multi-tenancy test would prove itself. We keep the same honesty boundary as in ``test_delivery_e2e``: the SQL is not executed here, the semantics of the policies are proved by a live PostgreSQL (``tests/db/test_rls_isolation.py``).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import Message

from src.bots import telegram as tg
from src.bots.i18n import bot_t

_CHAT_ID = 555
_RU_ONLY = "ыэщъёй"


# --------------------------------------------------------------------------- #
# Mini-DB: two active channels of one chat_id of DIFFERENT tenants.
# --------------------------------------------------------------------------- #
def _channel(user_id: str, *, status: str = "active", days: int = 0, locale=None):
    return SimpleNamespace(
        id=f"uc-{user_id}",
        user_id=user_id,
        channel="telegram",
        external_id=str(_CHAT_ID),
        status=status,
        linked_at=datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(days=days),
        locale=locale,
    )


class _FakeDb:
    """A session stub that respects the GUC of the current tenant (the join policy 0022)."""

    def __init__(self, channels, users, resolves_to):
        self.channels = channels
        self.users = users  # user_id -> SimpleNamespace(locale=...)
        self.resolves_to = resolves_to  # what app_resolve_tenant_id returns
        self.tenant_of = {ch.user_id: users[ch.user_id].tenant_id for ch in channels}
        self.current_tenant = None
        self.trace: list[str] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        if statement is tg._RESOLVE_TENANT_SQL:
            self.trace.append("resolve_tenant")
            return SimpleNamespace(scalar=lambda: self.resolves_to)
        # Any other read — already under the policy: without the GUC 0 rows (fail-closed).
        self.trace.append("select_channel")
        if self.current_tenant is None:
            raise AssertionError(
                "the read of user_channels without set_current_tenant — under FORCE RLS "
                "it is 0 rows, not “the whole chat”"
            )
        visible = [
            ch
            for ch in self.channels
            if self.tenant_of[ch.user_id] == self.current_tenant
        ]
        visible.sort(key=lambda ch: (ch.status != "active", -ch.linked_at.timestamp()))
        first = visible[0] if visible else None
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: first))

    async def get(self, _model, user_id):
        self.trace.append("get_user")
        return self.users.get(user_id)

    async def commit(self):
        self.commits += 1


def _db(resolves_to="tenant-a", **over):
    users = {
        "u-a": SimpleNamespace(id="u-a", tenant_id="tenant-a", locale="ru"),
        "u-b": SimpleNamespace(id="u-b", tenant_id="tenant-b", locale="ru"),
    }
    channels = [
        _channel("u-a", days=0, **over),
        _channel(
            "u-b", days=5
        ),  # the fresher — the “foreign” channel of the same chat_id
    ]
    return _FakeDb(channels, users, resolves_to)


@asynccontextmanager
async def _scope_of(db):
    yield db


def _patched(db):
    """session_scope → our mini-DB; set_current_tenant → setting the GUC."""

    async def _set_tenant(session, tenant_id):
        session.trace.append("set_tenant")
        session.current_tenant = tenant_id

    return (
        patch("src.bots.telegram.session_scope", lambda: _scope_of(db)),
        patch("src.bots.telegram.set_current_tenant", _set_tenant),
    )


def _callback(data: str) -> AsyncMock:
    # spec=Message — the handler narrows the type (``isinstance``): an InaccessibleMessage
    # (a message older than 48 hours) has nothing to answer. A bare AsyncMock would not pass
    # the narrowing, and the test would silently check the branch “there is nowhere to answer”.
    cb = AsyncMock()
    cb.data = data
    cb.message = AsyncMock(spec=Message)
    cb.message.chat = SimpleNamespace(id=_CHAT_ID)
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _message() -> AsyncMock:
    msg = AsyncMock()
    msg.from_user = SimpleNamespace(id=_CHAT_ID)
    msg.answer = AsyncMock()
    return msg


def _answer_text(mock: AsyncMock) -> str:
    return mock.answer.await_args.args[0]


# --------------------------------------------------------------------------- #
# /language — the invitation and the keyboard.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_command_offers_all_languages_with_own_prefix():
    """The keyboard is one for the two entry points, the prefix is its own (not ``ob:lang:``).

    A common prefix would mean that the language choice outside the onboarding is caught by the wizard step’s handler (or vice versa) — by the registration order, not by the meaning.
    """
    db = _db()
    p_scope, p_tenant = _patched(db)
    msg = _message()
    with p_scope, p_tenant:
        await tg.cmd_language(msg)

    markup = msg.answer.await_args.kwargs["reply_markup"]
    codes = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert codes == ["lang:set:ru", "lang:set:sr", "lang:set:en"]
    assert all(not c.startswith("ob:lang:") for c in codes)


@pytest.mark.asyncio
async def test_command_prompt_says_cabinet_language_is_not_touched():
    """The wording must separate the axes — otherwise they wait for a language change in the cabinet."""
    db = _db()
    p_scope, p_tenant = _patched(db)
    msg = _message()
    with p_scope, p_tenant:
        await tg.cmd_language(msg)
    assert "app.nab.rs" in _answer_text(msg)


# --------------------------------------------------------------------------- #
# The language choice: we write into the channel, we do not touch the cabinet.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_choice_writes_channel_locale_and_leaves_user_locale():
    db = _db()
    p_scope, p_tenant = _patched(db)
    with p_scope, p_tenant:
        await tg.on_language_set(_callback("lang:set:sr"))

    own = db.channels[0]
    assert own.locale == "sr", "the language is not written to the command’s channel"
    assert (
        db.users["u-a"].locale == "ru"
    ), "users.locale must not be touched — another axis"
    assert db.commits >= 1


@pytest.mark.asyncio
async def test_other_tenant_channel_of_same_chat_untouched():
    """‼️ One chat_id — several active channels of DIFFERENT tenants (production)."""
    db = _db()
    p_scope, p_tenant = _patched(db)
    with p_scope, p_tenant:
        await tg.on_language_set(_callback("lang:set:en"))

    assert db.channels[0].locale == "en"
    assert (
        db.channels[1].locale is None
    ), "the foreign channel of the same chat is changed"
    assert db.users["u-b"].locale == "ru"


@pytest.mark.asyncio
async def test_channel_is_read_only_after_tenant_guc_is_set():
    """The bootstrap order: SECURITY DEFINER → GUC → the read of the channel."""
    db = _db()
    p_scope, p_tenant = _patched(db)
    with p_scope, p_tenant:
        await tg.set_channel_locale(db, _CHAT_ID, "sr")

    assert db.trace[:3] == ["resolve_tenant", "set_tenant", "select_channel"]


@pytest.mark.asyncio
async def test_confirmation_is_already_in_new_language():
    db = _db()
    p_scope, p_tenant = _patched(db)
    cb = _callback("lang:set:sr")
    with p_scope, p_tenant:
        await tg.on_language_set(cb)

    text = _answer_text(cb.message)
    assert text == bot_t("sr", "lang_changed").format(lang="Srpski")
    assert not {ch for ch in text.lower() if ch in _RU_ONLY}, text


@pytest.mark.asyncio
async def test_unlinked_chat_gets_hint_and_writes_nothing():
    db = _db(resolves_to=None)
    p_scope, p_tenant = _patched(db)
    cb = _callback("lang:set:sr")
    with p_scope, p_tenant:
        await tg.on_language_set(cb)

    assert _answer_text(cb.message) == bot_t("ru", "lang_no_account")
    assert all(ch.locale is None for ch in db.channels)
    assert db.commits == 0


@pytest.mark.asyncio
async def test_unsupported_locale_is_rejected_without_write():
    """The CHECK of 0038 knows exactly sr/ru/en — garbage must not get to the DB."""
    db = _db()
    p_scope, p_tenant = _patched(db)
    with p_scope, p_tenant:
        outcome = await tg.set_channel_locale(db, _CHAT_ID, "de")

    assert outcome == "bad_locale"
    assert all(ch.locale is None for ch in db.channels)
    assert db.commits == 0


# --------------------------------------------------------------------------- #
# resolve_bot_locale: the channel outranks the cabinet, NULL inherits the cabinet.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_prefers_channel_locale_over_cabinet():
    db = _db(locale="en")
    db.users["u-a"].locale = "ru"
    _, p_tenant = _patched(db)
    with p_tenant:
        assert await tg.resolve_bot_locale(db, _CHAT_ID) == "en"
    assert (
        "get_user" not in db.trace
    ), "with the channel’s language there is no reason to read the cabinet"


@pytest.mark.asyncio
async def test_resolve_inherits_cabinet_locale_when_channel_is_null():
    """NULL in the channel = “inherit”, not «ru»: a language change in the cabinet reaches the bot."""
    db = _db()
    db.users["u-a"].locale = "sr"
    _, p_tenant = _patched(db)
    with p_tenant:
        assert await tg.resolve_bot_locale(db, _CHAT_ID) == "sr"


@pytest.mark.asyncio
async def test_choice_sets_bot_locale_to_new_language():
    """After choosing a language the outgoing messages go in the NEW language (sr → ru: not Latin)."""
    from src.bots.sr_latin import bot_locale

    bot_locale.set("sr")
    db = _db()
    p_scope, p_tenant = _patched(db)
    with p_scope, p_tenant:
        await tg.on_language_set(_callback("lang:set:ru"))
    assert bot_locale.get() == "ru"
