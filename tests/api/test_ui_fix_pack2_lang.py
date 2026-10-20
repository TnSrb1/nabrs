"""Package 2 of the interface audit (UI_AUDIT_2026-08-02_stage1_public.md):
HIGH-1, HIGH-2, M-7 — the language of the public funnel.

The checks are by the RENDER of the live routes, not by the template: the audit caught exactly the render (the showcase gave sr for the same request, while /signup gave ru). The markers are taken FROM THE CATALOGUE (`CABINET[lang][key]`), not by “no Cyrillic”: the absence of Russian letters coexists with a broken chrome too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import auth_service as svc
from src.api.cabinet import routes_auth
from src.api.cabinet.i18n import CABINET
from src.api.config import get_cabinet_settings
from src.api.emailer import MockEmailSender
from src.api.main import app
from src.api.public.i18n import UI
from tests.api.conftest import (
    FakeSession,
    good_email,
    make_session_factory,
    ok_turnstile,
)

_TID = "11111111-1111-1111-1111-111111111111"
_UID = "22222222-2222-2222-2222-222222222222"

# The login screens and the catalog key by which the page language is visible.
_SCREENS = [
    ("/signup", "sgn_page_title"),
    ("/login", "log_get_link"),
    ("/auth/resend", "rsn_title"),
]

pytestmark = pytest.mark.sr_default


def teardown_function():
    app.dependency_overrides.clear()


def _client(*, sess: dict | None = None, locale: str | None = None) -> TestClient:
    """TestClient with a mocked session and users.locale (we do not touch the DB)."""

    def factory():
        return FakeSession(user_locale=locale)

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# HIGH-2: the default language of the login forms is sr (as on the showcase), not the cabinet ru
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,key", _SCREENS)
def test_anon_entry_screens_default_to_sr(path, key):
    """Without Accept-Language and without a cookie the login screen gives sr.

    BEFORE the fix these same requests gave ru (confirmed on production: `GET /signup` without headers → `<title>` in Russian…), while the showcase on the same request gave sr. TestClient does not send the Accept-Language header — and that is the “empty” measurement.
    """
    resp = _client().get(path)
    assert resp.status_code == 200
    assert 'lang="sr"' in resp.text
    assert CABINET["sr"][key] in resp.text
    assert CABINET["ru"][key] not in resp.text


@pytest.mark.parametrize("path,key", _SCREENS)
@pytest.mark.parametrize(
    "header",
    ["ru", "ru-RU,ru;q=0.9", "en-US,en;q=0.9", "hr-HR,hr;q=0.9", "bs,en;q=0.3"],
)
def test_anon_entry_screens_ignore_accept_language(path, key, header):
    """The owner’s decision 30.09.2026: Accept-Language is NOT taken into account — always sr.

    A Serb with an en browser must not see English; ru/en — only by an explicit choice (?lang= / cookie).
    """
    resp = _client().get(path, headers={"accept-language": header})
    assert resp.status_code == 200
    assert 'lang="sr"' in resp.text
    assert CABINET["sr"][key] in resp.text


@pytest.mark.parametrize("path,key", _SCREENS)
def test_anon_entry_screens_explicit_lang_query_and_cookie(path, key):
    """The explicit choice works: ?lang=ru → ru, cookie lang=en → en."""
    resp = _client().get(path + "?lang=ru", headers={"accept-language": "en"})
    assert 'lang="ru"' in resp.text
    assert CABINET["ru"][key] in resp.text
    assert "lang=" in resp.headers.get("set-cookie", "")
    client = _client()
    client.cookies.set("lang", "en")
    resp = client.get(path, headers={"accept-language": "ru"})
    assert 'lang="en"' in resp.text
    assert CABINET["en"][key] in resp.text


def test_anon_hr_only_header_falls_back_to_sr():
    """Only hr/bs in the header → sr."""
    for header in ("hr", "bs-BA,bs;q=0.9", "sr-Latn-RS,hr;q=0.8"):
        resp = _client().get("/signup", headers={"accept-language": header})
        assert 'lang="sr"' in resp.text, header


def test_authed_user_locale_still_wins_on_entry_screens():
    """The fix does not touch the logged-in user: users.locale takes priority over everything."""
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale="ru").get(
        "/login", headers={"accept-language": "en"}
    )
    assert resp.status_code == 200
    assert 'lang="ru"' in resp.text


def test_public_showcase_and_signup_agree_on_default():
    """The same client sees the showcase and the registration form in ONE language."""
    client = _client()
    assert 'lang="sr"' in client.get("/").text
    assert 'lang="sr"' in client.get("/signup").text


@pytest.mark.parametrize(
    "header,cookie,expected",
    [(None, None, "sr"), ("ru", None, "sr"), (None, "en", "en"), ("hr", None, "sr")],
)
def test_signup_passes_page_language_to_service(monkeypatch, header, cookie, expected):
    """The locale of the new account and the language of the e-mail = the language of the PAGE that the client saw.

    The resolver of the chrome and of the handler must be one: otherwise the client registers on the sr form, and the account and the e-mail are created in ru.
    """
    seen: dict = {}

    async def _fake_signup(session, **kwargs):
        seen.update(kwargs)
        return svc.SignupResult(svc.TRIAL_STARTED, "ok")

    monkeypatch.setattr(routes_auth.svc, "signup", _fake_signup)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        FakeSession()
    )
    app.dependency_overrides[deps.get_turnstile] = lambda: ok_turnstile()
    app.dependency_overrides[deps.get_email_validator] = lambda: good_email()
    app.dependency_overrides[deps.get_email_sender] = lambda: MockEmailSender()
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    if cookie:
        client.cookies.set("lang", cookie)
    resp = client.post(
        "/signup",
        data={
            "email": "user@company.rs",
            "cf-turnstile-response": "t",
            "consent": "yes",
        },
        headers={"accept-language": header} if header else None,
    )
    assert resp.status_code == 200
    assert seen["locale"] == expected


# --------------------------------------------------------------------------- #
# HIGH-1: the language switch on the anonymous screens does not throw off the form
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,key", _SCREENS)
def test_anon_lang_switch_is_get_link_not_post_form(path, key):
    """For an anonymous user the pills are GET links ?lang=, there is no POST form /cabinet/locale.

    The form itself was the defect: `POST /cabinet/locale` with `session is None` gives a 303 to /login, so a click on “SR” on the registration form threw the client off the form (routes_locale.py:50-52 — the behavior was NOT changed).
    """
    html = _client().get(path).text
    assert 'action="/cabinet/locale"' not in html
    for code in ("sr", "ru", "en"):
        assert re.search(rf'<a href="\?lang={code}"[^>]*class="lang-pill', html), code


def test_authed_lang_switch_keeps_post_form():
    """For a logged-in user the mechanism is the former one — the POST form (the regression is not broken).

    ‼️ The `authed` flag in the template comes from PARSING the real cookie (`templating._auth_context`), not from a substitution of the dependency — therefore here the session is signed for real.
    """
    client = _client(sess={"uid": _UID, "tid": _TID}, locale="ru")
    client.cookies.set(
        deps.SESSION_COOKIE,
        security.sign_session(
            _UID, _TID, get_cabinet_settings().cabinet_secret_key, locale="ru"
        ),
    )
    html = client.get("/login").text
    assert 'lang="ru"' in html
    assert 'action="/cabinet/locale"' in html
    assert '<a href="?lang=' not in html


def test_authed_locale_post_still_writes_and_redirects():
    """`POST /cabinet/locale` for a logged-in user works as before (303 + cookie)."""
    fake = FakeSession(user_locale="ru")
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: fake)
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": _UID,
        "tid": _TID,
    }
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    resp = client.post("/cabinet/locale", data={"lang": "en", "return_to": "/app"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"
    assert any(
        "UPDATE users SET locale" in sql for sql, _ in fake.executed
    ), "users.locale not written"
    assert client.cookies.get("lang") == "en"


@pytest.mark.parametrize("path,key", _SCREENS)
def test_anon_lang_click_renders_and_persists_choice(path, key):
    """A click on the pill: the page is in en AND the choice survived the transition to another screen."""
    client = _client()
    resp = client.get(f"{path}?lang=en")
    assert resp.status_code == 200  # not 303 — it no longer leads the anonymous user anywhere
    assert 'lang="en"' in resp.text
    assert CABINET["en"][key] in resp.text
    assert client.cookies.get("lang") == "en"
    # The next screen without ?lang= — in the chosen language (the cookie remembers the choice).
    nxt = client.get("/login")
    assert 'lang="en"' in nxt.text
    assert CABINET["en"]["log_get_link"] in nxt.text


def test_anon_lang_choice_survives_post_of_the_form():
    """The consent error on POST /signup comes in the CHOSEN language (en)."""
    client = _client()
    client.get("/signup?lang=en")  # the pill’s choice → cookie
    resp = client.post("/signup", data={"email": "user@company.rs", "consent": ""})
    assert resp.status_code == 400
    assert 'lang="en"' in resp.text
    assert CABINET["en"]["sgn_consent_error"] in resp.text
    assert CABINET["ru"]["sgn_consent_error"] not in resp.text


def test_anon_never_gets_303_on_language_switch():
    """No login screen answers with a redirect to a language change."""
    client = _client()
    for path, _ in _SCREENS:
        for code in ("sr", "ru", "en"):
            resp = client.get(f"{path}?lang={code}")
            assert resp.status_code == 200, f"{path}?lang={code} → {resp.status_code}"


# --------------------------------------------------------------------------- #
# M-7: the Russian chrome of the public frame went into the UI catalog
# --------------------------------------------------------------------------- #
_TPL = Path(__file__).resolve().parents[2] / "src" / "api" / "templates"
_JINJA_COMMENT = re.compile(r"{#.*?#}", re.S)


def test_public_base_has_no_raw_cyrillic():
    """The guard duplicates the allowlist test by address: the file left the allowlist for good."""
    txt = _JINJA_COMMENT.sub(
        "", (_TPL / "public_base.html").read_text(encoding="utf-8")
    )
    assert not re.search(r"[а-яёА-ЯЁ]", txt), "raw Cyrillic in public_base.html"


@pytest.mark.parametrize("key", ["aria_lang", "to_top", "footer_privacy"])
def test_public_ui_catalog_covers_new_chrome_keys(key):
    for lang in ("sr", "ru", "en"):
        assert UI[lang].get(key), f"{key} is empty in {lang}"


@pytest.mark.parametrize(
    "lang,aria,top",
    [
        ("sr", "Jezik", "Na vrh"),
        ("ru", "Язык", "Наверх"),
        ("en", "Language", "Back to top"),
    ],
)
def test_public_chrome_labels_follow_page_language(lang, aria, top):
    """The aria-label of the languages and of the “To top” button are rendered in the language of the page."""
    html = _client().get(f"/?lang={lang}").text
    assert f'<nav class="lang-switch" aria-label="{aria}"' in html
    assert f'aria-label="{top}" title="{top}"' in html
