"""Tests of the route ``GET /app/feed`` — the web archive of matches (cabinet, pagination).

DB/session/service are mocked (ADR-013). We check the session gate (anonymous → login), the page render, the passing of ?page= to the service and the pagination controls. The pagination logic of the service itself is not duplicated (see test_feed_service).
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import feed_service, routes_profiles
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


def _client(*, sess=None):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _item(tender_id="aaaa", title="Набавка рачунара"):
    return feed_service.FeedItem(
        tender_id=tender_id,
        title=title,
        customer_name="Општина Нови Сад",
        deadline_at=datetime.now(timezone.utc),
        tender_url="https://portal.rs/t/1",
        profile_id="pppp",
        profile_name="ИТ набавке",
        ai_level=None,
        sent_at=datetime.now(timezone.utc),
        ai_actions=feed_service.AiActions(
            delivered_analysis_level=None,
            has_bid_verdict=False,
            has_checklist=False,
            has_draft=False,
        ),
    )


def test_feed_archive_anonymous_redirected():
    resp = _client(sess=None).get("/app/feed")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Fapp%2Ffeed"


def test_feed_archive_renders(monkeypatch):
    monkeypatch.setattr(feed_service, "count_feed_total", _async(1))
    monkeypatch.setattr(feed_service, "get_feed_page", _async([_item()]))
    resp = _client(sess=SESS).get("/app/feed")
    assert resp.status_code == 200
    assert "Набавка рачунара" in resp.text
    # The heading promises exactly what is shown: the expired ones are no longer in the selection.
    assert "Мои совпадения" in resp.text


def test_feed_archive_empty_state(monkeypatch):
    monkeypatch.setattr(feed_service, "count_feed_total", _async(0))
    monkeypatch.setattr(feed_service, "get_feed_page", _async([]))
    resp = _client(sess=SESS).get("/app/feed")
    assert resp.status_code == 200
    assert "Текущих совпадений нет." in resp.text


@pytest.mark.parametrize("junk", ["0", "-3", "abc", "1.5", "2²"])
def test_feed_archive_junk_page_is_first_page(monkeypatch, junk):
    asked = []

    async def _page(*args, **kwargs):
        asked.append(kwargs["page"])
        return [_item()]

    monkeypatch.setattr(feed_service, "count_feed_total", _async(45))
    monkeypatch.setattr(feed_service, "get_feed_page", _page)
    resp = _client(sess=SESS).get(f"/app/feed?page={junk}")
    assert resp.status_code == 200
    assert asked == [1]


def test_feed_archive_page_past_last_is_last_page(monkeypatch):
    # 45 matches at 20 per page → 3 pages. A request past the end → page 3.
    asked = []

    async def _page(*args, **kwargs):
        asked.append(kwargs["page"])
        return [_item()]

    monkeypatch.setattr(feed_service, "count_feed_total", _async(45))
    monkeypatch.setattr(feed_service, "get_feed_page", _page)
    resp = _client(sess=SESS).get("/app/feed?page=9999")
    assert resp.status_code == 200
    assert asked == [9999, 3]
    assert '<span class="fd-pager__num" aria-current="page">3</span>' in resp.text


# --------------------------------------------------------------------------- #
# Stage 3: the card in the cockpit language, the AI cluster, the urgency sections, the animation
# --------------------------------------------------------------------------- #


def _item_deadline(days: int, tender_id="dl", **ai):
    """A match with a deadline in ``days`` days (negative — expired).

    Minus a minute — so that ``days_left`` (rounding up) shows EXACTLY ``days``: the test must rely on the same number the subscriber sees.
    """
    it = _item(tender_id=tender_id)
    return feed_service.FeedItem(
        **{
            **it.__dict__,
            "deadline_at": datetime.now(timezone.utc)
            + timedelta(days=days, minutes=-1),
            "ai_actions": feed_service.AiActions(
                delivered_analysis_level=ai.get("analysis"),
                has_bid_verdict=ai.get("bid", False),
                has_checklist=ai.get("checklist", False),
                has_draft=ai.get("draft", False),
            ),
        }
    )


def _render(monkeypatch, items, *, total=None, page=1):
    monkeypatch.setattr(
        feed_service, "count_feed_total", _async(len(items) if total is None else total)
    )
    monkeypatch.setattr(feed_service, "get_feed_page", _async(items))
    resp = _client(sess=SESS).get(f"/app/feed?page={page}")
    assert resp.status_code == 200
    return resp.text


def test_feed_archive_groups_urgent_into_own_section(monkeypatch):
    """The burning (≤7 d.) is separated by a subheading from the rest — scannability."""
    html = _render(monkeypatch, [_item_deadline(2, "hot"), _item_deadline(40, "cold")])
    assert "Скоро дедлайн" in html
    assert "Остальные совпадения" in html
    # An urgent card gets an accent spine, a distant one does not.
    assert html.count("fd-card is-urgent") == 1


def test_feed_archive_hides_group_headers_when_nothing_burns(monkeypatch):
    """No urgent ones — no two headings either: nothing to divide, we do not draw the noise."""
    html = _render(monkeypatch, [_item_deadline(40, "cold")])
    assert "Скоро дедлайн" not in html
    assert "Остальные совпадения" not in html
    assert "fd-card" in html  # the card stays in place


def test_feed_archive_expired_is_not_urgent(monkeypatch):
    """An expired deadline is not “burning”: the application can no longer be submitted."""
    html = _render(monkeypatch, [_item_deadline(-5, "old")])
    assert "Скоро дедлайн" not in html
    assert "срок подачи истёк" in html
    assert "fd-card is-urgent" not in html


def test_feed_archive_shows_single_cta_when_no_ai_ready(monkeypatch):
    """Nothing generated — one visible CTA instead of a scatter of badges."""
    html = _render(monkeypatch, [_item_deadline(3, "x")])
    assert "fd-card__cta" in html
    assert "🧠 Открыть AI-разбор →" in html
    assert "Готово:" not in html
    assert "🎯 вердикт" not in html


def test_feed_archive_shows_ready_cluster_when_ai_exists(monkeypatch):
    """The ready artifacts — in a compact set “Ready”, the links are the same (#anchors)."""
    html = _render(
        monkeypatch,
        [_item_deadline(3, "x", analysis="standard", bid=True, checklist=True)],
    )
    assert "Готово:" in html
    assert "/app/tenders/x?profile_id=pppp#analysis" in html
    assert "/app/tenders/x?profile_id=pppp#bid" in html
    assert "/app/tenders/x?profile_id=pppp#checklist" in html
    assert "📝 черновик" not in html  # no draft — we do not invent one
    assert "fd-card__cta" not in html


def test_feed_archive_loads_animation_script_with_sri(monkeypatch):
    """The GSAP entrance is connected by an external file with SRI and defer (inline JS is forbidden by CSP)."""
    html = _render(monkeypatch, [_item_deadline(3, "x")])
    assert '<script src="/static/js/feed.js?v=' in html
    assert 'integrity="sha384-' in html
    assert "defer" in html
    assert "<script>" not in html  # not a single inline block


def test_feed_js_respects_reduced_motion_and_missing_gsap():
    """Animations are off under reduce and when GSAP failed to load."""
    js = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "api"
        / "static"
        / "js"
        / "feed.js"
    ).read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in js
    assert "if (reduceMotion) return;" in js
    assert "if (!gsap) return;" in js


