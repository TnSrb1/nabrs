"""The profile form, block ``#pf-sources``: under the source — what it gives, and the link to its showcase anchor ``/izvori`` (owner decision 02.10.2026).

Expectations — literals. The link must stand OUTSIDE the ``<label>``: interactive content inside interactive is invalid, the checkbox’s click target is the whole label row.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_profiles
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "00000000-0000-0000-0000-0000000000aa", "tid": "t-1"}

JN = SimpleNamespace(
    source_key="jnportal",
    display_name="Portal javnih nabavki (UJN)",
    country_codes=["RS"],
    available_for_tiers=None,
    enabled=True,
)
TED = SimpleNamespace(
    source_key="ted_balkan",
    display_name="TED — Zapadni Balkan",
    country_codes=["BA", "ME", "MK"],
    available_for_tiers=None,
    enabled=True,
)
PAUSED = SimpleNamespace(
    source_key="bih_enabavke",
    display_name="e-Nabavke BiH",
    country_codes=["BA"],
    available_for_tiers=None,
    enabled=False,
)


def _async(value):
    async def _stub(*args, **kwargs):
        return value

    return _stub


@pytest.fixture(autouse=True)
def _no_gate(monkeypatch):
    # The gate of the plan/AI limit of the form is not the subject of the test (its own SELECTs).
    monkeypatch.setattr(routes_profiles, "_gate_ctx", _async({}))


def teardown_function():
    app.dependency_overrides.clear()


def _block(monkeypatch, locale: str) -> str:
    profile = SimpleNamespace(
        id="p1",
        name="P",
        is_active=True,
        ai_analysis_level="none",
        regions=[],
        delivery_channels=["email"],
        delivery_schedule="immediate",
        enabled_source_keys=["jnportal", "bih_enabavke"],
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
    session = FakeSession(enabled_sources=[JN, TED, PAUSED], user_locale=locale)
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: session)
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    resp = TestClient(app, follow_redirects=False).get("/profiles/p1/edit")
    assert resp.status_code == 200
    return resp.text.split('id="pf-sources"', 1)[1].split("</fieldset>", 1)[0]


def test_block_links_each_enabled_source_to_its_anchor(monkeypatch):
    block = _block(monkeypatch, "sr")
    assert 'href="/izvori#jnportal"' in block
    assert 'href="/izvori#ted_balkan"' in block
    # A disabled source is not on the showcase — and there is no link to it either.
    assert "/izvori#bih_enabavke" not in block


def test_short_capability_lines_are_literal_facts(monkeypatch):
    sr = _block(monkeypatch, "sr")
    assert (
        "rok · vrednost ako je navedena · dokumentacija za AI · CPV · izabrani ponuđač"
        in sr
    )
    assert "rok · vrednost ako je navedena · opis · CPV" in sr
    ru = _block(monkeypatch, "ru")
    assert "срок · сумма, если указана · документация для AI · CPV · победитель" in ru
    assert "Подробнее об источнике" in ru
    en = _block(monkeypatch, "en")
    assert "deadline · value if stated · documents for AI · CPV · selected bidder" in en


def test_more_link_is_outside_label(monkeypatch):
    block = _block(monkeypatch, "sr")
    labels = re.findall(r"<label\b.*?</label>", block, flags=re.S)
    assert len(labels) == 3
    assert all("<a " not in lbl for lbl in labels)
    # The label row is still whole — the checkbox’s click target.
    assert all('class="field-check w-full items-start"' in lbl for lbl in labels)
