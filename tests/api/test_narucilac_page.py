"""Tests of the /cabinet/narucilac page (A3): route + render + navigation.

The service is mocked (its logic is in test_narucilac_page_service); here we check the HTTP contract and that the template really draws the metrics/table/empty states.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import routes_narucilac
from src.api.cabinet.customer_analytics_service import (
    AnalyticsExportView,
    CustomerAnalyticsView,
    TrendYearView,
    WinnerView,
)
from src.api.cabinet.narucilac_page_service import DirectoryItemView, NarucilacPageView
from src.api.config import get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


def _session_cookie() -> str:
    settings = get_cabinet_settings()
    return security.sign_session(SESS["uid"], SESS["tid"], settings.cabinet_secret_key)


def _client(*, sess=None):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def _authed_client(monkeypatch, page: NarucilacPageView):
    async def _fake(db, **kwargs):
        _authed_client.last_kwargs = kwargs
        return page

    monkeypatch.setattr(routes_narucilac.svc, "get_narucilac_page", _fake)
    client = _client(sess=SESS)
    client.cookies.set("tb_session", _session_cookie())
    return client


def teardown_function():
    app.dependency_overrides.clear()


_CANDIDATES = [
    DirectoryItemView(
        name="Општина Нови Сад", pib="100236236", awards=12, last_date_fmt="04.05.2026"
    )
]

_PROFILE = CustomerAnalyticsView(
    status="success",
    customer_name="Општина Нови Сад",
    customer_pib="100236236",
    total_awards=10,
    distinct_winners=3,
    with_amount=6,
    total_rsd_fmt="120 000 000",
    total_eur_fmt="1 000 000",
    concentration_pct=60,
    cr3_pct=90,
    is_monopolized=False,
    top_winners=[
        WinnerView(rank=1, name="Лидер д.о.о.", wins=6, sum_rsd_fmt="90 000 000 RSD")
    ],
    trend=[
        TrendYearView(
            year=2025,
            awards=6,
            distinct_winners=3,
            total_rsd_fmt="70 000 000 RSD",
            total_eur_fmt="600 000",
            bar_pct=100,
        ),
        TrendYearView(
            year=2026,
            awards=4,
            distinct_winners=2,
            total_rsd_fmt="50 000 000 RSD",
            total_eur_fmt="400 000",
            bar_pct=67,
        ),
    ],
    avg_rsd_fmt="20 000 000 RSD",
    median_rsd_fmt="15 000 000 RSD",
    period_fmt="12.03.2019 — 04.05.2026",
    without_date=2,
)


def test_anon_redirects_to_login():
    resp = _client(sess=None).get("/cabinet/narucilac")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_directory_renders_candidates(monkeypatch):
    client = _authed_client(
        monkeypatch, NarucilacPageView(status="success", candidates=_CANDIDATES)
    )
    resp = client.get("/cabinet/narucilac")

    assert resp.status_code == 200
    assert "Општина Нови Сад" in resp.text
    assert "Самые активные заказчики" in resp.text


def test_selected_profile_renders_metrics_period_and_winners(monkeypatch):
    client = _authed_client(
        monkeypatch,
        NarucilacPageView(
            status="success",
            selected_name="Општина Нови Сад",
            selected_status="success",
            selected=_PROFILE,
            candidates=_CANDIDATES,
        ),
    )
    resp = client.get(
        "/cabinet/narucilac?name=%D0%9E%D0%BF%D1%88%D1%82%D0%B8%D0%BD%D0%B0"
    )

    assert resp.status_code == 200
    # The sampling period is shown explicitly — an aggregate without it is not interpretable.
    assert "Период выборки" in resp.text
    assert "12.03.2019 — 04.05.2026" in resp.text
    assert "ещё 2 реш. без даты" in resp.text
    # Prices.
    assert "15 000 000 RSD" in resp.text
    assert "медиана" in resp.text
    # Concentration and the winners table.
    assert "концентрация топ-1" in resp.text
    assert "Лидер д.о.о." in resp.text


def test_query_params_reach_service(monkeypatch):
    client = _authed_client(
        monkeypatch, NarucilacPageView(status="success", candidates=_CANDIDATES)
    )
    client.get("/cabinet/narucilac?q=Нови&name=Општина+Нови+Сад")

    assert _authed_client.last_kwargs["term"] == "Нови"
    assert _authed_client.last_kwargs["customer_name"] == "Општина Нови Сад"
    assert _authed_client.last_kwargs["tid"] == "t-1"


def test_gate_pro_renders_cta_not_profile(monkeypatch):
    client = _authed_client(monkeypatch, NarucilacPageView(status="gate_pro"))
    resp = client.get("/cabinet/narucilac")

    assert resp.status_code == 200
    assert "Доступно на тарифе Pro и выше" in resp.text
    assert "/plans" in resp.text
    # The directory is not drawn — the gate has closed it too.
    assert "Самые активные заказчики" not in resp.text


def test_frozen_renders_renew_cta(monkeypatch):
    client = _authed_client(monkeypatch, NarucilacPageView(status="frozen"))
    resp = client.get("/cabinet/narucilac")

    assert resp.status_code == 200
    assert "Доступ приостановлен" in resp.text


def test_no_data_renders_empty_state_not_empty_table(monkeypatch):
    """Empty state: a clear hint, NOT an empty table and not a 500."""
    client = _authed_client(
        monkeypatch,
        NarucilacPageView(
            status="success",
            selected_name="Општина Без Решења",
            selected_status="no_data",
            candidates=_CANDIDATES,
        ),
    )
    resp = client.get("/cabinet/narucilac?name=x")

    assert resp.status_code == 200
    assert "пока нет данных о решениях" in resp.text
    assert "Кто выигрывает чаще всего" not in resp.text


def test_empty_search_renders_hint(monkeypatch):
    client = _authed_client(
        monkeypatch, NarucilacPageView(status="success", term="зззз", candidates=[])
    )
    resp = client.get("/cabinet/narucilac?q=зззз")

    assert resp.status_code == 200
    assert "Заказчики не найдены" in resp.text


def test_candidate_link_urlencodes_name(monkeypatch):
    """A name with '&'/spaces in the link — escaped, otherwise the link breaks."""
    client = _authed_client(
        monkeypatch,
        NarucilacPageView(
            status="success",
            candidates=[
                DirectoryItemView(
                    name="ЈКП A & B", pib="", awards=3, last_date_fmt="04.05.2026"
                )
            ],
        ),
    )
    resp = client.get("/cabinet/narucilac")

    assert "name=%D0%88%D0%9A%D0%9F%20A%20%26%20B" in resp.text


def test_cabinet_nav_marks_narucilac_active(monkeypatch):
    """The section is available from the cabinet navigation, not only by URL."""
    client = _authed_client(monkeypatch, NarucilacPageView(status="success"))
    resp = client.get("/cabinet/narucilac")

    assert resp.status_code == 200
    assert 'href="/cabinet/narucilac" class="nav-link is-active"' in resp.text
    assert 'href="/app" class="nav-link"' in resp.text


@pytest.mark.parametrize("param", ["q", "name"])
def test_overlong_params_render_page_not_422(monkeypatch, param):
    """A long paste into the search — this is a page, not a 422 JSON.

    The service trims it (MAX_TERM_LEN/MAX_NAME_LEN); there is no length validation at the route on purpose — Query(max_length=...) would show the user a validation error.
    """
    client = _authed_client(
        monkeypatch, NarucilacPageView(status="success", candidates=_CANDIDATES)
    )
    resp = client.get(f"/cabinet/narucilac?{param}={'x' * 1000}")

    assert resp.status_code == 200


def test_period_unknown_does_not_claim_extra_undated(monkeypatch):
    """No decision has a date → “N more without a date” would lie: N would be all of them."""
    profile = replace(_PROFILE, period_fmt="", without_date=10, total_awards=10)
    client = _authed_client(
        monkeypatch,
        NarucilacPageView(
            status="success", selected_status="success", selected=profile
        ),
    )
    resp = client.get("/cabinet/narucilac?name=x")

    assert "ни у одного из 10 решений нет даты" in resp.text
    assert "ещё 10 реш. без даты" not in resp.text


def test_period_known_shows_undated_remainder(monkeypatch):
    """Some decisions with dates → the remainder honestly “N more”."""
    client = _authed_client(
        monkeypatch,
        NarucilacPageView(
            status="success", selected_status="success", selected=_PROFILE
        ),
    )
    resp = client.get("/cabinet/narucilac?name=x")

    assert "12.03.2019 — 04.05.2026" in resp.text
    assert "ещё 2 реш. без даты" in resp.text


# --------------------------------------------------------------------------- #
#  The export button + the route /cabinet/narucilac.csv (C4)
# --------------------------------------------------------------------------- #


_SELECTED_PAGE = NarucilacPageView(
    status="success",
    selected_name="ЈКП A & B",
    selected_status="success",
    selected=_PROFILE,
    candidates=_CANDIDATES,
)

_CSV = "﻿".encode("utf-8") + "Наручилац;Општина Нови Сад\r\n".encode("utf-8")


def _export_client(monkeypatch, export: AnalyticsExportView):
    async def _fake(db, **kwargs):
        _export_client.last_kwargs = kwargs
        return export

    monkeypatch.setattr(routes_narucilac.svc, "get_narucilac_export", _fake)
    client = _client(sess=SESS)
    client.cookies.set("tb_session", _session_cookie())
    return client


def test_export_button_shown_on_selected_profile_with_urlencoded_name(monkeypatch):
    """The button leads to the export under the SAME name by which the profile is computed."""
    client = _authed_client(monkeypatch, _SELECTED_PAGE)
    resp = client.get("/cabinet/narucilac?name=x")

    assert "Экспорт в CSV" in resp.text
    assert "/cabinet/narucilac.csv?name=%D0%88%D0%9A%D0%9F%20A%20%26%20B" in resp.text


def test_export_button_absent_without_selected_profile(monkeypatch):
    """The customer is not selected — there is nothing to export, the button is not drawn."""
    client = _authed_client(
        monkeypatch, NarucilacPageView(status="success", candidates=_CANDIDATES)
    )
    resp = client.get("/cabinet/narucilac")

    assert "Экспорт в CSV" not in resp.text


def test_export_button_absent_under_gate(monkeypatch):
    """The gate closed the page — the export button cannot be there."""
    client = _authed_client(monkeypatch, NarucilacPageView(status="gate_pro"))
    resp = client.get("/cabinet/narucilac")

    assert "Экспорт в CSV" not in resp.text


def test_csv_anon_redirects_to_login():
    resp = _client(sess=None).get("/cabinet/narucilac.csv?name=x")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_csv_success_returns_file_headers_and_content(monkeypatch):
    client = _export_client(
        monkeypatch,
        AnalyticsExportView(
            status="success", filename="awards_customer_100236236_all.csv", content=_CSV
        ),
    )
    resp = client.get("/cabinet/narucilac.csv?name=Општина+Нови+Сад")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    assert (
        resp.headers["content-disposition"]
        == 'attachment; filename="awards_customer_100236236_all.csv"'
    )
    # The bytes are served as is: the BOM is in place → Excel will display the Cyrillic correctly.
    assert resp.content == _CSV
    assert resp.content.startswith(b"\xef\xbb\xbf")


def test_csv_passes_selected_name_and_tenant_to_service(monkeypatch):
    client = _export_client(
        monkeypatch,
        AnalyticsExportView(status="success", filename="a.csv", content=_CSV),
    )
    client.get("/cabinet/narucilac.csv?name=Општина+Нови+Сад")

    assert _export_client.last_kwargs["customer_name"] == "Општина Нови Сад"
    assert _export_client.last_kwargs["tid"] == "t-1"


@pytest.mark.parametrize("status", ["gate_pro", "frozen"])
def test_csv_gate_redirects_to_plans_not_bare_403(monkeypatch, status):
    """Gate / freeze — a CTA to the plans (as the export from the tender page does)."""
    client = _export_client(monkeypatch, AnalyticsExportView(status=status))
    resp = client.get("/cabinet/narucilac.csv?name=x")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/plans"


@pytest.mark.parametrize("status", ["no_data", "no_customer"])
def test_csv_nothing_to_export_is_404(monkeypatch, status):
    client = _export_client(monkeypatch, AnalyticsExportView(status=status))
    resp = client.get("/cabinet/narucilac.csv?name=x")

    assert resp.status_code == 404
    assert "Нечего выгружать" in resp.text


# --------------------------------------------------------------------------- #
#  Sorting / pagination / search in the directory
# --------------------------------------------------------------------------- #


def _dir_page(**over):
    base = dict(
        status="success",
        term="",
        candidates=_CANDIDATES,
        sort="decisions_desc",
        page=1,
        total_pages=1,
        total=1,
    )
    base.update(over)
    return NarucilacPageView(**base)


def test_route_passes_sort_and_page_to_service(monkeypatch):
    client = _authed_client(monkeypatch, _dir_page())
    assert client.get("/cabinet/narucilac?sort=last_desc&page=2").status_code == 200
    kw = _authed_client.last_kwargs
    assert (kw["sort"], kw["page"]) == ("last_desc", 2)


def test_route_garbage_page_is_page_one_not_422(monkeypatch):
    client = _authed_client(monkeypatch, _dir_page())
    assert client.get("/cabinet/narucilac?page=abc").status_code == 200
    assert _authed_client.last_kwargs["page"] == 1


def test_headers_are_sort_links_with_aria_sort(monkeypatch):
    client = _authed_client(monkeypatch, _dir_page(sort="last_desc", term="нови"))
    html = client.get("/cabinet/narucilac").text

    # The active column — descending, the name — none; q is kept in the links.
    assert html.count('aria-sort="descending"') == 1
    assert html.count('aria-sort="none"') == 2
    assert (
        'href="/cabinet/narucilac?sort=name_asc&amp;q=%D0%BD%D0%BE%D0%B2%D0%B8"' in html
    )
    assert 'href="/cabinet/narucilac?sort=decisions_desc&amp;q=' in html


def test_name_sort_is_ascending_aria(monkeypatch):
    client = _authed_client(monkeypatch, _dir_page(sort="name_asc"))
    html = client.get("/cabinet/narucilac").text
    assert html.count('aria-sort="ascending"') == 1


def test_pager_links_keep_q_and_sort(monkeypatch):
    client = _authed_client(
        monkeypatch,
        _dir_page(term="нови", sort="name_asc", page=2, total_pages=3, total=50),
    )
    html = client.get("/cabinet/narucilac?page=2").text

    assert 'rel="prev"' in html and 'rel="next"' in html
    assert (
        'href="/cabinet/narucilac?sort=name_asc&amp;q=%D0%BD%D0%BE%D0%B2%D0%B8"' in html
    )
    assert (
        'href="/cabinet/narucilac?sort=name_asc&amp;page=3&amp;q=%D0%BD%D0%BE%D0%B2%D0%B8"'
        in html
    )
    assert '<span class="fd-pager__num" aria-current="page">2</span>' in html


def test_no_pager_on_single_page(monkeypatch):
    client = _authed_client(monkeypatch, _dir_page())
    html = client.get("/cabinet/narucilac").text
    assert 'rel="next"' not in html and "fd-pager" not in html


def test_search_box_labelled_and_reset_only_with_query(monkeypatch):
    client = _authed_client(monkeypatch, _dir_page(sort="last_desc"))
    html = client.get("/cabinet/narucilac").text
    assert 'for="q"' in html and "Название или PIB" in html
    assert 'name="sort" value="last_desc"' in html
    assert 'data-testid="nar-reset"' not in html

    client = _authed_client(monkeypatch, _dir_page(term="нови"))
    html = client.get("/cabinet/narucilac?q=нови").text
    assert 'data-testid="nar-reset"' in html
    # The reset must lose q (owner review 02.10: the link reloaded the same results).
    assert (
        '<a href="/cabinet/narucilac?sort=decisions_desc" class="btn-secondary" data-testid="nar-reset"'
        in html
    )


@pytest.mark.parametrize("raw", ["²", "99999999999999999999", "-1", "abc"])
def test_bad_page_param_is_not_500(monkeypatch, raw):
    client = _authed_client(monkeypatch, _dir_page())
    assert client.get(f"/cabinet/narucilac?page={raw}").status_code == 200


def test_row_link_keeps_sort_page_and_q(monkeypatch):
    client = _authed_client(
        monkeypatch, _dir_page(term="нови", sort="name_asc", page=2, total_pages=3)
    )
    html = client.get("/cabinet/narucilac").text
    assert (
        'href="/cabinet/narucilac?sort=name_asc&amp;page=2&amp;q=%D0%BD%D0%BE%D0%B2%D0%B8'
        "&amp;name=%D0%9E%D0%BF%D1%88%D1%82%D0%B8%D0%BD%D0%B0%20%D0%9D%D0%BE%D0%B2%D0%B8%20%D0%A1%D0%B0%D0%B4"
        in html
    )


def test_pager_window_shows_edges_current_and_ellipses(monkeypatch):
    client = _authed_client(
        monkeypatch, _dir_page(sort="name_asc", page=6, total_pages=120, total=2400)
    )
    html = client.get("/cabinet/narucilac?sort=name_asc&page=6").text

    assert '<span class="fd-pager__num" aria-current="page">6</span>' in html
    assert (
        'href="/cabinet/narucilac?sort=name_asc" class="btn-ghost fd-pager__num"'
        in html
    )
    # 4 and 8 — at a distance of 2 from the current one: a wide window, hidden below <640px (fd-pager__wide).
    for n, cls in (
        (4, "fd-pager__num fd-pager__wide"),
        (5, "fd-pager__num"),
        (7, "fd-pager__num"),
        (8, "fd-pager__num fd-pager__wide"),
        (120, "fd-pager__num"),
    ):
        assert (
            f'href="/cabinet/narucilac?sort=name_asc&amp;page={n}" class="btn-ghost {cls}"'
            in html
        )
    assert 'aria-label="стр. 9"' not in html
    assert html.count('class="fd-pager__gap"') == 2


def test_pager_edges_on_first_page_are_disabled_not_links(monkeypatch):
    client = _authed_client(monkeypatch, _dir_page(page=1, total_pages=3, total=50))
    html = client.get("/cabinet/narucilac").text

    assert 'rel="first"' not in html and 'rel="prev"' not in html
    assert 'rel="last"' in html and 'rel="next"' in html


def test_pager_jump_form_keeps_sort_q_and_name_as_hidden_fields(monkeypatch):
    client = _authed_client(
        monkeypatch,
        _dir_page(term="нови", sort="name_asc", page=2, total_pages=3, total=50),
    )
    html = client.get(
        "/cabinet/narucilac?sort=name_asc&q=%D0%BD%D0%BE%D0%B2%D0%B8&name=A&page=2"
    ).text

    assert 'action="/cabinet/narucilac"' in html
    assert 'name="page" min="1" max="3"' in html
    assert '<input type="hidden" name="sort" value="name_asc">' in html
    assert '<input type="hidden" name="q" value="нови">' in html
    assert '<input type="hidden" name="name" value="A">' in html
    assert 'name="page" value=' not in html


def test_route_past_the_end_is_rendered_on_last_page(monkeypatch):
    seen = {}

    async def _fake(db, **kwargs):
        seen.update(kwargs)
        return _dir_page(page=3, total_pages=3, total=50)

    monkeypatch.setattr(routes_narucilac.svc, "get_narucilac_page", _fake)
    client = _client(sess=SESS)
    client.cookies.set("tb_session", _session_cookie())
    resp = client.get("/cabinet/narucilac?page=9999")

    assert resp.status_code == 200
    assert seen["page"] == 9999
    assert '<span class="fd-pager__num" aria-current="page">3</span>' in resp.text
