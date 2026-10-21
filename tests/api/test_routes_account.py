"""Tests of the “My account” route ``src/api/cabinet/routes_account.py`` (owner 22.09.2026).

The reason — the client hit “Profile limit reached” and could not find what to delete (there was no list of ``/profiles``). The service layer is mocked via ``monkeypatch`` (ADR-013), the template renders for real — we check the session gate, the limit counter BEFORE the refusal, and the presence of the delete button on each profile row.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import channel_service as channel_svc
from src.api.cabinet import routes_account, routes_subscription
from src.api.cabinet.stats_service import AccessDays
from src.api.cabinet.trial_service import AccessStatus
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


def _profile(**kw):
    base = dict(
        id="p-1",
        name="Стройка",
        is_active=True,
        match_all=False,
        keywords_include=[{"all_of": ["строительство"]}],
        regions=["RS-00"],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _client(*, sess=None):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _setup(
    monkeypatch,
    *,
    tier="pro",
    status="active",
    profiles=None,
    telegram_live=False,
    email_state=None,
    access_status=None,
    subscription=None,
    access_days=None,
):
    # The plan/term/limits of the hub are collected by routes_subscription.build_subscription_context
    # (one resolver for the Status and Subscription sections) — we substitute its services.
    monkeypatch.setattr(
        routes_subscription.svc, "get_tenant_tier", _async((tier, status))
    )
    monkeypatch.setattr(
        routes_subscription.svc, "get_subscription", _async(subscription)
    )
    monkeypatch.setattr(routes_subscription.svc, "get_last_decision", _async(None))
    monkeypatch.setattr(
        routes_subscription.ai_limits_service,
        "get_ai_limits",
        _async(SimpleNamespace(rows=[], period="")),
    )
    monkeypatch.setattr(
        routes_subscription.stats_service,
        "get_days_left",
        _async(
            access_days
            or AccessDays(
                label="до 01.01.2027",
                days=30,
                until=datetime(2027, 1, 1, tzinfo=timezone.utc),
            )
        ),
    )
    monkeypatch.setattr(routes_account.svc, "list_profiles", _async(profiles or []))
    monkeypatch.setattr(
        routes_account.trial_service,
        "get_access_status",
        _async(
            access_status
            or AccessStatus(state="active", active=True, can_request=False, until=None)
        ),
    )
    telegram_state = SimpleNamespace(is_live=telegram_live) if telegram_live else None
    monkeypatch.setattr(
        routes_account.channel_svc, "telegram_channel_state", _async(telegram_state)
    )
    monkeypatch.setattr(
        routes_account.channel_svc, "email_channel_state", _async(email_state)
    )


# --------------------------------------------------------------------------- #
# The session gate
# --------------------------------------------------------------------------- #


def test_anonymous_redirected_to_login():
    resp = _client(sess=None).get("/cabinet/account")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Fcabinet%2Faccount"


# --------------------------------------------------------------------------- #
# The limit counter BEFORE the refusal
# --------------------------------------------------------------------------- #


def test_pro_tier_at_limit_shows_reached_banner(monkeypatch):
    """Pro=3, exactly 3 profiles → “3 of 3 used” is visible EXPLICITLY, not after the refusal."""
    _setup(monkeypatch, tier="pro", profiles=[_profile(id=f"p-{i}") for i in range(3)])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "3 из 3" in resp.text
    assert "Лимит исчерпан" in resp.text


def test_tenant_with_one_of_three_does_not_show_limit_reached(monkeypatch):
    """The counter-fact of the task: 1 of 3 profiles — the limit banner is absent."""
    _setup(monkeypatch, tier="pro", profiles=[_profile()])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "1 из 3" in resp.text
    assert "Лимит исчерпан" not in resp.text


def test_trial_tenant_sees_pro_limit_not_lite(monkeypatch):
    """‼️ The limit is from the EFFECTIVE plan: on the trial it is Pro=3, not lite=1.

    The expectation as a literal from a fact: the tenant on the trial access has ``tier='lite', status='trial'`` in the DB, while ``create_profile`` counts the limit via ``effective_tier`` → 3. The page by the RAW tier wrote “1 of 1 used — limit exhausted” where creating a third template passes calmly.
    """
    _setup(monkeypatch, tier="lite", status="trial", profiles=[_profile()])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "1 из 3" in resp.text
    assert "1 из 1" not in resp.text
    assert "Лимит исчерпан" not in resp.text


def test_enterprise_tier_shows_unlimited(monkeypatch):
    _setup(monkeypatch, tier="enterprise", profiles=[_profile()])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "Без лимита шаблонов" in resp.text


# --------------------------------------------------------------------------- #
# The profile list: deletion in the row (the task’s boundary — the existing route)
# --------------------------------------------------------------------------- #


def test_active_profile_row_has_delete_action(monkeypatch):
    """Fix 23.09: “Deactivate”→/delete was a mismatch of caption and action — the client switched off, and the template disappeared irrevocably. Now three separate actions: “Disable” (reversible, /deactivate) and “Delete” (irrevocable, /delete) — and the captions do NOT mix with them."""
    _setup(monkeypatch, tier="pro", profiles=[_profile()])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert 'action="/profiles/p-1/deactivate"' in resp.text
    assert 'action="/profiles/p-1/delete"' in resp.text
    assert 'action="/profiles/p-1/activate"' not in resp.text
    assert "Отключить" in resp.text
    assert "Удалить" in resp.text
    assert "Деактивировать" not in resp.text  # the old (wrong) caption is gone
    assert "data-confirm" in resp.text  # CSP: the confirmation is not an inline handler
    # Both toggles return to /cabinet/account, not to the dashboard.
    assert resp.text.count('name="next" value="/cabinet/account"') == 1


def test_inactive_profile_row_has_activate_and_delete_actions(monkeypatch):
    """A disabled row: “Enable” (without confirmation) and “Delete” (with it)."""
    _setup(monkeypatch, tier="pro", profiles=[_profile(is_active=False)])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert 'action="/profiles/p-1/activate"' in resp.text
    assert 'action="/profiles/p-1/delete"' in resp.text
    assert 'action="/profiles/p-1/deactivate"' not in resp.text
    assert "Включить" in resp.text
    assert "Удалить" in resp.text
    # “Enable” — without confirm, “Delete” — with it.
    activate_form = re.search(
        r'<form method="post" action="/profiles/p-1/activate"[^>]*>', resp.text
    )
    assert activate_form is not None
    assert "data-confirm" not in activate_form.group(0)


def test_inactive_profiles_grouped_separately(monkeypatch):
    _setup(
        monkeypatch,
        tier="pro",
        profiles=[
            _profile(id="p-1", is_active=True),
            _profile(id="p-2", is_active=False),
        ],
    )
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "Активные" in resp.text
    assert "Деактивированные" in resp.text


def test_no_profiles_shows_empty_state(monkeypatch):
    _setup(monkeypatch, tier="pro", profiles=[])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "Шаблонов поиска пока нет." in resp.text


# --------------------------------------------------------------------------- #
# The delivery channels — from user_channels (channel_service), not delivery_channels
# --------------------------------------------------------------------------- #


def test_channels_show_not_connected_by_default(monkeypatch):
    _setup(monkeypatch, tier="pro", profiles=[])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert resp.text.count("не подключён ❌") == 2  # Telegram and e-mail


def test_telegram_linked_shows_connected_badge(monkeypatch):
    _setup(monkeypatch, tier="pro", profiles=[], telegram_live=True)
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "подключён ✅" in resp.text
    assert resp.text.count("не подключён ❌") == 1  # only the e-mail remained


def test_email_pending_shows_masked_address(monkeypatch):
    state = SimpleNamespace(
        status=channel_svc.CHANNEL_STATUS_PENDING,
        external_id="user@example.com",
        is_live=False,
    )
    _setup(monkeypatch, tier="pro", profiles=[], email_state=state)
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "ждёт подтверждения" in resp.text


# --------------------------------------------------------------------------- #
# Subscription — the plan and the access term are back in the cabinet (owner 23.09)
# --------------------------------------------------------------------------- #


def test_subscription_section_shows_plan_and_expiry(monkeypatch):
    """The plan and “access until” are rendered in the “Subscription” section, not only on the dashboard.

    Mutation: remove the ``<dl>`` from account.html — the test goes red (see TESTING).
    """
    _setup(monkeypatch, tier="pro", profiles=[])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert 'id="pretplata"' in resp.text
    assert "Pro" in resp.text
    assert "01.01.2027" in resp.text
    assert (
        'action="/cabinet/trial/request"' not in resp.text
    )  # active — there is no request


def test_subscription_section_expired_access_is_marked(monkeypatch):
    """An expired access — an explicit red row, not silence."""
    _setup(
        monkeypatch,
        tier="pro",
        profiles=[],
        access_days=AccessDays(
            label="доступ истёк",
            days=-2,
            until=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ),
    )
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "доступ истёк" in resp.text
    assert "01.09.2026" in resp.text


def test_subscription_section_unlimited_access(monkeypatch):
    """``access_until IS NULL`` — “open-ended”, not “0 days left”."""
    _setup(
        monkeypatch,
        tier="enterprise",
        profiles=[],
        access_days=AccessDays(label="бессрочно", days=None, until=None),
    )
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "бессрочно" in resp.text
    assert "Enterprise" in resp.text


@pytest.mark.parametrize(
    "lang,state,expected",
    [
        ("en", "pending", "awaiting approval"),
        ("en", "expired", "access expired — request again"),
        ("en", "rejected", "declined — you can request again"),
        ("en", "none", "trial access not activated"),
        ("sr", "none", "probni pristup nije aktiviran"),
        ("ru", "none", "тестовый доступ не активирован"),
    ],
)
def test_trial_request_lives_in_account_localized(monkeypatch, lang, state, expected):
    """The trial request moved from the dashboard; the status caption — from the catalog by locale (the service does not know the locale). Expectations — literals of the catalog at the moment of the move, not ``t()``.

    Mutation: remove the request block from account.html — all the parameters fall.
    """
    can_request = state in ("expired", "rejected", "none")
    _setup(
        monkeypatch,
        profiles=[],
        access_status=AccessStatus(
            state=state,
            active=state == "pending",
            can_request=can_request,
            until=None,
        ),
    )
    resp = _client(sess=SESS).get(f"/cabinet/account?lang={lang}")
    assert resp.status_code == 200
    assert expected in resp.text
    assert ('action="/cabinet/trial/request"' in resp.text) is can_request


def test_account_owns_template_creation_and_drops_status(monkeypatch):
    """Creating a template — only on the templates page (the cabinet); the row status (“active”, “delivered: N”) — on the dashboard, not duplicated here.

    Mutation: return the status badge to the row — the test goes red on “active”.
    """
    _setup(monkeypatch, tier="pro", profiles=[_profile()])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert (
        'href="/profiles/new" class="btn-primary shrink-0">Новый шаблон<' in resp.text
    )
    assert (
        "1 из 3" in resp.text
    )  # the remainder BEFORE the refusal — next to the create button
    assert ">активен<" not in resp.text
    assert "доставлено:" not in resp.text
    assert "пока нет доставок" not in resp.text
    assert (
        "кнопкой «Включить» в списке шаблонов на дашборде" not in resp.text
    )  # no disabled ones


def test_inactive_hint_points_to_inline_toggle(monkeypatch):
    """Fix 23.09: the “Enable” button now sits directly in the row — the hint no longer
    sends to the dashboard (step 4 of the task “deactivate ≠ delete”)."""
    _setup(monkeypatch, tier="pro", profiles=[_profile(is_active=False)])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "Включить обратно — кнопкой «Включить» в строке шаблона ниже." in resp.text
    assert "на дашборде" not in resp.text
    assert "отметьте «активен» и сохраните" not in resp.text


def test_account_shows_limit_notice_banner(monkeypatch):
    """A refusal of POST /profiles/{id}/activate by the limit (next=/cabinet/account) must be VISIBLE here too, not only on the dashboard (.claude/rules/money-ai-tiers.md)."""
    _setup(monkeypatch, tier="pro", profiles=[_profile()])
    resp = _client(sess=SESS).get("/cabinet/account?limit=profiles")
    assert resp.status_code == 200
    assert "лимит тарифа занят целиком" in resp.text


def test_account_hides_limit_notice_banner_without_flag(monkeypatch):
    _setup(monkeypatch, tier="pro", profiles=[_profile()])
    resp = _client(sess=SESS).get("/cabinet/account")
    assert resp.status_code == 200
    assert "лимит тарифа занят целиком" not in resp.text
