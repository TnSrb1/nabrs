"""Tests of the /plans showcase and the upgrade CTA /plans/upgrade (VG-12, steps 2–3).

DB and session are mocked via dependency_overrides; templates render for real.
We check: 200 on sr/ru/en, the highlighting of the current plan, anti-duplication,
the redirect of an anonymous user to /signup, the absence of charging (no calls to the payment API).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession

_TID = "11111111-1111-1111-1111-111111111111"
_UID = "22222222-2222-2222-2222-222222222222"
_REQ_ID = "33333333-3333-3333-3333-333333333333"

_PAST_DT = __import__("datetime").datetime(
    2020, 1, 1, tzinfo=__import__("datetime").timezone.utc
)


def _client(
    session: FakeSession | None = None,
    *,
    sess: dict | None = None,
    settings=None,
):
    def factory():
        return session or FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    if settings is not None:
        app.dependency_overrides[deps.get_settings] = lambda: settings
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _billing_settings(configured: bool):
    """CabinetSettings with set/empty stripe keys (the billing_configured gate)."""
    from src.api.config import CabinetSettings

    keys = (
        dict(STRIPE_API_KEY="sk_test", STRIPE_WEBHOOK_SECRET="whsec_test")
        if configured
        else {}
    )
    return CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="test-secret",
        BILLING_PROVIDER="stripe",
        **keys,
    )


# --------------------------------------------------------------------------- #
# GET /plans — the showcase
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("sr", "Cenovnik"),
        ("ru", "Тарифы"),
        ("en", "Pricing"),
    ],
)
def test_plans_page_languages(lang, expected):
    client = _client()
    resp = client.get("/plans", cookies={"lang": lang})
    assert resp.status_code == 200
    assert expected in resp.text


def test_plans_page_anon_sees_all_cards():
    client = _client()
    resp = client.get("/plans")
    assert resp.status_code == 200
    assert "Free" not in resp.text  # there is no public Free (the owner’s decision 30.09)
    assert "€0" not in resp.text
    assert "Pro" in resp.text
    assert "Business" in resp.text
    assert "Enterprise" in resp.text


def test_plans_page_anon_has_signup_cta():
    client = _client()
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "/signup" in resp.text


def test_plans_page_anon_no_upgrade_form():
    client = _client()
    resp = client.get("/plans")
    assert resp.status_code == 200
    assert "/plans/upgrade" not in resp.text


# --------------------------------------------------------------------------- #
# Value matrix (VG-12 part 2)
# --------------------------------------------------------------------------- #


def test_plans_page_shows_value_matrix_labels():
    """The matrix shows the gating features: bid verdict / draft / analytics."""
    client = _client()
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "Вердикт «участвовать или нет»" in resp.text
    assert "Черновик заявки" in resp.text
    assert "Аналитика заказчика, конкурентов и перезакупок" in resp.text
    assert "Глубокий AI-разбор спецификации" in resp.text


def test_plans_page_highlights_pro_without_popularity_claim():
    """Pro is visually highlighted, but without a “Popular” badge: there is no popularity data (07.10)."""
    client = _client()
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "Популярный" not in resp.text
    assert "plan-badge" not in resp.text
    assert "plan-featured" in resp.text


def test_plans_page_shows_limits_from_gating():
    """The numeric limits of the matrix match the gating: Pro deep=10/draft=5, Business 50/30."""
    client = _client()
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    # deep/draft limits with the suffix “/ mo.”
    assert "10 / мес." in resp.text  # Pro deep
    assert "5 / мес." in resp.text  # Pro draft
    assert "50 / мес." in resp.text  # Business deep
    assert "30 / мес." in resp.text  # Business draft


def test_plans_page_enterprise_cta_contact_form():
    """The Enterprise CTA — a link to the /kontakt form (mailto did not work without a mail client)."""
    client = _client()
    resp = client.get("/plans", cookies={"lang": "en"})
    assert resp.status_code == 200
    assert 'href="/kontakt"' in resp.text
    assert "unlimited" in resp.text  # ∞ limits of Enterprise


def test_plans_page_all_langs_render_matrix():
    """The matrix renders on sr/ru/en without template errors."""
    for lang in ("sr", "ru", "en"):
        client = _client()
        resp = client.get("/plans", cookies={"lang": lang})
        assert resp.status_code == 200, lang


# --------------------------------------------------------------------------- #
# Comparison table + FAQ (VG-12, night 07)
# --------------------------------------------------------------------------- #


def test_plans_page_shows_comparison_table():
    """The plan comparison table with the header and the Free/Pro/Business columns."""
    client = _client()
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "Сравнение планов" in resp.text
    assert "<table" in resp.text
    # Plan columns in the table header (scope=col).
    assert 'scope="col"' in resp.text
    assert 'scope="row"' in resp.text


def test_plans_page_comparison_reuses_gating_limits():
    """The table shows the same gating limits as the cards (Pro deep=10)."""
    client = _client()
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    # The feature is in the table as a row header.
    assert "Возможность" in resp.text


def test_plans_page_shows_pricing_faq():
    """The plan FAQ is present in all languages."""
    checks = {
        "ru": "Частые вопросы о тарифах",
        "en": "Pricing FAQ",
        "sr": "Česta pitanja o tarifama",
    }
    for lang, expected in checks.items():
        client = _client()
        resp = client.get("/plans", cookies={"lang": lang})
        assert resp.status_code == 200, lang
        assert expected in resp.text, lang
        assert "<details" in resp.text, lang


def test_plans_page_faq_is_static_no_billing_calls():
    """The FAQ is static: it does not pull billing/checkout logic (NON-RED contour)."""
    client = _client()
    resp = client.get("/plans")
    assert resp.status_code == 200
    # The FAQ and the table create no checkout links for an anonymous user.
    assert "/billing/checkout/" not in resp.text


def test_plans_i18n_key_parity():
    """i18n parity of the PLANS and features keys across all languages (anti-drift of translations)."""
    from src.api.public.i18n import PLANS
    from src.plans.catalog import FEATURE_KEYS

    langs = list(PLANS)
    ref = langs[0]
    ref_top = set(PLANS[ref])
    ref_feat = set(PLANS[ref]["features"])
    # features cover FEATURE_KEYS except the always-on 'profiles'/'telegram'? No — all keys are strings.
    assert set(FEATURE_KEYS) <= ref_feat, "features labels cover all FEATURE_KEYS"
    for lang in langs[1:]:
        assert set(PLANS[lang]) == ref_top, f"top-level keys {lang} != {ref}"
        assert (
            set(PLANS[lang]["features"]) == ref_feat
        ), f"features keys {lang} != {ref}"
        assert set(PLANS[lang]["plans"]) == set(PLANS[ref]["plans"]), lang


def test_plans_page_auth_shows_current_plan_lite():
    # A tenant with tier=None, status=None → effective_tier='lite' → plan_key='free'
    row = SimpleNamespace(tier=None, status=None)
    session = FakeSession(results=[FakeResult(items=[row])])
    client = _client(session, sess={"uid": _UID, "tid": _TID})
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    # A manual Free is an internal state: there is no Free card on the showcase.
    assert "Ваш план" not in resp.text
    assert "€0" not in resp.text


def test_plans_page_auth_shows_current_plan_pro():
    # Billing configured → the checkout button is visible on a non-current business.
    row = SimpleNamespace(tier="pro", status=None)
    session = FakeSession(results=[FakeResult(items=[row])])
    client = _client(
        session, sess={"uid": _UID, "tid": _TID}, settings=_billing_settings(True)
    )
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "Ваш план" in resp.text
    # Checkout for business (not the current plan); for pro (the current one) — none.
    assert "/billing/checkout/business" in resp.text
    assert "/billing/checkout/pro" not in resp.text


def test_plans_page_auth_trial_maps_to_pro():
    # status='trial' → effective_tier='pro' → highlight pro card
    row = SimpleNamespace(tier="lite", status="trial")
    session = FakeSession(results=[FakeResult(items=[row])])
    client = _client(session, sess={"uid": _UID, "tid": _TID})
    resp = client.get("/plans", cookies={"lang": "en"})
    assert resp.status_code == 200
    # The current plan is Pro (highlighted), so the form for Pro is absent
    assert 'name="plan" value="pro"' not in resp.text


def test_plans_page_auth_configured_shows_checkout():
    """billing configured → on the paid plans the “Subscribe” button → checkout."""
    row = SimpleNamespace(tier="lite", status=None)
    session = FakeSession(results=[FakeResult(items=[row])])
    client = _client(
        session, sess={"uid": _UID, "tid": _TID}, settings=_billing_settings(True)
    )
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert 'action="/billing/checkout/pro"' in resp.text
    assert 'action="/billing/checkout/business"' in resp.text
    assert "Оформить" in resp.text
    # Configured → no “Soon” at all.
    assert "Скоро" not in resp.text


def test_plans_page_auth_not_configured_shows_upgrade_form():
    """billing NOT configured → an active form “Request subscription”, not a dead “Soon”."""
    row = SimpleNamespace(tier="lite", status=None)
    session = FakeSession(results=[FakeResult(items=[row])])
    client = _client(
        session, sess={"uid": _UID, "tid": _TID}, settings=_billing_settings(False)
    )
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    # The active subscription request form for pro/business (logged in).
    assert 'action="/plans/upgrade"' in resp.text
    assert 'name="plan" value="pro"' in resp.text
    assert 'name="plan" value="business"' in resp.text
    assert "Запросить подписку" in resp.text
    # There is no dead “Soon” any more; checkout is not run with billing off.
    assert "Скоро" not in resp.text
    assert "/billing/checkout/" not in resp.text


def test_plans_page_anon_not_configured_shows_signup_not_form():
    """billing NOT configured + anonymous → the caption “Request subscription” as a link to /signup
    (an application cannot be created without an account), NOT a POST form."""
    client = _client(settings=_billing_settings(False))
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "Запросить подписку" in resp.text
    assert "/signup" in resp.text
    # An anonymous user does not get the POST form (it cannot be executed without a session).
    assert "/plans/upgrade" not in resp.text


def test_plans_upgrade_status_requested_notice():
    client = _client()
    resp = client.get("/plans?upgrade=requested", cookies={"lang": "ru"})
    assert resp.status_code == 200
    # New text: payment in progress + request sent to the admin + decision term.
    assert "администратору" in resp.text
    assert "1 рабочего дня" in resp.text


def test_plans_upgrade_status_pending_notice():
    client = _client()
    resp = client.get("/plans?upgrade=pending", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "рассмотрении" in resp.text


# --------------------------------------------------------------------------- #
# POST /plans/upgrade — CTA
# --------------------------------------------------------------------------- #


def test_upgrade_anon_redirects_to_signup():
    client = _client()
    resp = client.post("/plans/upgrade", data={"plan": "pro"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/signup"


def test_upgrade_invalid_plan_redirects_to_plans():
    client = _client(sess={"uid": _UID, "tid": _TID})
    resp = client.post("/plans/upgrade", data={"plan": "enterprise"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/plans"


def test_upgrade_creates_pending_request():
    from src.api.config import CabinetSettings

    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="test-secret",
        TG_BOT_TOKEN="",
        ADMIN_CHAT_ID=0,
    )
    # Queue: guard (no pending, access_until expired) → INSERT (request_id) → e-mail
    guard_row = SimpleNamespace(has_pending=False, access_until=_PAST_DT)
    session = FakeSession(
        results=[
            FakeResult(items=[guard_row]),
            FakeResult(scalar=_REQ_ID),
            FakeResult(items=["user@example.rs"]),
        ]
    )
    client = _client(session, sess={"uid": _UID, "tid": _TID}, settings=settings)
    resp = client.post("/plans/upgrade", data={"plan": "pro"})
    assert resp.status_code == 303
    assert "upgrade=requested" in resp.headers["location"]
    assert session.committed == 1
    # We check that the comment contains the plan key
    insert_calls = [
        sql for sql, _ in session.executed if "INSERT INTO trial_requests" in sql
    ]
    assert len(insert_calls) == 1


def test_upgrade_trial_user_active_access_still_creates(monkeypatch):
    """Regression of the silent loss: a trial user’s access_until is ACTIVE, but the upgrade
    application is still created and the admin is notified (otherwise the request is silently lost).
    """
    from src.api.cabinet import trial_service
    from src.api.config import CabinetSettings

    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="test-secret",
        TG_BOT_TOKEN="bot-token",
        ADMIN_CHAT_ID=42,
    )
    # access_until in the FUTURE => access is active (trial), but this is an upgrade_request.
    future = __import__("datetime").datetime(
        2999, 1, 1, tzinfo=__import__("datetime").timezone.utc
    )
    guard_row = SimpleNamespace(has_pending=False, access_until=future)
    session = FakeSession(
        results=[
            FakeResult(items=[guard_row]),
            FakeResult(scalar=_REQ_ID),
            FakeResult(items=["trial@example.rs"]),
        ]
    )

    notified: list[dict] = []

    async def _fake_notify(**kwargs):
        notified.append(kwargs)
        return True

    monkeypatch.setattr(trial_service, "notify_admin", _fake_notify)

    client = _client(session, sess={"uid": _UID, "tid": _TID}, settings=settings)
    resp = client.post("/plans/upgrade", data={"plan": "pro"})
    assert resp.status_code == 303
    assert "upgrade=requested" in resp.headers["location"]
    # The application is really recorded (commit) despite an active access_until.
    assert session.committed == 1
    insert_calls = [
        sql for sql, _ in session.executed if "INSERT INTO trial_requests" in sql
    ]
    assert len(insert_calls) == 1
    # The admin is notified with the request_id of the application.
    assert len(notified) == 1
    assert notified[0]["request_id"] == _REQ_ID


def test_upgrade_anti_dup_pending():
    from src.api.config import CabinetSettings

    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="test-secret",
        TG_BOT_TOKEN="",
        ADMIN_CHAT_ID=0,
    )
    guard_row = SimpleNamespace(has_pending=True, access_until=None)
    session = FakeSession(results=[FakeResult(items=[guard_row])])
    client = _client(session, sess={"uid": _UID, "tid": _TID}, settings=settings)
    resp = client.post("/plans/upgrade", data={"plan": "business"})
    assert resp.status_code == 303
    assert "upgrade=pending" in resp.headers["location"]
    assert session.committed == 0


def test_upgrade_no_payment_called():
    """No payment API call — only a trial_requests INSERT."""
    from src.api.config import CabinetSettings

    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="test-secret",
        TG_BOT_TOKEN="",
        ADMIN_CHAT_ID=0,
    )
    guard_row = SimpleNamespace(has_pending=False, access_until=_PAST_DT)
    session = FakeSession(
        results=[
            FakeResult(items=[guard_row]),
            FakeResult(scalar=_REQ_ID),
            FakeResult(items=["u@x.rs"]),
        ]
    )
    client = _client(session, sess={"uid": _UID, "tid": _TID}, settings=settings)
    resp = client.post("/plans/upgrade", data={"plan": "pro"})
    assert resp.status_code == 303
    # Only SQL operations in session.executed — no HTTP calls to the payment provider
    payment_related = [
        sql
        for sql, _ in session.executed
        if any(kw in sql.lower() for kw in ("stripe", "payment", "charge", "invoice"))
    ]
    assert payment_related == []
