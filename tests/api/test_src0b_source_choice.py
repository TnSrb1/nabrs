"""SRC-0b (R6): the subscriber chooses the sources themselves in the profile form of the cabinet.

The route + the profile service together, on ``FakeSession`` (without a live DB): the form is POSTed as from the browser, and what went into the ORM object of the profile is checked — ``enabled_source_keys`` and ``source_opt_in`` (the subscription moment, R5).

The clock is frozen by substituting ``datetime`` in :mod:`profile_service` — the subscription moment is compared by a literal, not “approximately now”.

The registry of sources — ``FakeSession(enabled_sources=...)``: two enabled sources, as it will be after the TED is enabled (production fact 30.09 — one jnportal).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import profile_service, routes_profiles
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession, make_tenant

SESS = {"uid": "00000000-0000-0000-0000-0000000000aa", "tid": "t-1"}
FROZEN = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
OLD_OPT_IN = "2026-09-01T10:00:00+00:00"

JN = SimpleNamespace(
    source_key="jnportal",
    display_name="Portal javnih nabavki (UJN)",
    country_codes=["RS"],
    available_for_tiers=None,
)
TED = SimpleNamespace(
    source_key="ted_balkan",
    display_name="TED (EU)",
    country_codes=["RS", "ME"],
    available_for_tiers=None,
)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D102 — freezing of the service clock
        return FROZEN


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    monkeypatch.setattr(profile_service, "datetime", _FrozenDatetime)

    async def _no_gate(session_factory, tenant_id):
        return {}

    # The gate of the plan/AI limit of the form is not the subject of these tests (its own SELECTs
    # would shift the queue of FakeSession).
    monkeypatch.setattr(routes_profiles, "_gate_ctx", _no_gate)
    yield
    app.dependency_overrides.clear()


def _client(session: FakeSession) -> TestClient:
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: session)
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    return TestClient(app, follow_redirects=False)


def _existing(**kw):
    base = dict(
        id="p1",
        tenant_id="t-1",
        version=1,
        name="P",
        ai_analysis_level="none",
        quiet_hours_start=None,
        quiet_hours_end=None,
        enabled_source_keys=["jnportal"],
        source_opt_in={"jnportal": OLD_OPT_IN},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _form(**extra):
    # dict (not a list of pairs): httpx encodes a list of pairs as a raw body.
    form = {"name": "P", "match_all": "on", "delivery_channels": "email"}
    form.update(extra)
    return form


# --------------------------------------------------------------------------- #
# (i) a key that is not among the enabled sources
# --------------------------------------------------------------------------- #
def test_create_with_only_unknown_source_is_rejected_visibly():
    """A forged key is discarded; if no platforms remain after that —
    a 400 refusal with a VISIBLE error at the field, and the profile is NOT created (previously a dead profile with an empty list was saved — ``forms.py`` 0a).
    """
    session = FakeSession(
        results=[FakeResult(scalar=0)],
        get_map={"Tenant": make_tenant(status="trial")},
        enabled_sources=[JN, TED],
        user_locale="sr",
    )
    resp = _client(session).post("/profiles", data=_form(enabled_source_keys="hacked"))
    assert resp.status_code == 400
    assert not [o for o in session.added if type(o).__name__ == "Profile"]
    # The error is at the platforms field (the block’s id), not only in the general list at the top.
    field = resp.text.split('id="pf-sources"', 1)[1].split("</fieldset>", 1)[0]
    assert "Izaberite bar jedan izvor" in field


def test_create_drops_unknown_key_keeps_enabled_one():
    session = FakeSession(
        results=[FakeResult(scalar=0)],
        get_map={"Tenant": make_tenant(status="trial")},
        enabled_sources=[JN, TED],
        user_locale="sr",
    )
    resp = _client(session).post(
        "/profiles", data=_form(enabled_source_keys=["hacked", "ted_balkan"])
    )
    assert resp.status_code == 303
    profile = next(o for o in session.added if type(o).__name__ == "Profile")
    assert profile.enabled_source_keys == ["ted_balkan"]
    assert profile.source_opt_in == {"ted_balkan": "2026-10-01T09:30:00+00:00"}


def test_create_with_no_source_at_all_is_rejected():
    """All checkboxes are removed (the field did not come at all) — the same visible refusal."""
    session = FakeSession(
        results=[FakeResult(scalar=0)],
        get_map={"Tenant": make_tenant(status="trial")},
        enabled_sources=[JN, TED],
        user_locale="sr",
    )
    resp = _client(session).post("/profiles", data=_form())
    assert resp.status_code == 400
    assert "Izaberite bar jedan izvor" in resp.text
    assert not [o for o in session.added if type(o).__name__ == "Profile"]


# --------------------------------------------------------------------------- #
# (ii) we enable a second source — the subscription moment only for the new one
# --------------------------------------------------------------------------- #
def test_update_adding_second_source_stamps_only_new_one():
    existing = _existing()
    session = FakeSession(
        results=[FakeResult(items=[existing])],
        get_map={"Tenant": make_tenant(status="trial")},
        enabled_sources=[JN, TED],
        user_locale="sr",
    )
    resp = _client(session).post(
        "/profiles/p1", data=_form(enabled_source_keys=["jnportal", "ted_balkan"])
    )
    assert resp.status_code == 303
    assert existing.enabled_source_keys == ["jnportal", "ted_balkan"]
    assert existing.source_opt_in == {
        "jnportal": "2026-09-01T10:00:00+00:00",
        "ted_balkan": "2026-10-01T09:30:00+00:00",
    }


# --------------------------------------------------------------------------- #
# (iii) removing a key deletes it from source_opt_in
# --------------------------------------------------------------------------- #
def test_update_removing_source_drops_its_opt_in():
    existing = _existing(
        enabled_source_keys=["jnportal", "ted_balkan"],
        source_opt_in={"jnportal": OLD_OPT_IN, "ted_balkan": OLD_OPT_IN},
    )
    session = FakeSession(
        results=[FakeResult(items=[existing])],
        get_map={"Tenant": make_tenant(status="trial")},
        enabled_sources=[JN, TED],
        user_locale="sr",
    )
    resp = _client(session).post(
        "/profiles/p1", data=_form(enabled_source_keys="ted_balkan")
    )
    assert resp.status_code == 303
    assert existing.enabled_source_keys == ["ted_balkan"]
    assert existing.source_opt_in == {"ted_balkan": "2026-09-01T10:00:00+00:00"}


def test_update_removing_last_source_is_rejected_and_keeps_profile():
    existing = _existing()
    session = FakeSession(
        results=[FakeResult(items=[existing])],
        get_map={"Tenant": make_tenant(status="trial")},
        enabled_sources=[JN, TED],
        user_locale="sr",
    )
    resp = _client(session).post("/profiles/p1", data=_form())
    assert resp.status_code == 400
    assert "Izaberite bar jedan izvor" in resp.text
    assert existing.enabled_source_keys == ["jnportal"]
    assert existing.source_opt_in == {"jnportal": OLD_OPT_IN}
    assert session.committed == 0


# --------------------------------------------------------------------------- #
# The render: platform captions, countries, the mark of a disabled one (owner 30.09 evening, item 3)
# --------------------------------------------------------------------------- #
PAUSED = SimpleNamespace(
    source_key="old_portal",
    display_name="Stari portal",
    country_codes=["ME"],
    available_for_tiers=None,
    enabled=False,
)


def _async(value):
    async def _stub(*args, **kwargs):
        return value

    return _stub


@pytest.mark.parametrize(
    "locale,expected",
    [
        (
            "sr",
            [
                "Izvori (portali)",
                "Portal javnih nabavki (UJN)",
                "· Srbija, Crna Gora",
                "Stari portal",
                "· privremeno isključen",
                "Dobijaćete samo tendere objavljene od trenutka uključivanja",
            ],
        ),
        (
            "ru",
            [
                "Источники (порталы)",
                "· Сербия, Черногория",
                "· временно отключён",
                "Вы будете получать только тендеры, опубликованные с момента включения",
            ],
        ),
        (
            "en",
            [
                "Sources (portals)",
                "· Serbia, Montenegro",
                "· temporarily disabled",
            ],
        ),
    ],
)
def test_edit_form_renders_sources_block(monkeypatch, locale, expected):
    profile = SimpleNamespace(
        id="p1",
        name="P",
        is_active=True,
        ai_analysis_level="none",
        regions=[],
        delivery_channels=["email"],
        delivery_schedule="immediate",
        enabled_source_keys=["jnportal", "old_portal"],
        classifications={},
        min_amount_eur=None,
        max_amount_eur=None,
        min_days_until_deadline=None,
        include_no_amount=False,
        keywords_include=None,
        keywords_exclude=None,
        keyword_dict=False,
        match_all=True,
    )
    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(profile))
    monkeypatch.setattr(routes_profiles.svc, "get_tenant_tier", _async("pro"))
    session = FakeSession(enabled_sources=[JN, TED, PAUSED], user_locale=locale)
    resp = _client(session).get("/profiles/p1/edit")
    assert resp.status_code == 200
    block = resp.text.split('id="pf-sources"', 1)[1].split("</fieldset>", 1)[0]
    for literal in expected:
        assert literal in block, literal
    # The selected are marked, the enabled but unselected one — not.
    assert 'value="jnportal" checked' in block
    assert 'value="old_portal" checked' in block
    assert 'value="ted_balkan" >' in block


# --------------------------------------------------------------------------- #
# Step 5: the preview on an empty choice — 0 + a hint, without a request to the tenders
# --------------------------------------------------------------------------- #
def test_preview_without_sources_is_zero_with_hint():
    session = FakeSession(user_locale="sr")
    resp = _client(session).get(
        "/profiles/preview-count",
        params={"match_all": "on"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Izaberite bar jedan izvor" in resp.text
    # Before SRC-0b an empty list meant “all sources”: an SQL on the tenders went out.
    assert not [q for q, _ in session.executed if "FROM tenders" in q]


def test_preview_with_source_still_queries():
    session = FakeSession(user_locale="sr")
    resp = _client(session).get(
        "/profiles/preview-count",
        params={"match_all": "on", "enabled_source_keys": "jnportal"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Izaberite bar jedan izvor" not in resp.text
    assert [q for q, _ in session.executed if "FROM tenders" in q]
