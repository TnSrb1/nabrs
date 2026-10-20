"""Guards of the cabinet i18n (Phase 1.1): catalog, resolver, switcher, chrome hook.

The DB and the session are mocked by dependency_overrides; the templates are rendered for real. The axis of the
INTERFACE language (users.locale) — not to be confused with the axis of the AI content (profiles.target_language).
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from src.api import deps
from src.api.cabinet import (
    feed_service,
    i18n,
    routes_library,
    routes_profiles,
    routes_subscription,
    source_choices,
    stats_service,
    workflow_service,
)
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession

_TID = "11111111-1111-1111-1111-111111111111"
_UID = "22222222-2222-2222-2222-222222222222"


def teardown_function():
    app.dependency_overrides.clear()


def _client(*, sess: dict | None = None, locale: str | None = None):
    """TestClient with a mocked session and users.locale returned from the DB."""

    def factory():
        # users.locale is returned by the FakeSession as a separate field (not consuming the queue) —
        # the same as set_config (see conftest.FakeSession.execute).
        return FakeSession(user_locale=locale)

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


def _req(query: bytes = b"", cookie: str | None = None, accept: str | None = None):
    headers = [(b"host", b"x")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    if accept:
        headers.append((b"accept-language", accept.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/app",
            "query_string": query,
            "headers": headers,
        }
    )


# --------------------------------------------------------------------------- #
# Parity of the catalog keys (pattern test_plans_i18n_key_parity)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("catalog", [i18n.CABINET, i18n.COMMON])
def test_cabinet_i18n_key_parity(catalog):
    """All three languages have ONE AND THE SAME set of keys — anti-drift of the translations."""
    assert set(catalog) == set(i18n.SUPPORTED)
    ref = set(catalog[i18n.DEFAULT])
    for lang in i18n.SUPPORTED:
        assert set(catalog[lang]) == ref, f"the keys of {lang} diverge from {i18n.DEFAULT}"
        # The values are non-empty in all languages (we did not leave “TODO”/an empty string).
        for key, val in catalog[lang].items():
            assert isinstance(val, str) and val.strip(), f"{lang}.{key} is empty"


def test_signup_result_heading_keys_present_all_langs():
    """M-5 (audit batch 1): the new keys of the signup_result heading/link — in
    all three languages, non-empty. The parity of the key set is already held by
    ``test_cabinet_i18n_key_parity`` (generic over the whole CABINET); here —
    a subject-matter check that these exact three keys really exist."""
    for key in ("sgr_h1_success", "sgr_h1_rejected", "sgr_back_signup"):
        for lang in i18n.SUPPORTED:
            val = i18n.CABINET[lang][key]
            assert isinstance(val, str) and val.strip(), f"{lang}.{key} is empty"


def test_common_status_labels_match_source():
    """The ru statuses COMMON = STATUS_LABELS (a single source of the funnel bot↔cabinet).

    If they desync, the same tender_user_status string would be called differently
    in the bot and in the cabinet. COMMON is an overlay over the source, not a copy.
    """
    from src.db.models.tender_status import STATUS_LABELS, STATUSES

    for st in STATUSES:
        assert i18n.COMMON["ru"][f"status_{st}"] == STATUS_LABELS[st], st
    # And EXACTLY the funnel statuses are covered (we did not forget a new one, nor leave an extra).
    common_status_keys = {k for k in i18n.COMMON["ru"] if k.startswith("status_")}
    assert common_status_keys == {f"status_{s}" for s in STATUSES}


# --------------------------------------------------------------------------- #
# Pluralization by locale (Phase 1.2): parity of the keys + the rule for choosing a form
# --------------------------------------------------------------------------- #
def test_plurals_key_parity_and_shape():
    """All languages carry ONE set of pluralization keys; Slavic languages have 3 forms, en has 2.

    If they diverge, the template calls `plural(lang, key, n)` with a key that is missing in
    some languages, and the cockpit fails in another locale (not in the test one).
    """
    assert set(i18n.PLURALS) == set(i18n.SUPPORTED)
    ref = set(i18n.PLURALS[i18n.DEFAULT])
    for lang in i18n.SUPPORTED:
        assert set(i18n.PLURALS[lang]) == ref, f"the pluralization keys of {lang} diverged"
        expected_forms = 2 if lang == "en" else 3
        for key, forms in i18n.PLURALS[lang].items():
            assert (
                len(forms) == expected_forms
            ), f"{lang}.{key}: expecting {expected_forms} forms"
            for form in forms:
                assert isinstance(form, str) and form.strip(), f"{lang}.{key} is empty"


# Boundary numbers, where it is easiest to err “by eye”: the form changes at
# 1/21 (one), 2–4/22 (few), 5/11/25 (many). 11 is the exception (many, not one).
_PLURAL_WORK = {
    "ru": {
        1: "тендер в работе",
        2: "тендера в работе",
        4: "тендера в работе",
        5: "тендеров в работе",
        11: "тендеров в работе",
        21: "тендер в работе",
        22: "тендера в работе",
        25: "тендеров в работе",
    },
    "sr": {
        1: "tender u radu",
        2: "tendera u radu",
        4: "tendera u radu",
        5: "tendera u radu",
        11: "tendera u radu",
        21: "tender u radu",
        22: "tendera u radu",
        25: "tendera u radu",
    },
    "en": {
        1: "tender in progress",
        2: "tenders in progress",
        4: "tenders in progress",
        5: "tenders in progress",
        11: "tenders in progress",
        21: "tenders in progress",
        22: "tenders in progress",
        25: "tenders in progress",
    },
}


@pytest.mark.parametrize("lang", ["ru", "sr", "en"])
@pytest.mark.parametrize("n", [1, 2, 4, 5, 11, 21, 22, 25])
def test_plural_boundary_numbers(lang, n):
    """The plural rule BY LOCALE on boundary numbers.

    ru/sr — Slavic 3-form (one/few/many); en — 2-form (one/other).
    Exactly the class where the error is not visible “by eye”.
    """
    assert i18n.plural(lang, "tenders_in_work", n) == _PLURAL_WORK[lang][n]


def test_plural_substitutes_number_and_is_locale_specific():
    """Forms with `{n}` substitute the number; the form rule differs by locale."""
    # ru: “d.” does not inflect — the number is substituted, one form for all numbers.
    assert i18n.plural("ru", "days_short", 5) == "5 дн."
    assert i18n.plural("ru", "days_short", 1) == "1 дн."
    # sr: 1 → «dan», 2–4 → «dana», 5+/11–14 → «dana».
    assert i18n.plural("sr", "days_short", 21) == "21 dan"
    assert i18n.plural("sr", "days_short", 22) == "22 dana"
    assert i18n.plural("sr", "days_short", 11) == "11 dana"
    # en: 1 → day, otherwise days.
    assert i18n.plural("en", "days_short", 1) == "1 day"
    assert i18n.plural("en", "days_short", 5) == "5 days"
    # abs(): a negative deadline (the deadline has passed) does not break the choice of form.
    assert i18n.plural("en", "days_left_short", -1) == "1 day left"
    # An unknown language → the cabinet default (ru).
    assert i18n.plural("xx", "days_short", 5) == i18n.plural("ru", "days_short", 5)


# --------------------------------------------------------------------------- #
# Rendering of the cockpit in each of the three locales (Phase 1.2): the marker — its own per language
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "locale,title,tiles",
    [
        ("sr", "Moja nedelja", ("Ističe za 7 dana", "Novo za vas, 7 dana", "U radu")),
        (
            "ru",
            "Моя неделя",
            ("Истекает за 7 дней", "Новое для вас, 7 дней", "В работе"),
        ),
        ("en", "My week", ("Closing in 7 days", "New for you, 7 days", "In progress")),
    ],
)
def test_cockpit_hero_renders_in_locale(locale, title, tiles):
    """The cockpit v2 hero is localized: the heading and three tile captions — from CABINET.

    The earlier version checked the funnel status chip and the plural “N tenders in progress” —
    the “In progress” tile is now a number + a short caption, there are no chips in the hero (owner 13.10).
    The expectations are literals per language, not via t().

    Mutation: put a ru hard-code of the tile caption back into the template — sr/en will lose the marker.
    """
    from src.api.templating import templates

    t = {**i18n.COMMON[locale], **i18n.CABINET[locale]}
    html = templates.get_template("_cockpit_hero.html").render(
        t=t,
        lang=locale,
        urgent_count=3,
        flow_pulse=27,
        work_summary=SimpleNamespace(total=2, buckets=[]),
    )
    assert title in html
    for tile in tiles:
        assert tile in html
    assert 'data-count-to="27">27' in html
    if locale != "ru":
        assert "в работе" not in html.lower()


# --------------------------------------------------------------------------- #
# The cockpit strings FROM THE PYTHON services (weekdays, month, the subscription event, the status
# of the funnel in the agenda) — previously they came from feed_service as a Russian hard-code and
# stayed Russian on the en/sr cockpit (owner’s complaint, 23.07). Now the service
# returns a KEY/number, and the template assembles the string by locale.
# --------------------------------------------------------------------------- #
_CAL_NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def _calendar_html(locale: str) -> str:
    """Rendering of `_calendar.html` in the given locale with the subscription events and the action.

    The action (a tender in progress, status=active) gives the status caption from
    COMMON in the agenda; the subscription event — the cal_sub_title heading. Both are markers that
    the chrome was assembled BY LOCALE, and not left as a Russian literal of the service.
    """
    from src.api.cabinet import feed_service
    from src.api.templating import templates

    my_row = SimpleNamespace(
        tender_id="t-1",
        title="Набавка рачунара",  # content — “as is”, not localized
        customer_name=None,
        deadline_at=_CAL_NOW + timedelta(days=3),
        tender_url="https://portal.rs/t/1",
        status="active",
    )
    events = feed_service.build_calendar_events(
        my_tenders=[my_row],
        access_until=_CAL_NOW + timedelta(days=5),
        now=_CAL_NOW,
    )
    month = feed_service.build_calendar_month(events, now=_CAL_NOW)
    t = {**i18n.COMMON[locale], **i18n.CABINET[locale]}
    # v2: the month grid — `_calendar.html`, the events list — `_rokovi.html` (ROKOVI).
    grid = templates.get_template("_calendar.html").render(
        t=t, lang=locale, calendar_month=month
    )
    agenda = templates.get_template("_rokovi.html").render(
        t=t, lang=locale, agenda_days=30, agenda_events=events
    )
    return grid + agenda


# --------------------------------------------------------------------------- #
# Rendering of the “My tenders” / library / archive pages in each locale (Phase 1.3).
# The route is real (Jinja + includes), the service is mocked: the chrome from the catalog, the content
# “as is”. The markers are language-specific, so a mutation (put a ru hard-code back into any of
# these keys — funnel_title / nav_library / nav_feed / the funnel status) breaks
# the sr/en render and the test turns red.
# --------------------------------------------------------------------------- #
def _async(value):
    async def _stub(*args, **kwargs):
        return value

    return _stub


def _my_data():
    """Aggregate “My tenders”: a row in progress (watching) + a folder with an outcome."""
    return workflow_service.MyTenders(
        folders=[
            workflow_service.FolderView(
                id="f1",
                name="Prioritet",  # folder name — data, not translated by the UI language
                tenders=[
                    workflow_service.MyTenderRow(
                        tender_id="t-2",
                        title="Nabavka 2",
                        customer_name="Grad",
                        deadline_at=None,
                        tender_url=None,
                        status="won",
                    )
                ],
            )
        ],
        funnel=[
            workflow_service.MyTenderRow(
                tender_id="t-1",
                title="Nabavka",
                customer_name="Grad",
                deadline_at=datetime.now(timezone.utc) + timedelta(days=5),
                tender_url="https://portal.rs/t/1",
                status="watching",
            )
        ],
    )


@pytest.mark.parametrize(
    "locale,weekday,month_label,sub_title,status_sub",
    [
        ("sr", "pon", "jul 2026", "Pretplata ističe", "Učestvujem"),
        ("ru", "Пн", "июль 2026", "Подписка истекает", "Участвую"),
        ("en", "Mon", "July 2026", "Subscription is expiring", "Participating"),
    ],
)
def test_calendar_widget_renders_in_locale(
    locale, weekday, month_label, sub_title, status_sub
):
    """The cockpit calendar is localized entirely: weekday, month, the subscription event
    and the funnel status in the agenda — in the interface language, without a Russian hard-code.

    Mutation: put any ru literal back into feed_service (e.g. the heading "Subscription
    expires" instead of the cal_sub_title key, or _WEEKDAYS_RU in CalendarMonth) —
    the sr/en render will lose its marker, and the “ru did not leak” check below turns red.
    """
    html = _calendar_html(locale)
    assert weekday in html  # CABINET.wd_1 (the week starts on Monday)
    assert month_label in html  # CABINET.mon_7 + year
    assert sub_title in html  # CABINET.cal_sub_title (subscription event)
    assert status_sub in html  # COMMON.status_active as the action subtitle
    # Russian variants must not leak into sr/en (a mutation anchor).
    if locale != "ru":
        for ru_leak in ("Пн", "июль 2026", "Подписка истекает", "Участвую"):
            assert ru_leak not in html, ru_leak


@pytest.mark.parametrize(
    "locale,funnel_title,stage_chip,folders_word",
    [
        ("sr", "Levak", "👀 Pratim", "Fascikle"),
        ("ru", "Воронка", "👀 Изучаю", "Папки"),
        ("en", "Funnel", "👀 Reviewing", "Folders"),
    ],
)
def test_my_tenders_page_renders_in_locale(
    monkeypatch, locale, funnel_title, stage_chip, folders_word
):
    """The “My tenders” funnel board is localized: the heading and the folder captions from
    CABINET, the stage caption from COMMON via ``t['status_'~status]``.

    Mutation: put a ru hard-code back into the template (e.g. “Funnel”) — the sr/en render will lose
    its marker and the test turns red.
    """
    monkeypatch.setattr(workflow_service, "get_my_tenders", _async(_my_data()))
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get("/app/my")
    assert resp.status_code == 200
    html = resp.text
    assert funnel_title in html  # CABINET.funnel_title
    assert stage_chip in html  # COMMON.status_watching via t['status_'~status]
    assert folders_word in html  # CABINET.folders_word
    if locale != "ru":
        # The Russian chrome must not leak into the sr/en render.
        assert "Воронка" not in html and "Папки" not in html


def _lib_tpl():
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        name="Gradnja",  # template name — data, “as is”
        description="Asfalt",
        activity_kind="Gradnja",
        keywords_include=[{"all_of": ["asfalt"]}],
        classifications={"include": [{"system_code": "CPV", "code": "45233"}]},
    )


@pytest.mark.parametrize(
    "locale,title,packages_title,use_tpl,tpl_count",
    [
        (
            "sr",
            "Biblioteka šablona",
            "Cela oblast",
            "Koristi šablon",
            "8 šablona",
        ),
        (
            "ru",
            "Библиотека шаблонов",
            "Вся отрасль",
            "Использовать шаблон",
            "8 шаблонов",
        ),
        (
            "en",
            "Template library",
            "Whole sector",
            "Use template",
            "8 templates",
        ),
    ],
)
def test_library_page_renders_in_locale(
    monkeypatch, locale, title, packages_title, use_tpl, tpl_count
):
    """The template library is localized: the heading/packages/CTA from CABINET, the counter
    “N templates” — via pluralization by locale. The template names are data.

    Mutation: put a ru hard-code into any of the keys — the sr/en will lose the marker.
    """
    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([_lib_tpl()])
    )
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get("/library")
    assert resp.status_code == 200
    html = resp.text
    assert title in html  # CABINET.nav_library
    assert packages_title in html  # CABINET.lib_sector_badge (a package = the whole sector)
    assert use_tpl in html  # CABINET.lib_use_template
    assert tpl_count in html  # plural(lang, "templates_count", 1 template + 7 packages)
    if locale != "ru":
        assert "Использовать шаблон" not in html


_PKG_CARD_TEXT_RE = re.compile(r'class="tpl-card__(?:title|desc)">([^<]*)<')


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_library_vertical_packages_render_has_no_russian(monkeypatch, locale):
    """VG-14 showcase of packages: the name/description of the packages come from
    vertical_packages.display_packages(lang), and not from a hard-coded “ru” —
    the route previously ignored the interface locale for the package data.

    Scope — only name/description (what really switches
    display_packages(lang)). The keyword tags (``p.keywords``) are stored
    only in Russian and are deliberately NOT touched in this task (see CLAUDE.md /
    BOUNDARIES — keyword normalization is a separate PR), so they are excluded
    from the audit.

    Mutation: put display_packages(“ru”) back into the route — sr/en again contain
    the Russian package names (“Construction”, “Electricity” etc.), and
    the test turns red.
    """
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([]))
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get("/library")
    assert resp.status_code == 200
    texts = _PKG_CARD_TEXT_RE.findall(resp.text)
    assert texts, "package cards not found in the render"
    leaks = [t for t in texts if _RU_SPECIFIC.search(t)]
    assert not leaks, f"Russian name/description of packages in {locale}: {leaks}"


def _feed_item():
    return feed_service.FeedItem(
        tender_id="aaaa",
        title="Nabavka",
        customer_name="Grad",
        deadline_at=datetime.now(timezone.utc) + timedelta(days=2),
        tender_url="https://portal.rs/t/1",
        profile_id="pppp",
        profile_name="IT",
        ai_level=None,
        sent_at=datetime.now(timezone.utc),
        ai_actions=feed_service.AiActions(
            delivered_analysis_level=None,
            has_bid_verdict=False,
            has_checklist=False,
            has_draft=False,
        ),
    )


@pytest.mark.parametrize(
    "locale,h1,urgent_title,back",
    [
        ("sr", "Moja poklapanja", "Uskoro rok", "← Na kontrolnu tablu"),
        ("ru", "Мои совпадения", "Скоро дедлайн", "← На дашборд"),
        ("en", "My matches", "Deadline soon", "← To the dashboard"),
    ],
)
def test_feed_archive_page_renders_in_locale(
    monkeypatch, locale, h1, urgent_title, back
):
    """The web archive of matches is localized: the heading from the COMMON navigation (nav_feed),
    the urgency section and “back to the dashboard” — from CABINET. The tender cards are data.

    Mutation: put a ru hard-code into the chrome — sr/en will lose the marker and the test turns red.
    """
    monkeypatch.setattr(feed_service, "count_feed_total", _async(1))
    monkeypatch.setattr(feed_service, "get_feed_page", _async([_feed_item()]))
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get("/app/feed")
    assert resp.status_code == 200
    html = resp.text
    assert h1 in html  # COMMON/CABINET.nav_feed
    assert urgent_title in html  # CABINET.archive_urgent_title
    assert back in html  # CABINET.back_to_dashboard
    if locale != "ru":
        assert "Мои совпадения" not in html and "Скоро дедлайн" not in html


# --------------------------------------------------------------------------- #
# The resolver: each priority level separately + an unknown code → the default
# --------------------------------------------------------------------------- #
def test_resolver_query_wins_over_everything():
    # ?lang= is stronger than the saved users.locale and the cookie
    assert (
        i18n.resolve_authed_lang(_req(query=b"lang=en", cookie="lang=sr"), "sr") == "en"
    )


def test_resolver_user_locale_over_cookie():
    # ‼️ users.locale ABOVE the cookie: the saved choice is stronger than a random cookie
    assert i18n.resolve_authed_lang(_req(cookie="lang=en"), "sr") == "sr"


def test_resolver_cookie_when_no_user_locale():
    assert i18n.resolve_authed_lang(_req(cookie="lang=en"), None) == "en"


@pytest.mark.sr_default
@pytest.mark.parametrize("accept", ["en-US,en;q=0.9", "ru-RU,ru;q=0.9", "sr-RS"])
def test_resolver_ignores_accept_language(accept):
    # Owner’s decision 30.09.2026: Accept-Language is not taken into account
    assert i18n.resolve_authed_lang(_req(accept=accept), None) == "sr"


@pytest.mark.sr_default
def test_resolver_default_is_sr():
    # Cabinet default — sr (owner decision 30.09.2026)
    assert i18n.resolve_authed_lang(_req(), None) == "sr" == i18n.DEFAULT


@pytest.mark.sr_default
def test_resolver_unknown_codes_fall_through_to_default():
    # Unknown codes are ignored at each level
    assert (
        i18n.resolve_authed_lang(_req(query=b"lang=xx", cookie="lang=zz"), "qq") == "sr"
    )


def test_cabinet_context_shape():
    ctx = i18n.cabinet_context(_req(query=b"lang=sr"), None)
    assert ctx["lang"] == "sr"
    assert ctx["lang_names"] == i18n.LANG_NAMES
    # t — a flat dict: COMMON ∪ CABINET of the chosen language
    assert ctx["t"]["nav_dashboard"] == i18n.CABINET["sr"]["nav_dashboard"]
    assert ctx["t"]["status_new"] == i18n.COMMON["sr"]["status_new"]


# --------------------------------------------------------------------------- #
# Rendering of the base.html header in each language (via the cabinet route /login)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "locale,marker",
    [
        ("sr", "Registracija"),
        ("ru", "Регистрация"),
        ("en", "Sign up"),
    ],
)
def test_base_chrome_renders_in_user_locale(locale, marker):
    """users.locale sets the header chrome language; the switcher is rendered."""
    client = _client(sess={"uid": _UID, "tid": _TID}, locale=locale)
    resp = client.get("/login")
    assert resp.status_code == 200
    assert f'lang="{locale}"' in resp.text
    assert marker in resp.text
    assert "lang-switch" in resp.text
    assert 'aria-current="true"' in resp.text  # the active language is marked


# --------------------------------------------------------------------------- #
# POST /cabinet/locale
# --------------------------------------------------------------------------- #
def test_locale_post_writes_own_row_and_sets_cookie():
    """Writes users.locale by the uid from the SESSION (not from the form), set_tenant FIRST,
    sets the cookie and redirects to the same page (next)."""
    fake = FakeSession(results=[FakeResult(items=[SimpleNamespace(locale=None)])])
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: fake)
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": _UID,
        "tid": _TID,
    }
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    resp = client.post("/cabinet/locale", data={"lang": "en", "return_to": "/app/feed"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/feed"
    assert "lang=en" in resp.headers.get("set-cookie", "")

    # RLS: the first execute is set_config (set_tenant), then UPDATE users by the session’s uid.
    stmts = [sql for sql, _ in fake.executed]
    assert "set_config" in stmts[0] or "app.current_tenant_id" in stmts[0]
    update = next((p for sql, p in fake.executed if "UPDATE users" in sql), None)
    assert update is not None, "UPDATE users was not executed"
    assert update["locale"] == "en"
    assert update["uid"] == _UID  # strictly its own uid from the session
    assert fake.committed == 1


def test_locale_post_invalid_code_is_noop():
    """An unknown code: no DB write and no cookie (only a redirect back)."""
    fake = FakeSession(results=[FakeResult(items=[SimpleNamespace(locale=None)])])
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: fake)
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": _UID,
        "tid": _TID,
    }
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    resp = client.post("/cabinet/locale", data={"lang": "xx", "return_to": "/app"})
    assert resp.status_code == 303
    assert "lang=" not in (resp.headers.get("set-cookie") or "")
    assert not any("UPDATE users" in sql for sql, _ in fake.executed)
    assert fake.committed == 0


def test_locale_post_anonymous_redirects_to_login():
    client = _client(sess=None)
    resp = client.post("/cabinet/locale", data={"lang": "en"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_locale_post_rejects_open_redirect_next():
    """next with a foreign host is discarded (safe_next_path) → /app."""
    client = _client(sess={"uid": _UID, "tid": _TID}, locale=None)
    resp = client.post(
        "/cabinet/locale", data={"lang": "en", "return_to": "//evil.com"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


# --------------------------------------------------------------------------- #
# The public pages are NOT affected (their `t` catalog is not overridden by the processor)
# --------------------------------------------------------------------------- #
def test_public_pages_keep_own_catalog():
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    # The public default stays sr (not the cabinet’s ru), the showcase catalog is in place.
    landing = client.get("/")
    assert 'lang="sr"' in landing.text
    assert "samo one koje su za vas" in landing.text  # LANDING[sr], not the cabinet chrome


# --------------------------------------------------------------------------- #
# Guard: raw Cyrillic in a template OUTSIDE the catalog (temporary allowlist)
# --------------------------------------------------------------------------- #
_TPL_DIR = Path(__file__).resolve().parents[2] / "src" / "api" / "templates"
_CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
_JINJA_COMMENT = re.compile(r"{#.*?#}", re.S)

# The templates whose strings are NOT yet translated via the CABINET/COMMON catalog. The list
# is TEMPORARY: each next cabinet localization PR removes the migrated
# page from here (and the test turns red if the file was forgotten to be removed after localization).
# The public templates (landing/plans/help/tender/legal/public_base) are already
# multilingual via their own mechanism (public.i18n) — they are not here, or they are clean.
# base.html was localized in #418 and must be clean. The cockpit (dashboard.html,
# _cockpit_hero/_cockpit_todo/_cockpit_feed_teaser/_calendar/_agenda_item) —
# localized in Phase 1.2: 6 files left here and must be clean). "My
# tenders" + funnel + library + archive (my_tenders/_my_tender_row/
# _funnel_board/_funnel_column/_tender_actions/library/feed_archive) —
# localized in Phase 1.3: 7 files left here and must be clean). The feed +
# the tender card (feed-card, card-title, tender-description, tender_detail,
# _ai_slot/_ai_artifact and the buyer analytics) — localized in Phase 1.4: 13
# files left here and must be clean. The account pages (subscription/
# telegram_panel/telegram_link/email_panel/profile_form/profile_detail/login/
# signup/signup_result/resend_verification/message) — localized in
# Phase 1.4b: 11 files left here and must be clean. The conditional partials
# of the dashboard (_delivery_gap/_onboarding_steps/_region_hidden_banner) — localized
# in Phase 1.5 (the render audit of /app): 3 files left here and must be clean.
# i18n remainder of the cabinet (edge/error): _hint_tip.html is localized — the macro takes the label
# by default from t.tip_what (import `with context`), the raw ru default is removed.
# public_base.html left here in the audit batch 2 (M-7): the language aria-labels,
# the aria/title of the “Back to top” button and the legal link captions moved into the UI catalog
# (public.i18n) — the Russian there was chrome bypassing the catalog and lived in all three languages.
_RAW_CYRILLIC_ALLOWLIST = {
    "admin_audit.html",
    "admin_base.html",
    "admin_health.html",
    "admin_login.html",
    "admin_moderation.html",
    "admin_requests.html",
    "admin_sources.html",
    "admin_templates.html",
    "admin_user_detail.html",
    "admin_users.html",
}


def _has_raw_cyrillic(path: Path) -> bool:
    txt = _JINJA_COMMENT.sub("", path.read_text(encoding="utf-8"))
    return bool(_CYRILLIC.search(txt))


def test_no_raw_cyrillic_outside_catalog():
    """Localized templates carry no raw Cyrillic (only via `t`).

    A file outside the allowlist with Cyrillic → a string passed around the catalog. base.html
    is under guard here: put any ru hard-code back into it — the test turns red.
    """
    offenders = [
        p.name
        for p in sorted(_TPL_DIR.glob("*.html"))
        if p.name not in _RAW_CYRILLIC_ALLOWLIST and _has_raw_cyrillic(p)
    ]
    assert not offenders, f"raw Cyrillic outside the catalog: {offenders}"


def test_cyrillic_allowlist_has_no_stale_entries():
    """The allowlist only shrinks: you localized the file — remove it from here."""
    stale = [
        name
        for name in _RAW_CYRILLIC_ALLOWLIST
        if not _has_raw_cyrillic(_TPL_DIR / name)
    ]
    assert not stale, f"files already without Cyrillic — remove from the allowlist: {stale}"


# --------------------------------------------------------------------------- #
# Render audit of the feed and the tender card (Phase 1.4): BY THE RENDER RESULT, not by
# the templates. The owner’s complaint of 23.07: "when the language changes, most of the texts stay
# in Russian“. The check ”no Cyrillic in the template" missed it — the strings came from
# Python (constants, aria/title, empty states, the captions of the AI sections). Here all the
# fixture DATA is in Latin, so ANY Cyrillic in the en/sr render = chrome bypassing the
# catalog. Mutation: put one Russian literal back into the catalog/template — the test turns red
# (see test_feed_mutation_reintroduced_russian_is_caught).
# --------------------------------------------------------------------------- #
from src.api.cabinet import customer_analytics_service as _cas  # noqa: E402
from src.api.cabinet import routes_tender_ai as _rta  # noqa: E402
from src.api.cabinet import tender_ai_service as _tas  # noqa: E402


def _feed_item_full():
    """FeedItem with all AI artifacts (the “Ready:” cluster and badges) — Latin data."""
    return feed_service.FeedItem(
        tender_id="aaaaaaaa-0000-0000-0000-000000000001",
        title="Nabavka asfaltne mase",
        customer_name="Grad Beograd",
        deadline_at=datetime.now(timezone.utc) + timedelta(days=2),
        tender_url="https://portal.rs/t/1",
        profile_id="pppppppp-0000-0000-0000-000000000001",
        profile_name="Gradnja",
        ai_level="deep",
        sent_at=datetime.now(timezone.utc),
        ai_actions=feed_service.AiActions(
            delivered_analysis_level="deep",
            has_bid_verdict=True,
            has_checklist=True,
            has_draft=True,
        ),
    )


def _tender_analyses_full():
    """TenderAnalyses with all levels (bid/checklist/draft/deep) — Latin data."""
    return _tas.TenderAnalyses(
        tender_id="aaaaaaaa-0000-0000-0000-000000000001",
        profile_id="pppppppp-0000-0000-0000-000000000001",
        profile_name="Gradnja",
        title="Nabavka asfaltne mase",
        customer_name="Grad Beograd",
        deadline_at=datetime.now(timezone.utc) + timedelta(days=3),
        source_url="https://portal.rs/t/1",
        description="Predmet nabavke je asfaltna masa.\nRok isporuke 30 dana.",
        outputs={
            "deep": {
                "match_score": 4,
                "summary": "Nabavka odgovara profilu Gradnja.",
                "match_reasons": ["Asfalt je u profilu"],
                "red_flags": ["Kratak rok"],
                "required_qualifications": ["ISO 9001"],
                "work_breakdown": ["Priprema", "Asfaltiranje"],
                "participation_checklist": ["Registracija na portalu"],
            },
            "bid": {
                "verdict": "go",
                "score": 78,
                "pros": ["Dobra marža"],
                "cons": ["Konkurencija"],
            },
            "checklist": {
                "documents": [
                    {
                        "mandatory": True,
                        "title": "APR izvod",
                        "category": "Pravno",
                        "note": "ne stariji od 6 meseci",
                    },
                    {
                        "mandatory": False,
                        "title": "ISO sertifikat",
                        "category": None,
                        "note": None,
                    },
                ],
                "deadlines": [{"label": "Podnošenje", "value": "30.09.2026"}],
                "disqualification_risks": ["Nepotpuna dokumentacija"],
                "summary": "Pripremiti dokumente na vreme.",
            },
            "draft": {
                "sections": [
                    {"heading": "Uvod", "body": "Poštovani, dostavljamo ponudu."}
                ],
                "criteria_mapping": [{"criterion": "Cena", "response": "Konkurentna"}],
                "summary": "Nacrt spreman za doradu.",
                "disclaimer": "Proverite pre slanja.",
            },
        },
    )


def _bundle_full():
    """NarucilacAnalyticsBundle with all success sections — Latin data."""
    customer = _cas.CustomerAnalyticsView(
        status="success",
        customer_name="Grad Beograd",
        customer_pib="100000001",
        total_awards=42,
        distinct_winners=10,
        with_amount=30,
        total_rsd_fmt="1 200 000",
        total_eur_fmt="10 200",
        concentration_pct=55,
        cr3_pct=80,
        is_monopolized=True,
        top_winners=[
            _cas.WinnerView(
                rank=1, name="Asfalt d.o.o.", wins=12, sum_rsd_fmt="800 000"
            )
        ],
        trend=[
            _cas.TrendYearView(
                year=2025,
                awards=20,
                distinct_winners=6,
                total_rsd_fmt="600 000",
                total_eur_fmt="5 100",
                bar_pct=100,
            ),
            _cas.TrendYearView(
                year=2026,
                awards=22,
                distinct_winners=7,
                total_rsd_fmt="600 000",
                total_eur_fmt="5 100",
                bar_pct=90,
            ),
        ],
        avg_rsd_fmt="40 000",
        median_rsd_fmt="35 000",
        period_fmt="2024–2026",
        without_date=3,
    )
    competitor = _cas.CompetitorAnalyticsView(
        status="success",
        winner_name="Asfalt d.o.o.",
        winner_pib="100000002",
        total_wins=12,
        distinct_customers=5,
        with_amount=8,
        total_rsd_fmt="800 000",
        total_eur_fmt="6 800",
        top_customers=[
            _cas.RivalView(rank=1, name="Grad Novi Sad", wins=4, sum_rsd_fmt="300 000")
        ],
    )
    reprocurement = _cas.ReprocurementForecastView(
        status="success",
        customer_name="Grad Beograd",
        customer_pib="100000001",
        horizon_months=12,
        items=[
            _cas.ReprocurementItemView(
                rank=1,
                lot_title="Održavanje puteva",
                winner_name="Asfalt d.o.o.",
                expiry_fmt="31.12.2026",
                months_until_expiry=5,
                sum_rsd_fmt="500 000",
            )
        ],
    )
    incumbent = _cas.IncumbentView(
        status="success",
        customer_name="Grad Beograd",
        items=[
            _cas.IncumbentItemView(
                winner_name="Asfalt d.o.o.",
                pib="100000002",
                date_fmt="01.06.2026",
                sum_rsd_fmt="300 000",
                lots=2,
                is_joint=True,
            ),
            _cas.IncumbentItemView(
                winner_name="Put d.o.o.",
                pib="",
                date_fmt="",
                sum_rsd_fmt="",
                lots=1,
                is_joint=False,
            ),
        ],
    )
    return _cas.NarucilacAnalyticsBundle(
        customer=customer,
        competitor=competitor,
        reprocurement=reprocurement,
        incumbent=incumbent,
    )


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_feed_page_render_has_no_cyrillic_outside_data(monkeypatch, locale):
    """The render of /app/feed in en/sr contains no Cyrillic: all the fixture data is in Latin,
    so any Cyrillic = Russian chrome that got past the catalog (the card,
    the deadline badges, the “Ready:” cluster, the profile filter)."""
    prof = [
        SimpleNamespace(id="pppppppp-0000-0000-0000-000000000001", name="Gradnja"),
        SimpleNamespace(id="pppppppp-0000-0000-0000-000000000002", name="IT"),
    ]
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async(prof))
    monkeypatch.setattr(feed_service, "count_feed_total", _async(1))
    monkeypatch.setattr(feed_service, "get_feed_page", _async([_feed_item_full()]))
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get("/app/feed")
    assert resp.status_code == 200
    leaked = sorted(set(re.findall(r"[а-яёА-ЯЁ][а-яёА-ЯЁ .,«»„“”()/-]*", resp.text)))
    assert not leaked, f"Russian chrome in the {locale} render of the feed: {leaked}"


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_tender_detail_render_has_no_cyrillic_outside_data(monkeypatch, locale):
    """The render of /app/tenders/{id} in en/sr contains no Cyrillic: the fixture data
    is in Latin → any Cyrillic = Russian chrome (the AI sections, the verdict, the checklist,
    the buyer/competitor/re-procurement analytics, the translation toggles)."""
    monkeypatch.setattr(
        _rta.svc, "get_tender_analyses", _async(_tender_analyses_full())
    )
    monkeypatch.setattr(_rta.cust, "get_narucilac_analytics", _async(_bundle_full()))
    monkeypatch.setattr(_rta.workflow_service, "get_tender_state", _async(None))
    monkeypatch.setattr(_rta.workflow_service, "list_folders", _async([]))
    url = "/app/tenders/aaaaaaaa-0000-0000-0000-000000000001?profile_id=pppppppp-0000-0000-0000-000000000001"
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get(url)
    assert resp.status_code == 200
    leaked = sorted(set(re.findall(r"[а-яёА-ЯЁ][а-яёА-ЯЁ .,«»„“”()/-]*", resp.text)))
    assert not leaked, f"Russian chrome in the {locale} render of the card: {leaked}"


def test_feed_mutation_reintroduced_russian_is_caught(monkeypatch):
    """Mutation anchor of the guard: put one Russian literal back into the catalog — the render
    of the feed in en contains Cyrillic again. If this test is green without the mutation,
    the guard above checks nothing."""
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([]))
    monkeypatch.setattr(feed_service, "count_feed_total", _async(1))
    monkeypatch.setattr(feed_service, "get_feed_page", _async([_feed_item_full()]))
    monkeypatch.setitem(i18n.CABINET["en"], "fd_ready", "Готово:")
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale="en").get("/app/feed")
    assert resp.status_code == 200
    assert re.search(
        r"[а-яёА-ЯЁ]", resp.text
    ), "the mutation did not show — the guard does not work"


@pytest.mark.parametrize(
    "locale,translate_label,foreign",
    [
        ("en", "Translate description", "Prevedi opis"),
        ("ru", "Перевести описание", "Translate description"),
    ],
)
def test_description_fragment_chrome_follows_ui_locale_not_content_lang(
    monkeypatch, locale, translate_label, foreign
):
    """‼️ Two axes: ``?lang=`` in the fragment of the description is the translation of the CONTENT
    (sr = original), NOT the interface language. The button chrome must follow the user’s
    locale. A regression on the collision: earlier ``?lang=`` would recolour the chrome.

    ‼️ The ``sr`` locale was REMOVED from the parameters (owner’s decision, 25.07): on the Serbian
    interface there is no translation button at all — the tender’s original is already Serbian,
    there is nothing to translate. This behavior is checked by
    ``tests/api/test_translation_lang_axis.py``; here the locales remain where
    the button exists and its chrome must follow the interface."""
    monkeypatch.setattr(
        _rta.svc, "get_tender_analyses", _async(_tender_analyses_full())
    )
    url = (
        "/app/tenders/aaaaaaaa-0000-0000-0000-000000000001/description"
        "?profile_id=pppppppp-0000-0000-0000-000000000001&lang=sr"
    )
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get(url)
    assert resp.status_code == 200
    assert translate_label in resp.text  # the chrome = the user’s interface language
    assert foreign not in resp.text  # NOT the language of another locale, does not follow ?lang=


# --------------------------------------------------------------------------- #
# Render audit of the ACCOUNT PAGES (Phase 1.4b): BY THE RENDER RESULT, not by
# the templates. The owner’s complaint of 23.07: "when the language changes, most of the texts stay
# in Russian" — strings came from Python too (the choice constants, feature captions, the tile
# of access, statuses), not only from the templates. Here all the fixture DATA is in Latin,
# so the Russian chrome gives itself away by the Russian-specific letters U+044B U+044D U+0449 U+044A U+0451 U+0439 (they are absent
# neither in Serbian Latin nor in Serbian Cyrillic district names — those remain
# “as is” as data). Mutation — test_account_mutation_reintroduced_russian.
# --------------------------------------------------------------------------- #
_RU_SPECIFIC = re.compile(r"[ыэщъёйЫЭЩЪЁЙ]")


def _ru_leaks(text: str) -> list[str]:
    """Fragments with the Russian-specific letters — outside the Serbian data."""
    return sorted(
        set(re.findall(r"[а-яёА-ЯЁ][а-яёА-ЯЁ .,«»„“”()/@→↗-]*", text))
    )  # noqa: E501,RUF001


def _assert_no_russian(html: str, where: str) -> None:
    """The render must contain no Russian-specific letters (Serbian Cyrillic is ok)."""
    hits = [f for f in _ru_leaks(html) if _RU_SPECIFIC.search(f)]
    assert not hits, f"Russian chrome in {where}: {hits}"


def _sub_now():
    from datetime import datetime, timezone

    return datetime(2026, 12, 1, tzinfo=timezone.utc)


def _stub_hub(
    monkeypatch, *, tg_linked=False, email_state=None, subscription=None, decision=None
):
    """The “Moj kabinet” hub: services are substituted, the fixture data is in Latin."""
    from tests.api.test_routes_account import _profile, _setup

    _setup(
        monkeypatch,
        tier="pro",
        profiles=[
            _profile(name="Gradnja"),
            _profile(id="p-2", name="IT", is_active=False),
        ],
        telegram_live=tg_linked,
        email_state=email_state,
        subscription=subscription,
    )
    monkeypatch.setattr(routes_subscription.svc, "get_last_decision", _async(decision))


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_hub_subscription_section_render_has_no_russian(monkeypatch, locale):
    """The “Subscription” section of the hub (formerly /cabinet/subscription) in en/sr: statuses,
    details, the request decision, the value ladder, the upsell — without Russian chrome."""
    sub = SimpleNamespace(
        status="active",
        provider="paddle",
        plan_key="pro",
        current_period_end=_sub_now(),
    )
    decision = SimpleNamespace(
        approved=True, plan="pro", decided_at=_sub_now(), comment="OK note"
    )
    _stub_hub(monkeypatch, subscription=sub, decision=decision)
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get(
        "/cabinet/account"
    )
    assert resp.status_code == 200
    _assert_no_russian(resp.text, f"{locale}-подписка")


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize("linked", [True, False])
def test_hub_telegram_channel_render_has_no_russian(monkeypatch, locale, linked):
    """The “Channels” section, Telegram, in en/sr: status, bot commands — clean."""
    _stub_hub(monkeypatch, tg_linked=linked)
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get(
        "/cabinet/account"
    )
    assert resp.status_code == 200
    _assert_no_russian(resp.text, f"{locale}-telegram(linked={linked})")


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize("state", ["none", "pending", "active"])
def test_hub_email_channel_render_has_no_russian(monkeypatch, locale, state):
    """The “Channels” section, Email, in en/sr in all three states — without Russian chrome."""
    if state == "none":
        st = None
    else:
        st = SimpleNamespace(
            is_live=(state == "active"),
            status=state,
            external_id="john@example.com",
        )
    _stub_hub(monkeypatch, email_state=st)
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get(
        "/cabinet/account"
    )
    assert resp.status_code == 200
    _assert_no_russian(resp.text, f"{locale}-email({state})")


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_profile_form_render_has_no_russian(monkeypatch, locale):
    """GET /profiles/new in en/sr: sections, hints, gate notes and the choice of the
    AI/schedule level (captions from the catalog, not a Russian literal) — clean. The names of the
    districts (Serbian Cyrillic) are data, and the Russian-specific letters are not in them."""
    monkeypatch.setattr(routes_profiles.svc, "get_tenant_tier", _async("lite"))
    monkeypatch.setattr(routes_profiles.svc, "count_active_profiles", _async(0))
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get(
        "/profiles/new?manual=1"
    )
    assert resp.status_code == 200
    _assert_no_russian(resp.text, f"{locale}-новый-профиль")


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_profile_detail_render_has_no_russian(monkeypatch, locale):
    """GET /profiles/{id} in en/sr: AI/channels/schedule captions, buttons, "to the
    library" — clean. The profile name/region code are data."""
    prof = SimpleNamespace(
        id="pppppppp-0000-0000-0000-000000000001",
        name="Nabavka IT",
        is_active=True,
        ai_analysis_level="deep",
        regions=["RS-00"],
        delivery_channels=["telegram", "email"],
        delivery_schedule="digest_custom",
        enabled_source_keys=[],
    )
    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(prof))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    # The sources block of the card reads the registry (SRC-0b); in this test the registry is empty.
    monkeypatch.setattr(source_choices, "list_sources_for_selection", _async([]))
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get(
        "/profiles/pppppppp-0000-0000-0000-000000000001"
    )
    assert resp.status_code == 200
    _assert_no_russian(resp.text, f"{locale}-карточка-профиля")


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize("path", ["/login", "/signup"])
def test_auth_pages_anon_render_has_no_russian(locale, path):
    """‼️ Login/registration are rendered WITHOUT a session (anonymous): the locale is taken from
    ?lang= (the resolver: query → cookie → Accept-Language). Both languages are clean."""
    resp = _client(sess=None, locale=None).get(f"{path}?lang={locale}")
    assert resp.status_code == 200
    assert f'lang="{locale}"' in resp.text  # ?lang= worked for an anonymous user
    _assert_no_russian(resp.text, f"{locale}-{path}")


