"""Anti-regression (FIX-PLANS-CSS): every static CSS class from the templates
must be defined in app.css (a design-system component or a row of the Tailwind subset). It protects against a repeat of the bug “/plans falls apart” — the class is in the template but silently does not apply, because it is missing from the compiled (frozen) Tailwind subset.

The same check — for classes that JS sets via `.className = '…'`
and `classList.add|remove|toggle(...)` (`src/api/static/js/*.js`). There is no
Tailwind build, so “the class is not in the subset” is a real, not hypothetical, bug: cpv_widget.js set classes outside app.css five times, and they silently did not apply (the CPV translation stuck to the title without a space, see CLAUDE.md).

Dynamic/third-party classes that are not CSS utilities and must not be defined in app.css — in an explicit WHITELIST below, with a reason.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = ROOT / "src" / "api" / "templates"
JS_DIR = ROOT / "src" / "api" / "static" / "js"
APP_CSS = ROOT / "src" / "api" / "static" / "css" / "app.css"

# Classes that are not CSS utilities (third-party JS hook etc.) — deliberately
# not defined in app.css.
WHITELIST = {
    "cf-turnstile",  # hook of the Cloudflare Turnstile widget (signup.html), the widget sets its own styles
}

# Vendored JS files — not our code, className/classList are not checked in them.
JS_VENDOR_EXCLUDE = {
    "htmx.min.js": "вендорный htmx, служебные классы задаёт сам (.htmx-request уже в app.css)",
    "gsap.min.js": "вендорная анимационная библиотека, className/classList не использует",
}

_CLASS_ATTR_RE = re.compile(r'class\s*=\s*"([^"]*)"', re.DOTALL)
_JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)
_CSS_SELECTOR_RE = re.compile(r"\.((?:[a-zA-Z0-9_-]|\\.)+)")

# String literals (single/double quotes, with \'/\" inside) —
# are used so that only the literal part is taken from the concatenation `'a ' + b`
# 'a ' and do not invent a class from the variable b.
_STRING_LIT_RE = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")
_CLASSNAME_ASSIGN_RE = re.compile(r"\.className\s*=\s*([^;\n]+)")
_CLASSLIST_ADD_REMOVE_RE = re.compile(r"\.classList\.(?:add|remove)\(([^)]*)\)")
# Only the FIRST argument of toggle() is a class; the second (`cond`) is a boolean condition,
# not a class (regression case: `classList.toggle("is-peek", on)`, `.toggle("is-visible", y)`
# were caught falsely as the classes "on"/"y" — the second argument does not get here at all).
_CLASSLIST_TOGGLE_RE = re.compile(r"\.classList\.toggle\(([^)]*)\)")


def _literal_classes(fragment: str) -> set[str]:
    classes: set[str] = set()
    for m in _STRING_LIT_RE.finditer(fragment):
        literal = m.group(1) if m.group(1) is not None else m.group(2)
        classes.update(literal.split())
    return classes


def _classes_used_in_js_text(text: str) -> list[tuple[int, str]]:
    """[(string, class), ...] by .className = / classList.add|remove|toggle."""
    occurrences: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in _CLASSNAME_ASSIGN_RE.finditer(line):
            occurrences.extend((lineno, c) for c in _literal_classes(m.group(1)))
        for m in _CLASSLIST_ADD_REMOVE_RE.finditer(line):
            occurrences.extend((lineno, c) for c in _literal_classes(m.group(1)))
        for m in _CLASSLIST_TOGGLE_RE.finditer(line):
            first_arg = m.group(1).split(",", 1)[0]
            occurrences.extend((lineno, c) for c in _literal_classes(first_arg))
    return occurrences


def _classes_used_in_templates() -> dict[str, set[str]]:
    """path -> set of static classes met in class="..." (Jinja tags inside the attribute are cut out — both branches {% if %}/{% else %} remain as literals, purely dynamic {{ expr }} are cut out entirely)."""
    result: dict[str, set[str]] = {}
    for path in sorted(TEMPLATES_DIR.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        classes: set[str] = set()
        for m in _CLASS_ATTR_RE.finditer(text):
            cleaned = _JINJA_RE.sub(" ", m.group(1))
            classes.update(cleaned.split())
        if classes:
            result[str(path.relative_to(TEMPLATES_DIR))] = classes
    return result


def _classes_defined_in_css() -> set[str]:
    css_text = APP_CSS.read_text(encoding="utf-8")
    defined = set()
    for m in _CSS_SELECTOR_RE.finditer(css_text):
        unescaped = re.sub(r"\\(.)", r"\1", m.group(1))
        defined.add(unescaped)
    return defined


def test_all_static_template_classes_are_defined_in_app_css():
    defined = _classes_defined_in_css()
    used_by_file = _classes_used_in_templates()

    missing_by_file: dict[str, list[str]] = {}
    for template_name, classes in used_by_file.items():
        missing = sorted(c for c in classes if c not in defined and c not in WHITELIST)
        if missing:
            missing_by_file[template_name] = missing

    assert not missing_by_file, (
        "Template classes without a definition in app.css (see WHITELIST for "
        "legitimate exceptions): "
        f"{missing_by_file}"
    )


def _classes_used_in_js() -> list[tuple[str, int, str]]:
    """[(file, line, class), ...] over all non-vendored src/api/static/js/*.js."""
    occurrences: list[tuple[str, int, str]] = []
    for path in sorted(JS_DIR.glob("*.js")):
        if path.name in JS_VENDOR_EXCLUDE:
            continue
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(ROOT))
        occurrences.extend(
            (rel, lineno, cls) for lineno, cls in _classes_used_in_js_text(text)
        )
    return occurrences


def test_all_js_widget_classes_are_defined_in_app_css():
    defined = _classes_defined_in_css()
    missing = [
        f"{rel}:{lineno} .{cls}"
        for rel, lineno, cls in _classes_used_in_js()
        if cls not in defined and cls not in WHITELIST
    ]
    assert not missing, (
        "JS sets classes that are not in app.css (they silently do not apply — "
        "there is no Tailwind build, see WHITELIST/JS_VENDOR_EXCLUDE for "
        f"legitimate exceptions): {missing}"
    )


def test_classlist_toggle_second_argument_is_not_treated_as_class():
    """Regression case: classList.toggle(cls, cond) — cond is NOT a class.

    Real product lines (cockpit_calendar.js, scroll_nav.js) — the second argument is a variable ('on'/'y'), not a literal; a naive scan by classList.
    would take them for the classes 'on'/'y' and would require them to be added to app.css.
    """
    text = (
        'cell.classList.toggle("is-peek", on);\n'
        "btn.classList.toggle('is-visible', y);\n"
    )
    found = {cls for _lineno, cls in _classes_used_in_js_text(text)}
    assert found == {"is-peek", "is-visible"}
    assert "on" not in found
    assert "y" not in found
