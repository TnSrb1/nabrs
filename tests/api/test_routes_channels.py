"""Tests of the channel-linking routes ``src/api/cabinet/routes_channels.py``.

The DB, session and service layer are mocked via ``dependency_overrides`` and ``monkeypatch`` (ADR-013: no live external APIs). Templates render for real — we check the session gate (anonymous → /login), the response codes, the “already connected” branch and the deep-link generation branch. The logic of ``channel_service`` is NOT re-checked here (see ``test_channel_link.py``), only the behavior of the route.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_account, routes_channels
from src.api.main import app
from src.workers.matching import ChannelState
from tests.api.conftest import FakeSession
from tests.api.test_routes_account import _setup as _hub_setup

SESS = {"uid": "u-1", "tid": "t-1"}


def _client(*, sess=None):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    # TestClient without auto-following of redirects — we check the 303 itself.
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()
    # The e-mail limiter is module-level (it lives in the api process), between the tests it must
    # be reset: otherwise the test running fourth would catch someone else’s limit.
    routes_channels._EMAIL_CONFIRM_PER_USER.reset()


def _async(return_value):
    """Returns an async stub function with a fixed result."""

    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _patch_bot(monkeypatch, username="Nabrs_bot"):
    from types import SimpleNamespace

    monkeypatch.setattr(
        routes_channels,
        "get_bot_settings",
        lambda: SimpleNamespace(bot_username=username),
    )


# --------------------------------------------------------------------------- #
# The session gate: anonymous → /login (303)
# --------------------------------------------------------------------------- #


def test_anonymous_redirected_to_login():
    resp = _client(sess=None).post("/cabinet/telegram/link")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# --------------------------------------------------------------------------- #
# Already linked → the “already” branch, without generating a token
# --------------------------------------------------------------------------- #


def test_link_when_already_active_renders_already(monkeypatch):
    _patch_bot(monkeypatch)
    monkeypatch.setattr(routes_channels.svc, "has_active_telegram", _async(True))

    # If it reached the generation — it would fall; we guarantee the not-called branch.
    async def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("create_bind_token must not be called on already")

    monkeypatch.setattr(routes_channels.svc, "create_bind_token", _boom)
    _hub_setup(monkeypatch, telegram_live=True)

    resp = _client(sess=SESS).post("/cabinet/telegram/link")
    assert resp.status_code == 200
    # The response — the hub itself, the “Channels” section: the status “connected” + a button to the bot’s chat.
    assert 'id="kanali"' in resp.text
    assert "Telegram подключён — тендеры приходят в этот чат." in resp.text
    assert "?start=" not in resp.text


# --------------------------------------------------------------------------- #
# Not linked → token generation and the deep-link render
# --------------------------------------------------------------------------- #


def test_link_generates_deep_link_when_not_active(monkeypatch):
    _patch_bot(monkeypatch, username="Nabrs_bot")
    monkeypatch.setattr(routes_channels.svc, "has_active_telegram", _async(False))
    monkeypatch.setattr(routes_channels.svc, "create_bind_token", _async("raw-tok-123"))
    _hub_setup(monkeypatch)

    resp = _client(sess=SESS).post("/cabinet/telegram/link")
    assert resp.status_code == 200
    # The deep-link is built from bot_username and the raw token and shown IN THE HUB.
    assert 'id="kanali"' in resp.text
    assert "https://t.me/Nabrs_bot?start=raw-tok-123" in resp.text
    assert "уже подключён" not in resp.text


# --------------------------------------------------------------------------- #
# FIX-RLS-ROUTE-CTX: the explicit tenant context is set BEFORE the RLS query
# --------------------------------------------------------------------------- #


def test_link_sets_tenant_before_has_active_telegram(monkeypatch):
    """repo.set_tenant(db, session['tid']) must be called before has_active_telegram (SELECT user_channels, RLS policy 0022) — otherwise under the tendersrb_app role (FORCE RLS) the query falls with UndefinedObjectError (500)."""
    _patch_bot(monkeypatch)
    calls: list[str] = []

    async def _set_tenant(db, tid):
        calls.append(f"set_tenant:{tid}")

    async def _has_active(db, uid):
        calls.append(f"has_active_telegram:{uid}")
        return False

    async def _create_token(db, *, user_id, settings):
        calls.append("create_bind_token")
        return "raw-tok"

    monkeypatch.setattr(routes_channels.repo, "set_tenant", _set_tenant)
    monkeypatch.setattr(routes_channels.svc, "has_active_telegram", _has_active)
    monkeypatch.setattr(routes_channels.svc, "create_bind_token", _create_token)
    _hub_setup(monkeypatch)

    resp = _client(sess=SESS).post("/cabinet/telegram/link")
    assert resp.status_code == 200
    assert calls[0] == f"set_tenant:{SESS['tid']}"
    assert calls.index(f"set_tenant:{SESS['tid']}") < calls.index(
        f"has_active_telegram:{SESS['uid']}"
    )


# --------------------------------------------------------------------------- #
# VG-31 part 2: GET /cabinet/telegram — the “My Telegram” panel
# --------------------------------------------------------------------------- #


def test_panel_anonymous_redirected_to_login():
    resp = _client(sess=None).get("/cabinet/telegram")
    assert resp.status_code == 303
    # the bot button “Connect Telegram” → login → back to this same address,
    # which will bring a 303 to the hub (the chain check — test_cabinet_hub)
    assert resp.headers["location"] == "/login?next=%2Fcabinet%2Ftelegram"


def test_panel_get_redirects_to_hub_channels_303():
    """The old address (the bot, e-mails) → 303 to the hub section; NOT 301 (cached forever)."""
    resp = _client(sess=SESS).get("/cabinet/telegram")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account#kanali"


def test_hub_renders_telegram_linked_state(monkeypatch):
    _patch_bot(monkeypatch, username="Nabrs_bot")
    monkeypatch.setattr(
        routes_account,
        "get_bot_settings",
        lambda: SimpleNamespace(bot_username="Nabrs_bot"),
    )
    _hub_setup(monkeypatch, telegram_live=True)

    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "Telegram подключён — тендеры приходят в этот чат." in resp.text
    assert 'href="https://t.me/Nabrs_bot"' in resp.text
    assert 'action="/cabinet/telegram/link#kanali"' not in resp.text


def test_hub_renders_telegram_not_linked_state(monkeypatch):
    _hub_setup(monkeypatch)

    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert 'action="/cabinet/telegram/link#kanali"' in resp.text


def test_hub_sets_tenant_before_reading_telegram_channel(monkeypatch):
    """The RLS contour of the hub: set_tenant first, before reading user_channels."""
    _hub_setup(monkeypatch)
    calls: list[str] = []

    async def _set_tenant(db, tid):
        calls.append(f"set_tenant:{tid}")

    async def _tg_state(db, tid):
        calls.append("telegram_channel_state")
        return None

    monkeypatch.setattr(routes_channels.repo, "set_tenant", _set_tenant)
    monkeypatch.setattr(routes_account.channel_svc, "telegram_channel_state", _tg_state)

    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert calls[0] == f"set_tenant:{SESS['tid']}"
    assert calls.index(f"set_tenant:{SESS['tid']}") < calls.index(
        "telegram_channel_state"
    )


# --------------------------------------------------------------------------- #
# Reliability: the HTTP contract (methods, codes, absence of a leak in the anonymous branch)
# --------------------------------------------------------------------------- #


def test_link_get_method_not_allowed():
    """/cabinet/telegram/link — POST only; GET → 405 (not a render, not a 303)."""
    resp = _client(sess=SESS).get("/cabinet/telegram/link")
    assert resp.status_code == 405


def test_panel_post_method_not_allowed():
    """The old GET address /cabinet/telegram does not accept POST → 405."""
    resp = _client(sess=SESS).post("/cabinet/telegram")
    assert resp.status_code == 405


def test_anonymous_link_does_not_touch_db(monkeypatch):
    """An anonymous user is cut off BEFORE the session is opened: set_tenant / has_active_telegram are not called (otherwise an extra connection and a potential RLS query without tid)."""
    _patch_bot(monkeypatch)
    called: list[str] = []

    async def _set_tenant(db, tid):  # pragma: no cover - must not be called
        called.append("set_tenant")

    async def _has_active(db, uid):  # pragma: no cover - must not be called
        called.append("has_active")
        return True

    monkeypatch.setattr(routes_channels.repo, "set_tenant", _set_tenant)
    monkeypatch.setattr(routes_channels.svc, "has_active_telegram", _has_active)

    resp = _client(sess=None).post("/cabinet/telegram/link")
    assert resp.status_code == 303
    assert called == []


def test_anonymous_panel_does_not_touch_db(monkeypatch):
    """The same for the GET panel: anonymous → /login without a DB access."""
    _patch_bot(monkeypatch)
    called: list[str] = []

    async def _set_tenant(db, tid):  # pragma: no cover - must not be called
        called.append("set_tenant")

    async def _has_active(db, uid):  # pragma: no cover - must not be called
        called.append("has_active")
        return True

    monkeypatch.setattr(routes_channels.repo, "set_tenant", _set_tenant)
    monkeypatch.setattr(routes_channels.svc, "has_active_telegram", _has_active)

    resp = _client(sess=None).get("/cabinet/telegram")
    assert resp.status_code == 303
    assert called == []


# --------------------------------------------------------------------------- #
# Reliability: the “already” branch — the tenant context is set, but the token is not generated
# --------------------------------------------------------------------------- #


def test_link_already_sets_tenant_and_skips_token(monkeypatch):
    """With Telegram already linked: set_tenant runs (the RLS query has_active_telegram under the app role), but create_bind_token is NOT called."""
    _patch_bot(monkeypatch)
    calls: list[str] = []

    async def _set_tenant(db, tid):
        calls.append(f"set_tenant:{tid}")

    async def _has_active(db, uid):
        calls.append("has_active")
        return True

    async def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("create_bind_token must not be called on already")

    monkeypatch.setattr(routes_channels.repo, "set_tenant", _set_tenant)
    monkeypatch.setattr(routes_channels.svc, "has_active_telegram", _has_active)
    monkeypatch.setattr(routes_channels.svc, "create_bind_token", _boom)
    _hub_setup(monkeypatch)

    resp = _client(sess=SESS).post("/cabinet/telegram/link")
    assert resp.status_code == 200
    assert calls[0] == f"set_tenant:{SESS['tid']}"
    assert "create_bind_token" not in calls


def test_link_already_renders_no_deep_link(monkeypatch):
    """The “already” branch must not contain the one-time deep-link."""
    _patch_bot(monkeypatch)
    monkeypatch.setattr(routes_channels.svc, "has_active_telegram", _async(True))
    monkeypatch.setattr(
        routes_channels.svc,
        "create_bind_token",
        _async("must-not-appear"),
    )
    _hub_setup(monkeypatch)

    resp = _client(sess=SESS).post("/cabinet/telegram/link")
    assert resp.status_code == 200
    assert "?start=" not in resp.text
    assert "must-not-appear" not in resp.text


# --------------------------------------------------------------------------- #
# Reliability: the token is generated strictly for the user_id/tenant from the signed session
# --------------------------------------------------------------------------- #


def test_link_generates_token_for_session_user(monkeypatch):
    """create_bind_token gets the user_id exactly from the session (not from the request) — protection against linking someone else’s channel on substitution."""
    _patch_bot(monkeypatch, username="Nabrs_bot")
    monkeypatch.setattr(routes_channels.svc, "has_active_telegram", _async(False))
    seen: dict = {}

    async def _create(db, *, user_id, settings):
        seen["user_id"] = user_id
        return "raw-xyz"

    monkeypatch.setattr(routes_channels.svc, "create_bind_token", _create)
    _hub_setup(monkeypatch)

    resp = _client(sess=SESS).post("/cabinet/telegram/link")
    assert resp.status_code == 200
    assert seen["user_id"] == SESS["uid"]
    assert "start=raw-xyz" in resp.text


def test_link_deep_link_uses_patched_bot_username(monkeypatch):
    """bot_username in the link is taken from get_bot_settings (VG-31): a change of the bot’s nick is reflected in the deep-link without editing the route."""
    _patch_bot(monkeypatch, username="Other_bot")
    monkeypatch.setattr(routes_channels.svc, "has_active_telegram", _async(False))
    monkeypatch.setattr(routes_channels.svc, "create_bind_token", _async("tok9"))
    _hub_setup(monkeypatch)

    resp = _client(sess=SESS).post("/cabinet/telegram/link")
    assert resp.status_code == 200
    assert "https://t.me/Other_bot?start=tok9" in resp.text


# --------------------------------------------------------------------------- #
# A2: e-mail channel — GET /cabinet/email, POST /cabinet/email/link|unlink
# --------------------------------------------------------------------------- #


def _email_state(*, status="active", external_id="john.doe@example.com"):
    return ChannelState(channel="email", status=status, external_id=external_id)


def _patch_email_state(monkeypatch, state):
    monkeypatch.setattr(routes_channels.svc, "email_channel_state", _async(state))


def test_email_panel_anonymous_redirected_to_login():
    resp = _client(sess=None).get("/cabinet/email")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Fcabinet%2Femail"


def test_email_link_anonymous_redirected_to_login():
    resp = _client(sess=None).post("/cabinet/email/link", data={"email": "a@b.rs"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_email_unlink_anonymous_redirected_to_login():
    resp = _client(sess=None).post("/cabinet/email/unlink")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_email_get_redirects_to_hub_channels_303():
    resp = _client(sess=SESS).get("/cabinet/email")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account#kanali"


def test_email_panel_renders_not_linked_state(monkeypatch):
    """There is no channel at all → “not connected” + the linking form (a hub section)."""
    _hub_setup(monkeypatch, email_state=None)

    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "не подключён" in resp.text
    assert 'action="/cabinet/email/link"' in resp.text
    assert 'action="/cabinet/email/unlink"' not in resp.text


def test_email_panel_shows_masked_address_not_full(monkeypatch):
    """The address is shown masked: PII is not shown in full (the same principle as the address in the logs)."""
    _hub_setup(
        monkeypatch, email_state=_email_state(external_id="john.doe@example.com")
    )

    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "j***e@example.com" in resp.text
    assert "john.doe@example.com" not in resp.text
    assert 'action="/cabinet/email/unlink"' in resp.text


def test_email_panel_blocked_channel_reads_as_not_linked(monkeypatch):
    """The row exists, but status='blocked' → there is nowhere to deliver: “not connected”
    (the same is_live predicate as in routing and delivery)."""
    _hub_setup(monkeypatch, email_state=_email_state(status="blocked"))

    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "не подключён" in resp.text
    assert 'action="/cabinet/email/link"' in resp.text


def test_email_link_saves_normalized_address_for_session_user(monkeypatch):
    """The address is normalized (trim+lowercase), user_id is taken from the signed session — not from the request; after success a PRG redirect to the panel."""
    seen: dict = {}

    async def _request(db, *, user_id, address, settings, email_sender, **kwargs):
        seen["user_id"] = user_id
        seen["address"] = address

    monkeypatch.setattr(routes_channels.svc, "request_email_confirmation", _request)

    resp = _client(sess=SESS).post(
        "/cabinet/email/link", data={"email": "  John.Doe@Example.COM "}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account#kanali"
    assert seen == {"user_id": SESS["uid"], "address": "john.doe@example.com"}


def test_email_link_invalid_address_renders_form_error_without_write(monkeypatch):
    """Bad input → a form error (200), not a 500; we write nothing to the DB."""
    _patch_email_state(monkeypatch, None)

    async def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("a letter must not go out for a bad address")

    monkeypatch.setattr(routes_channels.svc, "request_email_confirmation", _boom)

    _hub_setup(monkeypatch, email_state=None)
    client = _client(sess=SESS)
    resp = client.post("/cabinet/email/link", data={"email": "не-адрес"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account?ch_err=emp_err_at#kanali"
    page = client.get(resp.headers["location"])
    assert page.status_code == 200
    assert "name@example.com" in page.text  # the form error text
    assert 'action="/cabinet/email/link"' in page.text


def test_email_link_empty_address_renders_form_error(monkeypatch):
    _patch_email_state(monkeypatch, None)

    async def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("a letter must not go out for an empty address")

    monkeypatch.setattr(routes_channels.svc, "request_email_confirmation", _boom)

    _hub_setup(monkeypatch, email_state=None)
    client = _client(sess=SESS)
    resp = client.post("/cabinet/email/link", data={"email": "   "})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account?ch_err=emp_err_empty#kanali"
    assert "Введите адрес" in client.get(resp.headers["location"]).text


def test_email_panel_shows_pending_state_with_resend(monkeypatch):
    """owner: waiting for confirmation is a separate state, not “not connected”:
    the user must see that the letter went out and what is expected of them."""
    _hub_setup(
        monkeypatch,
        telegram_live=True,
        email_state=_email_state(status="pending", external_id="john.doe@example.com"),
    )

    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "ждёт подтверждения" in resp.text
    assert 'action="/cabinet/email/resend"' in resp.text
    # The address — masked, as also in the confirmed state.
    assert "j***e@example.com" in resp.text
    assert "john.doe@example.com" not in resp.text
    assert "не подключён" not in resp.text


def test_email_resend_uses_session_user_and_tenant(monkeypatch):
    seen: dict = {}

    async def _resend(db, *, user_id, tenant_id, settings, email_sender, **kwargs):
        seen["user_id"] = user_id
        seen["tenant_id"] = tenant_id
        return True

    monkeypatch.setattr(routes_channels.svc, "resend_email_confirmation", _resend)

    resp = _client(sess=SESS).post("/cabinet/email/resend")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account#kanali"
    assert seen == {"user_id": SESS["uid"], "tenant_id": SESS["tid"]}


def test_email_resend_anonymous_redirected_to_login():
    resp = _client(sess=None).post("/cabinet/email/resend")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_email_confirm_anonymous_redirected_to_login():
    resp = _client(sess=None).get("/cabinet/email/confirm?token=x")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_email_confirm_valid_token_renders_confirmed_panel(monkeypatch):
    _patch_email_state(monkeypatch, _email_state(status="active"))
    seen: dict = {}

    async def _confirm(db, *, user_id, tenant_id, raw_token):
        seen["user_id"] = user_id
        seen["tenant_id"] = tenant_id
        seen["raw_token"] = raw_token
        return True

    monkeypatch.setattr(routes_channels.svc, "confirm_email", _confirm)

    _hub_setup(monkeypatch, email_state=_email_state(status="active"))
    client = _client(sess=SESS)
    resp = client.get("/cabinet/email/confirm?token=raw-123")
    # The result of the confirmation — in the hub (the mutating GET from the e-mail is not changed).
    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/cabinet/account?ch_notice=emp_confirm_ok#kanali"
    )
    assert "Адрес подтверждён" in client.get(resp.headers["location"]).text
    assert seen == {
        "user_id": SESS["uid"],
        "tenant_id": SESS["tid"],
        "raw_token": "raw-123",
    }


def test_email_confirm_bad_token_renders_page_not_500(monkeypatch):
    """An expired/someone else’s/repeated token → a clear page, not a 500."""
    _patch_email_state(monkeypatch, _email_state(status="pending"))
    monkeypatch.setattr(routes_channels.svc, "confirm_email", _async(False))

    _hub_setup(monkeypatch, email_state=_email_state(status="pending"))
    client = _client(sess=SESS)
    resp = client.get("/cabinet/email/confirm?token=stale")
    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/cabinet/account?ch_err=emp_confirm_invalid#kanali"
    )
    page = client.get(resp.headers["location"])
    assert "Ссылка недействительна" in page.text
    assert "Адрес подтверждён" not in page.text


def test_email_confirm_without_token_renders_page_not_500(monkeypatch):
    _patch_email_state(monkeypatch, _email_state(status="pending"))
    monkeypatch.setattr(routes_channels.svc, "confirm_email", _async(False))

    resp = _client(sess=SESS).get("/cabinet/email/confirm")
    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/cabinet/account?ch_err=emp_confirm_invalid#kanali"
    )


def test_email_confirm_sets_tenant_before_reading_channel(monkeypatch):
    calls: list[str] = []
    _trace_set_tenant(monkeypatch, calls)

    async def _confirm(db, *, user_id, tenant_id, raw_token):
        calls.append("confirm_email")
        return False

    async def _state(db, tid):
        calls.append("email_channel_state")
        return None

    monkeypatch.setattr(routes_channels.svc, "confirm_email", _confirm)
    monkeypatch.setattr(routes_channels.svc, "email_channel_state", _state)

    resp = _client(sess=SESS).get("/cabinet/email/confirm?token=x")
    assert resp.status_code == 303
    assert calls[0] == f"set_tenant:{SESS['tid']}"


def test_email_confirm_prefetched_token_still_shows_success(monkeypatch):
    """Anti-prefetch (idempotence): the e-mail scanner opened the link first and
    burned the token → ``confirm_email`` returned False, but the channel is already active and the token is ours (``email_confirm_already`` → True) → a SUCCESS page, not an error.

    Mutation: remove the ``email_confirm_already`` branch in the route (return the old behavior “False → an error at once”) → the test goes red.
    """
    _patch_email_state(monkeypatch, _email_state(status="active"))
    monkeypatch.setattr(routes_channels.svc, "confirm_email", _async(False))
    monkeypatch.setattr(routes_channels.svc, "email_confirm_already", _async(True))

    _hub_setup(monkeypatch, email_state=_email_state(status="active"))
    client = _client(sess=SESS)
    resp = client.get("/cabinet/email/confirm?token=prefetched")
    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/cabinet/account?ch_notice=emp_confirm_ok#kanali"
    )
    page = client.get(resp.headers["location"])
    assert "Адрес подтверждён" in page.text
    assert "Ссылка недействительна" not in page.text


def test_email_confirm_foreign_token_still_shows_error(monkeypatch):
    """Idempotence does not replace EVERYTHING with success: a truly foreign/unknown token without a confirmed link (``email_confirm_already`` → False) → an error."""
    _patch_email_state(monkeypatch, _email_state(status="pending"))
    monkeypatch.setattr(routes_channels.svc, "confirm_email", _async(False))
    monkeypatch.setattr(routes_channels.svc, "email_confirm_already", _async(False))

    _hub_setup(monkeypatch, email_state=_email_state(status="pending"))
    client = _client(sess=SESS)
    resp = client.get("/cabinet/email/confirm?token=foreign")
    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/cabinet/account?ch_err=emp_confirm_invalid#kanali"
    )
    page = client.get(resp.headers["location"])
    assert "Ссылка недействительна" in page.text
    assert "Адрес подтверждён" not in page.text


# --------------------------------------------------------------------------- #
# owner: anti-abuse — a mail bomb to someone else’s address
# --------------------------------------------------------------------------- #


def test_email_link_rate_limited_after_three_letters(monkeypatch):
    """Otherwise the linking form is a free mail bomb to someone else’s address
    (the reputation of the SMTP domain hits the delivery to ALL subscribers)."""
    _patch_email_state(monkeypatch, None)
    sent: list[str] = []

    async def _request(db, *, user_id, address, settings, email_sender, **kwargs):
        sent.append(address)

    monkeypatch.setattr(routes_channels.svc, "request_email_confirmation", _request)

    client = _client(sess=SESS)
    for i in range(3):
        resp = client.post("/cabinet/email/link", data={"email": f"victim{i}@x.rs"})
        assert resp.status_code == 303

    _hub_setup(monkeypatch, email_state=None)
    resp = client.post("/cabinet/email/link", data={"email": "victim4@x.rs"})
    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/cabinet/account?ch_err=emp_err_rate_limit#kanali"
    )
    assert "через 15 минут" in client.get(resp.headers["location"]).text
    assert len(sent) == 3  # the fourth letter did not go out


def test_email_resend_shares_limit_with_link(monkeypatch):
    """A common limit for “link” and “resend”: both send a letter, and a separate count would double the real quota."""
    _patch_email_state(monkeypatch, _email_state(status="pending"))
    sent: list[str] = []

    async def _request(db, *, user_id, address, settings, email_sender, **kwargs):
        sent.append("link")

    async def _resend(db, *, user_id, tenant_id, settings, email_sender, **kwargs):
        sent.append("resend")
        return True

    monkeypatch.setattr(routes_channels.svc, "request_email_confirmation", _request)
    monkeypatch.setattr(routes_channels.svc, "resend_email_confirmation", _resend)

    client = _client(sess=SESS)
    for _ in range(3):
        assert (
            client.post("/cabinet/email/link", data={"email": "a@b.rs"}).status_code
            == 303
        )

    _hub_setup(monkeypatch, email_state=_email_state(status="pending"))
    resp = client.post("/cabinet/email/resend")
    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/cabinet/account?ch_err=emp_err_rate_limit#kanali"
    )
    assert "через 15 минут" in client.get(resp.headers["location"]).text
    assert sent == ["link", "link", "link"]


def test_email_link_limit_is_per_user_not_global(monkeypatch):
    """One user’s limit must not mute the letters of another."""
    _patch_email_state(monkeypatch, None)
    sent: list[str] = []

    async def _request(db, *, user_id, address, settings, email_sender, **kwargs):
        sent.append(user_id)

    monkeypatch.setattr(routes_channels.svc, "request_email_confirmation", _request)

    client = _client(sess=SESS)
    for _ in range(4):
        client.post("/cabinet/email/link", data={"email": "a@b.rs"})

    other = _client(sess={"uid": "u-2", "tid": "t-2"})
    resp = other.post("/cabinet/email/link", data={"email": "a@b.rs"})
    assert resp.status_code == 303
    assert sent.count("u-2") == 1


def test_email_unlink_calls_service_for_session_user(monkeypatch):
    seen: dict = {}

    async def _unlink(db, *, user_id):
        seen["user_id"] = user_id

    monkeypatch.setattr(routes_channels.svc, "unlink_email", _unlink)

    resp = _client(sess=SESS).post("/cabinet/email/unlink")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account#kanali"
    assert seen == {"user_id": SESS["uid"]}


# --------------------------------------------------------------------------- #
# A2 / FIX-RLS-ROUTE-CTX: the tenant context BEFORE any RLS query on e-mail
# --------------------------------------------------------------------------- #


def _trace_set_tenant(monkeypatch, calls: list[str]):
    async def _set_tenant(db, tid):
        calls.append(f"set_tenant:{tid}")

    monkeypatch.setattr(routes_channels.repo, "set_tenant", _set_tenant)


def test_hub_sets_tenant_before_reading_email_channel(monkeypatch):
    """Without set_tenant the SELECT user_channels under the tendersrb_app role (FORCE RLS) falls with UndefinedObjectError (500) — or gives 0 rows, fail-closed."""
    _hub_setup(monkeypatch)
    calls: list[str] = []
    _trace_set_tenant(monkeypatch, calls)

    async def _state(db, tid):
        calls.append(f"email_channel_state:{tid}")
        return None

    monkeypatch.setattr(routes_channels.svc, "email_channel_state", _state)

    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert calls == [f"set_tenant:{SESS['tid']}", f"email_channel_state:{SESS['tid']}"]


def test_email_link_sets_tenant_before_write(monkeypatch):
    """The INSERT into user_channels passes the WITH CHECK of the join policy (0022) only with app.current_tenant_id set."""
    calls: list[str] = []
    _trace_set_tenant(monkeypatch, calls)

    async def _request(db, *, user_id, address, settings, email_sender, **kwargs):
        calls.append("request_email_confirmation")

    monkeypatch.setattr(routes_channels.svc, "request_email_confirmation", _request)

    resp = _client(sess=SESS).post(
        "/cabinet/email/link", data={"email": "user@company.rs"}
    )
    assert resp.status_code == 303
    assert calls == [f"set_tenant:{SESS['tid']}", "request_email_confirmation"]


def test_email_unlink_sets_tenant_before_write(monkeypatch):
    calls: list[str] = []
    _trace_set_tenant(monkeypatch, calls)

    async def _unlink(db, *, user_id):
        calls.append("unlink_email")

    monkeypatch.setattr(routes_channels.svc, "unlink_email", _unlink)

    resp = _client(sess=SESS).post("/cabinet/email/unlink")
    assert resp.status_code == 303
    assert calls == [f"set_tenant:{SESS['tid']}", "unlink_email"]


def test_email_link_invalid_address_touches_no_db(monkeypatch):
    """The form-error branch — only a redirect with a flash: the DB is not opened at all."""
    calls: list[str] = []
    _trace_set_tenant(monkeypatch, calls)

    resp = _client(sess=SESS).post("/cabinet/email/link", data={"email": "bad"})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/cabinet/account?ch_err=emp_err_")
    assert calls == []


# --------------------------------------------------------------------------- #
# A2: HTTP contract
# --------------------------------------------------------------------------- #


def test_email_panel_post_method_not_allowed():
    resp = _client(sess=SESS).post("/cabinet/email")
    assert resp.status_code == 405


def test_email_link_get_method_not_allowed():
    resp = _client(sess=SESS).get("/cabinet/email/link")
    assert resp.status_code == 405


def test_email_unlink_get_method_not_allowed():
    resp = _client(sess=SESS).get("/cabinet/email/unlink")
    assert resp.status_code == 405


def test_anonymous_email_routes_do_not_touch_db(monkeypatch):
    """An anonymous user is cut off BEFORE the session is opened — neither a read nor a write."""
    called: list[str] = []

    async def _set_tenant(db, tid):  # pragma: no cover - must not be called
        called.append("set_tenant")

    async def _state(db, tid):  # pragma: no cover - must not be called
        called.append("email_channel_state")
        return None

    async def _link(db, **k):  # pragma: no cover - must not be called
        called.append("link_email")

    async def _unlink(db, **k):  # pragma: no cover - must not be called
        called.append("unlink_email")

    monkeypatch.setattr(routes_channels.repo, "set_tenant", _set_tenant)
    monkeypatch.setattr(routes_channels.svc, "email_channel_state", _state)
    monkeypatch.setattr(routes_channels.svc, "link_email", _link)
    monkeypatch.setattr(routes_channels.svc, "unlink_email", _unlink)

    client = _client(sess=None)
    assert client.get("/cabinet/email").status_code == 303
    assert (
        client.post("/cabinet/email/link", data={"email": "a@b.rs"}).status_code == 303
    )
    assert client.post("/cabinet/email/unlink").status_code == 303
    assert called == []


# --------------------------------------------------------------------------- #
# i18n remainder of the cabinet (edge/error): the e-mail validation and the confirmation status under
# a non-ru locale are localised through the catalog (emp_err_*/emp_confirm_*). The fixture data
# is in Latin → ANY Cyrillic in the en/sr render = a chrome outside the catalog.
# Mutation — test_email_error_mutation_reintroduced_russian_is_caught.
# --------------------------------------------------------------------------- #
import re  # noqa: E402

import pytest  # noqa: E402

from src.api.cabinet import i18n  # noqa: E402

_CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_email_link_bad_address_error_localized(monkeypatch, locale):
    """POST of a broken e-mail under en/sr → a form error from the catalog, without Cyrillic."""
    _patch_email_state(monkeypatch, None)

    async def _boom(*a, **k):  # pragma: no cover — we do not send a letter on bad input
        raise AssertionError("a letter must not go out for a bad address")

    monkeypatch.setattr(routes_channels.svc, "request_email_confirmation", _boom)

    _hub_setup(monkeypatch, email_state=None)
    sess = {"uid": "u-1", "tid": "t-1", "locale": locale}
    client = _client(sess=sess)
    resp = client.post("/cabinet/email/link", data={"email": "   "})
    assert resp.status_code == 303
    page = client.get(resp.headers["location"])
    assert page.status_code == 200
    assert not _CYRILLIC.search(page.text), "the error/chrome leaked in Cyrillic"
    assert i18n.CABINET[locale]["emp_err_empty"] in page.text


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_email_confirm_invalid_notice_localized(monkeypatch, locale):
    """An expired confirmation token under en/sr → a clear error from the catalog."""
    _patch_email_state(monkeypatch, _email_state(status="pending"))
    monkeypatch.setattr(routes_channels.svc, "confirm_email", _async(False))

    _hub_setup(monkeypatch, email_state=_email_state(status="pending"))
    sess = {"uid": "u-1", "tid": "t-1", "locale": locale}
    client = _client(sess=sess)
    resp = client.get("/cabinet/email/confirm?token=stale")
    assert resp.status_code == 303
    page = client.get(resp.headers["location"])
    assert not _CYRILLIC.search(page.text), "the chrome leaked in Cyrillic"
    assert i18n.CABINET[locale]["emp_confirm_invalid"] in page.text


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_email_confirm_ok_notice_localized(monkeypatch, locale):
    """A successful confirmation under en/sr → a notice from the catalog, without Cyrillic."""
    _patch_email_state(monkeypatch, _email_state(status="active"))
    monkeypatch.setattr(routes_channels.svc, "confirm_email", _async(True))

    _hub_setup(monkeypatch, email_state=_email_state(status="active"))
    sess = {"uid": "u-1", "tid": "t-1", "locale": locale}
    client = _client(sess=sess)
    resp = client.get("/cabinet/email/confirm?token=raw-123")
    assert resp.status_code == 303
    page = client.get(resp.headers["location"])
    assert not _CYRILLIC.search(page.text), "the chrome leaked in Cyrillic"
    assert i18n.CABINET[locale]["emp_confirm_ok"] in page.text


def test_email_error_mutation_reintroduced_russian_is_caught(monkeypatch):
    """Mutation anchor: return a Russian literal of the e-mail error into the en catalog —
    the en render contains Cyrillic again. Green without the mutation = the guard is blind."""
    _patch_email_state(monkeypatch, None)

    async def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("a letter must not go out for a bad address")

    monkeypatch.setattr(routes_channels.svc, "request_email_confirmation", _boom)
    monkeypatch.setitem(
        i18n.CABINET["en"], "emp_err_empty", "Введите адрес электронной почты."
    )
    _hub_setup(monkeypatch, email_state=None)
    sess = {"uid": "u-1", "tid": "t-1", "locale": "en"}
    client = _client(sess=sess)
    resp = client.post("/cabinet/email/link", data={"email": "   "})
    assert resp.status_code == 303
    page = client.get(resp.headers["location"])
    assert _CYRILLIC.search(page.text), "the mutation did not show — the guard is blind"