def test_account_mutation_reintroduced_russian_is_caught(monkeypatch):
    """Mutation anchor of the guard: put one Russian literal back into the catalog with a letter from
    U+044B U+044D U+0449 U+044A U+0451 U+0439 — the en render contains it again. Green without the mutation = the guard is blind."""
    _stub_hub(monkeypatch)
    monkeypatch.setitem(
        i18n.CABINET["en"],
        "tgp_unlinked_body",
        "Telegram ещё не подключён — вы не получаете тендеры.",
    )
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale="en").get("/cabinet/account")
    assert resp.status_code == 200
    assert _RU_SPECIFIC.search(resp.text), "the mutation did not show — the guard does not work"


def test_login_locale_does_not_rebound_after_login():
    """“Does not bounce back after login”: the session carries the locale (set_session_cookie
    puts it into the signature), so after login the chrome follows the saved language,
    and not a random cookie. We check that a user logged in with locale=en sees en
    even with cookie lang=sr (users.locale has priority)."""
    client = _client(sess={"uid": _UID, "tid": _TID}, locale="en")
    resp = client.get("/login", headers={"cookie": "lang=sr"})
    assert resp.status_code == 200
    assert 'lang="en"' in resp.text


# --------------------------------------------------------------------------- #
# Render audit of the DASHBOARD (Phase 1.5): BY THE RENDER RESULT, not by the templates.
# The owner’s complaint of 23.07: “the dashboard is a large block of text in Russian”. The check "no
# Cyrillic in the template" missed it — Russian came from NOT-localized partials,
# included only conditionally (_delivery_gap when there are gaps, _region_hidden_banner
# when the district is narrowed, _onboarding_steps when onboarding is unfinished) and from the
# aria-label of the macro’s default hint. Here ALL the fixture data is in Latin (the names
# of profiles/tenders/buyers — Serbian Latin), so ANY Cyrillic in the
# en/sr render = Russian chrome bypassing the catalog. Mutation —
# test_dashboard_mutation_reintroduced_russian_is_caught.
# --------------------------------------------------------------------------- #
_DASH_NOW = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


