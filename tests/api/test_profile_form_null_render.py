"""Guard: nullable fields of the profile form render EMPTY, not “None”.

The root of the subscribers' defect “Field ”Min. amount“: a number is expected” — the ``val`` macro in ``profile_form.html`` rendered ``None`` as the string “None” (``value=“None”``), the submit sent “None” → ``forms._to_decimal(“None”)`` fell. The backend does NOT count the field as required — only the render was at fault.

Mutation check: return the old ``val`` macro (``{{ pf.get(key, default) }}``) → ``test_edit_form_renders_null_amount_as_empty`` goes red (``value=“None”``).
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_profiles
from src.api.forms import parse_profile_form
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


def _profile_null_amounts(**kw):
    """A profile-from-template: amounts/days = None (the key exists, the value is None)."""
    base = dict(
        id="p-1",
        name="Из шаблона",
        is_active=True,
        ai_analysis_level="standard",
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
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _async(value):
    async def _stub(*args, **kwargs):
        return value

    return _stub


def _client():
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: FakeSession())
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def test_edit_form_renders_null_amount_as_empty(monkeypatch):
    """min/max = None → the inputs pf-min/pf-max have value="" (NOT value="None")."""
    monkeypatch.setattr(
        routes_profiles.svc, "get_profile", _async(_profile_null_amounts())
    )
    resp = _client().get("/profiles/p-1/edit")
    assert resp.status_code == 200
    # exactly an empty value — not “None”
    assert 'id="pf-min" name="min_amount_eur" value=""' in resp.text
    assert 'id="pf-max" name="max_amount_eur" value=""' in resp.text
    assert 'id="pf-days" name="min_days_until_deadline" value=""' in resp.text
    assert 'value="None"' not in resp.text


def test_edit_form_keeps_real_amount_value(monkeypatch):
    """A non-null value is still pre-filled (the macro did not eat the real data)."""
    monkeypatch.setattr(
        routes_profiles.svc,
        "get_profile",
        _async(_profile_null_amounts(min_amount_eur=1000)),
    )
    resp = _client().get("/profiles/p-1/edit")
    assert resp.status_code == 200
    assert 'id="pf-min" name="min_amount_eur" value="1000"' in resp.text


def test_roundtrip_template_profile_edit_submit_no_number_error(monkeypatch):
    """Round-trip: a profile from a template (amounts None) → render of edit → submit without changes.

    Before, the render put “None” into the inputs, the browser sent “None” back, and the parser fell with “a number is expected”. We check both ends: (1) the render gives value="", (2) the parser of empty amounts does not complain and returns NULL.
    """
    monkeypatch.setattr(
        routes_profiles.svc, "get_profile", _async(_profile_null_amounts())
    )
    resp = _client().get("/profiles/p-1/edit")
    assert 'name="min_amount_eur" value=""' in resp.text

    # The browser submits exactly what was rendered: empty strings, not "None".
    payload, errors = parse_profile_form(
        {"name": "Из шаблона", "min_amount_eur": "", "max_amount_eur": ""}
    )
    assert not any("число" in e for e in errors)
    assert payload["min_amount_eur"] is None
    assert payload["max_amount_eur"] is None
