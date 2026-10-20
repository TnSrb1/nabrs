"""Tests of the profile CRUD routes ``src/api/cabinet/routes_profiles.py``.

DB, session and service layer are mocked via ``dependency_overrides`` and ``monkeypatch`` (ADR-013: no live external APIs). Templates render for real — we check the session gate (anonymous → /login), response codes, redirects and the branches of the form/error renders. The logic of the services is NOT re-checked here (see ``test_profiles.py``), only the behavior of the routes.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps, forms
from src.api.cabinet import feed_service, routes_profiles
from src.api.cabinet.profile_service import SaveResult
from src.api.main import app
from src.workers.matching import ChannelState
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


def _profile(**kw):
    base = dict(
        id="p-1",
        name="Стройка",
        is_active=True,
        # Older than the “new template” window (7 d.): the “already open” hint is not shown.
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ai_analysis_level="standard",
        regions=["RS-00"],
        delivery_channels=["email"],
        delivery_schedule="immediate",
        enabled_source_keys=["jnportal"],
        classifications={},
        min_amount_eur=None,
        max_amount_eur=None,
        min_days_until_deadline=None,
        include_no_amount=False,
        keywords_include=None,
        keywords_exclude=None,
        keyword_dict=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _client(*, sess=None):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    # TestClient without auto-following of redirects — we check the 303 itself.
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Session gate: anonymous is always → /login (303)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/app"),
        ("get", "/profiles/new"),
        ("post", "/profiles"),
        ("get", "/profiles/p-1"),
        ("get", "/profiles/p-1/edit"),
        ("post", "/profiles/p-1"),
        ("post", "/profiles/p-1/delete"),
        ("post", "/profiles/p-1/deactivate"),
        ("post", "/profiles/p-1/activate"),
    ],
)
def test_anonymous_redirected_to_login(method, path):
    """The GET page returns here after login; POST — simply to the login
    (the browser will return with a GET, and POST addresses have no GET handler)."""
    client = _client(sess=None)
    resp = getattr(client, method)(path)
    assert resp.status_code == 303
    if method == "get":
        assert resp.headers["location"] == "/login?next=" + path.replace("/", "%2F")
    else:
        assert resp.headers["location"] == "/login"


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #


def test_dashboard_lists_profiles_without_management_duplicates(monkeypatch):
    """owner 22.09: the template on the dashboard has a status and a toggle; “Edit”, “New template” and the Telegram status live in the cabinet, here — a single link there.

    Mutation: return the “Edit” link to the template row — the test goes red.
    """
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([_profile()]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(True)
    )
    resp = _client(sess=SESS).get("/app")
    assert resp.status_code == 200
    assert "Стройка" in resp.text
    assert 'href="/cabinet/account#acct-active-title"' in resp.text
    assert "Управление шаблонами →" in resp.text
    assert ">Изменить</a>" not in resp.text
    assert ">Новый шаблон</a>" not in resp.text
    assert "уже подключён" not in resp.text


BANNER_TITLE = "Канал доставки отключён — тендеры не приходят"


def _dashboard_with_dead(monkeypatch, dead):
    """The dashboard with the set result of channel_service.dead_channels."""
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([_profile()]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )
    monkeypatch.setattr(routes_profiles.channel_svc, "dead_channels", _async(dead))
    return _client(sess=SESS).get("/app")


def test_dashboard_shows_banner_when_channel_dead(monkeypatch):
    """A channel dropped out → the user must see this at once, not guess."""
    resp = _dashboard_with_dead(monkeypatch, ["Telegram"])
    assert resp.status_code == 200
    assert BANNER_TITLE in resp.text
    assert "/cabinet/account#kanali" in resp.text  # the link to reconnect


def test_dashboard_hides_banner_when_channels_live(monkeypatch):
    """Everything is alive → no banner at all (otherwise a false alarm)."""
    resp = _dashboard_with_dead(monkeypatch, [])
    assert resp.status_code == 200
    assert BANNER_TITLE not in resp.text


def test_dashboard_banner_names_the_dead_channel(monkeypatch):
    resp = _dashboard_with_dead(monkeypatch, ["Telegram"])
    assert "Telegram больше не принимает сообщения" in resp.text


def test_anonymous_never_sees_banner():
    """An anonymous user is redirected to /login — the banner is not rendered at all."""
    resp = _client(sess=None).get("/app")
    assert resp.status_code == 303
    assert BANNER_TITLE not in resp.text


NO_CHANNELS_TITLE = "Шаблон создан, но не выбран ни один канал доставки"


def test_dashboard_warns_when_no_channel_at_all(monkeypatch):
    """Case (a): active template, no live channel → "tenders are visible in the cabinet, connect a channel"
    + a button to link a channel (not a one-off flag, but a persistent state)."""
    resp = _dashboard_with_delivery(monkeypatch, has_live=False, gaps=[])
    assert resp.status_code == 200
    assert (
        "Tenderi su vam vidljivi u kabinetu: povežite Telegram ili e-poštu za obaveštenja"
        in resp.text
        or "Тендеры видны в кабинете: подключите Telegram или e-mail, чтобы получать уведомления"
        in resp.text
    )
    assert 'href="/cabinet/account#kanali"' in resp.text
    assert "data-no-channel" in resp.text


def test_dashboard_no_channel_warning_absent_with_live_channel(monkeypatch):
    resp = _dashboard_with_delivery(monkeypatch, has_live=True, gaps=[])
    assert "data-no-channel" not in resp.text
    assert "data-channelless" not in resp.text


def test_dashboard_no_channel_warning_absent_without_active_profile(monkeypatch):
    resp = _dashboard_with_delivery(
        monkeypatch,
        has_live=False,
        gaps=[],
        profiles=[_profile(is_active=False)],
    )
    assert "data-no-channel" not in resp.text


def test_dashboard_hides_no_channels_notice_without_flag(monkeypatch):
    """An ordinary visit to /app (without the flag) — no warning (otherwise a false alarm on every visit)."""
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([_profile()]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )
    monkeypatch.setattr(routes_profiles.channel_svc, "dead_channels", _async([]))
    resp = _client(sess=SESS).get("/app")
    assert resp.status_code == 200
    assert NO_CHANNELS_TITLE not in resp.text


def _dashboard_with_delivery(monkeypatch, *, has_live, gaps, profiles=None):
    """The dashboard with the set channel_service.delivery_channel_status."""
    monkeypatch.setattr(
        routes_profiles.svc, "list_profiles", _async(profiles or [_profile()])
    )
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )
    monkeypatch.setattr(routes_profiles.channel_svc, "dead_channels", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc,
        "delivery_channel_status",
        _async({"has_live_channel": has_live, "gaps": gaps, "channelless": []}),
    )
    return _client(sess=SESS).get("/app")


GAP_TITLE = "Доставка настроена не до конца"


def _email_gap(*, pending=False):
    return {
        "channel": "email",
        "label": "Email",
        "pending": pending,
        "link": "/cabinet/account#kanali",
    }


def test_dashboard_gap_notice_defers_to_amber_when_no_live_channel(monkeypatch):
    """The profile asks for e-mail, no live channel → one amber banner, the blue nudge is silent.

    Both say “channel not connected” (owner: one banner per meaning). The expectation — from ``_delivery_warning.html``: the sign of the amber case ``data-no-channel``."""
    resp = _dashboard_with_delivery(monkeypatch, has_live=False, gaps=[_email_gap()])
    assert resp.status_code == 200
    assert "data-no-channel" in resp.text
    assert GAP_TITLE not in resp.text


def test_dashboard_hides_gap_notice_when_all_channels_live(monkeypatch):
    """All channels are linked → no nudge (otherwise a false alarm)."""
    resp = _dashboard_with_delivery(monkeypatch, has_live=True, gaps=[])
    assert resp.status_code == 200
    assert GAP_TITLE not in resp.text


def test_dashboard_gap_notice_pending_is_not_duplicated(monkeypatch):
    """E-mail awaits confirmation, no live channel → only amber, without the blue nudge.

    Previously the blue said “awaits confirmation”; now one banner per meaning (amber). The hint “confirm” is not shown in amber — a deliberate trade-off."""
    resp = _dashboard_with_delivery(
        monkeypatch, has_live=False, gaps=[_email_gap(pending=True)]
    )
    assert "data-no-channel" in resp.text
    assert GAP_TITLE not in resp.text
    assert "Подтвердить Email →" not in resp.text


def test_dashboard_channel_step_done_reflects_live_channel(monkeypatch):
    """‼️ The step “connect a channel” is done ⇔ there is a live channel (has_live_channel), not telegram_linked. With a live channel and an empty feed: profile+channel = 2/3; without a live channel — 1/3. A mutation (the step on telegram_linked) will make this test fall."""
    done = _dashboard_with_delivery(monkeypatch, has_live=True, gaps=[])
    assert "Подключить канал доставки" in done.text
    assert "2 из 3" in done.text

    missing = _dashboard_with_delivery(monkeypatch, has_live=False, gaps=[_email_gap()])
    assert "1 из 3" in missing.text
    # The path to both channels, not only to Telegram (the task: e-mail OR telegram).
    assert "или получать на email →" in missing.text


def test_dashboard_shows_downgrade_notice(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([_profile()]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async(["AI-анализ"]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )
    resp = _client(sess=SESS).get("/app")
    assert resp.status_code == 200
    assert "AI-анализ" in resp.text
    assert "Подключить Telegram" in resp.text  # telegram_linked=False


# --------------------------------------------------------------------------- #
# Create/edit forms (GET)
# --------------------------------------------------------------------------- #


def test_new_profile_form_renders():
    resp = _client(sess=SESS).get("/profiles/new?manual=1")
    assert resp.status_code == 200
    assert "Новый шаблон" in resp.text


def test_edit_profile_form_renders(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_profile()))
    resp = _client(sess=SESS).get("/profiles/p-1/edit")
    assert resp.status_code == 200
    assert "Редактирование шаблона" in resp.text


def test_new_profile_form_shows_onboarding_for_first_profile(monkeypatch):
    # FakeSession → 0 profiles → a micro-onboarding “why a profile”.
    monkeypatch.setattr(routes_profiles.svc, "count_active_profiles", _async(0))
    resp = _client(sess=SESS).get("/profiles/new?manual=1")
    assert resp.status_code == 200
    assert "Зачем нужен шаблон" in resp.text


def test_new_profile_form_hides_onboarding_when_profiles_exist(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "count_active_profiles", _async(2))
    resp = _client(sess=SESS).get("/profiles/new?manual=1")
    assert resp.status_code == 200
    assert "Зачем нужен шаблон" not in resp.text


def test_new_profile_form_cpv_search_fields_have_accessible_name():
    """The CPV search fields (include/exclude) — the placeholder is not a caption: both must have an aria-label (the programmatic name for a screen reader)."""
    resp = _client(sess=SESS).get("/profiles/new?manual=1")
    assert resp.status_code == 200
    assert 'id="cpvIncludeSearch"' in resp.text
    assert 'id="cpvExcludeSearch"' in resp.text
    inc_input = re.search(r'<input[^>]*id="cpvIncludeSearch"[^>]*>', resp.text).group(0)
    exc_input = re.search(r'<input[^>]*id="cpvExcludeSearch"[^>]*>', resp.text).group(0)
    assert re.search(r'aria-label="[^"]+"', inc_input), inc_input
    assert re.search(r'aria-label="[^"]+"', exc_input), exc_input


def test_lite_form_shows_gate_notes_with_plans_cta(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "get_tenant_tier", _async("lite"))
    resp = _client(sess=SESS).get("/profiles/new?manual=1")
    assert resp.status_code == 200
    # the gate fields of an unavailable plan are visible, with a CTA to /plans (not silently hidden).
    assert "🔒" in resp.text
    assert 'href="/plans"' in resp.text
    # The auto-analysis is back (15.08.2026, owner decision) → for Lite it is gated again
    # and this is SAID: the field is visible, the caption explains, the CTA leads to the plans.
    # A paid feature cannot be silently hidden — the subscriber decides whether to buy it.
    assert "AI-разбор ТЗ доступен с тарифа Pro" in resp.text


def test_pro_form_has_no_gate_notes(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_profile()))
    monkeypatch.setattr(routes_profiles.svc, "get_tenant_tier", _async("pro"))
    resp = _client(sess=SESS).get("/profiles/p-1/edit")
    assert resp.status_code == 200
    assert "🔒" not in resp.text


def test_edit_profile_form_404_when_missing(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(None))
    resp = _client(sess=SESS).get("/profiles/missing/edit")
    assert resp.status_code == 404
    assert "не найден" in resp.text


# --------------------------------------------------------------------------- #
# VG-14: prefill of the form from a vertical package (?from_package=)
# --------------------------------------------------------------------------- #


def test_new_profile_form_prefilled_from_package():
    resp = _client(sess=SESS).get("/profiles/new?from_package=gradjevina")
    assert resp.status_code == 200
    # the name, keywords and CPV of the package got into the form
    assert 'value="Строительство"' in resp.text
    assert "građevin" in resp.text
    assert "строительство" not in resp.text
    # ‼️ The CPV is substituted as a PREFIX (45000000 is the section code, the tenders carry
    # the codes of the descendants; the exact code matched none of the tenders — measurement 04.08).
    assert "eu_cpv:45*" in resp.text
    assert "eu_cpv:45000000" not in resp.text


def test_new_profile_form_unknown_package_falls_back_to_blank():
    resp = _client(sess=SESS).get("/profiles/new?from_package=nope")
    assert resp.status_code == 200
    # an unknown package → a clean form, without a crash
    assert "Новый шаблон" in resp.text
    assert 'value="Строительство"' not in resp.text


# --------------------------------------------------------------------------- #
# Creating a profile (POST /profiles)
# --------------------------------------------------------------------------- #


def test_create_profile_form_errors_render_400(monkeypatch):
    monkeypatch.setattr(
        routes_profiles,
        "parse_profile_form",
        lambda form: ({}, ["Название обязательно"]),
    )
    resp = _client(sess=SESS).post("/profiles", data={"name": ""})
    assert resp.status_code == 400
    assert "Название обязательно" in resp.text


def test_create_profile_service_error_render_400(monkeypatch):
    monkeypatch.setattr(
        routes_profiles, "parse_profile_form", lambda form: ({"name": "X"}, [])
    )
    monkeypatch.setattr(
        routes_profiles.svc,
        "create_profile",
        _async(SaveResult(errors=["Достигнут лимит профилей"])),
    )
    resp = _client(sess=SESS).post("/profiles", data={"name": "X"})
    assert resp.status_code == 400
    assert "Достигнут лимит профилей" in resp.text


def test_create_profile_limit_error_links_to_account(monkeypatch):
    """owner 22.09.2026: err_profile_limit leads the client to /cabinet/account, not just reports the refusal text — the client hit it and could not find what to delete (BOUNDARIES: err_profile_not_found and other errors do NOT get the link)."""
    monkeypatch.setattr(
        routes_profiles, "parse_profile_form", lambda form: ({"name": "X"}, [])
    )
    monkeypatch.setattr(
        routes_profiles.svc,
        "create_profile",
        _async(SaveResult(errors=["err_profile_limit"])),
    )
    resp = _client(sess=SESS).post("/profiles", data={"name": "X"})
    assert resp.status_code == 400
    assert 'href="/cabinet/account"' in resp.text


def test_create_profile_other_error_has_no_account_link(monkeypatch):
    """Counter-fact: an error other than err_profile_limit does not get the link."""
    monkeypatch.setattr(
        routes_profiles, "parse_profile_form", lambda form: ({"name": "X"}, [])
    )
    monkeypatch.setattr(
        routes_profiles.svc,
        "create_profile",
        _async(SaveResult(errors=["err_profile_not_found"])),
    )
    resp = _client(sess=SESS).post("/profiles", data={"name": "X"})
    assert resp.status_code == 400
    assert 'href="/cabinet/account"' not in resp.text


def test_create_profile_success_redirects_to_result_screen(monkeypatch):
    """After creation — the result screen, not a silent /app (step 5, owner 22.09)."""
    monkeypatch.setattr(
        routes_profiles, "parse_profile_form", lambda form: ({"name": "X"}, [])
    )
    monkeypatch.setattr(
        routes_profiles.svc,
        "create_profile",
        _async(SaveResult(profile=_profile())),
    )
    resp = _client(sess=SESS).post("/profiles", data={"name": "X"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/profiles/p-1/created"


def test_create_profile_no_channels_redirects_with_notice_flag(monkeypatch):
    """Step 2 profile_channels: a saved template WITHOUT delivery channels must not look like an ordinary success — the redirect carries a flag, which the result screen turns into a visible banner (symptom profile b0d433a4/f2cc3032).

    ‼️ Branch #531 survived the move from /app to the result screen: the sign “no channels” is still separated from the ordinary success, only the display address changed."""
    monkeypatch.setattr(
        routes_profiles, "parse_profile_form", lambda form: ({"name": "X"}, [])
    )
    monkeypatch.setattr(
        routes_profiles.svc,
        "create_profile",
        _async(SaveResult(profile=_profile(delivery_channels=[]))),
    )
    resp = _client(sess=SESS).post("/profiles", data={"name": "X"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/profiles/p-1/created?no_channels=1"


# --------------------------------------------------------------------------- #
# Profile detail (GET /profiles/{id})
# --------------------------------------------------------------------------- #


def test_profile_detail_renders(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_profile()))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    resp = _client(sess=SESS).get("/profiles/p-1")
    assert resp.status_code == 200
    assert "Стройка" in resp.text


def test_profile_detail_shows_gate_cta_when_suppressed(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_profile()))
    monkeypatch.setattr(
        routes_profiles.svc, "downgrade_notice", _async(["Своё расписание (Pro+)"])
    )
    resp = _client(sess=SESS).get("/profiles/p-1")
    assert resp.status_code == 200
    assert "Доступно на Pro" in resp.text
    assert 'href="/plans"' in resp.text


