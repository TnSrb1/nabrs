"""/signup: Turnstile UX (audit 07.10 UX-01/A11Y-02) and closed /docs (SEC-02)."""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.emailer import MockEmailSender
from src.api.main import app
from tests.api.conftest import (
    FakeSession,
    bad_turnstile,
    good_email,
    make_session_factory,
)

_STATIC_JS = pathlib.Path("src/api/static/js/signup_turnstile.js")


def teardown_function():
    app.dependency_overrides.clear()


def _post(email: str = "keep.me@company.rs"):
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        FakeSession()
    )
    app.dependency_overrides[deps.get_turnstile] = lambda: bad_turnstile()
    app.dependency_overrides[deps.get_email_validator] = lambda: good_email()
    app.dependency_overrides[deps.get_email_sender] = lambda: MockEmailSender()
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("lang", "ru")
    return client.post(
        "/signup",
        data={"email": email, "cf-turnstile-response": "bad", "consent": "yes"},
    )


def test_turnstile_failure_rerenders_form_with_email():
    resp = _post()
    assert resp.status_code == 400
    html = resp.text
    assert 'action="/signup"' in html  # the same form, not signup_result
    assert 'value="keep.me@company.rs"' in html
    assert 'id="turnstile-error"' in html
    assert "не пройдена" in html  # sgr_msg_turnstile (ru), as a literal


def test_turnstile_failure_escapes_email():
    resp = _post('"><script>x</script>@a.rs')
    assert "<script>x</script>" not in resp.text


def test_signup_wires_external_callback_without_inline_js():
    html = TestClient(app).get("/signup").text
    assert 'data-callback="tsrbTurnstileOk"' in html
    assert "data-turnstile-form" in html
    assert '<script src="/static/js/signup_turnstile.js?v=' in html
    # CSP script-src 'self': not a single inline script (<script> without src).
    assert not re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    # Without JS the form is alive: there is no disabled in the markup (only JS enables it).
    btn = re.search(r'<button type="submit"[^>]*>', html).group(0)
    assert "disabled" not in btn


def test_external_js_defines_callbacks_and_fallback():
    js = _STATIC_JS.read_text(encoding="utf-8")
    for name in ("tsrbTurnstileOk", "tsrbTurnstileReset", "tsrbTurnstileError"):
        assert f"window.{name}" in js
    assert (
        "setTimeout" in js
    )  # a timeout fallback: a failure to load the widget does not kill the form


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_closed_by_default(path):
    assert TestClient(app).get(path).status_code == 404


def test_openapi_still_buildable_in_code():
    assert "/signup" in app.openapi()["paths"]
