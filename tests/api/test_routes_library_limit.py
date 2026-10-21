"""The profile limit counter BEFORE the refusal on ``/library`` (owner 22.09.2026, step 3).

The reason — the client clicked “Apply package” at an exhausted profile limit and got only the error text `err_profile_limit` in the form, having already spent the click. The route now shows “used X of Y” BEFORE the click, and at an exhausted limit the CTA buttons lead to ``/cabinet/account`` (where there is deletion), not to ``/profiles/new`` (where the inevitable refusal of ``create_profile`` waits). The service layer is mocked (ADR-013), the template renders for real.
"""

from __future__ import annotations

from src.api.cabinet import routes_library
from tests.api.test_cabinet_i18n import _TID, _UID, _async, _client


def _no_packages_no_templates(monkeypatch):
    """Removes packages/templates from the showcase — the test looks only at the card “show all” and the counter, not caring about the catalog contents."""
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([]))
    monkeypatch.setattr(routes_library.vertical_packages, "PACKAGES", [])
    monkeypatch.setattr(
        routes_library.vertical_packages, "display_packages", lambda lang: []
    )


def test_counter_shown_before_any_click(monkeypatch):
    """The limit is visible BEFORE the click on the card — not only after the form’s refusal."""
    _no_packages_no_templates(monkeypatch)
    monkeypatch.setattr(
        routes_library.subscription_service,
        "get_tenant_tier",
        _async(("pro", "active")),
    )
    monkeypatch.setattr(routes_library.profile_service, "list_profiles", _async([1, 2]))
    resp = _client(sess={"uid": _UID, "tid": _TID}).get("/library")
    assert resp.status_code == 200
    assert "Занято 2 из 3" in resp.text


def test_limit_reached_redirects_cta_to_account(monkeypatch):
    """3 of 3 (Pro) — the card “show all” leads to /cabinet/account,
    not to /profiles/new (where the inevitable refusal waits)."""
    _no_packages_no_templates(monkeypatch)
    monkeypatch.setattr(
        routes_library.subscription_service,
        "get_tenant_tier",
        _async(("pro", "active")),
    )
    monkeypatch.setattr(
        routes_library.profile_service, "list_profiles", _async([1, 2, 3])
    )
    resp = _client(sess={"uid": _UID, "tid": _TID}).get("/library")
    assert resp.status_code == 200
    assert "Занято 3 из 3" in resp.text
    assert 'href="/cabinet/account"' in resp.text
    assert 'href="/profiles/new/confirm?preset=all"' not in resp.text


def test_below_limit_cta_still_creates_profile(monkeypatch):
    """The counter-fact: 2 of 3 — the card leads to /profiles/new as usual."""
    _no_packages_no_templates(monkeypatch)
    monkeypatch.setattr(
        routes_library.subscription_service,
        "get_tenant_tier",
        _async(("pro", "active")),
    )
    monkeypatch.setattr(routes_library.profile_service, "list_profiles", _async([1, 2]))
    resp = _client(sess={"uid": _UID, "tid": _TID}).get("/library")
    assert resp.status_code == 200
    assert 'href="/profiles/new/confirm?preset=all"' in resp.text


def test_enterprise_no_counter_shown(monkeypatch):
    """Enterprise = unlimited — the counter is not drawn at all (None, not to lie with a number)."""
    _no_packages_no_templates(monkeypatch)
    monkeypatch.setattr(
        routes_library.subscription_service,
        "get_tenant_tier",
        _async(("enterprise", "active")),
    )
    monkeypatch.setattr(
        routes_library.profile_service, "list_profiles", _async([1, 2, 3, 4, 5])
    )
    resp = _client(sess={"uid": _UID, "tid": _TID}).get("/library")
    assert resp.status_code == 200
    assert "Занято" not in resp.text


def test_trial_limit_counts_as_pro_on_library(monkeypatch):
    """The trial (tier='lite', status='trial') = Pro rights → “of 3”, as on the dashboard and the account, not “of 1” by the raw plan.

    Mutation: return ``tiers.profile_limit(tier)`` in ``library()`` → “Used 1 of 1”.
    """
    _no_packages_no_templates(monkeypatch)
    monkeypatch.setattr(
        routes_library.subscription_service,
        "get_tenant_tier",
        _async(("lite", "trial")),
    )
    monkeypatch.setattr(routes_library.profile_service, "list_profiles", _async([1]))
    resp = _client(sess={"uid": _UID, "tid": _TID}).get("/library")
    assert resp.status_code == 200
    assert "Занято 1 из 3" in resp.text
    assert (
        'href="/profiles/new/confirm?preset=all"' in resp.text
    )  # the limit is not exhausted


def test_trial_limit_counts_as_pro_on_library_detail(monkeypatch):
    """The same logic on the contents page ``/library/package/<key>``.

    Mutation: return ``tiers.profile_limit(tier)`` in ``library_detail()`` → “of 1”.
    """
    from src.api.cabinet import preview_service

    async def _stub(db, payload, examples_limit=0):
        return preview_service.PreviewResult(count=0, unfound=[], examples=[])

    monkeypatch.setattr(routes_library.preview_service, "preview_count", _stub)
    monkeypatch.setattr(
        routes_library.subscription_service,
        "get_tenant_tier",
        _async(("lite", "trial")),
    )
    monkeypatch.setattr(routes_library.profile_service, "list_profiles", _async([1]))
    resp = _client(sess={"uid": _UID, "tid": _TID}).get("/library/package/it")
    assert resp.status_code == 200
    assert "Занято 1 из 3" in resp.text
