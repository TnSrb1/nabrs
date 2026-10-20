"""Examples under the live counter of the template form (template_wizard_2, step 2).

owner 01.10: the counter + 3–5 fresh found tenders are recalculated on the change of ANY filter of the form, by the same query as the matching. The matched word is highlighted.
Expectations — literals (headings, markup), not via the same code.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet.preview_service import highlight_parts
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


def teardown_function():
    app.dependency_overrides.clear()


def _client(results):
    def factory():
        return FakeSession(results=list(results))

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    return TestClient(app, follow_redirects=False)


def _row(n: int, title: str, norm: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"t-{n}",
        title=title,
        title_normalized=norm,
        description_normalized="",
        customer_name=f"Opština {n}",
        deadline_at=None,
    )


# --------------------------------------------------------------------------- #
#  Highlight: the original heading, positions via folding
# --------------------------------------------------------------------------- #


def test_highlight_word_in_other_case_with_diacritics():
    assert highlight_parts("Izgradnja škole u Nišu", ["skola"]) == [
        ("Izgradnja ", False),
        ("škole", True),
        (" u Nišu", False),
    ]


def test_highlight_cyrillic_title():
    assert highlight_parts("Набавка хране за вртић", ["hrana"]) == [
        ("Набавка ", False),
        ("хране", True),
        (" за вртић", False),
    ]


def test_highlight_dj_maps_back_to_single_letter():
    # đ folds into two characters “dj” — the highlight must not shift.
    assert highlight_parts("Radovi na građevini i putu", ["gradjevina"]) == [
        ("Radovi na ", False),
        ("građevini", True),
        (" i putu", False),
    ]


def test_highlight_nothing_when_match_only_in_description():
    assert highlight_parts("Usluge čišćenja", ["skola"]) == [("Usluge čišćenja", False)]


# --------------------------------------------------------------------------- #
#  A fragment of the live counter
# --------------------------------------------------------------------------- #

_URL = "/profiles/preview-count?keywords_include=skola&enabled_source_keys=jnportal"


def test_fragment_shows_up_to_five_examples_with_mark_and_public_link():
    rows = [
        _row(n, f"Opremanje škole br. {n}", f"opremanje skole br. {n}")
        for n in range(7)
    ]
    resp = _client([FakeResult(items=rows)]).get(_URL, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    html = resp.text
    assert "≈ 7 тендеров за 30 дней" in html
    step1 = html.split('id="previewStep2"')[0]
    links = re.findall(r'href="(/tender/t-\d+)"', step1)
    assert links == [
        "/tender/t-0",
        "/tender/t-1",
        "/tender/t-2",
        "/tender/t-3",
        "/tender/t-4",
    ]
    assert "Opremanje <mark>škole</mark> br. 0" in step1
    assert 'target="_blank" rel="noopener"' in step1
    assert "Opština 0" in step1


def test_fragment_examples_skip_rows_failing_keywords():
    rows = [
        _row(1, "Nabavka goriva", "nabavka goriva"),
        _row(2, "Rekonstrukcija škole", "rekonstrukcija skole"),
    ]
    html = (
        _client([FakeResult(items=rows)]).get(_URL, headers={"HX-Request": "true"}).text
    )
    assert "≈ 1 тендер за 30 дней" in html
    assert "/tender/t-2" in html
    assert "/tender/t-1" not in html


def test_fragment_title_is_escaped():
    rows = [_row(1, "Škola <script>x</script>", "skola <script>x</script>")]
    html = (
        _client([FakeResult(items=rows)]).get(_URL, headers={"HX-Request": "true"}).text
    )
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_json_contract_has_no_examples():
    rows = [_row(1, "Rekonstrukcija škole", "rekonstrukcija skole")]
    body = _client([FakeResult(items=rows)]).get(_URL).json()
    assert body["count"] == 1
    assert "examples" not in body


# --------------------------------------------------------------------------- #
#  Recalculation on ANY filter: the driver listens to the whole form
# --------------------------------------------------------------------------- #


def test_driver_recounts_on_any_form_field():
    html = _client([]).get("/profiles/new?manual=1").text
    driver = re.search(r'<div id="previewDriver"[^>]*>', html, re.S).group(0)
    assert 'hx-include="#profileForm"' in driver
    assert "input from:#profileForm" in driver
    assert "change from:#profileForm" in driver
    assert 'hx-indicator=".preview-live"' in driver
    form = html[html.index('<form method="post" action="/profiles" id="profileForm"') :]
    form = form[: form.index("</form>")]
    # All the selection filters are inside the form, so changing them wakes the driver.
    for name in (
        "keywords_include",
        "cls_include",
        "regions",
        "min_amount_eur",
        "max_amount_eur",
        "enabled_source_keys",
    ):
        assert f'name="{name}"' in form, name
