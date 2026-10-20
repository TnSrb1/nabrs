"""Reopening a one-time login link does not look like a breakage.

Production defect of 04.08.2026 (logs of ``deploy-api-1``): the client logged in via the link from the
email, reopened the email — and got “Link is invalid”, although the session in the
browser was alive. Mail scanners and preview bots do the same: the same
link is hit 2–3 times.

The one-time nature of the token is the correct behavior and is NOT checked here (it is held by
``test_auth.py::test_magic_link_consume_reused`` and the service). What is checked is the OUTCOME
of showing an unaccepted token, identical for both routes:

- a live session in the browser → 303 to the cabinet (``next`` is respected);
- anonymous → 200 with the FORM for requesting a new link (submit to the existing
  POST /login), with an honest text: the link is one-time and already used.

The expectations are taken from the ROUTE’S RESPONSE (both branches of both routes were hit), not from the
code model: an assert that pinned the wrong contract already let a 404 through to prod
(#462 → #463).
"""

from __future__ import annotations

import ast
import pathlib
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import routes_auth
from src.api.cabinet.i18n import CABINET
from src.api.config import get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession, make_token, make_user

_UID = "00000000-0000-0000-0000-0000000000aa"
_TID = "00000000-0000-0000-0000-0000000000bb"
# Russian-specific letters: their absence is the acceptance metric of the sr rendering.
_RU_ONLY = re.compile(r"[ыэщъёй]", re.IGNORECASE)


def teardown_function():
    app.dependency_overrides.clear()


def _consumed_session(purpose: str, *, verified: bool = True) -> FakeSession:
    """The token is in the DB, but already consumed — exactly the prod case.

    The service (``consume_magic_link`` / ``verify_email_token``) here is real:
    it itself returns None by ``consumed_at``. The results queue: the SELECT
    of the token, and for verify also the fact check ``email_verify_already_done``
    (the SELECT of the token + the tenant resolve).
    """
    user = make_user(email_verified_at=datetime.now(timezone.utc) if verified else None)
    token = make_token(
        purpose=purpose, consumed_at=datetime.now(timezone.utc), user_id=user.id
    )
    results = [FakeResult(items=[token])]
    if purpose == "email_verify":
        results += [FakeResult(items=[token]), FakeResult(items=[user.tenant_id])]
    return FakeSession(results=results, get_map={"User": user})


def _client(session: FakeSession, *, authed: bool = False) -> TestClient:
    def factory():
        return session

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    client = TestClient(app, follow_redirects=False)
    if authed:
        # A real signed cookie, not a dependency substitution: the client’s browser
        # arrives with exactly that one.
        settings = get_cabinet_settings()
        client.cookies.set(
            "tb_session",
            security.sign_session(_UID, _TID, settings.cabinet_secret_key),
        )
    return client


def _has_resend_form(html: str) -> bool:
    return (
        'action="/login"' in html and 'method="post"' in html and 'name="email"' in html
    )


# --- live session: to the cabinet, without an error page -------------------------- #


@pytest.mark.parametrize(
    "path,purpose",
    [("/auth/magic", "magic_link"), ("/auth/verify", "email_verify")],
)
def test_used_link_with_live_session_redirects_to_cabinet(path, purpose):
    """The person is already inside — we must not show them a dead end.

    Mutation: return a page instead of the redirect (remove the ``session is not
    None`` branch in ``_link_not_accepted``) → 200 instead of 303, the test turns red.
    """
    resp = _client(_consumed_session(purpose), authed=True).get(
        path, params={"token": "used"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


def test_used_magic_link_with_live_session_honours_next():
    """The return from the email is respected even on a consumed token (safe_next_path)."""
    resp = _client(_consumed_session("magic_link"), authed=True).get(
        "/auth/magic", params={"token": "used", "next": "/app/tenders"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/tenders"


def test_used_magic_link_with_live_session_ignores_hostile_next():
    """A foreign host in next does not lead the client outside (safe_next_path is in place)."""
    resp = _client(_consumed_session("magic_link"), authed=True).get(
        "/auth/magic", params={"token": "used", "next": "https://evil.example/x"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


# --- anonymous: the form for a new link, not a dead end -------------------------------- #


@pytest.mark.parametrize(
    "path,purpose",
    [("/auth/magic", "magic_link"), ("/auth/verify", "email_verify")],
)
def test_used_link_anonymous_shows_resend_form(path, purpose):
    """No session → 200 with the “send a new link” form to POST /login.

    Mutation: return ``message.html`` in the anonymous branch → no form, the test turns red.
    """
    resp = _client(_consumed_session(purpose, verified=False)).get(
        path, params={"token": "used"}
    )
    assert resp.status_code == 200
    assert _has_resend_form(resp.text)
    # An honest text instead of “Link is invalid” (the anonymous user’s default language is sr,
    # the showcase contour).
    assert CABINET["sr"]["authm_link_used_title"] in resp.text
    assert CABINET["sr"]["authm_new_link_cta"] in resp.text


def test_resend_form_has_no_inline_handlers():
    """CSP ``script-src 'self'``: inline ``on*`` handlers silently do not run —
    the form must be a plain one, without them."""
    resp = _client(_consumed_session("magic_link")).get(
        "/auth/magic", params={"token": "used"}
    )
    page = resp.text.split("<main", 1)[-1]
    assert not re.findall(r"\son[a-z]+\s*=", page), page


def test_verify_prefetched_token_keeps_already_confirmed_wording():
    """The idempotency of verify is not lost: a scanner consumed the token, the account is
    confirmed → for the anonymous user “e-mail already confirmed” + the login form, and not
    “the link is one-time”."""
    resp = _client(_consumed_session("email_verify", verified=True)).get(
        "/auth/verify", params={"token": "prefetched"}
    )
    assert resp.status_code == 200
    assert CABINET["sr"]["authm_verify_already_title"] in resp.text
    assert CABINET["sr"]["authm_link_used_title"] not in resp.text
    assert _has_resend_form(resp.text)


# --- three languages -------------------------------------------------------------- #


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_used_link_page_speaks_three_languages(lang):
    """The page is localized entirely; there are no Russian letters in the sr rendering.

    Mutation: hard-code any ru literal in the template → the sr branch turns red
    (and so does the guard for raw Cyrillic in templates).
    """
    resp = _client(_consumed_session("magic_link")).get(
        "/auth/magic", params={"token": "used", "lang": lang}
    )
    assert resp.status_code == 200
    assert f'lang="{lang}"' in resp.text
    assert CABINET[lang]["authm_magic_link_expired_body"] in resp.text
    assert CABINET[lang]["authm_new_link_cta"] in resp.text
    if lang == "sr":
        assert not _RU_ONLY.findall(resp.text)


# --- predicate over the render points -------------------------------------------- #


def test_no_token_failure_renders_error_message_page():
    """Guard for the requirement “fix BOTH routes”: no failure branch in
    ``routes_auth`` draws the service ``message.html`` with
    ``success: False`` any more.

    A predicate over the ast, not a list of addresses: a third such point, added later,
    will turn red by itself.
    """
    tree = ast.parse(pathlib.Path(routes_auth.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        renders_message = any(
            isinstance(a, ast.Constant) and a.value == "message.html" for a in node.args
        )
        if not renders_message:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Dict):
                for key, value in zip(arg.keys, arg.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "success"
                        and isinstance(value, ast.Constant)
                        and value.value is False
                    ):
                        offenders.append(node.lineno)
    assert not offenders, f"dead-end page on a token failure, lines: {offenders}"
