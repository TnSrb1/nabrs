"""Package 3 of the interface audit: the sizes of the targets on the public screens.

The measurement of 02.08 with the instrument `scripts/shots` on six public screens gave exactly
two real findings — the rest turned out to be an artifact of the measurement:

* the language pills `27×32` on ALL six screens: their height had already been raised to 2rem, but the width had not, and along that axis a finger misses just the same;
* the square of the consent checkbox `13×13` (the browser default) with a row 20px high.

‼️ The checkbox was NOT a “non-working tap zone” at the same time: it lies inside `<label>`, and a click on the consent text ticks the box (checked by a click 12px from the edge of the row — `checked` became `true`). Therefore the test pins the SIZE OF THE TARGET, not the “click zone”, and does not require a height from the inline links inside the consent text: a forced height is forbidden for them (the project rules, “Frontend”).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CSS = (_ROOT / "src" / "api" / "static" / "css" / "app.css").read_text(
    encoding="utf-8"
)
_SIGNUP = (_ROOT / "src" / "api" / "templates" / "signup.html").read_text(
    encoding="utf-8"
)


def _rule(selector: str) -> str:
    """The body of the CSS rule by the exact selector (the first occurrence)."""
    m = re.search(rf"(?:^|\}}|\*/)\s*{re.escape(selector)}\s*\{{(.*?)\}}", _CSS, re.S)
    assert m, f"rule {selector} not found in app.css"
    return m.group(1)


def test_lang_pill_has_min_width_not_only_min_height():
    """The language pill gets up to 2rem on BOTH axes.

    Before the fix: `min-height: 2rem` was there, `min-width` — no, the measurement gave 27×32.
    """
    body = _rule(".lang-pill")
    assert "min-height: 2rem" in body
    assert "min-width: 2rem" in body


def test_consent_checkbox_is_bigger_than_browser_default():
    """The consent square is set explicitly and is larger than the default 13px.

    13px = 0.8125rem, therefore any value ≥1rem is already more than the default; the fix sets 1.125rem (18px).
    """
    body = _rule('.consent-row input[type="checkbox"]')
    m = re.search(r"width:\s*([\d.]+)rem", body)
    assert m, "the consent checkbox has no width set"
    assert float(m.group(1)) >= 1.0
    assert "height" in body


def test_consent_row_reaches_tap_target_height():
    """The consent row is a target ≥2rem: it is exactly that which is clicked, not the square."""
    assert "min-height: 2rem" in _rule(".consent-row")


def test_signup_template_uses_consent_row_class():
    """The class is applied to the label that WRAPS the checkbox.

    The rule hits `.consent-row input[type="checkbox"]`, so the class on a neighbouring node would silently do nothing.
    """
    labels = re.findall(r'<label[^>]*class="([^"]*)"[^>]*>(.*?)</label>', _SIGNUP, re.S)
    wrapping = [cls for cls, inner in labels if 'name="consent"' in inner]
    assert wrapping, "the consent checkbox is not wrapped in a label"
    assert "consent-row" in wrapping[0]


def test_consent_links_are_not_forced_to_tap_height():
    """The links inside the consent text remain inline.

    A guard against a “fix” by the metric: the measurement marks them as small targets, but stretching them is forbidden — a rule for them must not appear.
    """
    assert ".consent-row a" not in _CSS


def test_footer_links_have_min_width_not_only_min_height():
    """The footer links get 2rem on BOTH axes (the same class as .lang-pill).

    The measurement of 03.08 (`shot.sh`, 390×844, `?lang=en`) on /plans, /signup, /login, /help: the short caption "Help" — 28×32, min-height was already there, min-width — not.
    """
    body = _rule("footer a")
    assert "min-height: 2rem" in body
    assert "min-width: 2rem" in body
