"""Tests of the common Jinja2 renderer of the cabinet (ADR-007)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from src.api import security, templating
from src.api.config import get_cabinet_settings


def _request(*, cookie: str | None = None, **state) -> Request:
    """A minimal Request for the context processor: cookie + request.state."""
    headers = []
    if cookie is not None:
        headers.append((b"cookie", f"tb_session={cookie}".encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "query_string": b"",
            "state": dict(state),
        }
    )


def _valid_cookie() -> str:
    settings = get_cabinet_settings()
    return security.sign_session("u-1", "t-1", settings.cabinet_secret_key)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_days_left_none_returns_none():
    assert templating._days_left(None) is None


def test_days_left_far_future_counts_calendar_days():
    # The rule is calendar dates in Belgrade time (tests/api/test_days_left_calendar.py
    # with a frozen “now”); here — only the order of magnitude without binding to the hour
    # of the launch: +5 days by the clock = exactly 5 calendar dates ahead.
    assert templating._days_left(_now() + timedelta(days=5)) == 5


def test_days_left_past_is_negative():
    # A passed deadline → a negative number of days (the deadline has expired).
    assert templating._days_left(_now() - timedelta(days=3)) == -3


def test_days_left_just_passed_is_negative():
    # Passed a minute ago — expired, even if it was “today”.
    assert templating._days_left(_now() - timedelta(minutes=1)) < 0


def test_days_left_naive_datetime_treated_as_utc():
    # A naive datetime is treated as UTC — the same result as an aware one.
    aware = _now() + timedelta(days=2, hours=8)
    naive = aware.replace(tzinfo=None)
    assert templating._days_left(naive) == templating._days_left(aware)


def test_days_left_filter_registered_on_env():
    assert templating.templates.env.filters["days_left"] is templating._days_left


def test_templates_is_jinja2templates_instance():
    assert isinstance(templating.templates, Jinja2Templates)


def test_templates_dir_points_to_api_templates():
    expected = Path(templating.__file__).resolve().parent / "templates"
    assert templating._TEMPLATES_DIR == expected
    assert templating._TEMPLATES_DIR.is_dir()


def test_known_template_is_loadable():
    # base.html lies in the template directory of the cabinet — the engine must find it.
    tmpl = templating.templates.get_template("base.html")
    assert tmpl is not None


# --- _auth_context: the cabinet navigation must not leak to an anonymous user (SITE-02) -------


def test_auth_context_anonymous_without_cookie():
    # No cookie → the cabinet navigation is hidden. A regression here shows an anonymous user
    # the items “Profiles/Library/Logout” on /login and /signup.
    ctx = templating._auth_context(_request())
    assert ctx == {
        "authed": False,
        "csrf_token": "",
        "is_platform_admin": False,
        "whoami_email": None,
        "whoami_tenant_name": None,
        "nav_access": None,
        "access_frozen": False,
        "extension_pending": False,
    }


def test_auth_context_valid_session_is_authed():
    ctx = templating._auth_context(_request(cookie=_valid_cookie()))
    assert ctx["authed"] is True


def test_auth_context_tampered_cookie_is_anonymous():
    # A forged signature must not open the cabinet navigation.
    ctx = templating._auth_context(_request(cookie=_valid_cookie() + "x"))
    assert ctx["authed"] is False


def test_auth_context_garbage_cookie_is_anonymous():
    ctx = templating._auth_context(_request(cookie="not-a-session"))
    assert ctx["authed"] is False


def test_auth_context_expired_cookie_is_anonymous():
    # A cookie older than session_max_age → anonymous (TTL is checked by verify_session).
    settings = get_cabinet_settings()
    stale = security.sign_session("u-1", "t-1", settings.cabinet_secret_key, now=1)
    ctx = templating._auth_context(_request(cookie=stale))
    assert ctx["authed"] is False


def test_auth_context_admin_flag_read_from_request_state():
    # The flag resolves the async dependency and puts it into request.state; here — the read.
    ctx = templating._auth_context(
        _request(cookie=_valid_cookie(), is_platform_admin=True)
    )
    assert ctx["is_platform_admin"] is True


def test_auth_context_admin_flag_absent_when_state_unset():
    # The page is rendered bypassing the dependency (for example, by the 404 handler):
    # no flag → there is no “Admin” link (and not a fall by AttributeError).
    ctx = templating._auth_context(_request(cookie=_valid_cookie()))
    assert ctx["is_platform_admin"] is False


def test_auth_context_registered_as_context_processor():
    # The flags must reach EVERY template — otherwise base.html will not see them.
    assert templating._auth_context in templating.templates.context_processors