def _dash_profiles():
    return [
        SimpleNamespace(
            id="pppppppp-0000-0000-0000-000000000001", name="Gradnja", is_active=True
        ),
        SimpleNamespace(
            id="pppppppp-0000-0000-0000-000000000002",
            name="IT nabavka",
            is_active=False,
        ),
    ]


def _dash_deadline(days, tid, title):
    return feed_service.HotDeadline(
        tender_id=tid,
        title=title,
        customer_name="Opstina Novi Sad",
        deadline_at=_DASH_NOW + timedelta(days=days),
        tender_url="https://portal.rs/t/1",
        days_left=int(days),
    )


def _dash_my_row(days, status, tid, title):
    return SimpleNamespace(
        tender_id=tid,
        title=title,
        customer_name=None,
        deadline_at=_DASH_NOW + timedelta(days=days),
        tender_url="https://portal.rs/t/my",
        status=status,
    )


def _stub_dashboard(monkeypatch):
    """The saturated dashboard fixtures — data in Latin, all conditional blocks visible.

    Onboarding unfinished (no channel connected) → the stepper is visible; delivery_gaps and
    region_hidden are non-empty → both banners are visible; feed_items is empty → the teaser has an
    empty state, and the onboarding step “first alert” is unfinished → the link "how it
    works". downgrade_notice gives the KEYS of gate_profile (gate_sup_*) — under
    the profile the downgrade notice is visible, localized via t: the remainder sentinel
    (the same keys that profile_detail renders). In en/sr there is no Cyrillic in it.
    """
    ch = routes_profiles.channel_svc
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async(_dash_profiles()))
    monkeypatch.setattr(
        routes_profiles.svc,
        "downgrade_notice",
        _async(
            ["gate_sup_ai_deep", "gate_sup_channels_email", "gate_sup_schedule_custom"]
        ),
    )
    monkeypatch.setattr(ch, "has_active_telegram", _async(False))
    monkeypatch.setattr(ch, "dead_channels", _async(["Telegram"]))
    monkeypatch.setattr(
        ch,
        "delivery_channel_status",
        _async(
            {
                "has_live_channel": True,
                "gaps": [
                    {"label": "Email", "pending": True, "link": "/cabinet/email"},
                    {
                        "label": "Telegram",
                        "pending": False,
                        "link": "/cabinet/telegram",
                    },
                ],
                "channelless": [
                    {"id": "pppppppp-0000-0000-0000-000000000001", "name": "Gradnja"}
                ],
            }
        ),
    )
    monkeypatch.setattr(
        routes_profiles.trial_service,
        "get_access_status",
        _async(
            routes_profiles.trial_service.AccessStatus(
                state="active", active=True, can_request=False, until="31.12.2026"
            )
        ),
    )
    monkeypatch.setattr(
        routes_profiles.stats_service,
        "get_system_stats",
        _async(
            stats_service.SystemStats(
                all_time=12345, this_month=678, today=42, since=date(2026, 5, 30)
            )
        ),
    )
    monkeypatch.setattr(
        routes_profiles.stats_service,
        "get_tenant_delivery_stats",
        _async(
            stats_service.TenantStats(
                total=99,
                by_profile=[
                    stats_service.ProfileDelivery(
                        profile_id="pppppppp-0000-0000-0000-000000000001",
                        profile_name="Gradnja",
                        count=50,
                    )
                ],
            )
        ),
    )
    monkeypatch.setattr(
        routes_profiles.stats_service,
        "get_days_left",
        _async(
            stats_service.AccessDays(
                label="5", days=5, until=_DASH_NOW + timedelta(days=5)
            )
        ),
    )
    monkeypatch.setattr(routes_profiles.feed_service, "get_feed_items", _async([]))
    monkeypatch.setattr(
        routes_profiles.feed_service,
        "get_calendar_deadlines",
        _async([_dash_deadline(3, "t-a", "Nabavka racunara")]),
    )
    monkeypatch.setattr(
        routes_profiles.workflow_service,
        "get_my_tenders",
        _async(
            SimpleNamespace(
                funnel=[
                    _dash_my_row(2, "active", "t-1", "Rekonstrukcija"),
                    _dash_my_row(4, "watching", "t-2", "Sanacija"),
                    _dash_my_row(1, "submitted", "t-3", "Izgradnja"),
                ]
            )
        ),
    )
    monkeypatch.setattr(
        routes_profiles.feed_service,
        "get_region_hidden_notice",
        _async(
            feed_service.RegionHiddenNotice(
                hidden=7, profile_id="pppppppp-0000-0000-0000-000000000001"
            )
        ),
    )


