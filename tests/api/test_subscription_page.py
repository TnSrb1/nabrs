"""Tests of the “Subscription and limits” section of the hub ``/cabinet/account`` (formerly /cabinet/subscription).

Since the owner’s decision of 10.10.2026 there is no separate subscription page: ``GET /cabinet/subscription`` is a 303 to ``/cabinet/account#pretplata``, and the hub renders the blocks by the partials ``_sub_*.html``. DB/session are mocked via dependency_overrides; the SUBSCRIPTION queries go through the real ``build_subscription_context`` in the order of the FakeSession queue (the same order: subscription → tier → decision → access_until), the hub-specific services (templates/channels/access application) are stubbed. We check: the redirects; the render with an active subscription (plan/status/date); without a subscription (Free/trial + a CTA to /plans); set_tenant FIRST (RLS); only one’s own (an explicit WHERE tenant_id in the SQL).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import pytest

from src.api import deps
from src.api.cabinet import routes_account
from src.api.cabinet.trial_service import AccessStatus
from src.api.main import app
from src.domain.tz import to_local
from tests.api.conftest import FakeResult, FakeSession

_TID = "11111111-1111-1111-1111-111111111111"
_UID = "22222222-2222-2222-2222-222222222222"
_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES = _ROOT / "src" / "api" / "templates"
_STATIC = _ROOT / "src" / "api" / "static"


def _client(session: FakeSession | None = None, *, sess: dict | None = None):
    def factory():
        return session or FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _hub_only_services(monkeypatch):
    """The hub-specific reads do not touch the FakeSession queue (they are not tested here)."""

    async def _none(*a, **k):
        return None

    async def _empty(*a, **k):
        return []

    async def _access(*a, **k):
        return AccessStatus(state="active", active=True, can_request=False, until=None)

    monkeypatch.setattr(routes_account.trial_service, "get_access_status", _access)
    monkeypatch.setattr(routes_account.svc, "list_profiles", _empty)
    monkeypatch.setattr(routes_account.channel_svc, "telegram_channel_state", _none)
    monkeypatch.setattr(routes_account.channel_svc, "email_channel_state", _none)


def test_subscription_anon_redirects_login_and_back_to_old_address():
    """Anonymous: to login with a return to the old address (the 303 will lead it to the hub)."""
    client = _client(sess=None)
    resp = client.get("/cabinet/subscription")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Fcabinet%2Fsubscription"


def test_old_subscription_address_redirects_303_to_hub_section():
    """303, not 301 (cached forever); Location — as a literal."""
    client = _client(sess={"uid": _UID, "tid": _TID})
    resp = client.get("/cabinet/subscription")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account#pretplata"


def test_subscription_active_renders_plan_and_date():
    sub = SimpleNamespace(
        provider="stripe",
        status="active",
        plan_key="pro",
        current_period_end=datetime(2026, 12, 1, tzinfo=timezone.utc),
    )
    tenant = SimpleNamespace(tier="pro", status=None)
    session = FakeSession(results=[FakeResult(items=[sub]), FakeResult(items=[tenant])])
    client = _client(session, sess={"uid": _UID, "tid": _TID})
    resp = client.get("/cabinet/account")
    assert resp.status_code == 200
    assert "Подписка и лимиты" in resp.text
    assert 'id="pretplata"' in resp.text
    assert "Pro" in resp.text
    assert "Активна" in resp.text
    assert "01.12.2026" in resp.text  # renewal date


def test_subscription_canceled_shows_update_cta():
    sub = SimpleNamespace(
        provider="stripe",
        status="canceled",
        plan_key="business",
        current_period_end=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    tenant = SimpleNamespace(tier="business", status=None)
    session = FakeSession(results=[FakeResult(items=[sub]), FakeResult(items=[tenant])])
    client = _client(session, sess={"uid": _UID, "tid": _TID})
    resp = client.get("/cabinet/account")
    assert resp.status_code == 200
    assert "Отменена" in resp.text
    assert "/plans" in resp.text  # CTA to renew the subscription


def test_subscription_none_shows_free_and_cta():
    tenant = SimpleNamespace(tier="lite", status=None)
    session = FakeSession(results=[FakeResult(items=[]), FakeResult(items=[tenant])])
    client = _client(session, sess={"uid": _UID, "tid": _TID})
    resp = client.get("/cabinet/account")
    assert resp.status_code == 200
    assert "Free" in resp.text
    assert "/plans" in resp.text  # CTA to choose a plan


def test_subscription_none_trial_shows_trial():
    tenant = SimpleNamespace(tier="lite", status="trial")
    session = FakeSession(results=[FakeResult(items=[]), FakeResult(items=[tenant])])
    client = _client(session, sess={"uid": _UID, "tid": _TID})
    resp = client.get("/cabinet/account")
    assert resp.status_code == 200
    assert "Пробный доступ" in resp.text
    assert "/plans" in resp.text


def test_subscription_free_shows_value_ladder_and_upsell():
    """VG-12: Free shows “Your plan already gives” + the upsell “Pro/Business will unlock”."""
    tenant = SimpleNamespace(tier="lite", status=None)
    session = FakeSession(results=[FakeResult(items=[]), FakeResult(items=[tenant])])
    client = _client(session, sess={"uid": _UID, "tid": _TID})
    resp = client.get("/cabinet/account")
    assert resp.status_code == 200
    assert "Ваш план уже даёт" in resp.text
    assert "В Pro откроется" in resp.text
    assert "В Business откроется" in resp.text
    # The Pro delta includes the features that are closed on Free.
    assert "Вердикт «участвовать или нет»" in resp.text
    assert "Уведомления в Telegram" in resp.text
    # The upsell CTA leads to the showcase.
    assert "/plans" in resp.text


def test_subscription_business_has_no_upsell_sections():
    """Business is the top of the showcase: there are no upgrade targets (Enterprise is excluded)."""
    sub = SimpleNamespace(
        provider="stripe", status="active", plan_key="business", current_period_end=None
    )
    tenant = SimpleNamespace(tier="business", status=None)
    session = FakeSession(results=[FakeResult(items=[sub]), FakeResult(items=[tenant])])
    client = _client(session, sess={"uid": _UID, "tid": _TID})
    resp = client.get("/cabinet/account")
    assert resp.status_code == 200
    assert "Ваш план уже даёт" in resp.text
    assert "откроется" not in resp.text  # there are no upsell sections


def test_subscription_set_tenant_first_and_own_only():
    """RLS: set_tenant FIRST; the subscription read — with an explicit WHERE tenant_id (one’s own)."""
    sub = SimpleNamespace(
        provider="stripe", status="active", plan_key="pro", current_period_end=None
    )
    tenant = SimpleNamespace(tier="pro", status=None)
    session = FakeSession(results=[FakeResult(items=[sub]), FakeResult(items=[tenant])])
    client = _client(session, sess={"uid": _UID, "tid": _TID})
    resp = client.get("/cabinet/account")
    assert resp.status_code == 200

    stmts = [sql for sql, _ in session.executed]
    # The first executed statement is the setting of app.current_tenant_id.
    assert "set_config" in stmts[0] or "app.current_tenant_id" in stmts[0]
    # The subscription read filters by tenant_id (defense-in-depth to RLS).
    sub_reads = [s for s in stmts if "billing_subscriptions" in s]
    assert sub_reads
    assert all("tenant_id = CAST(:tid AS uuid)" in s for s in sub_reads)
    # set_tenant was called BEFORE the subscription read.
    set_idx = next(i for i, s in enumerate(stmts) if "app.current_tenant_id" in s)
    sub_idx = next(i for i, s in enumerate(stmts) if "billing_subscriptions" in s)
    assert set_idx < sub_idx


# --------------------------------------------------------------------------- #
# Portal, Stage 6: the counter “how many days of access are left”
#
# The counter is a PRESENTATION on top of stats_service.get_days_left (tenants.access_until),
# the same source as the dashboard tile. The billing date (current_period_end) —
# a separate axis, and lives under its own captions “Renewal”/“Valid until”.
# --------------------------------------------------------------------------- #


def _in_days(days: float):
    """access_until in about ``days`` days (a minute ago — so that ceil gives exactly days)."""
    return datetime.now(timezone.utc) + timedelta(days=days) - timedelta(minutes=1)


def _session(*, sub=None, tenant=None, decision=None, access_until=None):
    """The queue of results of the route: subscription → tier → decision → access_until."""
    return FakeSession(
        results=[
            FakeResult(items=[sub] if sub is not None else []),
            FakeResult(items=[tenant or SimpleNamespace(tier="lite", status=None)]),
            FakeResult(items=[decision] if decision is not None else []),
            FakeResult(items=[SimpleNamespace(access_until=access_until)]),
        ]
    )


def _get(session):
    client = _client(session, sess={"uid": _UID, "tid": _TID})
    return client.get("/cabinet/account")


def _status_text(resp) -> str:
    """The text of the Status card of the hub (the #status section) without markup."""
    block = re.search(r'id="status".*?</section>', resp.text, re.S).group(0)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block))


