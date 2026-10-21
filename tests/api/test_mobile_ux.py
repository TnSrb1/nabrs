"""Mobile layout + skip-link (Pack 3, design audit).

The CSS here is behavior, not cosmetics: the breakpoints decide whether the content fits on a phone, and the skip-link is keyboard accessibility. Therefore we check by the file (app.css + templates), not by eye. Boundaries: palette/CSP/matching are not touched.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CSS = (_ROOT / "src" / "api" / "static" / "css" / "app.css").read_text(
    encoding="utf-8"
)
_TPL = _ROOT / "src" / "api" / "templates"


def _rule(selector: str) -> str:
    bodies = re.findall(
        r"(?:^|\n)" + re.escape(selector) + r"\s*\{([^}]*)\}", _CSS, flags=re.S
    )
    assert bodies, f"rule {selector} not found in app.css"
    return "\n".join(bodies)


def _media_blocks(cond: str) -> list[str]:
    """The bodies of all @media (<cond>) { ... } — up to the closing brace of the block (in column 0).
    Nested one-line rules with an indented `}` do not break the boundary."""
    return re.findall(
        r"@media \(" + re.escape(cond) + r"\) \{(.*?)\n\}", _CSS, flags=re.S
    )


# --------------------------------------------------------------------------- #
# B1 — plans: 1 column, 3 columns only from 768px (own .plan-grid, not sm:grid-cols-3)
# --------------------------------------------------------------------------- #
def test_plans_use_custom_grid_not_sm_cols_3():
    plans = (_TPL / "plans.html").read_text(encoding="utf-8")
    assert "plan-grid" in plans
    assert "sm:grid-cols-3" not in plans  # the old breakpoint (640px) is removed


def test_plan_grid_is_single_col_then_row_at_768():
    assert "grid-template-columns: 1fr" in _rule(".plan-grid")
    at_768 = "\n".join(_media_blocks("min-width: 768px"))
    assert re.search(r"\.plan-grid\s*\{\s*grid-auto-flow:\s*column", at_768)


# --------------------------------------------------------------------------- #
# B2 — ck-hero: 2 columns from 640px, 4 — only from 1024px
# --------------------------------------------------------------------------- #
def test_ck_hero_grid_is_three_tiles_in_a_row_at_every_width():
    """v2 (owner 13.10): THREE tiles in the hero, always in one row.

    The former contract “2 columns from 640, 4 — from 1024” was about four tiles; there are three now (the subscription term is removed), and 2×2 with one orphan would look like a hole.
    """
    assert re.search(r"grid-template-columns:\s*repeat\(3", _rule(".ck-hero__grid"))
    for width in ("min-width: 640px", "min-width: 1024px"):
        block = "\n".join(_media_blocks(width))
        assert not re.search(
            r"\.ck-hero__grid\s*\{\s*grid-template-columns:", block
        ), width


# --------------------------------------------------------------------------- #
# B3 — help-steps: 3 columns only from 768px (not from 640px)
# --------------------------------------------------------------------------- #
def test_help_steps_three_cols_moved_to_768():
    at_640 = "\n".join(_media_blocks("min-width: 640px"))
    at_768 = "\n".join(_media_blocks("min-width: 768px"))
    assert re.search(r"\.help-steps\s*\{\s*grid-template-columns:\s*repeat\(3", at_768)
    assert not re.search(
        r"\.help-steps\s*\{\s*grid-template-columns:\s*repeat\(3", at_640
    )


# --------------------------------------------------------------------------- #
# B4 — the cabinet header on the phone: tap zone 2.75rem, “Plans”/“Logout” in the dropdown
# --------------------------------------------------------------------------- #
def test_mobile_header_nav_link_has_full_tap_target():
    narrow = "\n".join(_media_blocks("max-width: 639px"))
    assert "--header-h" in narrow  # the header rhythm is fixed together with the layout
    assert "min-height: 2.75rem" in narrow  # the tap norm (was ~26px)
    assert ".site-header .nav-hide-mobile" in narrow
    assert ".site-header .nav-show-mobile" in narrow


def test_mobile_dropdown_copies_hidden_on_desktop():
    # On desktop the copies of “Plans”/“Logout” inside the menu are hidden — the item is not doubled.
    assert re.search(r"\.nav-more__menu \.nav-show-mobile\s*\{\s*display:\s*none", _CSS)


def test_base_has_single_mobile_menu_and_hides_desktop_row():
    base = (_TPL / "_cabinet_nav.html").read_text(encoding="utf-8")
    # the desktop row and the logout are hidden on the phone…
    assert 'action="/logout" class="whoami-logout-form nav-hide-mobile"' in base
    assert 'class="nav-more nav-hide-mobile"' in base
    # …instead of them ONE “Meni” menu with the groups and the language inside.
    assert base.count('class="nav-more nav-mobile"') == 1
    assert base.count("nav-group-title") == 3
    # Cenovnik/Izvori/Novosti — not in the cabinet header (the footer of base.html).
    assert 'href="/plans"' not in base and 'href="/izvori"' not in base


# --------------------------------------------------------------------------- #
# B5 — .seg: on the phone a horizontal scroll tab, on desktop a wrap
# --------------------------------------------------------------------------- #
def test_seg_scrolls_horizontally_on_mobile_only():
    # desktop: wrap
    assert "flex-wrap: wrap" in _rule(".seg")
    # phone: nowrap + overflow-x
    narrow = "\n".join(_media_blocks("max-width: 639px"))
    assert re.search(r"\.seg\s*\{[^}]*flex-wrap:\s*nowrap", narrow)
    assert re.search(r"\.seg\s*\{[^}]*overflow-x:\s*auto", narrow)


# --------------------------------------------------------------------------- #
# B6 — the skip-link is the FIRST element of body in BOTH layouts + id="main"
# --------------------------------------------------------------------------- #
def test_skip_link_is_first_body_element_in_both_layouts():
    for name, key in (
        ("base.html", "t.skip_link"),
        ("public_base.html", "ui.skip_link"),
    ):
        html = (_TPL / name).read_text(encoding="utf-8")
        body_inner = html.split("<body", 1)[1].split(">", 1)[1]
        # the first non-empty/non-comment node — the skip-link itself
        stripped = re.sub(r"\{#.*?#\}", "", body_inner, flags=re.S).lstrip()
        assert stripped.startswith(
            f'<a href="#main" class="skip-link">{{{{ {key} }}}}</a>'
        ), f"skip-link is not first in {name}"
        assert 'id="main"' in html


def test_skip_link_styled_offscreen_and_returns_on_focus():
    body = _rule(".skip-link")
    assert "position: fixed" in body
    assert "translateY(-120%)" in body  # moved beyond the top of the screen
    z = int(re.search(r"z-index:\s*(\d+)", body).group(1))
    header_z = int(re.search(r"z-index:\s*(\d+)", _rule(".site-header")).group(1))
    assert z > header_z  # above the sticky header
    assert ".skip-link:focus" in _CSS
    assert "translateY(0)" in _rule(".skip-link:focus")  # returns on focus


def test_skip_link_key_in_all_langs_both_catalogs():
    from src.api.cabinet.i18n import CABINET, SUPPORTED
    from src.api.public.i18n import UI

    for lang in SUPPORTED:
        assert CABINET[lang].get("skip_link"), f"cabinet skip_link is empty: {lang}"
        assert UI[lang].get("skip_link"), f"public skip_link is empty: {lang}"


# --------------------------------------------------------------------------- #
# B7 — funnel buttons: a secondary look (act-btn), not the blue badge-new
# --------------------------------------------------------------------------- #
def test_funnel_actions_use_secondary_button_not_badge_new():
    tpl = (_TPL / "_tender_actions.html").read_text(encoding="utf-8")
    # “Follow” (action) — act-btn, not badge-new.
    assert 'class="act-btn"' in tpl
    # No static blue “action” is left (the funnel stage is colored badge-new
    # dynamically by status — it is a state indicator, not an action button).
    assert "badge badge-new cursor-pointer" not in tpl


def test_act_btn_is_secondary_with_tap_target():
    body = _rule(".act-btn")
    assert "min-height: 2rem" in body  # tap zone ≥2rem
    # secondary look: neutral background + primary text (not the status blue)
    assert "var(--color-neutral-0)" in body
    assert "var(--color-primary-800)" in body


# --------------------------------------------------------------------------- #
# B8 — the public header (public_base.html) on the phone: the brand/languages/navigation do not
# leave the viewport (it was scrollWidth 510 > 390 after the tap zones #446). We fix it
# by the LAYOUT — the line wrap; the tap zones are not touched.
# --------------------------------------------------------------------------- #
def test_public_header_marks_scoping_classes():
    html = (_TPL / "public_base.html").read_text(encoding="utf-8")
    # The header is marked with a modifier + the wrap carriers.
    assert 'class="site-header site-header--public"' in html
    assert "site-header__bar" in html
    assert "site-header__actions" in html
    # The languages are moved out of the navigation block into a separate row node.
    assert re.search(
        r'</a>\s*\{#.*?#\}\s*<nav class="lang-switch"', html, flags=re.S
    ), "lang-switch must be a neighbour of .brand, not inside .site-header__actions"


def test_public_header_wraps_on_mobile_not_desktop():
    # Desktop: languages + navigation pressed to the right (margin-left:auto for the languages).
    assert re.search(
        r"\.site-header--public \.lang-switch\s*\{[^}]*margin-left:\s*auto", _CSS
    )
    # Phone: the header row wraps, the actions block goes to the full width.
    narrow = "\n".join(_media_blocks("max-width: 639px"))
    assert re.search(
        r"\.site-header--public \.site-header__bar\s*\{[^}]*flex-wrap:\s*wrap", narrow
    )
    assert re.search(
        r"\.site-header--public \.site-header__actions\s*\{[^}]*flex-basis:\s*100%",
        narrow,
    )


# --------------------------------------------------------------------------- #
# B9 — the comparison tables (/plans) and the customer page (/cabinet/narucilac) scroll
# INSIDE the wrapper (overflow-x-auto), they do not pull the body. The class is in the compiled subset.
# --------------------------------------------------------------------------- #
def test_wide_tables_wrapped_in_overflow_scroller():
    for name in ("plans.html", "narucilac.html"):
        html = (_TPL / name).read_text(encoding="utf-8")
        # Every <table class="w-full…"> is wrapped in a container with overflow-x-auto.
        for m in re.finditer(r'<table class="w-full', html):
            head = html[: m.start()]
            # the nearest <div …> to the table carries overflow-x-auto
            last_div = head.rfind("<div")
            assert (
                "overflow-x-auto" in head[last_div : m.start()]
            ), f"{name}: the table is not in overflow-x-auto — it will pull the body on the phone"
    # …and the utility is really present in the frozen subset (otherwise the class is silent).
    assert ".overflow-x-auto{overflow-x:auto}" in _CSS


# --------------------------------------------------------------------------- #
# B10 — the cockpit calendar does not inflate the column by the min-content of the wide agenda
# (it was cal__head/cal__grid beyond the edge, cockpit 442 > 390).
# --------------------------------------------------------------------------- #
def test_calendar_column_can_shrink_below_content():
    # On the phone (one-column grid) — minmax(0,1fr), NOT a bare 1fr.
    body = _rule(".cal")
    assert "grid-template-columns: minmax(0, 1fr)" in body
    assert "grid-template-columns: 1fr;" not in body  # a bare 1fr = min-content blowout
    # A long token in the agenda wraps, rather than pushing the column.
    assert "overflow-wrap: anywhere" in _rule(".agenda__title")
    assert "overflow-wrap: anywhere" in _rule(".agenda__meta")


# --------------------------------------------------------------------------- #
# B11 — the contextual hints (tip__bubble) do not go beyond the viewport edges.
# --------------------------------------------------------------------------- #
def test_tip_bubble_capped_to_viewport_width():
    body = _rule(".tip__bubble")
    # the width — not more than the viewport minus the margins (it was max-width: 16rem without a clamp).
    assert "max-width: min(16rem, calc(100vw - 2rem))" in body


def test_tip_bubble_clamped_to_viewport_on_mobile():
    narrow = "\n".join(_media_blocks("max-width: 639px"))
    # On the phone the bubble is pinned to the viewport (fixed + left/right margins) — it will not go
    # beyond the left (/profiles/new) or the right (cockpit) edge.
    tip = re.search(r"\.tip__bubble\s*\{([^}]*)\}", narrow)
    assert tip, "there is no mobile rule for .tip__bubble"
    body = tip.group(1)
    assert "position: fixed" in body
    assert "left: 1rem" in body
    assert "right: 1rem" in body


# --------------------------------------------------------------------------- #
# B12 — the CABINET header (base.html) on the phone: the brand tore into three lines
# (“Tender / Bot / Serbia”), the navigation wrapped on the right (owner audit 03.08,
# scrollWidth stayed 390 — the overflow metric does not see the defect at all).
# We fix it with the same wrap pattern as the public header (B8): the brand in the first
# row entirely, the navigation — in the second, full width.
# --------------------------------------------------------------------------- #
def test_cabinet_header_marks_scoping_classes():
    html = (_TPL / "_cabinet_nav.html").read_text(encoding="utf-8")
    assert 'class="site-header site-header--cabinet"' in html
    assert "site-header__bar" in html
    assert "site-header__actions" in html


def test_cabinet_header_wraps_on_mobile():
    narrow = "\n".join(_media_blocks("max-width: 639px"))
    assert re.search(
        r"\.site-header--cabinet \.site-header__bar\s*\{[^}]*flex-wrap:\s*wrap",
        narrow,
    )
    assert re.search(
        r"\.site-header--cabinet \.site-header__actions\s*\{[^}]*flex-basis:\s*100%",
        narrow,
    )


def test_cabinet_brand_does_not_shrink_or_wrap_text_on_mobile():
    """Without this the brand shrank below max-content and the text tore by words —
    exactly the audit defect (“Tender / Bot / Serbia” in three lines)."""
    narrow = "\n".join(_media_blocks("max-width: 639px"))
    body = re.search(r"\.site-header \.brand\s*\{([^}]*)\}", narrow)
    assert body, "there is no mobile rule for .site-header .brand"
    assert "flex-shrink: 0" in body.group(1)
    assert "white-space: nowrap" in body.group(1)


# --------------------------------------------------------------------------- #
# B13 — the help icon “?” (.tip__icon): 20×32, width below the norm 2rem
# (the same class of defect as .lang-pill in #480 and the footer a).
# --------------------------------------------------------------------------- #
def test_tip_icon_has_min_width_not_only_min_height():
    body = _rule(".tip__icon")
    assert "min-width: 2rem" in body


# --------------------------------------------------------------------------- #
# B14 — the items of the dropdowns “Stage”/“To folder” (_tender_actions.html): bare
# utilities gave ~24px height — below the norm 2rem.
# --------------------------------------------------------------------------- #
def test_act_menu_item_reaches_tap_target():
    assert "min-height: 2rem" in _rule(".act-menu-item")


def test_tender_actions_stage_and_folder_buttons_use_act_menu_item():
    tpl = (_TPL / "_tender_actions.html").read_text(encoding="utf-8")
    # The funnel stage + both variants of the folder button (add/remove).
    assert tpl.count("act-menu-item") == 3


# --------------------------------------------------------------------------- #
# B15 — “Folder name” (/app/my) and the tender note (_tender_actions.html):
# without a programmatic caption + 14px font (iOS Safari zooms and does not return).
# --------------------------------------------------------------------------- #
def test_folder_name_field_has_label_and_field_control():
    tpl = (_TPL / "my_tenders.html").read_text(encoding="utf-8")
    assert 'for="folder-name"' in tpl
    assert 'id="folder-name"' in tpl
    assert 'class="field-control"' in tpl
    assert (
        "border rounded px-3 py-2 text-sm" not in tpl
    )  # the old bare utilities are removed


def test_tender_note_textarea_has_aria_label_and_field_control():
    tpl = (_TPL / "_tender_actions.html").read_text(encoding="utf-8")
    assert 'name="body"' in tpl
    assert 'aria-label="{{ t.note_ph }}"' in tpl
    assert 'class="field-control"' in tpl


def test_folder_name_label_key_in_all_langs():
    from src.api.cabinet.i18n import CABINET, SUPPORTED

    for lang in SUPPORTED:
        assert CABINET[lang].get(
            "folder_name_label"
        ), f"folder_name_label is empty: {lang}"


# --------------------------------------------------------------------------- #
# B16 — false triggers of the measurement (shot.js): the teaser headings of the cards and
# the standalone back-links are fixed IN THE INSTRUMENT (exemptTargets), not in the
# product. The matching is by the SELECTOR/class, not by the visible text of the finding:
# otherwise the next tender with a different name will again falsely trigger.
# --------------------------------------------------------------------------- #
def test_shot_js_exempts_teaser_title_and_back_link_by_class_not_text():
    js = (_ROOT / "scripts" / "shots" / "shot.js").read_text(encoding="utf-8")
    assert '"font-medium hover:underline"' in js
    assert '"text-slate-600 underline"' in js
    # There must be no hardcoding of the specific findings of the measurement BY TEXT in the
    # comparison logic itself (the example comments above in the file do not count).
    assert 'includes("Електрична' not in js
    assert 'includes("Подробная справка' not in js
