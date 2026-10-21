"""The cabinet header: the language switcher — in the whoami row ABOVE the menu row (owner 11.10.2026).

Expectations — literals from the markup/measurement (scripts/shots/header_row_probe.js):
on desktop the language is in ``.site-header__identity``, not in ``.site-header__actions``,
otherwise the ru items + language did not fit in 960px and the language fell onto a third row.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_CSS = "src/api/static/css/app.css"


def teardown_function():
    app.dependency_overrides.clear()


def _header(locale: str) -> str:
    fs = FakeSession(results=[], tenant_tier="pro", tenant_status="active")
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": _UID,
        "tid": _TID,
    }
    c = TestClient(app, follow_redirects=False)
    secret = get_cabinet_settings().cabinet_secret_key
    c.cookies.set(
        "tb_session", security.sign_session(_UID, _TID, secret, locale=locale)
    )
    r = c.get(f"/help?lang={locale}")
    assert r.status_code == 200
    return re.search(r"<header.*?</header>", r.text, re.S).group(0)


def test_lang_switch_lives_in_identity_row_above_main_bar():
    head = _header("sr")
    ident = head.index('class="site-header__identity')
    bar = head.index('class="site-header__bar')
    lang = head.index('class="lang-switch nav-hide-mobile"')
    assert ident < lang < bar  # the language — inside the row above the menu row


def test_lang_switch_not_in_actions_row_for_authed():
    head = _header("ru")
    actions = head[head.index('class="site-header__actions') :]
    # only a copy inside the mobile “Meni” (.nav-menu-lang) remains in the menu row
    assert 'class="lang-switch nav-hide-mobile"' not in actions
    assert actions.count("lang-switch") == 1
    assert "nav-menu-lang" in actions


def test_css_keeps_logo_rigid_and_lang_right_in_identity_row():
    css = open(_CSS, encoding="utf-8").read()
    assert (
        ".site-header__identity .lang-switch { flex-shrink: 0; margin-left: auto; }"
        in css
    )
    assert (
        ".site-header--cabinet .brand { flex-shrink: 0; white-space: nowrap; }" in css
    )


def _css_block(css: str, selector: str) -> str:
    start = css.index("\n" + selector + " {") + 1
    return css[start : css.index("}", start) + 1]


def test_identity_row_spans_window_and_does_not_clip_focus_ring():
    """owner 11.10.2026: the language — to the right edge of the window (1rem), not to the container; the row
    does not clip the focus outline of .lang-pill (overflow:hidden was cutting it off).
    """
    css = open(_CSS, encoding="utf-8").read()
    block = _css_block(css, ".site-header__identity")
    assert "overflow: hidden" not in block
    assert "padding-right: 1rem;" in block
    assert "padding-left: max(1rem, calc((100% - 64rem) / 2 + 1rem));" in block
    # whoami truncates the email itself — overflow is not needed on the whole row
    assert "overflow: hidden" in _css_block(css, ".whoami")


def test_identity_container_not_capped_at_max_w_5xl():
    """The identification row must not be squeezed again into max-w-5xl: otherwise the language
    would move away from the window edge at 1920 (the right edge was 1456 instead of 1904).
    """
    head = _header("ru")
    ident = head[head.index('class="site-header__identity') :]
    ident = ident[: ident.index(">")]
    assert "max-w-5xl" not in ident
    assert "mx-auto" not in ident
