"""Package 1 of the interface audit (UI_AUDIT_2026-08-02_stage1_public.md):
HIGH-3, M-4, M-5, M-9 — the accessibility of the public entry. The checks are by the RENDER, not by the template: we call the real routes and read the HTML response.

M-8 (the iOS Safari zoom on field focus) is not checked in CSS by render — the headless
Chromium on the VPS does not start (there is no libatk), the fix is checked against the fact of the
app.css diff in the REPORT.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import i18n as cabinet_i18n
from src.api.emailer import MockEmailSender
from src.api.main import app
from tests.api.conftest import (
    FakeSession,
    disposable_email,
    good_email,
    make_session_factory,
    ok_turnstile,
)


def teardown_function():
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# HIGH-3: the only field of the three forms gets a programmatic caption
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/signup", "/login", "/auth/resend"])
def test_email_field_has_programmatic_label(path):
    """label[for] and input[id] coincide on all three login forms."""
    client = TestClient(app, follow_redirects=False)
    resp = client.get(path)
    assert resp.status_code == 200
    html = resp.text
    m = re.search(r'<label for="([^"]+)"[^>]*>\s*Email\s*</label>', html)
    assert m, f"label[for=...]Email not found in {path}"
    field_id = m.group(1)
    assert re.search(
        rf'<input[^>]*\bid="{re.escape(field_id)}"[^>]*>', html
    ), f"input id={field_id!r} not found in {path}"


# --------------------------------------------------------------------------- #
# M-4: the consent error in signup.html is linked to the checkbox
# --------------------------------------------------------------------------- #
def test_signup_missing_consent_links_checkbox_to_error_via_aria():
    client = TestClient(app, follow_redirects=False)
    resp = client.post("/signup", data={"email": "user@company.rs", "consent": ""})
    assert resp.status_code == 400
    html = resp.text
    assert 'id="consent-error"' in html
    assert 'aria-invalid="true"' in html
    m = re.search(r'aria-describedby="([^"]+)"', html)
    assert m, "aria-describedby is absent on the consent checkbox"
    assert f'id="{m.group(1)}"' in html


def test_signup_clean_form_has_no_aria_invalid_on_checkbox():
    """Without an error (a clean GET form) — the attributes aria-invalid/aria-describedby
    are absent on the checkbox altogether (by the condition `{% if consent_error %}`).
    """
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/signup")
    assert resp.status_code == 200
    assert "aria-invalid" not in resp.text
    assert "aria-describedby" not in resp.text


# --------------------------------------------------------------------------- #
# M-5: the registration result page — a heading + a conditional back link
# --------------------------------------------------------------------------- #
def _signup_post(*, turnstile, email_validator):
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        FakeSession()
    )
    app.dependency_overrides[deps.get_turnstile] = lambda: turnstile
    app.dependency_overrides[deps.get_email_validator] = lambda: email_validator
    app.dependency_overrides[deps.get_email_sender] = lambda: MockEmailSender()
    client = TestClient(app, follow_redirects=False)
    # We fix the language with a cookie: since package 2 of the audit the default of the anonymous login screens is
    # sr (the showcase contour), and this test is about the MARKUP of the result, not about the language.
    client.cookies.set("lang", "ru")
    return client.post(
        "/signup",
        data={
            "email": "user@company.rs",
            "cf-turnstile-response": "tok",
            "consent": "yes",
        },
    )


def test_signup_result_success_shows_h1_and_login_link():
    resp = _signup_post(turnstile=ok_turnstile(), email_validator=good_email())
    assert resp.status_code == 200
    html = resp.text
    ru = cabinet_i18n.CABINET["ru"]
    assert "<h1" in html
    assert ru["sgr_h1_success"] in html
    assert ru["sgr_h1_rejected"] not in html
    assert ru["sgr_back_login"] in html
    assert ru["sgr_back_signup"] not in html
    assert 'href="/login"' in html


def test_signup_result_rejected_shows_h1_and_signup_link():
    # Refusal by e-mail (not Turnstile): the failure of Turnstile now returns the form
    # (tests/api/test_signup_turnstile_ux.py), the result page — for the others.
    resp = _signup_post(turnstile=ok_turnstile(), email_validator=disposable_email())
    assert resp.status_code == 200
    html = resp.text
    ru = cabinet_i18n.CABINET["ru"]
    assert "<h1" in html
    assert ru["sgr_h1_rejected"] in html
    assert ru["sgr_h1_success"] not in html
    assert ru["sgr_back_signup"] in html
    assert ru["sgr_back_login"] not in html
    # The back link leads exactly to /signup (not the general href="/signup" from the header —
    # we check the caption of the CONCRETE link next to the text of the key).
    m = re.search(
        r'<a href="/signup" class="text-slate-600 underline">([^<]+)</a>', html
    )
    assert m, "the back link to /signup not found in the expected markup"
    assert m.group(1) == ru["sgr_back_signup"]


# --------------------------------------------------------------------------- #
# M-9: the scrollable table of plans is reachable from the keyboard
# --------------------------------------------------------------------------- #
def test_plans_comparison_wrapper_is_focusable_region():
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/plans", cookies={"lang": "ru"})
    assert resp.status_code == 200
    html = resp.text
    m = re.search(
        r'<div class="overflow-x-auto rounded-lg border border-slate-200"'
        r' tabindex="0" role="region" aria-label="([^"]+)">',
        html,
    )
    assert m, "the wrapper of the comparison table without tabindex/role/aria-label"
    assert m.group(1).strip() != ""