def test_counter_shows_days_left_server_rendered():
    """The term is rendered by the server in Status (“N days of access left”), without JS."""
    resp = _get(_session(access_until=_in_days(30)))
    assert resp.status_code == 200
    assert "(30 дней доступа осталось)" in _status_text(resp)


def test_counter_plural_agrees_with_number():
    """3 → the short form “d.”, not the full word."""
    resp = _get(_session(access_until=_in_days(3)))
    assert resp.status_code == 200
    assert "(3 дня доступа осталось)" in _status_text(resp)


def test_counter_expired_says_expired_not_negative_days():
    """Expired access — “access expired”, and NOT “-2 days left”."""
    resp = _get(_session(access_until=_in_days(-2)))
    assert resp.status_code == 200
    text = _status_text(resp)
    assert "доступ истёк" in text
    assert "доступа осталось" not in text
    assert "text-red-700" in resp.text


def test_counter_unlimited_says_unlimited_not_days():
    """access_until IS NULL — “open-ended”, without a day counter."""
    resp = _get(_session(access_until=None))
    assert resp.status_code == 200
    assert "бессрочно" in _status_text(resp)
    assert "доступа осталось" not in resp.text


def test_counter_counts_access_until_not_billing_period_end():
    """‼️ The counter counts the access (access_until), and the renewal shows the billing date.

    Two dates — two axes. If the counter starts counting from current_period_end, the tile here will diverge from the dashboard tile, which counts from access_until.
    """
    sub = SimpleNamespace(
        provider="stripe",
        status="active",
        plan_key="pro",
        current_period_end=datetime(2026, 12, 1, tzinfo=timezone.utc),
    )
    until = _in_days(10)
    resp = _get(
        _session(
            sub=sub, tenant=SimpleNamespace(tier="pro", status=None), access_until=until
        )
    )
    assert resp.status_code == 200
    # The counter — from access_until.
    assert "(10 дней доступа осталось)" in _status_text(resp)
    # The date is shown in Belgrade time (the boundary of presentation), as in the template —
    # d_local via to_local; the raw UTC formatting here would give a divergence
    # at the day boundary.
    assert f"доступ до {to_local(until).strftime('%d.%m.%Y')}" in _status_text(resp)
    # The billing date — in place and under its own caption.
    assert "Продление" in resp.text
    assert "01.12.2026" in resp.text