@pytest.mark.parametrize("locale", ["en", "sr"])
def test_dashboard_render_has_no_cyrillic_outside_data(monkeypatch, locale):
    """The render of /app in en/sr contains no Cyrillic: the fixture data is in Latin →
    any Cyrillic = Russian chrome (the delivery/district banners, the onboarding stepper,
    the aria-label of hints, the hero tiles, the calendar, the teaser, the settings).

    Mutation: test_dashboard_mutation_reintroduced_russian_is_caught."""
    _stub_dashboard(monkeypatch)
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get("/app")
    assert resp.status_code == 200
    leaked = sorted(
        set(re.findall(r"[а-яёА-ЯЁ][а-яёА-ЯЁ .,«»„“”()/@→↗:-]*", resp.text))
    )
    assert not leaked, f"Russian chrome in the {locale} render of the dashboard: {leaked}"


@pytest.mark.parametrize(
    "locale,title,cta",
    [
        (
            "ru",
            "Канал доставки не выбран",
            "Выбрать канал в «Gradnja» →",
        ),
        (
            "sr",
            "Kanal isporuke nije izabran",
            "Izaberite kanal u „Gradnja“ →",
        ),
        (
            "en",
            "No delivery channel selected",
            "Choose a channel in “Gradnja” →",
        ),
    ],
)
def test_dashboard_channelless_profile_warning_links_to_template_edit(
    monkeypatch, locale, title, cta
):
    """A template without a channel while a channel is alive → a banner with a link to EDIT the template.

    The expectation is a literal, not via ``t()`` (otherwise a tautology)."""
    _stub_dashboard(monkeypatch)
    # v2: ONE banner by priority (dead channel > no channel > limit > template without a
    # channel > gap) — the fixture holds a dead channel, which would displace the one being checked.
    monkeypatch.setattr(routes_profiles.channel_svc, "dead_channels", _async([]))
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get("/app")
    assert resp.status_code == 200
    body = html.unescape(resp.text)
    assert title in body
    assert cta in body
    assert 'href="/profiles/pppppppp-0000-0000-0000-000000000001/edit"' in body


