"""The remaining small tap zones in the cabinet: action links, expanders, the profile label.

The live measurement of production (25.07, 390px) after #446/#448 showed that the cabinet still has interactive elements below the 2rem invariant:

| element | was | where |
|---|---|---|
| `a.fd-card__profile` | 25px | the feed, 20 items — the filter link “show this profile” |
| `a.text-brand` (“← To the panel”, “My tenders →”) | 20px | the cockpit, the feed archive |
| `a.text-slate-500.underline` (“Need help?”) | 20px | the profile form |
| `summary.text-xs` (“by profiles”) | 16px | the cockpit |
| `button.text-slate-600` (“All sections”) | 16px | the CPV widget |

‼️ The card title links (`fd-card__title`, `fn-row__title`, 17px) are NOT touched on purpose: these are lines of text, clickable across the whole width; a forced height would break the vertical rhythm of the list. The 2rem invariant is addressed to buttons and badges.
"""

from __future__ import annotations

import pathlib
import re

_CSS = pathlib.Path("src/api/static/css/app.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """The body of the CSS rule by the exact selector (or an empty string)."""
    m = re.search(re.escape(selector) + r"[^{}]*\{([^}]*)\}", _CSS)
    return m.group(1) if m else ""


def test_feed_profile_badge_reaches_two_rem():
    """The profile label in the feed card is a badge-link, not a caption.

    Mutation: remove `min-height` from `.fd-card__profile` → the test goes red.
    """
    body = _rule(".fd-card__profile")
    assert body, "the rule .fd-card__profile not found"
    assert "min-height: 2rem" in body, body
    assert (
        "inline-flex" in body
    ), "without a flex min-height there will be no real height"


def test_inline_actions_in_main_reach_two_rem():
    """Action links and expanders inside #main are raised to 2rem."""
    body = _rule("#main button.text-slate-600")
    assert body, "the rule for small interactive elements of #main not found"
    assert "min-height: 2rem" in body, body
    for selector in (
        "#main a.text-brand",
        "#main a.text-slate-500.underline",
        "#main summary.text-xs",
    ):
        assert selector in _CSS, selector


def test_rule_is_scoped_to_main_not_global():
    """The rule is limited to `#main`: the header and the footer keep their sizes.

    A global selector by the utility classes would catch the navigation and the footer, whose tap zones are already set separately (#446).
    """
    assert "\na.text-brand {" not in _CSS, "the rule must not be global"
    assert "#main a.text-brand" in _CSS


def test_headline_links_are_left_alone():
    """The card titles do NOT get a forced height (on purpose)."""
    for selector in (".fd-card__title", ".fn-row__title"):
        body = _rule(selector)
        assert "min-height: 2rem" not in body, (selector, body)


def test_cpv_result_row_reaches_two_rem():
    """The row of the result/node of the CPV widget (the clickable target of drill-down,
    `role="button"` — see cpv_widget.js) is raised to 2rem following the pattern of the “+” button
    next to it (measurement 04.08: 219×28 on the live page).

    Mutation: remove `body.style.minHeight = '2rem';` → the test goes red.
    """
    js = pathlib.Path("src/api/static/js/cpv_widget.js").read_text(encoding="utf-8")
    m = re.search(
        r"body\.className = 'flex-1 min-w-0 py-1 text-sm';\s*\n\s*"
        r"body\.style\.minHeight = '2rem';",
        js,
    )
    assert m, "body (the CPV result row) does not get minHeight 2rem"


def test_sri_matches_css():
    """The SRI is recomputed after the CSS edit — otherwise the style will silently not apply."""
    import base64
    import hashlib

    digest = hashlib.sha384(
        pathlib.Path("src/api/static/css/app.css").read_bytes()
    ).digest()
    expected = "sha384-" + base64.b64encode(digest).decode()
    for name in ("base.html", "public_base.html"):
        html = pathlib.Path(f"src/api/templates/{name}").read_text(encoding="utf-8")
        m = re.search(r'app\.css\'\) \}\}" integrity="([^"]+)"', html)
        assert m, f"{name}: integrity for app.css not found"
        assert m.group(1) == expected, (name, m.group(1), expected)