def test_feature_ladder_matches_catalog_matrix():
    """The feature values did not move: the list = the enabled cells of the plan’s feature_matrix."""
    from src.plans.catalog import feature_matrix, get_plan

    resp = _get(_session(tenant=SimpleNamespace(tier="lite", status=None)))
    assert resp.status_code == 200
    labels = {
        "profiles": "Шаблоны поиска",
        "telegram": "Уведомления в Telegram",
        "ai_match": "AI-подбор и объяснение релевантности",
        "ai_deep": "Глубокий AI-разбор спецификации",
        "bid": "Вердикт «участвовать или нет»",
        "checklist": "Чек-лист документов для заявки",
        "draft": "Черновик заявки",
        "analytics": "Аналитика заказчика, конкурентов и перезакупок",
    }
    for cell in feature_matrix(get_plan("free")):
        text = labels[cell["key"]]
        if cell["on"]:
            assert text in resp.text, f"feature {cell['key']} disappeared from “your plan already gives”"


def test_hub_markup_has_no_inline_scripts():
    """CSP: the hub and its subscription partials — without inline JS (no <script> at all)."""
    for name in (
        "account.html",
        "_sub_details.html",
        "_sub_ai_limits.html",
        "_sub_plan.html",
    ):
        html = (_TEMPLATES / name).read_text(encoding="utf-8")
        assert not re.search(r"<script", html), f"<script> in {name}"
        assert not re.search(
            r"\bon[a-z]+\s*=\s*[\"']", html
        ), f"inline handler in {name}"


