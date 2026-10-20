"""Profile filters “Subject type” and “Customer: follow / exclude” (0046).

The pure parts of the contour: parsing of the form, the plan gate, the snapshot of the revision, stage B, the topic mirror in the preview, the customer hint and the render of the form. The SQL predicates of stage A and the preview are proven on a live PG — ``tests/db/test_profile_filters_routing.py``.

Customer names — literals from production (01.10.2026, the top of jnportal for 90 days).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from src.api import deps, tiers
from src.api.main import app
from src.api.cabinet import customer_suggest, preview_service
from src.api.cabinet.profile_service import _snapshot
from src.api.forms import MAX_CUSTOMERS, parse_profile_form
from src.workers.matching import Candidate, TenderView, stage_b_filter
from tests.api.conftest import FakeResult, FakeSession

EPS = "Акционарско друштво Електропривреда Србије, Београд"
VOJSKA = "Министарство одбране Републике Србије"
SESS = {"uid": "u-1", "tid": "t-1"}


def teardown_function():
    app.dependency_overrides.clear()
    customer_suggest._cache.clear()


# --------------------------------------------------------------------------- #
#  Form parsing
# --------------------------------------------------------------------------- #
def test_contract_types_whitelisted_in_canonical_order():
    form = FormData(
        [
            ("contract_types", "works"),
            ("contract_types", "bogus"),
            ("contract_types", "goods"),
        ]
    )
    payload, _ = parse_profile_form(form)
    assert payload["contract_types"] == ["goods", "works"]


def test_customer_name_with_comma_is_one_customer():
    """The comma is part of the name (“…Srbije, Beograd”), not a list separator."""
    form = FormData([("customers_include", ""), ("customers_include", EPS)])
    payload, _ = parse_profile_form(form)
    assert payload["customers_include"] == [EPS]


def test_customers_dedup_by_case_and_spaces_keeps_first_spelling():
    form = FormData(
        [
            ("customers_exclude", VOJSKA),
            ("customers_exclude", "  министарство   одбране Републике Србије "),
        ]
    )
    payload, _ = parse_profile_form(form)
    assert payload["customers_exclude"] == [VOJSKA]


def test_string_fallback_splits_by_lines_not_commas():
    payload, _ = parse_profile_form({"customers_include": f"{EPS}\n{VOJSKA}\n"})
    assert payload["customers_include"] == [EPS, VOJSKA]


def test_customer_in_both_lists_stays_only_in_exclude():
    form = FormData(
        [
            ("customers_include", EPS),
            ("customers_include", VOJSKA),
            ("customers_exclude", EPS.upper()),
        ]
    )
    payload, _ = parse_profile_form(form)
    assert payload["customers_include"] == [VOJSKA]
    assert payload["customers_exclude"] == [EPS.upper()]


def test_customers_list_is_capped():
    form = FormData([("customers_exclude", f"Naručilac {i}") for i in range(80)])
    payload, _ = parse_profile_form(form)
    assert len(payload["customers_exclude"]) == MAX_CUSTOMERS == 50


def test_empty_form_gives_empty_lists():
    payload, _ = parse_profile_form({})
    assert payload["contract_types"] == []
    assert payload["customers_include"] == []
    assert payload["customers_exclude"] == []


# --------------------------------------------------------------------------- #
#  Plan and revision
# --------------------------------------------------------------------------- #
_FILLED = {
    "contract_types": ["services"],
    "customers_include": [EPS],
    "customers_exclude": [VOJSKA],
}


def test_lite_suppresses_new_filters_like_procedure_types():
    clean, suppressed = tiers.gate_profile("lite", dict(_FILLED))
    assert clean["contract_types"] == []
    assert clean["customers_include"] == []
    assert clean["customers_exclude"] == []
    assert {
        "gate_sup_contract_types",
        "gate_sup_customers_include",
        "gate_sup_customers_exclude",
    } <= set(suppressed)


def test_starter_keeps_new_filters():
    clean, suppressed = tiers.gate_profile("starter", dict(_FILLED))
    assert clean["contract_types"] == ["services"]
    assert clean["customers_include"] == [EPS]
    assert clean["customers_exclude"] == [VOJSKA]
    assert not [s for s in suppressed if "customers" in s or "contract" in s]


def test_revision_snapshot_carries_new_fields():
    snap = _snapshot(dict(_FILLED))
    assert snap["contract_types"] == ["services"]
    assert snap["customers_include"] == [EPS]
    assert snap["customers_exclude"] == [VOJSKA]


# --------------------------------------------------------------------------- #
#  Stage B and the topic mirror in the preview
# --------------------------------------------------------------------------- #
def _cand(*, followed: bool, stop=None) -> Candidate:
    return Candidate(
        profile_id="p",
        tenant_id="t",
        keywords_include=[{"all_of": ["video"]}],
        keywords_exclude=stop,
        ai_analysis_level="none",
        delivery_channels=["email"],
        customer_followed=followed,
    )


_TENDER = TenderView(
    id="t-1",
    country_code="RS",
    source_key="jnportal",
    title_normalized="nabavka uglja",
    customer_name=EPS,
)


def test_stage_b_followed_customer_skips_include_words():
    assert stage_b_filter([_cand(followed=True)], _TENDER)
    assert not stage_b_filter([_cand(followed=False)], _TENDER)


def test_stage_b_followed_customer_still_obeys_stop_words():
    assert not stage_b_filter([_cand(followed=True, stop=["uglj"])], _TENDER)


def test_follow_counts_as_topic_for_preview_gate():
    base = {"enabled_source_keys": ["jnportal"]}
    assert not preview_service.matches_anything(base)
    assert preview_service.matches_anything({**base, "customers_include": [EPS]})


def test_preview_follow_only_profile_counts_only_followed_rows():
    passes = preview_service._keyword_filter(
        {"enabled_source_keys": ["jnportal"], "customers_include": [EPS]}
    )
    row = SimpleNamespace(title_normalized="nabavka", description_normalized="")
    assert passes(SimpleNamespace(**vars(row), customer_followed=True))
    assert not passes(SimpleNamespace(**vars(row), customer_followed=False))


def test_payload_from_profile_carries_new_fields():
    profile = SimpleNamespace(
        enabled_source_keys=["jnportal"],
        regions=None,
        classifications=None,
        min_amount_eur=None,
        max_amount_eur=None,
        min_days_until_deadline=None,
        include_no_amount=None,
        keywords_include=None,
        keywords_exclude=None,
        keyword_dict=False,
        match_all=False,
        **_FILLED,
    )
    payload = preview_service.payload_from_profile(profile)
    params = preview_service._build_params(payload)
    assert params["contract_types"] == ["services"]
    assert params["customers_include"] == [EPS]
    assert params["customers_exclude"] == [VOJSKA]


# --------------------------------------------------------------------------- #
#  Customer hint
# --------------------------------------------------------------------------- #
class _NamesDb:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[dict] = []

    async def execute(self, stmt, params=None):
        self.calls.append(params)
        return FakeResult(items=self.rows)


_ROWS = [
    SimpleNamespace(name=EPS, n=545),
    SimpleNamespace(name=VOJSKA, n=283),
    SimpleNamespace(name="ЕМС АД Београд", n=84),
]


@pytest.mark.asyncio
async def test_suggest_finds_cyrillic_name_by_latin_query():
    db = _NamesDb(_ROWS)
    hits = await customer_suggest.suggest(db, "RS", "elektroprivreda")
    assert [h.name for h in hits] == [EPS]
    assert hits[0].tenders == 545
    assert db.calls == [{"cc": "RS"}]


@pytest.mark.asyncio
async def test_suggest_short_query_returns_nothing_without_db():
    db = _NamesDb(_ROWS)
    assert await customer_suggest.suggest(db, "RS", " s ") == []
    assert db.calls == []


@pytest.mark.asyncio
async def test_suggest_caches_names_per_country():
    db = _NamesDb(_ROWS)
    await customer_suggest.suggest(db, "RS", "srbij")
    hits = await customer_suggest.suggest(db, "RS", "beograd")
    assert [h.name for h in hits] == [EPS, "ЕМС АД Београд"]
    assert len(db.calls) == 1


def test_suggest_route_requires_session():
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    resp = TestClient(app).get("/profiles/customer-suggest?q=eps")
    assert resp.status_code == 401


def test_suggest_route_returns_items_for_new_profile_country(monkeypatch):
    seen: dict = {}

    async def _suggest(db, country, q):
        seen.update(country=country, q=q)
        return [customer_suggest.CustomerHit(name=EPS, tenders=545)]

    monkeypatch.setattr(customer_suggest, "suggest", _suggest)
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: FakeSession())
    resp = TestClient(app).get(
        "/profiles/customer-suggest?q=elektro&profile_id=not-a-uuid"
    )
    assert resp.status_code == 200
    # ru/cabinet default: the caption = the name; sr — Latin (test below).
    assert resp.json() == {"items": [{"name": EPS, "label": EPS, "tenders": 545}]}
    assert seen == {"country": "RS", "q": "elektro"}


def test_suggest_route_sr_label_is_latin_name_stays_exact(monkeypatch):
    async def _suggest(db, country, q):
        return [customer_suggest.CustomerHit(name=EPS, tenders=545)]

    monkeypatch.setattr(customer_suggest, "suggest", _suggest)
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: FakeSession())
    item = (TestClient(app).get("/profiles/customer-suggest?q=elektro&lang=sr").json())[
        "items"
    ][0]
    assert item["name"] == EPS  # the filter value — letter by letter as in the data
    assert item["label"] == "Akcionarsko društvo Elektroprivreda Srbije, Beograd"


# --------------------------------------------------------------------------- #
#  Form render
# --------------------------------------------------------------------------- #
def _edit_html(monkeypatch, **fields) -> str:
    from src.api.cabinet import routes_profiles

    async def _get_profile(*a, **kw):
        return SimpleNamespace(
            id="p-1",
            name="Stroj",
            is_active=True,
            ai_analysis_level="none",
            regions=[],
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
            debug_match=False,
            match_all=False,
            **fields,
        )

    async def _downgrade(*a, **kw):
        return []

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _get_profile)
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _downgrade)
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    app.dependency_overrides[deps.get_session_factory] = lambda: (
        lambda: FakeSession(user_locale="sr")
    )
    return TestClient(app, follow_redirects=False).get("/profiles/p-1/edit").text


def test_edit_form_renders_saved_filters_as_checked(monkeypatch):
    html = _edit_html(
        monkeypatch,
        contract_types=["services"],
        customers_include=['ЈКП "Пут" Нови Сад'],
        customers_exclude=[VOJSKA],
    )
    assert 'name="contract_types" value="services" checked' in html
    assert 'name="contract_types" value="goods" >' in html
    # The quotes in the name are escaped — the value attribute does not break.
    # value — the machine name of the customer (compared letter by letter): NOT transliterated;
    # the caption next to it — in Latin (owner 10.10.2026).
    assert 'name="customers_include" value="ЈКП &#34;Пут&#34; Нови Сад" checked' in html
    assert "<span>JKP &#34;Put&#34; Novi Sad</span>" in html
    assert f'name="customers_exclude" value="{VOJSKA}" checked' in html
    # Captions — literals of the Serbian catalog (the cabinet language by default).
    assert "Tip predmeta" in html
    assert "Pratim naručioce" in html
    assert "Isključi naručioce" in html
    assert 'src="/static/js/customer_picker.js?v=' in html
    assert 'data-suggest-url="/profiles/customer-suggest?profile_id=p-1"' in html


def test_edit_form_without_filters_has_no_chips(monkeypatch):
    html = _edit_html(monkeypatch)
    assert 'value="services" checked' not in html
    assert 'name="customers_include" value="" >' not in html
    assert html.count('name="customers_include"') == 1  # only the empty hidden
