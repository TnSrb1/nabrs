"""Tests of workflow_service (ADR-013: mock session without a real DB).

We check the CRUD of folders/notes/watching + the gate of the ownership of a folder + the aggregate
“my tenders”. The RLS isolation under the app role — tests/db/test_rls_isolation.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from src.api.cabinet import workflow_service as svc
from src.db.models.tender_status import STATUSES
from tests.api.conftest import FakeResult, FakeSession

_TID = "11111111-1111-1111-1111-111111111111"
_TENDER = "22222222-2222-2222-2222-222222222222"
_FOLDER = "33333333-3333-3333-3333-333333333333"


class _Row:
    """A row-like object: access both by attribute and by position (r[0])."""

    def __init__(self, seq=(), **attrs):
        self._seq = tuple(seq)
        self.__dict__.update(attrs)

    def __getitem__(self, i):
        return self._seq[i]


def _executed_sql(session: FakeSession) -> str:
    return "\n".join(sql for sql, _ in session.executed)


# --------------------------------------------------------------------------- #
# Folders
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_folders_maps_rows():
    rows = [
        _Row(id="f1", name="Приоритет", item_count=3),
        _Row(id="f2", name="Позже", item_count=0),
    ]
    s = FakeSession(results=[FakeResult(items=rows)])
    folders = await svc.list_folders(s, _TID)
    assert [f.name for f in folders] == ["Приоритет", "Позже"]
    assert folders[0].item_count == 3
    # RLS: set_tenant is called (set_config in executed).
    assert "app.current_tenant_id" in _executed_sql(s)


@pytest.mark.asyncio
async def test_create_folder_blank_rejected():
    s = FakeSession()
    res = await svc.create_folder(s, tenant_id=_TID, name="   ")
    assert res.ok is False and res.reason == "blank"
    assert s.committed == 0


@pytest.mark.asyncio
async def test_create_folder_success_commits():
    s = FakeSession(results=[FakeResult(scalar="new-fid")])
    res = await svc.create_folder(s, tenant_id=_TID, name="  Важное  ")
    assert res.ok is True and res.folder_id == "new-fid"
    assert s.committed == 1
    # The name is trimmed before the insert.
    assert ("Важное", True) == (s.executed[-1][1]["name"], "name" in s.executed[-1][1])


@pytest.mark.asyncio
async def test_create_folder_duplicate_rolls_back():
    class _Raising(FakeSession):
        async def execute(self, statement, params=None):
            if "INSERT INTO tender_folders" in str(statement):
                self.executed.append((str(statement), params))
                raise IntegrityError("dup", {}, Exception("unique_violation"))
            return await super().execute(statement, params)

    s = _Raising()
    res = await svc.create_folder(s, tenant_id=_TID, name="Дубль")
    assert res.ok is False and res.reason == "duplicate"
    assert s.rolled_back == 1
    assert s.committed == 0


@pytest.mark.asyncio
async def test_add_to_folder_rejects_foreign_folder():
    # _FOLDER_OWNED_SQL → empty (the folder is not of the tenant) → False, without an insert/commit.
    s = FakeSession(results=[FakeResult(items=[])])
    ok = await svc.add_to_folder(
        s, tenant_id=_TID, folder_id=_FOLDER, tender_id=_TENDER
    )
    assert ok is False
    assert s.committed == 0
    assert "INSERT INTO folder_items" not in _executed_sql(s)


@pytest.mark.asyncio
async def test_add_to_folder_owned_inserts():
    s = FakeSession(results=[FakeResult(items=[1]), FakeResult()])
    ok = await svc.add_to_folder(
        s, tenant_id=_TID, folder_id=_FOLDER, tender_id=_TENDER
    )
    assert ok is True
    assert s.committed == 1
    assert "INSERT INTO folder_items" in _executed_sql(s)


@pytest.mark.asyncio
async def test_remove_from_folder_commits():
    s = FakeSession(results=[FakeResult()])
    await svc.remove_from_folder(
        s, tenant_id=_TID, folder_id=_FOLDER, tender_id=_TENDER
    )
    assert s.committed == 1
    assert "DELETE FROM folder_items" in _executed_sql(s)


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_save_note_upserts_non_empty():
    s = FakeSession(results=[FakeResult()])
    out = await svc.save_note(s, tenant_id=_TID, tender_id=_TENDER, body="  замечание ")
    assert out == "замечание"
    assert s.committed == 1
    assert "INSERT INTO tender_notes" in _executed_sql(s)


@pytest.mark.asyncio
async def test_save_note_blank_deletes():
    s = FakeSession(results=[FakeResult()])
    out = await svc.save_note(s, tenant_id=_TID, tender_id=_TENDER, body="   ")
    assert out is None
    assert s.committed == 1
    assert "DELETE FROM tender_notes" in _executed_sql(s)


@pytest.mark.asyncio
async def test_get_note_returns_body_or_none():
    s = FakeSession(results=[FakeResult(items=[SimpleNamespace(body="hi")])])
    assert await svc.get_note(s, tenant_id=_TID, tender_id=_TENDER) == "hi"
    s2 = FakeSession(results=[FakeResult(items=[])])
    assert await svc.get_note(s2, tenant_id=_TID, tender_id=_TENDER) is None


# --------------------------------------------------------------------------- #
# Following
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_set_watch_on_upserts_watching():
    s = FakeSession(results=[FakeResult()])
    out = await svc.set_watch(s, tenant_id=_TID, tender_id=_TENDER, watch=True)
    assert out is True
    assert s.committed == 1
    sql = _executed_sql(s)
    assert "INSERT INTO tender_user_status" in sql and "'watching'" in sql


@pytest.mark.asyncio
async def test_set_watch_off_downgrades_only_watching():
    s = FakeSession(results=[FakeResult()])
    out = await svc.set_watch(s, tenant_id=_TID, tender_id=_TENDER, watch=False)
    assert out is False
    sql = _executed_sql(s)
    assert "UPDATE tender_user_status" in sql
    assert "status = 'new'" in sql
    assert "status = 'watching'" in sql  # WHERE condition: we do not touch active/funnel


# --------------------------------------------------------------------------- #
# Funnel stage (VG-17w) — all 7, as in the bot
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("status", STATUSES)
async def test_set_status_writes_each_funnel_stage(status):
    """Each of the 7 stages is set from the web and goes into the DB by a bind.

    Mutation check: hardwire 'watching' into ``_SET_STATUS_SQL`` — 7 out of 7 go red (the parameter will not reach the params).
    """
    s = FakeSession(results=[FakeResult()])
    ok = await svc.set_status(s, tenant_id=_TID, tender_id=_TENDER, status=status)
    assert ok is True
    assert s.committed == 1
    sql, params = s.executed[-1]
    assert "INSERT INTO tender_user_status" in sql
    assert params["status"] == status
    assert params == {"tid": _TID, "tender": _TENDER, "status": status}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["", "  ", "bogus", "WATCHING", "active; DROP TABLE tenders_normalized", None],
)
async def test_set_status_rejects_value_outside_statuses(bad):
    """Junk → refusal BEFORE the write: no INSERT, no commit, not even set_tenant."""
    s = FakeSession(results=[FakeResult()])
    ok = await svc.set_status(s, tenant_id=_TID, tender_id=_TENDER, status=bad)
    assert ok is False
    assert s.committed == 0
    assert s.executed == [], "the refusal must be without a single access to the DB"


@pytest.mark.asyncio
async def test_set_status_sets_tenant_before_writing():
    """RLS: set_tenant — FIRST (otherwise the write goes past the tenant)."""
    s = FakeSession(results=[FakeResult()])
    await svc.set_status(s, tenant_id=_TID, tender_id=_TENDER, status="active")
    assert "app.current_tenant_id" in s.executed[0][0]


@pytest.mark.asyncio
async def test_set_status_active_does_not_depend_on_previous_stage():
    """An explicit change of stage has no WHERE gate of the watch toggle.

    The toggle protects the “rich” stages (`_WATCH_ON_SQL`), while the subscriber’s deliberate choice must pass from any stage — including won → active.
    """
    s = FakeSession(results=[FakeResult()])
    await svc.set_status(s, tenant_id=_TID, tender_id=_TENDER, status="active")
    sql = _executed_sql(s)
    assert "ON CONFLICT (tenant_id, tender_id) DO UPDATE" in sql
    assert "WHERE tender_user_status.status IN" not in sql


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_tender_state_aggregates():
    s = FakeSession(
        results=[
            FakeResult(items=[SimpleNamespace(status="watching")]),  # watch
            FakeResult(items=[SimpleNamespace(body="моя заметка")]),  # note
            FakeResult(items=[_Row(folder_id="f1"), _Row(folder_id="f2")]),  # folders
        ]
    )
    st = await svc.get_tender_state(s, tenant_id=_TID, tender_id=_TENDER)
    assert st.is_watched is True
    assert st.note == "моя заметка"
    assert st.folder_ids == frozenset({"f1", "f2"})
    assert st.status == "watching"
    assert st.status_label == "👀 Изучаю"


@pytest.mark.asyncio
async def test_get_tender_state_active_status_not_web_watched():
    # active is set by the bot; the web flag is_watched — only for watching.
    s = FakeSession(
        results=[
            FakeResult(items=[SimpleNamespace(status="active")]),
            FakeResult(items=[]),
            FakeResult(items=[]),
        ]
    )
    st = await svc.get_tender_state(s, tenant_id=_TID, tender_id=_TENDER)
    assert st.is_watched is False
    assert st.note is None
    assert st.folder_ids == frozenset()
    # ...but the stage is visible: the card will show “✅ Participating”, set by the bot.
    assert st.status == "active"
    assert st.status_label == "✅ Участвую"


@pytest.mark.asyncio
async def test_get_tender_state_without_row_defaults_to_new():
    """A tender without a funnel row — the new stage (the server default of the column)."""
    s = FakeSession(results=[FakeResult(items=[]), FakeResult(items=[]), FakeResult()])
    st = await svc.get_tender_state(s, tenant_id=_TID, tender_id=_TENDER)
    assert st.status == "new"
    assert st.is_watched is False


@pytest.mark.asyncio
async def test_load_feed_state_builds_maps():
    s = FakeSession(
        results=[
            FakeResult(
                items=[
                    _Row(tender_id="t1", status="watching"),
                    _Row(tender_id="t2", status="watching"),
                    # The bot’s stage: it does NOT fall into watched (the “Follow” button is only about
                    # watching), but the stage switch sees it.
                    _Row(tender_id="t4", status="won"),
                ]
            ),  # the funnel statuses
            FakeResult(items=[_Row(seq=("t1",), body="n1")]),  # notes
            FakeResult(
                items=[
                    _Row(tender_id="t1", folder_id="f1"),
                    _Row(tender_id="t1", folder_id="f2"),
                    _Row(tender_id="t3", folder_id="f1"),
                ]
            ),  # folder items
        ]
    )
    fs = await svc.load_feed_state(s, _TID)
    assert fs.watched == frozenset({"t1", "t2"})
    assert fs.notes == {"t1": "n1"}
    assert fs.folders_by_tender["t1"] == frozenset({"f1", "f2"})
    # for_tender assembles TenderState.
    st = fs.for_tender("t1")
    assert (
        st.is_watched and st.note == "n1" and st.folder_ids == frozenset({"f1", "f2"})
    )
    assert st.status == "watching"
    st3 = fs.for_tender("t3")
    assert st3.is_watched is False and st3.note is None
    assert st3.folder_ids == frozenset({"f1"})
    assert st3.status == "new"  # in the funnel there is no row → default
    st4 = fs.for_tender("t4")
    assert st4.is_watched is False and st4.status == "won"


@pytest.mark.asyncio
async def test_load_feed_state_reads_whole_funnel_not_only_watching():
    """The feed takes the stages in one selection with no WHERE by status.

    Otherwise the switch in the feed would show “New” for a tender that the bot had already moved to “Application submitted”.
    """
    s = FakeSession(results=[FakeResult(items=[]), FakeResult(items=[]), FakeResult()])
    await svc.load_feed_state(s, _TID)
    status_sql = s.executed[1][0]
    assert "FROM tender_user_status" in status_sql
    assert "status = 'watching'" not in status_sql


# --------------------------------------------------------------------------- #
# My tenders
# --------------------------------------------------------------------------- #


def _folder_row(**kw) -> _Row:
    base = dict(
        folder_id="f1",
        folder_name="Приоритет",
        tender_id="t1",
        title="Тендер 1",
        customer_name="Наручилац",
        deadline_at=None,
        tender_url="https://x/1",
        added_at=None,
        status="new",
        profile_id=None,
    )
    base.update(kw)
    return _Row(**base)


def _funnel_row(**kw) -> _Row:
    base = dict(
        tender_id="t9",
        title="Слежу",
        customer_name=None,
        deadline_at=None,
        tender_url="https://x/9",
        status="watching",
        profile_id=None,
    )
    base.update(kw)
    return _Row(**base)


@pytest.mark.asyncio
async def test_get_my_tenders_groups_folders_and_funnel():
    dl = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    folder_rows = [
        _folder_row(deadline_at=dl, added_at=dl, status="active"),
        # An empty folder: the LEFT JOIN gives a row with tender_id = None → no rows.
        _folder_row(
            folder_id="f2",
            folder_name="Пусто",
            tender_id=None,
            title=None,
            customer_name=None,
            tender_url=None,
        ),
    ]
    funnel_rows = [_funnel_row(deadline_at=dl)]
    notes_rows = [_Row(seq=("t1",), body="важно")]
    s = FakeSession(
        results=[
            FakeResult(items=folder_rows),
            FakeResult(items=funnel_rows),
            FakeResult(items=notes_rows),
        ]
    )
    data = await svc.get_my_tenders(s, _TID)
    assert [f.name for f in data.folders] == ["Приоритет", "Пусто"]
    prio = data.folders[0]
    assert len(prio.tenders) == 1
    assert prio.tenders[0].title == "Тендер 1"
    assert prio.tenders[0].note == "важно"
    assert prio.tenders[0].status == "active"  # the stage is visible inside the folder as well
    assert data.folders[1].tenders == []  # an empty folder
    assert len(data.funnel) == 1
    assert data.funnel[0].title == "Слежу"


@pytest.mark.asyncio
async def test_get_my_tenders_carries_resolved_profile_id_into_rows():
    """The profile_id given by the SQL subquery (_PROFILE_RESOLVE_SQL) reaches
    ``MyTenderRow`` — both in the folder and in the funnel. Without it the translation toggle in the row is not drawn (the defect class #464 on /app/my).
    """
    s = FakeSession(
        results=[
            FakeResult(items=[_folder_row(profile_id="p-folder")]),
            FakeResult(items=[_funnel_row(profile_id="p-funnel")]),
            FakeResult(items=[]),
        ]
    )
    data = await svc.get_my_tenders(s, _TID)
    assert data.folders[0].tenders[0].profile_id == "p-folder"
    assert data.funnel[0].profile_id == "p-funnel"


@pytest.mark.asyncio
async def test_get_my_tenders_row_without_resolved_profile_stays_none():
    """A tender without a business profile (the subquery did not find a row) → ``profile_id`` stays ``None`` — the row must stay an external link without the toggle.
    """
    s = FakeSession(
        results=[
            FakeResult(items=[_folder_row(profile_id=None)]),
            FakeResult(items=[_funnel_row(profile_id=None)]),
            FakeResult(items=[]),
        ]
    )
    data = await svc.get_my_tenders(s, _TID)
    assert data.folders[0].tenders[0].profile_id is None
    assert data.funnel[0].profile_id is None


def test_profile_resolve_sql_scopes_by_tenant_and_asyncpg_cast():
    """The profile-resolve subquery — through delivery_outbox↔profiles, with the tenant scope
    by an explicit ``CAST(:tid AS uuid)`` (defense-in-depth on top of RLS, as in feed_service._FEED_SELECT_BODY), not by a bare bind (asyncpg in ``text()`` requires CAST — CLAUDE.md).
    """
    sql = svc._PROFILE_RESOLVE_SQL
    assert "delivery_outbox" in sql
    assert "profiles" in sql
    assert "CAST(:tid AS uuid)" in sql
    assert "o.tender_id = t.id" in sql
    # The freshest delivery — sorting by sent_at, not the first random row.
    assert "ORDER BY o.sent_at DESC" in sql


def test_profile_resolve_subquery_wired_into_both_my_tenders_queries():
    """The subquery must stand in BOTH sources of the /app/my rows — the folders and the funnel: skip one, and half the page stays without the toggle (the same class of defect as #456 → #464 — the predicate across ALL render points).
    """
    assert "AS profile_id" in str(svc._FOLDER_TENDERS_SQL)
    assert "AS profile_id" in str(svc._MY_TENDERS_SQL)


@pytest.mark.asyncio
async def test_get_my_tenders_keeps_late_funnel_stages_on_page():
    """Marked “🏆 Won” — the tender stays in “My tenders”.

    Before VG-17w the page knew only watching/active; with 7 stages from the web such a tender would have disappeared from the page (and from the CSV) right after the marking.
    """
    s = FakeSession(
        results=[
            FakeResult(items=[]),
            FakeResult(
                items=[_funnel_row(tender_id="t7", title="Победа", status="won")]
            ),
            FakeResult(items=[]),
        ]
    )
    data = await svc.get_my_tenders(s, _TID)
    assert [r.title for r in data.funnel] == ["Победа"]
    assert [(g.status, g.label) for g in data.stages] == [("won", "🏆 Выиграли")]


@pytest.mark.asyncio
async def test_my_tenders_stages_grouped_in_funnel_order():
    """The groups go in the funnel order (STATUSES), there are no empty groups."""
    s = FakeSession(
        results=[
            FakeResult(items=[]),
            FakeResult(
                items=[
                    _funnel_row(tender_id="t3", status="won"),
                    _funnel_row(tender_id="t1", status="watching"),
                    _funnel_row(tender_id="t2", status="active"),
                    _funnel_row(tender_id="t4", status="watching"),
                ]
            ),
            FakeResult(items=[]),
        ]
    )
    data = await svc.get_my_tenders(s, _TID)
    assert [g.status for g in data.stages] == ["watching", "active", "won"]
    assert [len(g.tenders) for g in data.stages] == [2, 1, 1]
    # The reminder sends by watching/active — the UI marks exactly these groups.
    assert [g.status for g in data.stages if g.reminds] == ["watching", "active"]


def _my_tenders(*rows) -> svc.MyTenders:
    return svc.MyTenders(
        folders=[
            svc.FolderView(
                id="f1",
                name="Приоритет",
                tenders=[
                    svc.MyTenderRow("t1", "В папке", None, None, None, status="won")
                ],
            )
        ],
        funnel=list(rows),
    )


def test_filter_by_stage_keeps_only_that_stage_everywhere():
    """The filter cuts both the funnel and the folder contents (otherwise the filter is half-done)."""
    data = _my_tenders(
        svc.MyTenderRow("t2", "Участвую", None, None, None, status="active"),
        svc.MyTenderRow("t3", "Выиграли", None, None, None, status="won"),
    )
    out = svc.filter_by_stage(data, "won")
    assert [r.title for r in out.funnel] == ["Выиграли"]
    assert [t.title for t in out.folders[0].tenders] == ["В папке"]

    out_active = svc.filter_by_stage(data, "active")
    assert [r.title for r in out_active.funnel] == ["Участвую"]
    assert out_active.folders[0].tenders == []  # there is no active in the folder


def test_filter_by_stage_ignores_unknown_and_none():
    """Junk in ?stage= → the page as it is, not an empty one."""
    data = _my_tenders(
        svc.MyTenderRow("t2", "Участвую", None, None, None, status="active")
    )
    for bad in (None, "", "bogus", "ACTIVE"):
        assert svc.filter_by_stage(data, bad) == data


# --------------------------------------------------------------------------- #
# CSV export “My tenders” (F2)
# --------------------------------------------------------------------------- #


def _export_row(**kw) -> _Row:
    base = dict(
        tender_id="t1",
        title="Тендер 1",
        customer_name="Наручилац",
        amount=Decimal("1000"),
        currency="RSD",
        deadline_at=None,
        tender_url="https://x/1",
        user_status="watching",
        folder_names="Приоритет",
    )
    base.update(kw)
    return _Row(**base)


@pytest.mark.asyncio
async def test_get_my_tenders_export_maps_rows():
    s = FakeSession(results=[FakeResult(items=[_export_row()])])
    rows = await svc.get_my_tenders_export(s, _TID)
    assert len(rows) == 1
    r = rows[0]
    assert r.title == "Тендер 1"
    assert r.amount == Decimal("1000")
    assert r.user_status == "watching"
    assert r.folder_names == "Приоритет"


@pytest.mark.asyncio
async def test_get_my_tenders_export_empty_is_not_an_error():
    """No folders, no watching — an empty list, not an exception (the route will give the CSV)."""
    s = FakeSession(results=[FakeResult(items=[])])
    assert await svc.get_my_tenders_export(s, _TID) == []


@pytest.mark.asyncio
async def test_get_my_tenders_export_titleless_row_gets_placeholder():
    s = FakeSession(results=[FakeResult(items=[_export_row(title=None)])])
    assert (await svc.get_my_tenders_export(s, _TID))[0].title == "Без названия"


@pytest.mark.asyncio
async def test_get_my_tenders_export_sets_tenant_before_reading():
    """RLS: set_tenant — the FIRST action, otherwise the selection reads the wrong tenant.

    Mutation check: remove ``repo.set_tenant`` from get_my_tenders_export — the test must go red here (and not only in the live RLS regression).
    """
    s = FakeSession(results=[FakeResult(items=[_export_row()])])
    await svc.get_my_tenders_export(s, _TID)
    assert "app.current_tenant_id" in s.executed[0][0]


@pytest.mark.asyncio
async def test_get_my_tenders_export_scopes_every_tenant_table_by_tid():
    """Each tenant-scoped table of the selection — under an explicit WHERE tenant_id.

    A second layer on top of RLS (CLAUDE.md): tenders_normalized of the tenant does not know it, therefore the rows must be picked only through its own tenant-scoped tables.
    """
    s = FakeSession(results=[FakeResult(items=[])])
    await svc.get_my_tenders_export(s, _TID)
    sql, params = s.executed[1]
    assert params == {"tid": _TID}
    for table in ("tender_user_status", "folder_items", "tender_folders"):
        assert table in sql
    # tenant_id is bound only by the :tid bind — a literal UUID must not be there.
    assert sql.count("tenant_id = CAST(:tid AS uuid)") == 5


@pytest.mark.asyncio
async def test_export_mirrors_page_funnel_predicate():
    """The export and the page take “my tenders” from ONE predicate.

    If they diverged, the CSV would get something other than what the subscriber sees on /app/my. With VG-17w the predicate is wider (all stages except new), and BOTH places must move at once — they take it from one constant.
    """
    assert str(svc._MY_STATUS_IN) in str(svc._EXPORT_ROWS_SQL)
    assert str(svc._MY_STATUS_IN) in str(svc._MY_TENDERS_SQL)