def test_feed_archive_does_not_reorder_service_result(monkeypatch):
    """The order of the selection is not touched: inside the section the rows go as they came."""
    items = [_item_deadline(1, "first"), _item_deadline(5, "second")]
    html = _render(monkeypatch, items)
    assert html.index("/app/tenders/first") < html.index("/app/tenders/second")


# --------------------------------------------------------------------------- #
# Stage 4: a filter by profile (chips, pagination, a foreign id)
# --------------------------------------------------------------------------- #


def _profiles(*ids):
    """The tenant’s profiles as svc.list_profiles gives them (id + name)."""
    return [SimpleNamespace(id=i, name=f"Профиль {i}", is_active=True) for i in ids]


def _render_filtered(monkeypatch, *, profiles, query="", items=None, total=1):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async(profiles))
    monkeypatch.setattr(feed_service, "count_feed_total", _async(total))
    monkeypatch.setattr(
        feed_service, "get_feed_page", _async([_item()] if items is None else items)
    )
    resp = _client(sess=SESS).get(f"/app/feed{query}")
    assert resp.status_code == 200
    return resp.text


def test_feed_filter_hidden_for_single_profile(monkeypatch):
    """One profile — nothing to filter, we do not draw the noise."""
    html = _render_filtered(monkeypatch, profiles=_profiles("p-1"))
    assert "Все шаблоны" not in html