def test_old_subscription_page_artifacts_are_gone():
    """There is no separate page: the template and its animation (subscription.js) are removed."""
    assert not (_TEMPLATES / "subscription.html").exists()
    assert not (_STATIC / "js" / "subscription.js").exists()


# --------------------------------------------------------------------------- #
#  The block “AI limits” — the remainder BEFORE exhaustion (owner requirement of 15.08.2026)
# --------------------------------------------------------------------------- #


class _AiUsageSession(FakeSession):
    """The route queue + separate answers to the limit queries (tenants/ai_usage).

    The usage is the same across all levels — what matters to the test is that the number REACHES the markup, not the concrete distribution by levels.
    """

    def __init__(self, *, tier="pro", status="active", used=0, **kw):
        super().__init__(**kw)
        self.tier = tier
        self.status = status
        self.used = used

    async def execute(self, statement, params=None):
        sql = str(statement)
        # ‼️ The plan query OF THE ROUTE AND OF THE LIMIT COUNTER is the same one
        # (ai_limits_service calls subscription_service.get_tenant_tier) —
        # therefore we answer it here, not as an element of the queue: otherwise
        # the queue shifts and the next SELECT gets someone else’s row.
        if "FROM ai_usage" in sql:
            self.executed.append((sql, params))
            return FakeResult(items=[SimpleNamespace(count=self.used)])
        if "SELECT tier, status FROM tenants" in sql:
            self.executed.append((sql, params))
            return FakeResult(
                items=[SimpleNamespace(tier=self.tier, status=self.status)]
            )
        return await super().execute(statement, params)


def _ai_session(*, tier="pro", status="active", used=0, access_until=None):
    return _AiUsageSession(
        tier=tier,
        status=status,
        used=used,
        results=[
            FakeResult(items=[]),  # subscription
            FakeResult(items=[]),  # the decision on the application
            FakeResult(items=[SimpleNamespace(access_until=access_until)]),
        ],
    )


def test_ai_limits_block_shows_used_of_limit():
    """“Nothing is switched off silently”: the remainder is visible BEFORE exhaustion."""
    resp = _get(_ai_session(tier="pro", status="active", used=17))
    assert resp.status_code == 200
    assert "Лимиты AI" in resp.text
    assert "Автоматический разбор потока" in resp.text
    assert "17 из 400" in resp.text  # pro standard = 400 (_TIER_LIMITS)


def test_ai_limits_block_marks_exhausted_level():
    resp = _get(_ai_session(tier="pro", status="active", used=400))
    assert resp.status_code == 200
    assert "400 из 400" in resp.text
    assert "исчерпано" in resp.text


def test_ai_limits_block_shows_trial_demo_quota():
    """On the trial the auto-analysis line shows the DEMO, not the monthly Pro limit."""
    resp = _get(_ai_session(tier="pro", status="trial", used=3))
    assert resp.status_code == 200
    assert "3 из 10" in resp.text  # TRIAL_AUTO_DEMO_LIMIT
    assert "демо" in resp.text


def test_ai_limits_block_names_levels_absent_from_plan():
    """A level with limit 0 is not hidden: “not on your plan” + a link to the plans."""
    resp = _get(_ai_session(tier="lite", status="active", used=0))
    assert resp.status_code == 200
    assert "нет на вашем тарифе" in resp.text
    assert 'href="/plans"' in resp.text
