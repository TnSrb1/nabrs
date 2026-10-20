"""Keyword chips + a hint without jargon (template_wizard_2, step 4).

The chip behavior (Enter/comma/×/“+”) is checked in the browser —
scripts/shots/chips_e2e.js (see docs/reports/TESTING_template_wizard_2.md).
Here — the server markup: progressive enhancement and texts as literals.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


def teardown_function():
    app.dependency_overrides.clear()


def _html(locale: str, url: str = "/profiles/new?manual=1") -> str:
    app.dependency_overrides[deps.get_session_factory] = lambda: (
        lambda: FakeSession(user_locale=locale)
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    return TestClient(app, follow_redirects=False).get(url).text


def test_textarea_stays_source_of_truth_and_chips_start_hidden():
    html = _html("sr")
    assert re.search(r'<textarea id="pf-kw-inc" name="keywords_include"', html)
    box = re.search(r'<div class="kw-chips hidden" data-kw-chips[^>]*>', html)
    assert box and 'data-for="pf-kw-inc"' in box.group(0)
    # The chip input without a name is not sent in the POST, the per-letter counter does not wake up.
    chip_input = re.search(r'<input[^>]*id="pf-kw-chip-input"[^>]*>', html, re.S).group(
        0
    )
    assert "name=" not in chip_input
    assert "data-no-preview" in chip_input


def test_sr_hints_plain_language():
    html = _html("sr")
    assert (
        "Pišite osnovu reči — npr. škol, građevin, čišćenj — hvata sve padeže." in html
    )
    assert "Tender stiže ako sadrži bilo koju od ovih stavki." in html
    assert "AND-grupa" not in html
    assert "ILI (OR)" not in html


@pytest.mark.parametrize(
    "locale, jargon",
    [("ru", "группа И"), ("en", "AND group")],
)
def test_no_and_or_jargon_in_other_locales(locale, jargon):
    assert jargon not in _html(locale)


def test_prefilled_and_group_survives_in_textarea():
    # The package “Građevina” carries groups of two words: the server still gives them
    # as a string “a, b” — the chips show “a + b” and write the comma back.
    html = _html("sr", "/profiles/new?from_package=gradjevina")
    area = re.search(r'name="keywords_include"[^>]*>([^<]*)</textarea>', html).group(1)
    assert "remont, zgrad" in area


def test_script_loaded_with_sri():
    html = _html("sr")
    assert re.search(
        r'<script src="/static/js/keyword_chips.js\?v=[0-9a-f]{10}" integrity="sha384-[A-Za-z0-9+/=]+"',
        html,
    )