def test_feed_card_shows_profile_as_clickable_chip(monkeypatch):
    """“Matches with” — a noticeable tag, not a faded line; it leads to the filter."""
    html = _render_filtered(monkeypatch, profiles=_profiles("pppp", "p-2"))
    assert '<a href="/app/feed?profile_id=pppp" class="fd-card__profile"' in html
    assert "ИТ набавке" in html


def test_urgent_section_and_coloured_badge_share_one_boundary(
    monkeypatch, freeze_feed_clock
):
    """‼️ The window of the section “Deadline soon” == the window of the COLOURED badge, one boundary.

    In the code these are two literals (`FEED_URGENT_WINDOW_DAYS` in Python and `<= 7` in the cascade of the badge), therefore the test holds the invariant: if they diverge, the card lands in “Deadline soon” with a muted date “until DD.MM”, or vice versa.

    A calm date (>7 d.) is `badge-muted`, NOT `badge-expired`: the gray “expired” and the gray “plenty of time” are different meanings, and one token for both confused them (Part 2.1).

    The clocks are frozen: the deadlines are calendar (7 and 8 days in Belgrade), not “+N×24h”, which slide by a day across the switch to winter time.
    """
    deadline_in = freeze_feed_clock(datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    on_edge = _render(
        monkeypatch,
        [replace(_item_deadline(7, "edge"), deadline_at=deadline_in(7))],
    )
    assert "Скоро дедлайн" in on_edge
    assert "badge-new" in on_edge  # colored, not a muted date
    assert "badge-expired" not in on_edge

    past_edge = _render(
        monkeypatch,
        [replace(_item_deadline(8, "past"), deadline_at=deadline_in(8))],
    )
    assert "Скоро дедлайн" not in past_edge
    assert "badge-new" not in past_edge
    assert "badge-muted" in past_edge  # muted date “until DD.MM”
    assert "badge-expired" not in past_edge  # a calm deadline ≠ an expired one


# --------------------------------------------------------------------------- #
# owner 22.09: the mode “all / only current” (the second .seg next to the sorting)
# --------------------------------------------------------------------------- #


def test_feed_archive_lede_switches_with_actual_mode(monkeypatch):
    """The feed caption is honest about what is really shown (owner: “all” ≠ “only current”)."""
    html_default = _render_filtered(monkeypatch, profiles=_profiles("p-1"))
    assert "Прошедшие из рабочей ленты уходят" in html_default

    html_all = _render_filtered(
        monkeypatch, profiles=_profiles("p-1"), query="?actual=0"
    )
    assert "включая тендеры с истёкшим сроком подачи" in html_all
    assert "Прошедшие из рабочей ленты уходят" not in html_all


# --------------------------------------------------------------------------- #
# Search / filters / sorting of the feed (TASK feed_filters)
# --------------------------------------------------------------------------- #


def _render_q(monkeypatch, query="", *, profiles=None, items=None, total=None, **kw):
    """The render of /app/feed with a mocked DB; returns (html, filters, tenant_id)."""
    seen: dict = {}
    page_items = [_item()] if items is None else items

    async def _listing(db, tid, f, *, page, page_size):
        seen["f"], seen["tid"], seen["page"] = f, tid, page
        return page_items, (len(page_items) if total is None else total), False

    monkeypatch.setattr(
        routes_profiles.svc,
        "list_profiles",
        _async(_profiles("p-1") if profiles is None else profiles),
    )
    monkeypatch.setattr(feed_service, "get_feed_listing", kw.get("listing", _listing))
    resp = _client(sess=SESS).get(f"/app/feed{query}")
    assert resp.status_code == 200
    return resp.text, seen.get("f"), seen.get("tid")


def test_feed_pagination_controls_keep_page_semantics(monkeypatch):
    html, _, _ = _render_q(monkeypatch, "?page=2", total=45)
    assert '<span class="fd-pager__num" aria-current="page">2</span>' in html
    assert 'href="/app/feed" class="btn-secondary fd-pager__step" rel="prev"' in html
    assert (
        'href="/app/feed?page=3" class="btn-secondary fd-pager__step" rel="next"'
        in html
    )


def test_feed_pagination_preserves_every_filter(monkeypatch):
    """‼️ All filters and the sorting survive the paging (without JS, by links)."""
    html, f, _ = _render_q(
        monkeypatch,
        "?page=2&q=skola&region=RS-00&sort=amount_desc&min_amount=100&actual=0",
        total=45,
    )
    tail = (
        "q=skola&amp;region=RS-00&amp;min_amount=100&amp;actual=0&amp;sort=amount_desc"
    )
    assert (
        f'href="/app/feed?{tail}" class="btn-secondary fd-pager__step" rel="prev"'
        in html
    )
    assert f'href="/app/feed?{tail}&amp;page=3"' in html


def test_feed_sort_visible_with_single_profile(monkeypatch):
    """‼️ One template: the sorting is there, the choice of a template and “By template” are not."""
    html, _, _ = _render_q(monkeypatch, profiles=_profiles("p-1"))
    assert '<select id="pf-sort" name="sort"' in html
    for key in ("date", "deadline", "amount_desc", "amount_asc"):
        assert f'<option value="{key}"' in html
    assert '<option value="profile"' not in html
    assert 'name="profile_id"' not in html


def test_feed_profile_select_and_sort_by_profile_with_many_profiles(monkeypatch):
    html, f, _ = _render_q(
        monkeypatch,
        "?profile_id=p-2&sort=profile",
        profiles=_profiles("p-1", "p-2"),
    )
    assert '<option value="profile" selected>' in html
    assert '<option value="p-2" selected>Профиль p-2</option>' in html
    assert (f.profile_id, f.sort) == ("p-2", "profile")
    # the chip of the active template with the name and the remove cross
    assert "Шаблон: Профиль p-2" in html


def test_feed_filters_parsed_and_passed_with_tenant(monkeypatch):
    profiles = [SimpleNamespace(id="p-1", name="П", enabled_source_keys=["jnportal"])]
    html, f, tid = _render_q(
        monkeypatch,
        "?q=Škola+puta&customer=Zavod&region=RS-00&source=jnportal"
        "&min_amount=10&max_amount=500&no_amount=1&published_from=2026-09-01"
        "&published_to=2026-09-30&deadline_to=2026-12-31&actual=0&sort=amount_asc",
        profiles=profiles,
    )
    assert tid == "t-1"
    assert f.q_tokens == ("skola", "puta")
    assert (f.customer, f.region, f.source) == ("Zavod", "RS-00", "jnportal")
    assert str(f.min_amount) == "10.00" and str(f.max_amount) == "500.00"
    assert f.no_amount and not f.actual and f.sort == "amount_asc"
    assert str(f.deadline_to) == "2026-12-31"
    assert '<details class="pf__more" open>' in html  # nested filters are expanded


def test_feed_filters_ignore_garbage_and_foreign_values(monkeypatch):
    profiles = [SimpleNamespace(id="p-1", name="П", enabled_source_keys=["jnportal"])]
    _, f, _ = _render_q(
        monkeypatch,
        "?source=not-mine&region=XX&profile_id=p-other&sort=DROP+TABLE"
        "&min_amount=abc&deadline_to=2026-13-40&lang=sr",
        profiles=profiles,
    )
    assert (f.source, f.region, f.profile_id) == ("", "", "")
    assert f.sort == "date" and f.min_amount is None and f.deadline_to is None
    assert f.lang == ""  # the axis of the interface language: the query-lang is not read in the cabinet


def test_feed_source_options_only_tenant_enabled(monkeypatch):
    profiles = [SimpleNamespace(id="p-1", name="П", enabled_source_keys=["jnportal"])]
    html, _, _ = _render_q(monkeypatch, profiles=profiles)
    assert '<option value="jnportal">Portal javnih nabavki (UJN)</option>' in html


def test_feed_found_count_chips_and_reset(monkeypatch):
    html, _, _ = _render_q(monkeypatch, "?q=skola&region=RS-00", total=7)
    assert 'role="status">Найдено: 7</p>' in html
    assert 'href="/app/feed?region=RS-00" class="pf__chip"' in html  # remove q
    assert 'href="/app/feed?q=skola" class="pf__chip"' in html  # remove region
    assert 'href="/app/feed" class="pf__reset">Сбросить всё</a>' in html
    assert "×" in html


def test_feed_filters_form_is_get_without_js_and_summary_is_first(monkeypatch):
    html, _, _ = _render_q(monkeypatch)
    assert '<form method="get" action="/app/feed"' in html
    assert re.search(r'<details class="pf__more">\s*<summary', html)
    assert "<script>" not in html and " onclick=" not in html


def test_feed_empty_under_filters_offers_reset(monkeypatch):
    html, _, _ = _render_q(monkeypatch, "?q=nema", items=[], total=0)
    assert "Ни одно совпадение не подходит под поиск и фильтры." in html
    assert 'role="status">Найдено: 0</p>' in html


def test_feed_form_hidden_when_feed_empty_and_no_filters(monkeypatch):
    html, _, _ = _render_q(monkeypatch, items=[], total=0)
    assert "Текущих совпадений нет." in html
    assert 'name="q"' not in html


def test_feed_timeout_shows_hint_not_500(monkeypatch):
    async def _timed_out(db, tid, f, *, page, page_size):
        return [], 0, True

    html, _, _ = _render_q(monkeypatch, "?q=skola", items=[], listing=_timed_out)
    assert "Поиск занял слишком много времени" in html


def test_feed_filter_templates_have_no_cyrillic_outside_comments():
    """The texts — only from the cabinet catalog (ru/sr/en), not from the template."""
    root = Path(__file__).resolve().parents[2] / "src" / "api" / "templates"
    for name in ("_feed_filters.html", "feed_archive.html"):
        body = re.sub(
            r"\{#.*?#\}", "", (root / name).read_text(encoding="utf-8"), flags=re.S
        )
        assert not re.search("[А-Яа-яЁё]", body), name


def test_feed_filter_keys_present_in_all_three_catalogs():
    from src.api.cabinet.i18n import CABINET

    keys = {k for k in CABINET["ru"] if k.startswith("ff_")}
    assert {"ff_found_n", "ff_reset_all", "ff_sort_amount_desc"} <= keys
    for lang in ("sr", "en"):
        assert keys == {k for k in CABINET[lang] if k.startswith("ff_")}


def test_feed_empty_search_offers_same_query_over_all_tenders(monkeypatch):
    """An empty search result → “Search all tenders →” with the same q (owner 02.10).

    Mutation: remove pf.q from the link in feed_archive.html — the test goes red.
    """
    html, _, _ = _render_q(monkeypatch, "?q=skola+racunar", items=[], total=0)
    assert (
        'href="/tenderi?q=skola%20racunar" class="btn-ghost">'
        "Искать во всех тендерах →</a>" in html
    )


def test_feed_empty_without_query_has_no_all_tenders_link(monkeypatch):
    html, _, _ = _render_q(monkeypatch, "?region=RS-00", items=[], total=0)
    assert "/tenderi?q=" not in html
    html, _, _ = _render_q(monkeypatch, "", items=[], total=0)
    assert "/tenderi?q=" not in html


def test_feed_source_filter_links_to_izvori(monkeypatch):
    async def _sources(db, keys):
        return [SimpleNamespace(source_key="jnportal", display_name="Portal JN")]

    monkeypatch.setattr(routes_profiles, "list_sources_for_selection", _sources)
    profiles = [
        SimpleNamespace(
            id="p-1", name="П", is_active=True, enabled_source_keys=("jnportal",)
        )
    ]
    html, _, _ = _render_q(monkeypatch, "", profiles=profiles)
    assert (
        'href="/izvori" class="text-xs hover:underline">Что даёт каждая площадка →'
        in html
    )


# --------------------------------------------------------------------------- #
# The sorting “By template”: the group headings, the link “only this template”,
# the mark “continued” on a page that starts in the middle of a group.
# --------------------------------------------------------------------------- #


def _item_in(tender_id, pid, pname):
    it = _item(tender_id=tender_id)
    return feed_service.FeedItem(
        **{**it.__dict__, "profile_id": pid, "profile_name": pname}
    )


def _profile_groups():
    return [
        _item_in("g1", "p-1", "Альфа-шаблон"),
        _item_in("g2", "p-1", "Альфа-шаблон"),
        _item_in("g3", "p-2", "Бета-шаблон"),
    ]


def test_feed_profile_sort_has_group_headers_in_name_order(monkeypatch):
    html, _, _ = _render_q(
        monkeypatch,
        "?sort=profile",
        profiles=_profiles("p-1", "p-2"),
        items=_profile_groups(),
        total=3,
    )
    alpha = '<h2 class="fd-group__title" id="fd-prof-1">Альфа-шаблон</h2>'
    beta = '<h2 class="fd-group__title" id="fd-prof-3">Бета-шаблон</h2>'
    assert alpha in html and beta in html
    assert html.index(alpha) < html.index(beta)
    assert html.count('class="fd-group__title"') == 2  # one heading per group
    assert (
        html.count('class="fd-card__profile"') == 3
    )  # one tag per card, without duplicates
    assert "Скоро дедлайн" not in html  # the urgent section is not built in the grouping


def test_feed_profile_sort_links_only_this_profile_keeping_others(monkeypatch):
    html, _, _ = _render_q(
        monkeypatch,
        "?sort=profile&q=škola",
        profiles=_profiles("p-1", "p-2"),
        items=_profile_groups(),
        total=3,
    )
    assert "только этот шаблон" in html
    assert 'href="/app/feed?q=%C5%A1kola&amp;sort=profile&amp;profile_id=p-2"' in html


def test_feed_date_sort_has_no_group_headers(monkeypatch):
    html, _, _ = _render_q(
        monkeypatch,
        "",
        profiles=_profiles("p-1", "p-2"),
        items=_profile_groups(),
        total=3,
    )
    assert 'class="fd-group__title" id="fd-prof-' not in html
    assert "только этот шаблон" not in html
    assert "продолжение" not in html


def test_feed_profile_sort_marks_continued_group_on_later_page(monkeypatch):
    """Page 2 starts in the middle of the group p-1: a heading with the mark “continued”.

    The previous row (the last on page 1) — from get_feed_page with a window of 1 row.
    """

    async def _prev_row(db, tid, *, page, page_size, **kw):
        assert page_size == 1
        return [_item_in("prev", "p-1", "Альфа-шаблон")]

    monkeypatch.setattr(feed_service, "get_feed_page", _prev_row)
    html, _, _ = _render_q(
        monkeypatch,
        "?sort=profile&page=2",
        profiles=_profiles("p-1", "p-2"),
        items=[
            _item_in("g9", "p-1", "Альфа-шаблон"),
            _item_in("g10", "p-2", "Бета-шаблон"),
        ],
        total=45,
    )
    assert html.count("продолжение") == 1
    assert html.index("продолжение") < html.index("Бета-шаблон</h2>")
    assert 'id="fd-prof-1">Альфа-шаблон' in html


def test_feed_profile_sort_page_one_has_no_continuation_mark(monkeypatch):
    html, _, _ = _render_q(
        monkeypatch,
        "?sort=profile",
        profiles=_profiles("p-1", "p-2"),
        items=_profile_groups(),
        total=3,
    )
    assert "продолжение" not in html