def test_profile_detail_404_when_missing(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(None))
    resp = _client(sess=SESS).get("/profiles/missing")
    assert resp.status_code == 404
    assert "не найден" in resp.text


# --------------------------------------------------------------------------- #
# Profile update (POST /profiles/{id})
# --------------------------------------------------------------------------- #


def test_update_profile_form_errors_render_400(monkeypatch):
    monkeypatch.setattr(
        routes_profiles, "parse_profile_form", lambda form: ({}, ["Ошибка валидации"])
    )
    resp = _client(sess=SESS).post("/profiles/p-1", data={"name": ""})
    assert resp.status_code == 400
    assert "Ошибка валидации" in resp.text


def test_update_profile_service_error_render_400(monkeypatch):
    monkeypatch.setattr(
        routes_profiles, "parse_profile_form", lambda form: ({"name": "X"}, [])
    )
    monkeypatch.setattr(
        routes_profiles.svc,
        "update_profile",
        _async(SaveResult(errors=["Профиль не найден."])),
    )
    resp = _client(sess=SESS).post("/profiles/p-1", data={"name": "X"})
    assert resp.status_code == 400
    assert "Профиль не найден." in resp.text


def test_update_profile_success_redirects_to_detail(monkeypatch):
    monkeypatch.setattr(
        routes_profiles, "parse_profile_form", lambda form: ({"name": "X"}, [])
    )
    monkeypatch.setattr(
        routes_profiles.svc,
        "update_profile",
        _async(SaveResult(profile=_profile())),
    )
    resp = _client(sess=SESS).post("/profiles/p-1", data={"name": "X"})
    assert resp.status_code == 303
    # ?updated=1 — the screen after the edit shows the block “Already open tenders”.
    assert resp.headers["location"] == "/profiles/p-1?updated=1"


