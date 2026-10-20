"""The home page `/` for a logged-in user: the CTA leads to the cabinet, not to registration (17.10.2026).

The owner’s complaint: after #787 a logged-in client lands on `/` from the logo, and there is “Registracija → /signup” — nonsense. The render is under a SIGNED cookie (the real path `_auth_context`, not dependency_overrides for authed). Expectations — literals.

The link count — only inside `<main>`: the logged-in user’s header also leads to `/app`, and a count over the whole page would turn green without the fix.
"""

from __future__ import annotations

import re

import pytest

from tests.api.test_public_chrome_authed import _client

SIGNUP = 'href="/signup"'
CABINET = 'href="/app"'
TRIAL_SR = "30 dana Pro besplatno · samo email, bez kartice"
TRIAL_AFTER_SR = "Posle probnog perioda slanje staje, a kabinet ostaje otvoren;"


def _main(html: str) -> str:
    m = re.search(r'<main id="main">(.*?)</main>', html, re.S)
    assert m, 'there is no <main id="main"> in the response'
    return m.group(1)


@pytest.mark.parametrize("locale", ["sr", "ru", "en"])
def test_authed_landing_has_no_signup_cta_and_leads_to_cabinet(locale):
    main = _main(_client(locale=locale).get("/").text)
    assert SIGNUP not in main
    assert main.count(CABINET) == 2  # hero and bottom CTA — exactly two


def test_authed_landing_cabinet_cta_labels():
    assert "Moj kabinet" in _client(locale="sr").get("/").text
    assert "Мой кабинет" in _client(locale="ru").get("/").text
    assert "My cabinet" in _client(locale="en").get("/").text


def test_guest_landing_keeps_signup_cta():
    main = _main(_client(authed=False).get("/").text)
    assert main.count(SIGNUP) == 2  # hero and bottom CTA — exactly two
    assert CABINET not in main


def test_authed_landing_hides_trial_badge_and_text():
    main = _main(_client(locale="sr").get("/").text)
    assert TRIAL_SR not in main
    assert TRIAL_AFTER_SR not in main


def test_guest_landing_shows_trial_badge_and_text():
    main = _main(_client(authed=False, locale="sr").get("/").text)
    assert TRIAL_SR in main
    assert TRIAL_AFTER_SR in main