def test_dashboard_no_channel_shows_one_warning_not_two(monkeypatch):
    """No live channel → the amber “template without a channel” is there, the blue nudge does NOT duplicate it.

    Both say “channel not connected” (unconfirmed email / never) — the second
    banner is noise. The expectation is literals from the sr catalog (dgap_title, nochan_title)."""
    _stub_dashboard(monkeypatch)
    ch = routes_profiles.channel_svc
    monkeypatch.setattr(ch, "dead_channels", _async([]))  # v2: otherwise it will displace
    monkeypatch.setattr(
        ch,
        "delivery_channel_status",
        _async(
            {
                "has_live_channel": False,
                "gaps": [{"label": "Email", "pending": True, "link": "/cabinet/email"}],
                "channelless": [],
            }
        ),
    )
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale="sr").get("/app")
    assert resp.status_code == 200
    body = html.unescape(resp.text)
    assert "data-no-channel" in body
    assert 'data-dash-alert="no_channel"' in body
    assert "Isporuka nije do kraja podešena" not in body


def test_dashboard_shows_exactly_one_alert_by_priority(monkeypatch):
    """v2 (owner 13.10): one alert banner, priority "dead > no channel > limit >
    template without a channel > gap". The fixture includes ALL states at once — on the screen
    only the dead channel; we remove them one by one — the next one wins.
    The expectations are literals of the same keys as in the template, via data-dash-alert."""
    _stub_dashboard(monkeypatch)
    ch = routes_profiles.channel_svc

    def alerts():
        resp = _client(sess={"uid": _UID, "tid": _TID}, locale="sr").get(
            "/app?limit=profiles"
        )
        assert resp.status_code == 200
        return re.findall(r'data-dash-alert="([a-z_]+)"', resp.text), resp.text

    found, _ = alerts()
    assert found == ["dead"]

    monkeypatch.setattr(ch, "dead_channels", _async([]))
    status = {
        "has_live_channel": False,
        "gaps": [{"label": "Email", "pending": True, "link": "/cabinet/email"}],
        "channelless": [{"id": "p1", "name": "Gradnja"}],
    }
    monkeypatch.setattr(ch, "delivery_channel_status", _async(status))
    found, _ = alerts()
    assert found == ["no_channel"]

    monkeypatch.setattr(
        ch, "delivery_channel_status", _async({**status, "has_live_channel": True})
    )
    found, body = alerts()
    # The limit is a one-off response to an action: it wins over “template without a channel”.
    assert found == ["limit"]

    # The limit refusal is not lost when it is displaced by a higher banner (the invariant
    # “a limit refusal must be visible”): a paragraph in the same banner.
    monkeypatch.setattr(ch, "dead_channels", _async(["Telegram"]))
    found, body = alerts()
    assert found == ["dead"]
    assert "data-limit-notice" in body

    monkeypatch.setattr(ch, "dead_channels", _async([]))
    monkeypatch.setattr(
        ch, "delivery_channel_status", _async({**status, "has_live_channel": True})
    )
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale="sr").get("/app")
    assert re.findall(r'data-dash-alert="([a-z_]+)"', resp.text) == ["channelless"]


