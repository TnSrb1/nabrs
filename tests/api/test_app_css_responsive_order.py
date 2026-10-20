"""The responsive utilities of app.css must stand AFTER the base ones (order = specificity).

Bug 13.10.2026: `.sm\\:flex-row` in the media block (640px) stood before `.flex-col`
(the base one, without media). Equal specificity → the last one wins, i.e.
`flex-col`, and on desktop the template rows always went into a column.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_CSS = ROOT / "src" / "api" / "static" / "css" / "app.css"


def _css() -> str:
    return APP_CSS.read_text(encoding="utf-8")


def test_sm_flex_row_overrides_base_flex_col():
    css = _css()
    base = css.index(".flex-col{")
    responsive = css.index(".sm\\:flex-row{")
    assert base < responsive, (
        ".sm\\:flex-row stands BEFORE the base .flex-col — at ≥640px the rows "
        "of the templates go into a column (see docs/reports/TESTING_dash-v2-1-fixes.md)"
    )


def test_sm_items_center_and_justify_between_follow_base_utilities():
    css = _css()
    for base_cls in (
        ".items-start{",
        ".items-center{",
        ".justify-center{",
        ".justify-between{",
    ):
        base = css.index(base_cls)
        for responsive_cls in (".sm\\:items-center{", ".sm\\:justify-between{"):
            assert base < css.index(responsive_cls), (
                f"{responsive_cls} stands before {base_cls} — "
                "the base utility will override the responsive one"
            )


def _enclosing_at_rules(css: str, selector: str) -> list[str]:
    """The stack of enclosing blocks (@media…) at the first rule `selector{`.

    The braces are counted over the whole file: an unclosed `@media` in a previous block
    (bug 13.10: `}}` was moved) shifts the stack and gives a second enclosing @media.
    """
    import re

    target = css.index(selector + "{")
    stack: list[str] = []
    last = 0
    for m in re.finditer(r"[{}]", css[:target]):
        seg = re.sub(r"/\*.*?\*/", "", css[last : m.start()], flags=re.S)
        last = m.end()
        if m.group(0) == "{":
            stack.append(seg.split(";")[-1].split("}")[-1].strip())
        else:
            stack.pop()
    seg = re.sub(r"/\*.*?\*/", "", css[last:target], flags=re.S)
    return [h for h in stack + [seg.split(";")[-1].strip()] if h.startswith("@media")]


def test_sm_640_utilities_nested_only_in_640_media():
    css = _css()
    for selector in (".sm\\:flex-row", ".sm\\:grid-cols-3"):
        media = _enclosing_at_rules(css, selector)
        assert media == ["@media (min-width:640px)"], (
            f"{selector} is enclosed by {media}, exactly one @media (min-width:640px) was expected "
            "— an unclosed brace in the previous block (see TESTING_dash-v2-1-fixes.md, section “Rework”)"
        )
