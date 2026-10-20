"""Competition block: pure functions and partial render in three languages (no DB).

SQL (order, window, lot collapse, exclusion of the current tender) is checked by
``tests/db/test_competition_sql.py`` on live PG — mocks do not see it.
"""

from datetime import date

import pytest

from src.api.cabinet.competition_service import (
    CompetitionView,
    RivalView,
    cpv_group,
    window_start,
)
from src.api.cabinet.i18n import cabinet_context_for_lang
from src.api.templating import templates


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2026, 10, 13), date(2024, 10, 13)),
        (date(2026, 2, 28), date(2024, 2, 28)),
        (date(2026, 1, 5), date(2024, 1, 5)),
    ],
)
def test_window_start_24_months(today, expected):
    assert window_start(today) == expected


def test_window_start_clamps_month_end():
    assert window_start(date(2026, 10, 31), months=4) == date(2026, 6, 30)
    assert window_start(date(2028, 2, 29), months=24) == date(2026, 2, 28)
    assert window_start(date(2026, 3, 31), months=1) == date(2026, 2, 28)


def test_cpv_group_takes_first_cpv_three_digits():
    cls = [
        {"scheme": "PLAN", "year": 2026},
        {"scheme": "CPV", "code": "33183100"},
        {"scheme": "CPV", "code": "14210000"},
    ]
    assert cpv_group(cls) == "331"


@pytest.mark.parametrize(
    "cls", [None, [], "x", [{"scheme": "PLAN"}], [{"scheme": "CPV", "code": "3"}]]
)
def test_cpv_group_empty_when_no_usable_code(cls):
    assert cpv_group(cls) == ""


def _render(lang, view):
    return templates.get_template("_competition.html").render(
        **cabinet_context_for_lang(lang), competition=view
    )


_RIVAL = RivalView("Alfa DOO", "111", 4, "1 200 RSD", "01.09.2026", False)
_JOINT = RivalView("A, B", "", 1, "", "", True)


@pytest.mark.parametrize(
    "lang,head,cust,cpv,none,hidden",
    [
        (
            "sr",
            "Konkurencija",
            "Kod ovog naručioca",
            "CPV 331",
            "Nema podataka",
            "nije objavljen",
        ),
        (
            "ru",
            "Конкуренция",
            "У этого заказчика",
            "CPV 331",
            "Нет данных",
            "не опубликована",
        ),
        (
            "en",
            "Competition",
            "With this buyer",
            "CPV group 331",
            "No data",
            "not published",
        ),
    ],
)
def test_render_rows_and_labels_per_language(lang, head, cust, cpv, none, hidden):
    view = CompetitionView(
        customer_name="Zavod X",
        cpv_group="331",
        customer=[_RIVAL, _JOINT],
        cpv=[],
    )
    html = _render(lang, view)
    assert head in html and cust in html and cpv in html
    assert "Alfa DOO" in html and "PIB 111" in html and "1 200 RSD" in html
    assert "01.09.2026" in html
    # Consortium: badge, without the PIB line; amount not published — not “0 RSD”.
    assert hidden in html and ">0 RSD<" not in html
    # Empty CPV breakdown — an honest “no data”, not an empty table.
    assert none in html


def test_render_without_cpv_and_without_data():
    html = _render("ru", CompetitionView(customer_name="Z", cpv_group=""))
    assert html.count("Нет данных") == 1
    assert "нет кода CPV" in html
    assert "участников" in html  # caveat: there is no bidder list in open data
