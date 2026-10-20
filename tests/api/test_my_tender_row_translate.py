"""“My tenders”: a row with a profile-bound tender gets the title translation toggle
and a link INSIDE the product; a row without a profile stays an honest external link
without a toggle (the defect class #464 — it was: the `_my_tender_row.html` heading led to
eNabavke and was not translated at all, the only tender render point in the cabinet
without `_card_title.html`).

Mutation check (see CLAUDE.md §Pre-push checklist item 7): remove the
`{% if row.profile_id %}` branch from `_my_tender_row.html` (return only the
external link) — `test_row_with_profile_renders_internal_toggle` goes red.

The expectation is taken from a FACT, not from the model of the code: `test_internal_link_actually_resolves`
calls the real route `/app/tenders/{id}?profile_id=…`, rather than merely asserting that
the link is “valid” (this class of defect already let a 404 through to production — #462→#463).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_tender_ai
from src.api.cabinet import workflow_service as svc
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from src.api.templating import templates
from tests.api.conftest import FakeSession

_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"
_SOURCE_URL = "https://jnportal.src-ujn.example.invalid/tender-eo/1"


def _row(**kw) -> svc.MyTenderRow:
    base = dict(
        tender_id=_TENDER,
        title="Набавка рачунара",
        customer_name="Општина Нови Сад",
        deadline_at=None,
        tender_url=_SOURCE_URL,
        note=None,
        status="watching",
        profile_id=None,
    )
    base.update(kw)
    return svc.MyTenderRow(**base)


def _render(row: svc.MyTenderRow, *, in_folder: bool = False) -> str:
    return templates.get_template("_my_tender_row.html").render(
        row=row, in_folder=in_folder
    )


# --------------------------------------------------------------------------- #
# Mutation pair: a row WITH a profile vs WITHOUT a profile
# --------------------------------------------------------------------------- #


def test_row_with_profile_renders_internal_toggle():
    html = _render(_row(profile_id=_PROFILE))

    assert "data-card-title" in html, "there is no translation toggle in the markup"
    assert "Показать перевод" in html
    assert f'href="/app/tenders/{_TENDER}?profile_id={_PROFILE}"' in html
    assert f'href="{_SOURCE_URL}"' not in html, "the link must be internal"
    assert (
        'target="_blank"' not in html
    ), "the internal link does not open in a new tab"
    assert 'class="fn-row__title"' in html, "the row’s class must not be lost"


def test_row_without_profile_keeps_external_link_no_toggle():
    html = _render(_row(profile_id=None))

    assert "data-card-title" not in html, "there must be NO toggle without profile_id"
    assert "Показать перевод" not in html
    assert f'href="{_SOURCE_URL}"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener"' in html


def test_row_without_profile_and_without_url_falls_back_to_plain_text():
    """Neither a profile nor a link to the source — the heading stays as text (as it was)."""
    html = _render(_row(profile_id=None, tender_url=None))
    assert "data-card-title" not in html
    assert '<span class="fn-row__title">Набавка рачунара</span>' in html


# --------------------------------------------------------------------------- #
# ‼️ view="my_board"/"my_folder", not "feed" (see the comment in
# _my_tender_row.html): "feed" in GET /app/tenders/{id}/title recomputes
# the link to data.source_url — after the first click on “Show translation” the row
# would lead back to eNabavke, reproducing the defect this change fixes.
# Two kinds, not one: the board and the folder render ONE tender on ONE page
# /app/my at the same time; a shared view would give a duplicated DOM id (#48x).
# --------------------------------------------------------------------------- #


def test_toggle_uses_internal_my_view_not_feed():
    html = _render(_row(profile_id=_PROFILE), in_folder=False)
    assert "view=my_board" in html
    assert "view=feed" not in html
    assert "view=agenda" not in html


def test_folder_toggle_uses_distinct_view_from_board():
    """The same tender in the folder must get a DIFFERENT view than on the board —
    otherwise the id would coincide (see defect 2 in the REPORT)."""
    html = _render(_row(profile_id=_PROFILE), in_folder=True)
    assert "view=my_folder" in html
    assert "view=my_board" not in html


def test_template_source_wires_card_title_behind_profile_guard():
    """A property of the markup, not of the behavior: the partial stays ONE include,
    an edit of the toggle in _card_title.html is picked up automatically.
    """
    import pathlib

    src = pathlib.Path("src/api/templates/_my_tender_row.html").read_text(
        encoding="utf-8"
    )
    assert "{% if row.profile_id %}" in src
    assert '{% include "_card_title.html" %}' in src
    assert 'view=("my_folder" if in_folder else "my_board")' in src


# --------------------------------------------------------------------------- #
# Expectation from the fact: the link the row now builds does NOT 404
# --------------------------------------------------------------------------- #


def _client(*, sess) -> TestClient:
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def test_internal_link_actually_resolves(monkeypatch):
    """We call the REAL route by the link that the “my tenders” row builds
    for a tender with a resolved profile — the response code, not a guess about validity.
    """
    html = _render(_row(profile_id=_PROFILE))
    m = re.search(r'href="(/app/tenders/[^"]+)"', html)
    assert m, "the row did not build the internal link"
    href = m.group(1)

    async def _stub(*args, **kwargs):
        return TenderAnalyses(
            tender_id=_TENDER,
            profile_id=_PROFILE,
            profile_name="ИТ набавке",
            title="Набавка рачунара",
            customer_name="Општина Нови Сад",
            deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
            source_url=_SOURCE_URL,
            outputs={},
        )

    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _stub)
    resp = _client(sess={"uid": "u-1", "tid": "t-1"}).get(href)

    assert resp.status_code == 200, (
        f"the link of the “my tenders” row gave {resp.status_code} — "
        "exactly this is how 404s went to production (#462→#463)"
    )


def test_external_link_from_row_without_profile_is_the_source_url():
    """A row without a profile gives an HONEST external link — it does not build the
    internal one, which would be a guaranteed 404 without profile_id.
    """
    html = _render(_row(profile_id=None))
    m = re.search(r'href="([^"]+)"', html)
    assert m
    assert m.group(1) == _SOURCE_URL