# --------------------------------------------------------------------------- #
# Profile deletion (POST /profiles/{id}/delete)
# --------------------------------------------------------------------------- #


def test_delete_profile_redirects_to_app(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "soft_delete_profile", _async(True))
    resp = _client(sess=SESS).post("/profiles/p-1/delete")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


# --------------------------------------------------------------------------- #
# The toggle “Disable / Enable” (POST /profiles/{id}/deactivate|activate)
# --------------------------------------------------------------------------- #


def test_deactivate_profile_sets_inactive_and_redirects(monkeypatch):
    """Disabling touches ONLY is_active and does not call the deletion."""
    seen: dict = {}

    async def _set_active(db, **kw):
        seen.update(kw)
        return True

    async def _fail_delete(*a, **kw):  # pragma: no cover - a safety net
        raise AssertionError("the toggle has no right to call soft_delete_profile")

    monkeypatch.setattr(routes_profiles.svc, "set_profile_active", _set_active)
    monkeypatch.setattr(routes_profiles.svc, "soft_delete_profile", _fail_delete)
    resp = _client(sess=SESS).post("/profiles/p-1/deactivate")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"
    assert seen == {"tenant_id": "t-1", "profile_id": "p-1", "is_active": False}


def test_activate_profile_turns_it_back_on(monkeypatch):
    seen: dict = {}

    async def _set_active(db, **kw):
        seen.update(kw)
        return True

    monkeypatch.setattr(
        routes_profiles.svc, "get_profile", _async(_profile(is_active=False))
    )
    monkeypatch.setattr(routes_profiles.svc, "get_tenant_tier", _async("pro"))
    monkeypatch.setattr(routes_profiles.svc, "count_enabled_profiles", _async(1))
    monkeypatch.setattr(routes_profiles.svc, "set_profile_active", _set_active)
    resp = _client(sess=SESS).post("/profiles/p-1/activate")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"
    assert seen == {"tenant_id": "t-1", "profile_id": "p-1", "is_active": True}


