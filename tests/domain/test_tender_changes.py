"""Diff of tender changes (src/domain/tender_changes.py): deadline, amount, files, merging, rendering.

Language expectations — LITERALS (not via ``common_t``/``email_t``: an assertion through the same
catalog would be a tautology).
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.domain.tender_changes import (
    build_change,
    diff_documents,
    merge_changes,
    render_lines,
)
from src.i18n_common import common_t
from src.i18n_email import email_t


def _f(name, sha=None, ver="2026-10-10T00:54", st="Активан"):
    return {"name": name, "sha256": sha, "version": ver, "doc_status": st}


_DL1 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
_DL2 = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


# --- diff_documents --------------------------------------------------------- #
def test_files_added_changed_removed_by_sha():
    old = [_f("a.pdf", "1"), _f("b.docx", "2"), _f("c.xls", "3")]
    new = [_f("a.pdf", "1"), _f("b.docx", "22"), _f("d.pdf", "4")]
    assert diff_documents(old, new) == {
        "added": ["d.pdf"],
        "changed": ["b.docx"],
        "removed": ["c.xls"],
    }


def test_status_changed_but_same_bytes_is_not_a_change():
    old = [_f("a.pdf", "1")]
    new = [_f("a.pdf", "1", ver="2026-10-16T01:45", st="Измењен")]
    assert not any(diff_documents(old, new).values())


def test_without_sha_version_or_status_decides():
    old = [_f("a.pdf")]
    new = [_f("a.pdf", ver="2026-10-16T01:45", st="Измењен")]
    assert diff_documents(old, new)["changed"] == ["a.pdf"]


def test_no_baseline_index_is_silent():
    # tender accepted before the file list (#840): "everything added" would be a lie
    assert not any(diff_documents([], [_f("a.pdf", "1")]).values())
    assert not any(diff_documents(None, [_f("a.pdf", "1")]).values())


# --- build_change ----------------------------------------------------------- #
def test_deadline_old_to_new():
    c = build_change({"deadline_at": _DL1}, {"deadline_at": _DL2})
    assert c == {
        "deadline": {
            "old": "2026-10-20T09:00:00+00:00",
            "new": "2026-10-27T09:00:00+00:00",
        }
    }


def test_no_change_is_none_and_missing_new_deadline_is_not_a_change():
    assert build_change({"deadline_at": _DL1}, {"deadline_at": _DL1}) is None
    assert build_change({"deadline_at": _DL1}, {"deadline_at": None}) is None


def test_amount_and_note_only_with_structural_change():
    c = build_change(
        {"amount_original": "100.00", "currency_original": "RSD"},
        {"amount_original": "120.00", "currency_original": "RSD"},
        note="  Продлён срок  ",
    )
    assert c["amount"] == {"old": "100.00", "new": "120.00", "currency": "RSD"}
    assert c["note"] == "Продлён срок"
    # a comment without a structural change is not an event
    assert build_change({"deadline_at": _DL1}, {"deadline_at": _DL1}, note="x") is None


# --- merge_changes ---------------------------------------------------------- #
def test_merge_takes_first_old_and_last_new():
    a = {
        "deadline": {"old": "A", "new": "B"},
        "files": {"added": ["x"], "changed": [], "removed": []},
    }
    b = {
        "deadline": {"old": "B", "new": "C"},
        "files": {"added": [], "changed": ["y"], "removed": ["x"]},
    }
    m = merge_changes([a, b])
    assert m["deadline"] == {"old": "A", "new": "C"}
    # x added and removed — it is dropped from the message
    assert m["files"] == {"added": [], "changed": ["y"], "removed": []}


def test_merge_deadline_returned_to_original_is_nothing():
    a = {"deadline": {"old": "A", "new": "B"}}
    b = {"deadline": {"old": "B", "new": "A"}}
    assert merge_changes([a, b]) is None


# --- rendering: sr/ru/en literals, both catalogs ---------------------------------- #
_CHANGE = {
    "deadline": {
        "old": "2026-10-20T09:00:00+00:00",
        "new": "2026-10-27T09:00:00+00:00",
    },
    "files": {"added": [], "changed": ["TZ.docx", "Model.docx"], "removed": []},
}


def test_render_telegram_catalog_all_locales():
    assert render_lines(_CHANGE, "ru", common_t) == [
        "Срок подачи: 20.10.2026 11:00 → 27.10.2026 10:00",
        "Изменены файлы: TZ.docx, Model.docx",
        "Прежний AI-разбор устарел — закажите новый на карточке.",
    ]
    assert render_lines(_CHANGE, "sr", common_t) == [
        "Rok za podnošenje ponuda: 20.10.2026 11:00 → 27.10.2026 10:00",
        "Izmenjeni fajlovi: TZ.docx, Model.docx",
        "Prethodna AI analiza je zastarela — zatražite novu na kartici.",
    ]
    assert render_lines(_CHANGE, "en", common_t) == [
        "Bid deadline: 20.10.2026 11:00 → 27.10.2026 10:00",
        "Changed files: TZ.docx, Model.docx",
        "The previous AI analysis is out of date — request a new one on the card.",
    ]


def test_render_email_catalog_all_locales():
    assert render_lines(_CHANGE, "ru", email_t)[0].startswith("Срок подачи:")
    assert (
        render_lines(_CHANGE, "sr", email_t)[1]
        == "Izmenjeni fajlovi: TZ.docx, Model.docx"
    )
    assert (
        render_lines(_CHANGE, "en", email_t)[1] == "Changed files: TZ.docx, Model.docx"
    )


def test_render_truncates_long_file_list():
    ch = {
        "files": {
            "added": [f"f{i}.pdf" for i in range(8)],
            "changed": [],
            "removed": [],
        }
    }
    line = render_lines(ch, "en", common_t)[0]
    assert line == "Added files: f0.pdf, f1.pdf, f2.pdf, f3.pdf, f4.pdf (+3)"
