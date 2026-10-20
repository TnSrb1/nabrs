"""Navigation polish after #589: the “More” menu at 390px and the tap zone of the link rows.

Expectations — from the MEASUREMENT scripts/shots (390px, sr/ru/en): before the fix the menu went beyond the left edge (left −36 ru / −57 sr / −130 en), after — overflowingNodes is empty; the “sources” rows were 16–17px high, after — not less than 2rem. The test guards the markup/rules that gave this measurement; the render itself is checked by shot.sh.
"""

from __future__ import annotations

import hashlib
import base64
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "src/api/static/css/app.css").read_text(encoding="utf-8")
TPL = ROOT / "src/api/templates"


def _mobile_block() -> str:
    start = CSS.index("@media (max-width: 639px) {\n  /* Бренд+языки")
    return CSS[start : start + 12000]


def test_mobile_menu_spans_header_not_details():
    m = _mobile_block()
    assert ".site-header--cabinet .nav-more { position: static; }" in m
    menu = re.search(r"\.site-header--cabinet \.nav-more__menu \{(.*?)\}", m, re.S)
    assert menu, "there is no mobile rule for the “More” menu"
    body = menu.group(1)
    assert "left: var(--space-2)" in body and "right: var(--space-2)" in body
    assert "min-width: 0" in body


def test_desktop_menu_still_anchored_right():
    desk = re.search(r"\.nav-more__menu \{(.*?)\}", CSS, re.S).group(1)
    assert "right: 0" in desk and "min-width: 12.5rem" in desk


def test_sources_links_have_tap_zone_class():
    rule = re.search(r"\.src-link \{(.*?)\}", CSS).group(1)
    assert "min-height: 2rem" in rule and "inline-flex" in rule
    for name in ("_cockpit_feed_teaser.html", "tenderi_hub.html"):
        assert re.search(
            r'<a href="/izvori" class="src-link ', (TPL / name).read_text("utf-8")
        ), name


def test_sri_matches_css_in_both_bases():
    digest = (
        "sha384-"
        + base64.b64encode(hashlib.sha384(CSS.encode("utf-8")).digest()).decode()
    )
    for name in ("base.html", "public_base.html"):
        assert digest in (TPL / name).read_text("utf-8"), name


# ── #581 / #592: header geometry and the “More” menu (expectations — from the shots measurement 03.10) ─
def _css() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parents[2]
        / "src"
        / "api"
        / "static"
        / "css"
        / "app.css"
    ).read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    start = css.index(selector + " {")
    return css[start : css.index("}", start)]


def test_desktop_more_menu_is_height_capped_and_scrolls():
    """#592: a window below ~340px cut the menu at 336px — a ceiling and scrolling are needed
    already in the BASE rule (≥640px), not only in the mobile @media."""
    rule = _rule(_css(), ".nav-more__menu")
    assert "max-height: calc(100dvh - var(--header-h)" in rule
    assert "overflow-y: auto" in rule


def test_mobile_header_langs_move_to_brand_row_and_header_h_matches_measure():
    """#581: languages — into the brand row (order), --header-h = the worst-case measurement
    (145px → 9.1rem), not the former 3 rows of 149px (9.5rem)."""
    css = _css()
    assert "--header-h: 9.1rem;" in css and "--header-h: 9.5rem;" not in css
    assert ".site-header--cabinet .site-header__actions { display: contents; }" in css
    assert ".site-header--cabinet .lang-switch { order: 1; margin-left: auto; }" in css
