"""Login/registration in the showcase frame + 404 (UX audit 10.10, F-10..F-13, F-20).

The expectations are LITERALS from the measurement (BEFORE/AFTER, scripts/shots), not via t()/UI:
otherwise the test is a tautology (rules/tests-known-defects.md).
"""

from __future__ import annotations

import hashlib
import base64
import pathlib
import re

from fastapi.testclient import TestClient

from src.api.main import app

_TEMPLATES = pathlib.Path("src/api/templates")
_STATIC_JS = pathlib.Path("src/api/static/js/signup_turnstile.js")

# Showcase navigation (public_base.html, header) — literal hrefs.
_STOREFRONT_NAV = ('href="/tenderi"', 'href="/izvori"', 'href="/plans"', 'href="/help"')
# Showcase footer: the full set of links, including Impressum.
_STOREFRONT_FOOTER = ('href="/kontakt"', 'href="/legal/impressum"')


def _client() -> TestClient:
    return TestClient(app)


def test_login_renders_200_in_storefront_shell():
    resp = _client().get("/login?lang=sr")
    assert resp.status_code == 200
    html = resp.text
    for href in _STOREFRONT_NAV + _STOREFRONT_FOOTER:
        assert href in html, href
    # The cabinet header must not replace the showcase one (F-10).
    assert 'href="/app/feed"' not in html


def test_login_has_no_gsap_or_htmx_loaded():
    html = _client().get("/login?lang=sr").text
    assert "gsap" not in html
    assert "htmx" not in html


def test_login_h1_uses_storefront_h1_style():
    html = _client().get("/login?lang=sr").text
    assert (
        '<h1 class="text-3xl font-bold tracking-tight text-slate-900">Prijava</h1>'
        in html
    )


def test_signup_renders_in_storefront_shell_without_gsap():
    resp = _client().get("/signup?lang=sr")
    assert resp.status_code == 200
    html = resp.text
    for href in _STOREFRONT_NAV + _STOREFRONT_FOOTER:
        assert href in html, href
    assert "gsap" not in html
    assert "htmx" not in html


def test_signup_next_steps_line_is_latin_and_has_no_time_promise():
    html = _client().get("/signup?lang=sr").text
    assert (
        "Dalje: link stiže na email → izaberete šablon (proizvod ili usluga) "
        "→ tenderi se pojavljuju u vašem feedu."
    ) in html
    # Serbian — Latin script only (owner 10.10): not a single Cyrillic letter in the string.
    line = re.search(r"Dalje: [^<]*", html).group(0)
    assert not re.search(r"[а-яА-ЯёЁ]", line)
    # No time-based labels (F-12): neither “2 min” nor “danas”.
    assert "min" not in line and "danas" not in line


def test_signup_turnstile_wait_is_aria_live_and_hidden_until_blocked():
    html = _client().get("/signup?lang=sr").text
    assert 'aria-live="polite"' in html
    assert '<span id="turnstile-wait" hidden>Proveravamo da niste robot…</span>' in html


def test_signup_turnstile_wait_text_ru_and_en():
    ru = _client().get("/signup?lang=ru").text
    assert "Проверяем, что вы не робот…" in ru
    en = _client().get("/signup?lang=en").text
    assert "Checking that you are not a bot…" in en


def test_signup_script_sri_matches_file():
    """The SRI in the template = sha384 of the file. After a JS change the hash must be updated."""
    digest = hashlib.sha384(_STATIC_JS.read_bytes()).digest()
    expected = "sha384-" + base64.b64encode(digest).decode()
    html = (_TEMPLATES / "signup.html").read_text(encoding="utf-8")
    assert f'integrity="{expected}"' in html


def test_turnstile_js_toggles_wait_line_with_button_block():
    js = _STATIC_JS.read_text(encoding="utf-8")
    assert "getElementById('turnstile-wait')" in js
    assert "wait.hidden = !blocked" in js


def test_404_is_branded_container_with_tenderi_and_home_links():
    resp = _client().get("/nepostoji-xyz?lang=sr", headers={"accept": "text/html"})
    assert resp.status_code == 404
    html = resp.text
    assert "<title>Stranica nije pronađena — TenderSRB</title>" in html
    # The standard showcase container with horizontal padding (x=0 was at 390).
    assert '<section class="mx-auto max-w-5xl px-4 pt-14 pb-12">' in html
    # Tap zone 2rem in height: inline-block + py-2 (measurement shots: 20px → 36px).
    assert (
        '<a href="/tenderi" class="inline-block py-2 underline text-slate-600">Tenderi</a>'
        in html
    )
    assert (
        '<a href="/" class="inline-block py-2 underline text-slate-600">Na početnu</a>'
        in html
    )


def test_404_json_client_unchanged():
    resp = _client().get("/nepostoji-xyz", headers={"accept": "application/json"})
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
