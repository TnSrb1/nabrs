"""Tests of the “My tenders” funnel board (portal, Stage 2).

Three blocks:
(a) ``build_funnel_board`` — a pure function without DB: the order of stages along the process, the split “in work / outcome”, empty working columns, the filter;
(b) the route ``/app/my`` renders the board, the segment control and meaningful empty states (with stages and without);
(c) a regression of the contract: the HTMX endpoints and the swap target of ``_tender_actions.html`` have not changed, the page stays complete without JS (progressive enhancement).

DB/session/service are mocked (ADR-013). The page render is checked through the real Jinja — a broken include/renamed attribute is red here, not in production.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import workflow_service as svc
from src.api.cabinet.feed_service import WORK_STATUSES
from src.api.main import app
from src.db.models.tender_status import STATUS_LABELS, STATUSES
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}
_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES = _ROOT / "src" / "api" / "templates"
_STATIC = _ROOT / "src" / "api" / "static"

NOW = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)


def _row(status: str, *, tender_id=None, title=None, days=None):
    return svc.MyTenderRow(
        tender_id=tender_id or f"t-{status}",
        title=title or f"Тендер {status}",
        customer_name="Град Београд",
        deadline_at=None if days is None else NOW + timedelta(days=days),
        tender_url="https://portal.rs/t/1",
        status=status,
    )


def _data(*rows, folders=()):
    return svc.MyTenders(folders=list(folders), funnel=list(rows))


# --------------------------------------------------------------------------- #
# (a) build_funnel_board — a pure function, without DB
# --------------------------------------------------------------------------- #


def test_board_work_stages_mirror_cockpit_work_statuses():
    """“In work” on the board == “in work” on the dashboard — one predicate per meaning.

    If the lists diverge, the cockpit would count “N tenders in work”, and the board would show a different set of columns. Here it is red, not a silent divergence.
    """
    assert svc.BOARD_WORK_STATUSES == WORK_STATUSES
    assert set(svc.BOARD_WORK_STATUSES) <= set(STATUSES)
    # Outcomes — an addition to the work inside the funnel, without the default "new".
    assert set(svc.BOARD_WORK_STATUSES) & set(svc.BOARD_OUTCOME_STATUSES) == set()
    assert set(svc.BOARD_WORK_STATUSES) | set(svc.BOARD_OUTCOME_STATUSES) == set(
        STATUSES
    ) - {"new"}


def test_board_work_columns_follow_process_order():
    """The columns follow the work: studying → participating → application submitted."""
    board = svc.build_funnel_board(_data(_row("submitted"), _row("watching")))
    assert [g.status for g in board.work] == ["watching", "active", "submitted"]


def test_board_keeps_empty_work_columns_but_not_empty_outcomes():
    """An empty working stage — a column with zero (the process is visible); an outcome — no."""
    board = svc.build_funnel_board(_data(_row("watching")))
    assert [(g.status, len(g.tenders)) for g in board.work] == [
        ("watching", 1),
        ("active", 0),
        ("submitted", 0),
    ]
    assert board.outcomes == []


def test_board_splits_outcomes_into_archive():
    board = svc.build_funnel_board(
        _data(_row("active"), _row("won"), _row("skipped"), _row("lost"))
    )
    assert board.work_total == 1
    assert [g.status for g in board.outcomes] == ["won", "lost", "skipped"]
    assert board.outcome_total == 3
    assert board.is_empty is False


def test_board_is_empty_without_marked_tenders():
    board = svc.build_funnel_board(_data())
    assert board.is_empty is True
    assert board.work_total == board.outcome_total == 0


def test_board_narrows_to_single_column_under_stage_filter():
    """?stage=won — one outcome column; we do not draw three empty working rows next to it."""
    data = svc.filter_by_stage(_data(_row("active"), _row("won")), "won")
    board = svc.build_funnel_board(data, "won")
    assert board.work == []
    assert [g.status for g in board.outcomes] == ["won"]


def test_board_stage_filter_on_work_stage_keeps_it_in_work():
    data = svc.filter_by_stage(_data(_row("active"), _row("won")), "active")
    board = svc.build_funnel_board(data, "active")
    assert [g.status for g in board.work] == ["active"]
    assert board.outcomes == []


def test_board_empty_stage_filter_gives_empty_board_not_zero_column():
    """A stage without tenders (incl. new/junk) → an empty board, not a column with 0."""
    for stage in ("lost", "new"):
        board = svc.build_funnel_board(_data(_row("active")), stage)
        assert board.is_empty is True
        assert board.work == [] and board.outcomes == []


def test_board_labels_come_from_funnel_model():
    """The captions — from STATUS_LABELS (one funnel with the bot), not our own literals."""
    board = svc.build_funnel_board(_data(_row("watching"), _row("won")))
    assert [g.label for g in board.work] == [
        STATUS_LABELS[s] for s in svc.BOARD_WORK_STATUSES
    ]
    assert board.outcomes[0].label == STATUS_LABELS["won"]


def test_board_marks_reminder_stages():
    """The mark “reminders will come” — on the same stages as the worker."""
    board = svc.build_funnel_board(_data(_row("watching")))
    assert {g.status for g in board.work if g.reminds} == set(svc.REMINDER_STATUSES)


# --------------------------------------------------------------------------- #
# (b) render of /app/my
# --------------------------------------------------------------------------- #


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _client(*, sess=SESS):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _get(monkeypatch, data, url="/app/my"):
    monkeypatch.setattr(svc, "get_my_tenders", _async(data))
    resp = _client().get(url)
    assert resp.status_code == 200
    return resp.text


def test_page_renders_board_columns_for_every_work_stage(monkeypatch):
    html = _get(monkeypatch, _data(_row("watching", days=1)))
    assert html.count("data-funnel-col") == 3
    for status in svc.BOARD_WORK_STATUSES:
        assert f'id="fn-col-{status}"' in html


def test_page_shows_stage_counts_server_side(monkeypatch):
    """The stage counter is rendered by the server — without JS the number is visible, not an emptiness."""
    html = _get(monkeypatch, _data(_row("active", tender_id="a"), _row("active")))
    assert '<span class="fn-col__count" data-count-to="2">2</span>' in html


def test_page_puts_outcomes_into_collapsed_archive(monkeypatch):
    """Outcomes — in a <details>, so that the work stays in the foreground."""
    html = _get(monkeypatch, _data(_row("active"), _row("won")))
    board_at = html.index("data-funnel-col")
    archive_at = html.index("Итоги — выиграли")
    assert board_at < archive_at
    assert "<details" in html[:archive_at]
    # The outcome column is muted by a modifier, the working one is not.
    assert "fn-col--archive" in html[archive_at:]
    assert "fn-col--archive" not in html[:archive_at]


def test_page_renders_segment_control_with_active_stage(monkeypatch):
    html = _get(monkeypatch, _data(_row("watching"), _row("won")), "/app/my?stage=won")
    assert 'class="seg mb-5"' in html
    active = [
        a for a in re.findall(r"<a [^>]*seg__item[^>]*>", html) if "is-active" in a
    ]
    assert len(active) == 1 and 'aria-current="page"' in active[0]
    assert "stage=won" in active[0]
    # The filter links — the same ?stage=, one can leave the filter.
    assert "/app/my?stage=watching" in html


def test_page_shows_deadline_badge_with_dashboard_colour_code(monkeypatch):
    """The urgency on the row — the same color code as on the dashboard, and it depends on the
    MEANING (expired / urgent ≤2 d. / calm >7 d.), not on the time of the launch.

    We build the deadlines from the REAL now (the ``days_left`` filter counts from it) — otherwise the test falsely goes red/green depending on the date of the run.
    """
    now = datetime.now(timezone.utc)

    def _dl_row(tender_id, title, delta):
        return svc.MyTenderRow(
            tender_id=tender_id,
            title=title,
            customer_name="Град Београд",
            deadline_at=now + delta,
            tender_url="https://portal.rs/t/1",
            status="watching",
        )

    html = _get(
        monkeypatch,
        _data(
            _dl_row("a", "Горит", timedelta(days=1)),
            _dl_row("b", "Не горит", timedelta(days=20)),
            _dl_row("c", "Истёк", timedelta(days=-3)),
        ),
    )

    def _badge_of(title):
        block = html.split(title)[1].split("</li>")[0]
        m = re.search(r"badge-(urgent|expired|muted|new)", block)
        return m.group(0) if m else None

    # Each row — its own meaning, its own token; a gray “expired” ≠ a gray “calm”.
    assert _badge_of("Истёк") == "badge-expired"
    assert "срок истёк" in html.split("Истёк")[1].split("</li>")[0]
    assert _badge_of("Горит") == "badge-urgent"
    assert _badge_of("Не горит") == "badge-muted"


def test_page_without_deadline_shows_no_badge(monkeypatch):
    """There is no deadline data — the badge is not invented."""
    html = _get(monkeypatch, _data(_row("watching")))
    row = html.split("fn-row")[1]
    assert "осталось" not in row and "срок истёк" not in row


def test_page_offers_way_forward_when_nothing_marked(monkeypatch):
    """Empty — a meaningful empty state with a link to the feed, not a bare board."""
    html = _get(monkeypatch, _data())
    assert "empty-state" in html
    assert "/app/feed" in html
    assert "data-funnel-col" not in html


def test_page_keeps_folders_secondary_and_collapsed_when_none(monkeypatch):
    """Folders — under the funnel and collapsed, but the creation form is available."""
    html = _get(monkeypatch, _data(_row("active")))
    folders_at = html.index("fn-folders")
    assert html.index("data-funnel") < folders_at
    assert '<details class="ck-more fn-folders" >' in html  # without open
    assert 'action="/app/folders"' in html


def test_page_gives_the_same_tender_distinct_ids_in_board_and_folder(monkeypatch):
    """‼️ Defect 2 (rework of PR #484): a tender lying BOTH in a funnel stage AND in a folder
    renders ``_card_title.html`` TWICE with the same tender_id/profile_id — without different ``view`` values (my_board/my_folder) both containers would get the same id, and the hx-target of the lower button would land in the upper row.

    Plus the round-trip: the fragment by the URL of EACH button must return a container with the SAME id that was in the primary render — otherwise the second click on the row would miss its target.
    """
    tender_id = "11111111-1111-1111-1111-111111111111"
    profile_id = "33333333-3333-3333-3333-333333333333"
    row = svc.MyTenderRow(
        tender_id=tender_id,
        title="Общий тендер",
        customer_name="Град Београд",
        deadline_at=None,
        tender_url="https://portal.rs/t/1",
        status="watching",
        profile_id=profile_id,
    )
    data = svc.MyTenders(
        folders=[svc.FolderView(id="f1", name="Приоритет", tenders=[row])],
        funnel=[row],
    )
    html = _get(monkeypatch, data)

    boxes = re.findall(r'id="(card-title-[^"]*)"', html)
    targets = re.findall(r'hx-target="#(card-title-[^"]*)"', html)
    hrefs = re.findall(r'hx-get="([^"]*)"', html)

    assert len(boxes) == 2, f"two rows with card-title were expected: {boxes}"
    assert len(set(boxes)) == 2, f"the heading ids must differ: {boxes}"
    assert boxes == targets, "each button must swap its own container"
    assert f"card-title-my_board-{tender_id}-{profile_id}" in boxes
    assert f"card-title-my_folder-{tender_id}-{profile_id}" in boxes

    # Round-trip: we call the REAL fragment by the link of each button and compare
    # that the returned id coincides with the one in the primary render.
    from src.api.cabinet import routes_tender_ai
    from src.api.cabinet.tender_ai_service import TenderAnalyses

    async def _analyses(*args, **kwargs):
        return TenderAnalyses(
            tender_id=tender_id,
            profile_id=profile_id,
            profile_name="ИТ набавке",
            title="Общий тендер",
            customer_name="Град Београд",
            deadline_at=None,
            source_url="https://portal.rs/t/1",
            outputs={},
        )

    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _analyses)

    assert len(hrefs) == 2
    for box, href in zip(boxes, hrefs):
        frag_url = href.replace("&amp;", "&")
        resp = _client().get(frag_url)
        assert resp.status_code == 200
        assert f'id="{box}"' in resp.text
        assert f'hx-target="#{box}"' in resp.text


def test_page_opens_folders_block_when_folders_exist(monkeypatch):
    data = _data(
        _row("active"),
        folders=[svc.FolderView(id="f1", name="Приоритет", tenders=[_row("won")])],
    )
    html = _get(monkeypatch, data)
    assert 'class="ck-more fn-folders" open' in html
    assert "Приоритет" in html
    # Inside a folder the rows lie mixed — the stage is labelled.
    assert STATUS_LABELS["won"] in html


# --------------------------------------------------------------------------- #
# (c) HTMX contract + progressive enhancement
# --------------------------------------------------------------------------- #

_HTMX_ENDPOINTS = (
    "/app/tenders/{{ tender_id }}/watch",
    "/app/tenders/{{ tender_id }}/status",
    "/app/tenders/{{ tender_id }}/note",
    "/app/tenders/{{ tender_id }}/folders/{{ f.id }}/add",
    "/app/tenders/{{ tender_id }}/folders/{{ f.id }}/remove",
)


def test_htmx_contract_of_tender_actions_is_untouched():
    """The redesign of /app/my does not touch the contract of the tender actions.

    The swap target is one per tender (``#actions-{id}``, outerHTML) and all five hx-post are in place: if any breaks, the buttons in the feed/card will stop redrawing, while the page externally stays intact.
    """
    html = (_TEMPLATES / "_tender_actions.html").read_text(encoding="utf-8")
    assert 'id="actions-{{ tender_id }}"' in html
    for endpoint in _HTMX_ENDPOINTS:
        assert f'hx-post="{endpoint}"' in html
    assert html.count('hx-target="#actions-{{ tender_id }}"') == len(
        re.findall(r"hx-post=", html)
    )
    assert 'hx-swap="outerHTML"' in html


def test_my_tenders_page_does_not_ship_its_own_htmx_endpoints():
    """The page is presentation only: it does not set up its own hx-post/hx-target."""
    for name in ("my_tenders.html", "_funnel_board.html", "_funnel_column.html"):
        html = (_TEMPLATES / name).read_text(encoding="utf-8")
        assert "hx-post" not in html and "hx-target" not in html


@pytest.mark.parametrize(
    "name", ("my_tenders.html", "_funnel_board.html", "_funnel_column.html")
)
def test_my_tenders_markup_has_no_inline_scripts(name):
    """CSP: scripts only external and only 'self'."""
    html = (_TEMPLATES / name).read_text(encoding="utf-8")
    for tag in re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S):
        assert tag.strip() == "", f"inline JS in {name}"
    for src in re.findall(r'<script[^>]*src="([^"]+)"', html):
        assert src.startswith("{{ static_url("), f"external script {src} in {name}"


def test_my_tenders_js_respects_reduced_motion_and_missing_gsap():
    js = (_STATIC / "js" / "my_tenders.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in js
    assert "if (reduceMotion) return;" in js
    assert "if (!gsap) return;" in js
    # We animate only composite properties — without layout thrash.
    assert "clearProps" in js


def test_my_tenders_js_is_loaded_once_and_gsap_not_re_included():
    """GSAP is vendored in base.html — the page connects ONLY its own file."""
    html = (_TEMPLATES / "my_tenders.html").read_text(encoding="utf-8")
    srcs = re.findall(r'<script[^>]*src="([^"]+)"', html)
    assert srcs == ["{{ static_url('js/my_tenders.js') }}"]


def test_reduced_motion_disables_board_transitions_in_css():
    """The board transitions are off under prefers-reduced-motion.

    We look into ALL reduce blocks, not the last one: each section of the portal has its own (cockpit / board / match archive), and their order in the file is accidental. Taking `[-1]` meant going red on any new section further down the file, while checking nothing about the board.
    """
    css = (_STATIC / "css" / "app.css").read_text(encoding="utf-8")
    # ‼️ Each block is cut at its closing brace (a line break + `}`): without
    # the cut, a “block” would stretch to the end of the file, capture other people’s rules, and
    # the check would always pass, whatever lay in the real block.
    blocks = [
        chunk.split("\n}")[0]
        for chunk in css.split("@media (prefers-reduced-motion: reduce)")[1:]
    ]
    assert blocks, "there are no reduce blocks in app.css at all"
    assert any(".fn-row" in b and ".seg__item" in b for b in blocks)


def test_board_does_not_scroll_the_page_horizontally():
    """The board scrolls ITSELF: the columns in minmax(0, 1fr), overflow-x on the container."""
    css = (_STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "repeat(3, minmax(0, 1fr))" in css.split(".fn__board")[-1] or (
        "minmax(0, 1fr)" in css
    )
    board_rule = css.split(".fn__board {")[1].split("}")[0]
    assert "overflow-x: auto" in board_rule