def test_activate_unknown_profile_changes_nothing(monkeypatch):
    """A foreign/non-existent id: the row is not found → 0 changes, without a 500."""

    async def _fail(*a, **kw):  # pragma: no cover - a safety net
        raise AssertionError("there must be no record: the profile is not found")

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(None))
    monkeypatch.setattr(routes_profiles.svc, "set_profile_active", _fail)
    resp = _client(sess=SESS).post("/profiles/p-404/activate")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


def test_activate_refuses_over_plan_limit_visibly(monkeypatch):
    """The limit is fully taken → no record, a redirect with the refusal flag."""

    async def _fail(*a, **kw):  # pragma: no cover - a safety net
        raise AssertionError("enabling beyond the limit must not write to the DB")

    monkeypatch.setattr(
        routes_profiles.svc, "get_profile", _async(_profile(is_active=False))
    )
    monkeypatch.setattr(routes_profiles.svc, "get_tenant_tier", _async("pro"))
    # pro = 3 active (tiers.PROFILE_LIMITS), all three already working.
    monkeypatch.setattr(routes_profiles.svc, "count_enabled_profiles", _async(3))
    monkeypatch.setattr(routes_profiles.svc, "set_profile_active", _fail)
    resp = _client(sess=SESS).post("/profiles/p-1/activate")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app?limit=profiles"