@pytest.mark.parametrize(
    "key,ru",
    [
        ("dgap_title", "Доставка настроена не до конца"),
        ("rhid_cta", "Настроить фильтр округа →"),
        ("onb_heading", "Начало работы"),
        # The profile’s downgrade notice (gate_profile → key gate_sup_*): the anchor that
        # the downgrade notice really resolves through the catalog, not around it.
        ("gate_sup_ai_deep", "Глубокий AI-разбор доступен с тарифа Pro"),
    ],
)
def test_dashboard_mutation_reintroduced_russian_is_caught(monkeypatch, key, ru):
    """Mutation anchor of the guard: put one Russian literal back into the catalog — the render
    of /app in en contains Cyrillic again. Green without the mutation = the guard is blind."""
    _stub_dashboard(monkeypatch)
    if key == "dgap_title":
        # v2: the “gap” banner is shown only if there are no higher-priority ones.
        monkeypatch.setattr(routes_profiles.channel_svc, "dead_channels", _async([]))
        monkeypatch.setattr(
            routes_profiles.channel_svc,
            "delivery_channel_status",
            _async(
                {
                    "has_live_channel": True,
                    "gaps": [{"label": "Email", "pending": True, "link": "/x"}],
                    "channelless": [],
                }
            ),
        )
    monkeypatch.setitem(i18n.CABINET["en"], key, ru)
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale="en").get("/app")
    assert resp.status_code == 200
    assert re.search(
        r"[а-яёА-ЯЁ]", resp.text
    ), "the mutation did not show — the guard does not work"


# --------------------------------------------------------------------------- #
# The “By template” caption in the feed select: honestly says that this is a GROUPING
# (there is no template choice here). The expectation is a literal from the fact, not via t().
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "lang,label",
    [
        ("sr", "Grupisano po šablonu (A–Š)"),
        ("ru", "Группировать по шаблону (А–Я)"),
        ("en", "Group by template (A–Z)"),
    ],
)
def test_feed_sort_profile_label_says_grouping(lang, label):
    assert i18n.CABINET[lang]["ff_sort_profile"] == label
