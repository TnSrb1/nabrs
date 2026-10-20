"""The banner “enable the source in all templates” and the figure “you have K of N”.

Pure functions — without DB (``plan_enable``, ``pending_sources``, ``coverage``).
The dashboard render is live (route + Jinja), services are substituted. Expectations — literals: the text of the ru catalog, dates and counters from the fixture, not via ``t()``.
The tenant isolation under RLS — ``tests/db/test_source_banner_rls.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import feed_service, routes_profiles, source_banner
from src.api.config import get_cabinet_settings
from src.api.main import app
from src.domain.sources import SourceInfo
from tests.api.conftest import FakeSession
from tests.api.test_dashboard_v2_layout import _stub

SECRET = get_cabinet_settings().cabinet_secret_key
SESS = {"uid": "u-1", "tid": "t-1", "iat": 1_700_000_000}
JN_OPT = "2026-09-01T08:00:00+00:00"
NOW = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
NIS_AT = datetime(2026, 10, 16, 9, 40, tzinfo=timezone.utc)
TED_AT = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)

SIX_ENABLED = ["jnportal", "ted_balkan", "cejn", "cfcu", "eeas", "nis"]


@pytest.fixture(autouse=True)
def _no_open_now_hints(monkeypatch):
    """The hint “already open N” is not the subject of these tests; the profile stubs are incomplete.

    Fresh templates (created_at=NOW, without deliveries) would otherwise get into the batch preview_counts and would require the full payload_from_profile contract.
    """
    monkeypatch.setattr(routes_profiles, "_open_now_hints", _async({}))


def _profile(pid, keys, opt, *, match_all=False):
    return SimpleNamespace(
        id=pid,
        name=f"Шаблон {pid}",
        is_active=True,
        match_all=match_all,
        enabled_source_keys=list(keys),
        source_opt_in=dict(opt),
        deleted_at=None,
        created_at=NOW,
    )


def _src(key, name, countries=("RS",)):
    return SourceInfo(key, name, tuple(countries), None, enabled=True)


NIS = _src("nis", "NIS", ("RS",))
TED = _src("ted_balkan", "TED Balkan", ("BA", "ME", "MK", "RS"))


# --- pure functions ---------------------------------------------------------- #


def test_plan_enable_adds_only_where_missing_and_keeps_old_opt_in():
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    b = _profile(
        "b",
        ["jnportal", "nis"],
        {"jnportal": JN_OPT, "nis": "2026-10-01T00:00:00+00:00"},
    )
    changed = source_banner.plan_enable([a, b], "nis", ["jnportal", "nis"], NOW)
    assert changed == [a]
    assert a.enabled_source_keys == ["jnportal", "nis"]
    assert a.source_opt_in == {"jnportal": JN_OPT, "nis": NOW.isoformat()}
    # An already enabled template — the subscription moment is the PREVIOUS one, not overwritten.
    assert b.source_opt_in["nis"] == "2026-10-01T00:00:00+00:00"


def test_plan_enable_is_idempotent():
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    source_banner.plan_enable([a], "nis", ["jnportal", "nis"], NOW)
    before = dict(a.source_opt_in)
    later = NOW + timedelta(days=1)
    assert source_banner.plan_enable([a], "nis", ["jnportal", "nis"], later) == []
    assert a.source_opt_in == before


def test_plan_enable_refuses_source_not_in_registry():
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    assert source_banner.plan_enable([a], "nis", ["jnportal"], NOW) == []
    assert a.enabled_source_keys == ["jnportal"]


def test_pending_shows_missing_and_newer_than_seen_newest_first():
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    entries = [(TED, TED_AT), (NIS, NIS_AT)]
    got = source_banner.pending_sources(entries, [a], None)
    assert [p.source_key for p in got] == ["nis", "ted_balkan"]
    assert got[0].missing_in == 1 and got[0].countries == ("RS",)
    later = source_banner.pending_sources(entries, [a], TED_AT + timedelta(hours=1))
    assert [p.source_key for p in later] == ["nis"]


def test_pending_hidden_when_seen_after_enable_or_all_included():
    a = _profile(
        "a", ["jnportal", "nis"], {"jnportal": JN_OPT, "nis": NIS_AT.isoformat()}
    )
    assert source_banner.pending_sources([(NIS, NIS_AT)], [a], None) == []
    b = _profile("b", ["jnportal"], {"jnportal": JN_OPT})
    assert (
        source_banner.pending_sources(
            [(NIS, NIS_AT)], [b], NIS_AT + timedelta(minutes=1)
        )
        == []
    )


def test_pending_marks_match_all_and_hides_without_templates():
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT}, match_all=True)
    (got,) = source_banner.pending_sources([(NIS, NIS_AT)], [a], None)
    assert got.has_match_all is True
    assert source_banner.pending_sources([(NIS, NIS_AT)], [], None) == []


def test_coverage_is_honest_k_of_n_literal():
    """Fixture: six enabled sources, in the single template one → “1 of 6”."""
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    assert source_banner.coverage([a], SIX_ENABLED) == (1, 6)


def test_coverage_ignores_keys_no_longer_enabled():
    """A disabled/deleted source in the template does not make K greater than N."""
    a = _profile("a", ["jnportal", "gone_source"], {})
    b = _profile("b", ["jnportal", "nis"], {})
    assert source_banner.coverage([a, b], SIX_ENABLED) == (2, 6)


def test_parse_seen_rejects_garbage():
    assert source_banner.parse_seen(None) is None
    assert source_banner.parse_seen("вчера") is None
    assert source_banner.parse_seen("2026-10-16T10:00:00") == NOW.replace(
        tzinfo=timezone.utc
    )


# --- the dashboard render and the POST routes ------------------------------- #


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _client(profiles, entries, monkeypatch, *, cookie=None):
    _stub(monkeypatch)
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async(profiles))
    monkeypatch.setattr(source_banner, "load_enabled_sources", _async(entries))
    # The write goes only to a source from the registry: the fake registry = those enabled in entries.
    monkeypatch.setattr(
        source_banner, "list_enabled_sources", _async([e for e, _ in entries])
    )
    monkeypatch.setattr(feed_service, "get_feed_items", _async([]))
    app.dependency_overrides[deps.get_session_factory] = lambda: _factory
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(
        "tb_session", security.sign_session(SESS["uid"], SESS["tid"], SECRET)
    )
    if cookie:
        client.cookies.set(source_banner.SEEN_COOKIE, cookie)
    return client


def _async(value):
    async def _stub_fn(*a, **k):
        return value

    return _stub_fn


def _factory():
    return FakeSession()


def test_dashboard_shows_banner_with_plan_and_match_all_warning(monkeypatch):
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT}, match_all=True)
    html = _client([a], [(NIS, NIS_AT)], monkeypatch).get("/app").text
    assert 'data-sources-banner="nis"' in html
    assert "Включить во всех шаблонах" in html
    assert "Не сейчас" in html
    assert "до 30 в день" in html
    assert 'action="/app/izvori/vklyuchit"' in html
    assert 'action="/app/izvori/skip"' in html


def test_dashboard_hides_banner_after_not_now(monkeypatch):
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    seen = (NIS_AT + timedelta(minutes=5)).isoformat()
    html = _client([a], [(NIS, NIS_AT)], monkeypatch, cookie=seen).get("/app").text
    assert 'data-sources-banner="' not in html


def test_dashboard_hides_banner_when_everything_included(monkeypatch):
    a = _profile(
        "a", ["jnportal", "nis"], {"jnportal": JN_OPT, "nis": NIS_AT.isoformat()}
    )
    html = _client([a], [(NIS, NIS_AT)], monkeypatch).get("/app").text
    assert 'data-sources-banner="' not in html


def test_skip_sets_seen_cookie_without_writing_profiles(monkeypatch):
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    client = _client([a], [(NIS, NIS_AT)], monkeypatch)
    token = security.csrf_token(SESS, SECRET)
    r = client.post("/app/izvori/skip", data={"csrf": token})
    assert r.status_code == 303
    assert source_banner.SEEN_COOKIE in r.headers["set-cookie"]
    assert a.enabled_source_keys == ["jnportal"]


def test_enable_route_writes_all_live_templates_and_returns_fragment(monkeypatch):
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    b = _profile("b", ["jnportal"], {"jnportal": JN_OPT})
    client = _client([a, b], [(NIS, NIS_AT)], monkeypatch)
    monkeypatch.setattr(
        source_banner,
        "list_enabled_sources",
        _async([NIS]),
    )
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([a, b]))
    token = security.csrf_token(SESS, SECRET)
    r = client.post(
        "/app/izvori/vklyuchit",
        data={"csrf": token, "source_key": "nis"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "Включено в 2 шаблона" in r.text
    assert a.enabled_source_keys == ["jnportal", "nis"]
    assert b.enabled_source_keys == ["jnportal", "nis"]


def test_enable_route_rejects_bad_csrf_without_writing(monkeypatch):
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    client = _client([a], [(NIS, NIS_AT)], monkeypatch)
    r = client.post("/app/izvori/vklyuchit", data={"csrf": "nope", "source_key": "nis"})
    assert r.status_code == 303
    assert a.enabled_source_keys == ["jnportal"]


def test_dashboard_coverage_line_uses_k_of_n(monkeypatch):
    """The link “Your sources: K of N” on the dashboard: 1 of 6 (the fixture SIX_ENABLED)."""
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    entries = [(_src(k, k), NOW) for k in SIX_ENABLED]
    html = _client([a], entries, monkeypatch).get("/app").text
    assert "Ваши площадки: 1 из 6 →" in html
    assert "Собираем с площадок" not in html


# --- disabled templates (is_active=false) are not touched and not counted ----------- #


def _inactive(pid, keys, opt):
    p = _profile(pid, keys, opt)
    p.is_active = False
    return p


def test_plan_enable_skips_inactive_template_literally_unchanged():
    """“Enable in all” — only in the LIVE templates; a disabled one does not change at all."""
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    off = _inactive("off", ["jnportal"], {"jnportal": JN_OPT})
    changed = source_banner.plan_enable([a, off], "nis", ["jnportal", "nis"], NOW)
    assert changed == [a]
    assert off.enabled_source_keys == ["jnportal"]
    assert off.source_opt_in == {"jnportal": JN_OPT}


def test_pending_counts_only_active_templates_missing_source():
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    off = _inactive("off", ["jnportal"], {"jnportal": JN_OPT})
    (got,) = source_banner.pending_sources([(NIS, NIS_AT)], [a, off], None)
    assert got.missing_in == 1


def test_pending_hidden_when_only_inactive_template_lacks_source():
    """A disabled template without a source does not raise the banner: there are no deliveries there anyway."""
    a = _profile("a", ["jnportal", "nis"], {"jnportal": JN_OPT, "nis": JN_OPT})
    off = _inactive("off", ["jnportal"], {"jnportal": JN_OPT})
    assert source_banner.pending_sources([(NIS, NIS_AT)], [a, off], None) == []


def test_coverage_ignores_inactive_template_keys():
    """K — only by the live templates: a disabled one with a source does not give “2 of 2”."""
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    off = _inactive("off", ["jnportal", "nis"], {"jnportal": JN_OPT, "nis": JN_OPT})
    assert source_banner.coverage([a, off], ["jnportal", "nis"]) == (1, 2)


def test_enable_route_leaves_inactive_template_untouched(monkeypatch):
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    off = _inactive("off", ["jnportal"], {"jnportal": JN_OPT})
    client = _client([a, off], [(NIS, NIS_AT)], monkeypatch)
    token = security.csrf_token(SESS, SECRET)
    r = client.post(
        "/app/izvori/vklyuchit",
        data={"csrf": token, "source_key": "nis"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "Включено в 1 шаблон" in r.text
    assert a.enabled_source_keys == ["jnportal", "nis"]
    assert off.enabled_source_keys == ["jnportal"]
    assert off.source_opt_in == {"jnportal": JN_OPT}


# --- htmx with a wrong CSRF: a fragment, not a full page ------------------- #


def test_enable_route_htmx_bad_csrf_returns_error_fragment(monkeypatch):
    """An HX request with a bad CSRF: a 303 to /app would insert the whole page into #src-banner."""
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    client = _client([a], [(NIS, NIS_AT)], monkeypatch)
    r = client.post(
        "/app/izvori/vklyuchit",
        data={"csrf": "nope", "source_key": "nis"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert 'id="src-banner"' in r.text
    assert "Эта площадка сейчас недоступна для включения." in r.text
    assert "location" not in r.headers
    assert a.enabled_source_keys == ["jnportal"]


def test_banner_total_counts_only_live_templates_not_disabled(monkeypatch):
    """The denominator “not enabled in M of N” — only the LIVE templates: 1 active + 1 disabled → “of 1”."""
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    off = _inactive("off", ["jnportal"], {"jnportal": JN_OPT})
    html = _client([a, off], [(NIS, NIS_AT)], monkeypatch).get("/app").text
    assert 'data-sources-banner="nis"' in html
    assert "из 1 шаблонов" in html
    assert "из 2 шаблонов" not in html


def test_skip_bad_csrf_htmx_returns_empty_200_not_redirect(monkeypatch):
    """htmx inserts the response into #src-banner: a 303 to /app would drag the whole page inside the block."""
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    client = _client([a], [(NIS, NIS_AT)], monkeypatch)
    r = client.post(
        "/app/izvori/skip", data={"csrf": "nope"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200
    assert r.text == ""
    assert "location" not in r.headers
    assert "set-cookie" not in r.headers


def test_skip_bad_csrf_plain_form_still_redirects_to_app(monkeypatch):
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    client = _client([a], [(NIS, NIS_AT)], monkeypatch)
    r = client.post("/app/izvori/skip", data={"csrf": "nope"})
    assert r.status_code == 303
    assert r.headers["location"] == "/app"


@pytest.mark.parametrize(
    "path,data",
    [
        ("/app/izvori/vklyuchit", {"source_key": "nis", "csrf": "x"}),
        ("/app/izvori/skip", {"csrf": "x"}),
    ],
)
def test_no_session_htmx_gets_hx_redirect_to_login(monkeypatch, path, data):
    """No session + htmx: a 303 to the login would drag the login page into the block → HX-Redirect."""
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    client = _client([a], [(NIS, NIS_AT)], monkeypatch)
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    r = client.post(path, data=data, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect", "").startswith("/")
    assert "location" not in r.headers
    assert a.enabled_source_keys == ["jnportal"]


@pytest.mark.parametrize(
    "path,data",
    [
        ("/app/izvori/vklyuchit", {"source_key": "nis", "csrf": "x"}),
        ("/app/izvori/skip", {"csrf": "x"}),
    ],
)
def test_no_session_plain_form_still_redirects_to_login(monkeypatch, path, data):
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    client = _client([a], [(NIS, NIS_AT)], monkeypatch)
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    r = client.post(path, data=data)
    assert r.status_code == 303
    assert "HX-Redirect" not in r.headers


def test_banner_capability_label_names_what_source_gives_not_daje(monkeypatch):
    """The caption “What it gives” — “Data: rok” (ru); the sr “Daje” is not a caption, but a fragment of a verb."""
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT})
    html = _client([a], [(NIS, NIS_AT)], monkeypatch).get("/app").text
    assert "Данные: " in html
    assert "Что даёт" not in html


def test_banner_extra_lines_not_larger_than_body_text(monkeypatch):
    """The extra lines of the banner — text-sm, like the main text: not larger."""
    a = _profile("a", ["jnportal"], {"jnportal": JN_OPT}, match_all=True)
    html = _client([a], [(NIS, NIS_AT)], monkeypatch).get("/app").text
    assert 'class="alert__extra"' not in html
    assert 'class="alert__extra text-sm"' in html