def test_activate_already_active_is_not_blocked_by_limit(monkeypatch):
    """Re-enabling an enabled one does not hit the limit it itself belongs to."""
    seen: dict = {}

    async def _set_active(db, **kw):
        seen.update(kw)
        return True

    monkeypatch.setattr(
        routes_profiles.svc, "get_profile", _async(_profile(is_active=True))
    )
    monkeypatch.setattr(routes_profiles.svc, "get_tenant_tier", _async("lite"))
    monkeypatch.setattr(routes_profiles.svc, "count_enabled_profiles", _async(1))
    monkeypatch.setattr(routes_profiles.svc, "set_profile_active", _set_active)
    resp = _client(sess=SESS).post("/profiles/p-1/activate")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"
    assert seen == {}  # nothing to change: the gate is skipped, there is no record


# --------------------------------------------------------------------------- #
# A return to where one clicked from (a hidden field ``next``, a white list)
# --------------------------------------------------------------------------- #


def test_deactivate_next_returns_to_account(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "set_profile_active", _async(True))
    resp = _client(sess=SESS).post(
        "/profiles/p-1/deactivate", data={"next": "/cabinet/account"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account"


def test_activate_next_returns_to_account(monkeypatch):
    monkeypatch.setattr(
        routes_profiles.svc, "get_profile", _async(_profile(is_active=False))
    )
    monkeypatch.setattr(routes_profiles.svc, "get_tenant_tier", _async("pro"))
    monkeypatch.setattr(routes_profiles.svc, "count_enabled_profiles", _async(1))
    monkeypatch.setattr(routes_profiles.svc, "set_profile_active", _async(True))
    resp = _client(sess=SESS).post(
        "/profiles/p-1/activate", data={"next": "/cabinet/account"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account"


@pytest.mark.parametrize("evil", ["https://evil.com", "//evil.com", "/evil"])
def test_next_open_redirect_rejected(monkeypatch, evil):
    """``next`` — a white list {/app, /cabinet/account}, not an arbitrary path.

    ``/evil`` is also discarded: a white list, not “any relative path” (otherwise the form could lead to any internal page, not only to the two allowed).
    """
    monkeypatch.setattr(routes_profiles.svc, "set_profile_active", _async(True))
    resp = _client(sess=SESS).post("/profiles/p-1/deactivate", data={"next": evil})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


def test_activate_limit_reached_redirects_to_account_with_banner(monkeypatch):
    """A refusal by the limit with next=/cabinet/account goes THERE with the banner flag,
    not always to /app (.claude/rules/money-ai-tiers.md — the refusal must be visible where the request came from)."""

    async def _fail(*a, **kw):  # pragma: no cover - a safety net
        raise AssertionError("enabling beyond the limit must not write to the DB")

    monkeypatch.setattr(
        routes_profiles.svc, "get_profile", _async(_profile(is_active=False))
    )
    monkeypatch.setattr(routes_profiles.svc, "get_tenant_tier", _async("pro"))
    monkeypatch.setattr(routes_profiles.svc, "count_enabled_profiles", _async(3))
    monkeypatch.setattr(routes_profiles.svc, "set_profile_active", _fail)
    resp = _client(sess=SESS).post(
        "/profiles/p-1/activate", data={"next": "/cabinet/account"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account?limit=profiles"


def test_dashboard_shows_toggle_and_no_delete(monkeypatch):
    """On the dashboard — a toggle by state; creation/deletion does not move here."""
    monkeypatch.setattr(
        routes_profiles.svc,
        "list_profiles",
        _async([_profile(id="p-on"), _profile(id="p-off", is_active=False)]),
    )
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    resp = _client(sess=SESS).get("/app")
    assert resp.status_code == 200
    assert 'action="/profiles/p-on/deactivate"' in resp.text
    assert 'action="/profiles/p-off/activate"' in resp.text
    # Disabling — with confirmation, enabling — without (owner).
    assert re.search(
        r'action="/profiles/p-on/deactivate" data-confirm="[^"]+"', resp.text
    )
    assert 'action="/profiles/p-off/activate">' in resp.text
    # There is no deletion on the dashboard — it lives in the cabinet (owner 22.09).
    assert "/delete" not in resp.text


def _dashboard_tier(monkeypatch, tier="pro", status="active"):
    """The tenant’s plan/status for the dashboard subscription block (one source)."""
    monkeypatch.setattr(
        routes_profiles.subscription_service, "get_subscription", _async(None)
    )
    monkeypatch.setattr(
        routes_profiles.subscription_service, "get_tenant_tier", _async((tier, status))
    )


def test_dashboard_shows_limit_refusal_banner(monkeypatch):
    """?limit=profiles → a visible refusal with the limit number and a link to the plans."""
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([_profile()]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    _dashboard_tier(monkeypatch)
    resp = _client(sess=SESS).get("/app?limit=profiles")
    assert resp.status_code == 200
    assert "Шаблон не включён: лимит тарифа занят целиком" in resp.text
    assert "не больше 3" in resp.text  # tiers.PROFILE_LIMITS["pro"]
    assert 'href="/plans"' in resp.text
    # Without the banner flag there must be no banner.
    assert "Шаблон не включён" not in _client(sess=SESS).get("/app").text


# --------------------------------------------------------------------------- #
# The block “subscription and limit” on the dashboard (owner 22.09)
# --------------------------------------------------------------------------- #


def _dashboard(monkeypatch, *, profiles, days, tier="pro", status="active"):
    from datetime import datetime, timedelta, timezone

    from src.api.cabinet.stats_service import AccessDays

    until = None if days is None else datetime.now(timezone.utc) + timedelta(days=days)
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async(profiles))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.stats_service,
        "get_days_left",
        _async(AccessDays(label="", days=days, until=until)),
    )
    _dashboard_tier(monkeypatch, tier=tier, status=status)
    return _client(sess=SESS).get("/app")


def test_dashboard_shows_limit_in_templates_block_not_account_strip(monkeypatch):
    """“Used N of M” — in the templates block (v2, owner 13.10: the “Subscription” strip is removed; the plan and term are the header badge). There is no “Account management” entry on the dashboard any more."""
    resp = _dashboard(monkeypatch, profiles=[_profile(), _profile(id="p-2")], days=30)
    assert resp.status_code == 200
    assert "занято 2 шаблона из 3" in resp.text  # pro = 3 (tiers.PROFILE_LIMITS)
    assert 'class="card mb-6" aria-label="Подписка и лимит шаблонов"' not in resp.text
    assert "доступ до" not in resp.text
    assert "data-dash-sub-strip" not in resp.text  # 30 d. — there is no call-to-action strip


def test_dashboard_sub_strip_appears_only_when_week_left(monkeypatch):
    """‼️ The term is a call-to-action strip ONLY at ≤7 days (warn tone) or when expired (error)."""
    resp = _dashboard(monkeypatch, profiles=[_profile()], days=5)
    assert "Подписка заканчивается через 5 дн." in resp.text
    assert 'class="alert alert--warn" data-dash-sub-strip' in resp.text
    assert 'href="/cabinet/account#pretplata"' in resp.text
    resp8 = _dashboard(monkeypatch, profiles=[_profile()], days=8)
    assert "data-dash-sub-strip" not in resp8.text
    assert "Подписка заканчивается" not in resp8.text


def test_dashboard_sub_strip_expired_is_error_not_zero_days(monkeypatch):
    resp = _dashboard(monkeypatch, profiles=[_profile()], days=-2)
    assert 'class="alert alert--error" data-dash-sub-strip' in resp.text
    assert "Подписка истекла." in resp.text
    assert "через 0" not in resp.text


def test_dashboard_sub_strip_unlimited_access_is_silent(monkeypatch):
    """‼️ access_until IS NULL = open-ended: neither “0 days”, nor a strip at all."""
    resp = _dashboard(monkeypatch, profiles=[_profile()], days=None)
    assert "data-dash-sub-strip" not in resp.text
    assert "через 0" not in resp.text
    assert "без ограничения срока" not in resp.text


def test_dashboard_limit_matches_account_for_trial(monkeypatch):
    """The trial access = Pro rights: both the dashboard and the cabinet write “of 3”, not “of 1”.

    The expectation as a literal from a fact: PROFILE_LIMITS['pro'] = 3, the tenant's tier on the trial is stored as 'lite' (effective_tier raises it to 'pro').
    """
    resp = _dashboard(
        monkeypatch, profiles=[_profile()], days=10, tier="lite", status="trial"
    )
    assert "занято 1 шаблон из 3" in resp.text
    assert "занято 1 шаблон из 1" not in resp.text


def test_dashboard_does_not_repeat_subscription_term_of_account(monkeypatch):
    """The subscription term lives on /cabinet/account (and as the header badge); the dashboard v2 does not repeat it (before — 5 copies). The cabinet still shows the term.

    The former test required “30 days of access left” on BOTH screens — the dashboard has no such line any more by the owner’s decision 13.10."""
    dash_resp = _dashboard(monkeypatch, profiles=[_profile()], days=30, tier="pro")
    assert dash_resp.status_code == 200
    account_resp = _client(sess=SESS).get("/cabinet/account")
    assert account_resp.status_code == 200
    assert "30 дней доступа осталось" in account_resp.text
    assert "30 дней доступа осталось" not in dash_resp.text
    assert "дн. доступа" not in dash_resp.text


# --------------------------------------------------------------------------- #
# Flow pulse: “in the last 7 days N came”
# --------------------------------------------------------------------------- #


def _dashboard_pulse(monkeypatch, n):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([_profile()]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    _dashboard_tier(monkeypatch)
    monkeypatch.setattr(routes_profiles, "_flow_pulse", _async(n))
    return _client(sess=SESS).get("/app")


def test_dashboard_pulse_is_hero_tile_with_window_constant(monkeypatch):
    """The flow pulse — the hero tile “New for you, 7 days” (v2): the number from `_flow_pulse`, the window — from one constant PULSE_WINDOW_DAYS (the caption “7 days” in the catalog must match it)."""
    assert routes_profiles.PULSE_WINDOW_DAYS == 7
    for n in (12, 1, 2):
        html = _dashboard_pulse(monkeypatch, n).text
        assert (
            f'data-count-to="{n}">{n}</p>\n      <p class="ck-tile__label">Новое для вас, 7 дней'
            in html
        )


def test_pulse_zero_explains_and_links_to_help(monkeypatch):
    """‼️ A zero without an explanation is read by the client as a breakdown (#690 weakened this test to “just 0”). The zero tile — the number + an explanation + the link “How it works →” to /help#start; the tile is NOT a link as a whole (interactive content inside <a> is invalid).

    Mutation: return to _cockpit_hero.html a single <a>-block without the `else` branch — there is no explanation and no link, the test goes red."""
    html = _dashboard_pulse(monkeypatch, 0).text
    assert (
        'data-count-to="0">0</p>\n      <p class="ck-tile__label">Новое для вас' in html
    )
    assert "За последние 7 дней по вашим шаблонам не пришло ни одного тендера." in html
    assert '<a href="/help#start">Как это работает →</a>' in html
    assert 'class="ck-tile ck-tile--explain" data-cockpit-card data-pulse' in html
    assert (
        '<a href="/app/feed" class="ck-tile" data-cockpit-card data-pulse>' not in html
    )


def test_pulse_sql_mirrors_feed_predicates():
    """The pulse is a wrapper ABOVE the feed: both predicates of the feed stand in its SQL verbatim.

    If they diverged, the dashboard would say “N came”, and the feed would show something else.
    """
    sql = str(routes_profiles._PULSE_SQL)
    assert feed_service._CURRENT_ONLY_PREDICATE in sql
    assert feed_service._PROFILE_FILTER_PREDICATE in sql
    # owner 16.10: the pulse counts the same matches as the feed (incl. those trimmed by the limit)
    assert (
        "o.status IN ('sent', 'suppressed', 'delivering', 'pending', "
        "'ai_pending', 'analyzing', 'ai_done', 'web_only')"
    ) in sql
    assert "DISTINCT o.tender_id, o.profile_id" in sql
    assert (
        "COALESCE(o.sent_at, o.failed_at, o.created_at) >= now() - "
        "CAST(:days || ' days' AS interval)"
    ) in sql


async def test_flow_pulse_binds_every_sql_parameter():
    """Every bind parameter of the pulse SQL gets a value.

    The predicates of the pulse are taken from feed_service, and when a new parameter appears there (#536: ``:actual_only``), the wrapper must pass it — otherwise the dashboard falls on a live DB, which the route’s mocks do not see. A semantic conflict of two green branches, caught only by a run on PG.

    Mutation: remove ``"actual_only"`` from the parameters of ``_flow_pulse`` — the test goes red with the name of the missing parameter.
    """
    seen = {}

    class _DB:
        async def execute(self, stmt, params):
            seen["binds"] = set(stmt.compile().params)
            seen["params"] = dict(params)
            return SimpleNamespace(scalar_one=lambda: 4)

    assert await routes_profiles._flow_pulse(_DB(), "t-1") == 4
    missing = seen["binds"] - set(seen["params"])
    assert not missing, f"without a value: {sorted(missing)}"
    assert seen["params"]["actual_only"] is True  # as for the teaser: the working feed


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _async(return_value):
    """Returns an async stub function with a fixed result."""

    async def _stub(*args, **kwargs):
        return return_value

    return _stub


# --------------------------------------------------------------------------- #
# E2: the form offers only the channels that really deliver
# --------------------------------------------------------------------------- #


def test_channel_choices_match_available_channels():
    """The showcase of channels = what the parsing of the form will accept. One axis, not two.

    A divergence would mean a false promise (a channel is offered, validation throws it out) or a dead choice — exactly the E2 defect.
    """
    assert [v for v, _ in routes_profiles.CHANNEL_CHOICES] == ["email", "telegram"]
    assert {v for v, _ in routes_profiles.CHANNEL_CHOICES} == forms.AVAILABLE_CHANNELS


def test_profile_form_offers_no_undeliverable_channel():
    resp = _client(sess=SESS).get("/profiles/new?manual=1")
    assert resp.status_code == 200
    assert 'name="delivery_channels" value="telegram"' in resp.text
    assert 'name="delivery_channels" value="email"' in resp.text
    assert 'name="delivery_channels" value="viber"' not in resp.text
    assert 'name="delivery_channels" value="web"' not in resp.text


# --------------------------------------------------------------------------- #
# The prefill inherits the account’s live channels (symptom profile b0d433a4/f2cc3032:
# 300 suitable tenders in 30 days, 0 deliveries — the form ticked not a single
# checkbox, the channels were set by hand by the owner 22.09)
# --------------------------------------------------------------------------- #


def _channel_input(html: str, value: str) -> str:
    m = re.search(rf'<input[^>]*name="delivery_channels" value="{value}"[^>]*>', html)
    assert m, f"the checkbox of the channel {value!r} is not found in the form"
    return m.group(0)


def test_new_profile_form_from_package_inherits_live_telegram(monkeypatch):
    monkeypatch.setattr(
        routes_profiles.channel_svc,
        "telegram_channel_state",
        _async(ChannelState(channel="telegram", status="active", external_id="123456")),
    )
    monkeypatch.setattr(
        routes_profiles.channel_svc, "email_channel_state", _async(None)
    )
    resp = _client(sess=SESS).get("/profiles/new?from_package=gradjevina")
    assert resp.status_code == 200
    assert "checked" in _channel_input(resp.text, "telegram")
    assert "checked" not in _channel_input(resp.text, "email")


def test_new_profile_form_from_template_inherits_live_email(monkeypatch):
    monkeypatch.setattr(
        routes_profiles.lib_svc,
        "get_template",
        _async(
            SimpleNamespace(
                id="tpl-1",
                name="Стройка",
                classifications={"include": [], "exclude": []},
                keywords_include=[],
            )
        ),
    )
    monkeypatch.setattr(
        routes_profiles.channel_svc,
        "email_channel_state",
        _async(ChannelState(channel="email", status="active", external_id="a@b.rs")),
    )
    monkeypatch.setattr(
        routes_profiles.channel_svc, "telegram_channel_state", _async(None)
    )
    resp = _client(sess=SESS).get("/profiles/new?from_template=tpl-1")
    assert resp.status_code == 200
    assert "checked" in _channel_input(resp.text, "email")
    assert "checked" not in _channel_input(resp.text, "telegram")


def test_new_profile_form_no_channels_when_none_live():
    """Without connected channels the checkboxes stay empty — the visible refusal
    is taken on by step 2, not a silent saving."""
    resp = _client(sess=SESS).get("/profiles/new?from_package=gradjevina")
    assert resp.status_code == 200
    assert "checked" not in _channel_input(resp.text, "telegram")
    assert "checked" not in _channel_input(resp.text, "email")


def test_profile_detail_renders_legacy_channel(monkeypatch):
    """A profile with an old viber in the DB is read without a crash (backward compatibility)."""
    monkeypatch.setattr(
        routes_profiles.svc,
        "get_profile",
        _async(_profile(delivery_channels=["email", "viber"])),
    )
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    resp = _client(sess=SESS).get("/profiles/p-1")
    assert resp.status_code == 200
    assert "Email" in resp.text
