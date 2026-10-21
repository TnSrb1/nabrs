"""Anti-regression (UI touch/htmx): the tap zones of interactive elements and the visible
response of the htmx actions (the project rules, “Frontend”: tap zones from 2rem; any
htmx action gives a visible response via .htmx-request).

The diagnosis (24.07, a measurement in the DOM of headless Chromium): the language switcher (20px),
“Prikaži prevod” (16px), the footer links (20px), the <summary> of the FAQ on /plans
(24px), the logo link in the header (28px) — all below the norm 2rem; 20+3 htmx buttons
without hx-indicator. That PR (#446) closed only the showcase — on 25.07 a finishing pass was
added for the cabinet: `_card_title.html` (the translation button), `.tip__icon`,
`.field-check`, `.field__label`, `.seg__item`, `.fd-card__cta`.

The tests read the templates/CSS directly (they do not render the page) — they work without the DB
and the application, and mirror the predicate of the brief literally.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "api" / "templates"
APP_CSS = (
    Path(__file__).resolve().parents[2] / "src" / "api" / "static" / "css" / "app.css"
)

_JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)
# The opening/closing tag; the attributes allow '>' only inside the quotes.
_TAG_RE = re.compile(
    r"<(/?)([a-zA-Z][a-zA-Z0-9:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)\s*>",
    re.DOTALL,
)
_HX_ACTION_RE = re.compile(r"\bhx-(?:get|post|delete|patch)\s*=")
_HX_INDICATOR_RE = re.compile(r"\bhx-indicator\s*=")

# Elements without a closing tag do not go into the stack of ancestors (otherwise the stack
# gets desynchronized and the subsequent closing tags “eat” not their own
# parents).
_VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def _htmx_actions_without_indicator(text: str) -> list[str]:
    """The predicate of the brief: hx-get|hx-post|hx-delete|hx-patch without hx-indicator NEITHER
    on the tag itself, NOR on an ancestor. Returns readable snippets of the violations.
    """
    cleaned = _JINJA_RE.sub(" ", text)
    ancestor_has_indicator: list[bool] = []
    violations: list[str] = []
    for m in _TAG_RE.finditer(cleaned):
        closing, name, attrs, self_close = m.groups()
        if closing:
            if ancestor_has_indicator:
                ancestor_has_indicator.pop()
            continue
        has_indicator_here = bool(_HX_INDICATOR_RE.search(attrs))
        if (
            _HX_ACTION_RE.search(attrs)
            and not has_indicator_here
            and not any(ancestor_has_indicator)
        ):
            violations.append(m.group(0).strip()[:160])
        if not self_close and name.lower() not in _VOID_ELEMENTS:
            ancestor_has_indicator.append(has_indicator_here)
    return violations


def test_no_htmx_action_without_indicator():
    violations: dict[str, list[str]] = {}
    for path in sorted(TEMPLATES_DIR.glob("*.html")):
        found = _htmx_actions_without_indicator(path.read_text(encoding="utf-8"))
        if found:
            violations[path.name] = found

    assert not violations, (
        "htmx actions without a visible response (no hx-indicator on the tag, "
        f"nor on an ancestor): {violations}"
    )


def _rule_body(css_text: str, selector: str) -> str:
    """The body of the FIRST CSS rule with the exact selector (after '{' up to the first '}').
    It requires that the selector be preceded by the start of a line/'}'/';' — otherwise
    `.brand` would falsely match inside `.site-header .brand { ... }`."""
    pattern = re.compile(
        r"(?:^|[};])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}",
        re.MULTILINE,
    )
    m = pattern.search(css_text)
    assert m, f"the CSS rule {selector!r} not found in app.css"
    return m.group(1)


_MIN_HEIGHT_RE = re.compile(r"min-height:\s*([\d.]+)(rem|px)")


def _min_height_rem(body: str) -> float:
    m = _MIN_HEIGHT_RE.search(body)
    assert m, f"min-height is not set in the rule: {body!r}"
    value, unit = m.groups()
    return float(value) if unit == "rem" else float(value) / 16


def test_touch_targets_reach_two_rem():
    css_text = APP_CSS.read_text(encoding="utf-8")
    # .lang-pill — the SR/RU/EN switcher (the cabinet header and the public header).
    # .brand — the logo link in the header.
    # footer a — the footer links (Politika privatnosti and the neighbours).
    # .tip__icon — the icon of the “?” hint; .field-check/.field__label — the checkboxes
    # and the captions of the profile form (they are tapped by the label); .seg__item — the filters of /app/my;
    # .fd-card__cta — the CTA “Otvori AI-analizu →” in the feed (the finishing pass after #446,
    # which closed the tap zones only on the showcase).
    for selector in (
        ".lang-pill",
        ".brand",
        "footer a",
        ".tip__icon",
        ".field-check",
        ".field__label",
        ".seg__item",
        ".fd-card__cta",
    ):
        body = _rule_body(css_text, selector)
        assert (
            _min_height_rem(body) >= 2.0
        ), f"{selector}: the tap zone is less than the 2rem norm"


def test_card_title_translate_button_reuses_act_btn():
    """The translation button in _card_title.html (feed/cockpit, 20+3 occurrences per the brief)
    was on a bare `.text-xs.underline` (16px) — instead of a new class we reuse
    `.act-btn` (min-height 2rem), as in _tender_description.html."""
    text = (TEMPLATES_DIR / "_card_title.html").read_text(encoding="utf-8")
    buttons = re.findall(r'<button\b[^>]*class="([^"]*)"[^>]*hx-get=', text)
    assert buttons, "the translation toggle buttons not found in _card_title.html"
    for classes in buttons:
        assert (
            "act-btn" in classes.split()
        ), f"a button without .act-btn: class={classes!r}"
        assert (
            "underline" not in classes.split()
        ), f"the old class is not removed: class={classes!r}"


def test_plans_faq_reuses_faq_item_component():
    """The FAQ on /plans was on bare Tailwind classes without a tap zone (the summary
    24px) — the fix reuses .faq-item/.faq-item__q (already >2rem high, used in help.html), instead of introducing a new component.
    """
    text = (TEMPLATES_DIR / "plans.html").read_text(encoding="utf-8")
    assert 'class="faq-item"' in text
    assert 'class="faq-item__q"' in text
