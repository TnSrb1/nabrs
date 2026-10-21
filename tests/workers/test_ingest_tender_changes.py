"""Ingest: Correction (F14) — an update of the same tender; diff write; document recheck.

Without DB and network (fake session from test_ingest). Main properties:

* a Correction of a known tender does NOT create a second tender and does NOT overwrite the saved
  list part of the notice record (otherwise published_at would shift);
* a change of deadline/files on a content UPDATE writes an event into ``tender_changes``,
  unchanged content — does not write;
* a repeated intake of the same Correction does not re-download the card (probe by ``_corrections``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.domain.tender_changes import CORRECTION_KEY, CORRECTIONS_SEEN_KEY
from src.parsers.base import RawTender
from src.workers.ingest import compute_raw_hash
from tests.workers.test_ingest import (
    _FakeAdapter,
    _FakeSession,
    _make_worker,
    _norm,
)

_LIST_PART = {"id": "42", "naziv": "Nabavka opreme", "Id": 111}
_OLD_ENRICH = {
    "documents_index": [{"name": "TZ.docx", "sha256": "aa", "version": "v1"}]
}


def _existing(norm, **over):
    base = dict(
        id="norm-1",
        source_url=norm.source_url,
        title=norm.title,
        title_normalized=norm.title_normalized,
        description=norm.description,
        description_normalized=norm.description_normalized,
        customer_name=norm.customer_name,
        customer_tax_id=norm.customer_tax_id,
        customer_region_code=norm.customer_region_code,
        amount_original=norm.amount_original,
        currency_original=norm.currency_original,
        amount_eur=norm.amount_eur,
        is_amount_estimated=norm.is_amount_estimated,
        published_at=norm.published_at,
        deadline_at=norm.deadline_at,
        opening_at=norm.opening_at,
        classifications=norm.classifications,
        procedure_type=norm.procedure_type,
        contract_type=norm.contract_type,
        language=norm.language,
        regions=norm.regions,
        status=norm.status,
        documents=norm.documents,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _norm_with(**over):
    n = _norm("42")
    for k, v in over.items():
        setattr(n, k, v)
    return n


def _changes(session):
    return [p for sql, p in session.calls if "INSERT INTO tender_changes" in sql]


# --- diff on content UPDATE ------------------------------------------------- #
@pytest.mark.asyncio
async def test_content_update_records_deadline_and_file_diff():
    old = _norm_with(
        deadline_at=datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc),
        documents=[{"name": "TZ.docx", "sha256": "aa"}],
    )
    new = _norm_with(
        deadline_at=datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc),
        documents=[{"name": "TZ.docx", "sha256": "bb"}],
    )
    session = _FakeSession(
        raw_lookup=SimpleNamespace(id="raw-1", raw_hash="old"),
        norm_lookup=_existing(old),
    )
    adapter = _FakeAdapter(
        [RawTender("fake", {"id": "42", "rev": 2}, "RS")], normalized_by_uid={"42": new}
    )
    result = await _make_worker(session, adapter)._ingest(adapter, "fake", since=None)

    assert result.updated == 1
    [ch] = _changes(session)
    assert ch["tender_id"] == "norm-1" and ch["kind"] == "content"
    assert json.loads(ch["diff"]) == {
        "deadline": {
            "old": "2026-10-20T09:00:00+00:00",
            "new": "2026-10-27T09:00:00+00:00",
        },
        "files": {"added": [], "changed": ["TZ.docx"], "removed": []},
    }


@pytest.mark.asyncio
async def test_content_update_without_tracked_change_writes_no_event():
    old = _norm_with(title="Staro")
    new = _norm_with(title="Novo")
    session = _FakeSession(
        raw_lookup=SimpleNamespace(id="raw-1", raw_hash="old"),
        norm_lookup=_existing(old),
    )
    adapter = _FakeAdapter(
        [RawTender("fake", {"id": "42", "rev": 2}, "RS")], normalized_by_uid={"42": new}
    )
    result = await _make_worker(session, adapter)._ingest(adapter, "fake", since=None)
    assert result.updated == 1
    assert _changes(session) == []


# --- Correction -------------------------------------------------------------- #
def _correction_raw(enrich=None, note="Продужен рок"):
    payload = {
        "id": "42",
        "naziv": "Ispravka",
        "Id": 222,
        "DocumentTypeId": 9,
        CORRECTION_KEY: {"notice_id": 222, "note": note},
    }
    if enrich is not None:
        payload["_enrich"] = enrich
    return RawTender("fake", payload, "RS")


@pytest.mark.asyncio
async def test_correction_updates_same_tender_and_keeps_list_part():
    old = _norm_with(deadline_at=datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc))
    new = _norm_with(deadline_at=datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc))
    stored = {**_LIST_PART, "_enrich": _OLD_ENRICH}
    session = _FakeSession(
        raw_lookup=SimpleNamespace(
            id="raw-1", raw_hash=compute_raw_hash(stored), raw_payload=stored
        ),
        norm_lookup=_existing(old),
    )
    raw = _correction_raw(enrich={"documents_index": [], "deadline_raw": "27.10"})
    adapter = _FakeAdapter([raw], normalized_by_uid={"42": new})

    result = await _make_worker(session, adapter)._ingest(adapter, "fake", since=None)

    # the same tender: an update, not an insert
    assert (result.inserted, result.updated) == (0, 1)
    assert not session.ran("INSERT INTO tenders_raw")
    assert not session.ran("INSERT INTO tenders_normalized")
    assert session.params_for("UPDATE tenders_normalized")["id"] == "norm-1"
    # list part of the notice preserved, the marker in the DB did not move, the Correction Id counted
    saved = json.loads(session.params_for("UPDATE tenders_raw")["raw_payload"])
    assert saved["Id"] == 111 and saved["naziv"] == "Nabavka opreme"
    assert CORRECTION_KEY not in saved and "DocumentTypeId" not in saved
    assert saved[CORRECTIONS_SEEN_KEY] == [222]
    assert saved["_enrich"]["deadline_raw"] == "27.10"
    # the customer comment reached the event
    [ch] = _changes(session)
    assert json.loads(ch["diff"])["note"] == "Продужен рок"
    assert json.loads(ch["diff"])["deadline"]["new"] == "2026-10-27T09:00:00+00:00"


@pytest.mark.asyncio
async def test_correction_without_enrichment_changes_nothing():
    stored = {**_LIST_PART, "_enrich": _OLD_ENRICH}
    session = _FakeSession(
        raw_lookup=SimpleNamespace(
            id="raw-1", raw_hash=compute_raw_hash(stored), raw_payload=stored
        ),
        norm_lookup=_existing(_norm_with()),
    )
    adapter = _FakeAdapter([_correction_raw(enrich=None)])
    result = await _make_worker(session, adapter)._ingest(adapter, "fake", since=None)
    assert result.skipped == 1
    assert not session.ran("UPDATE tenders_raw")
    assert not session.ran("UPDATE tenders_normalized")


@pytest.mark.asyncio
async def test_correction_of_unknown_tender_is_accepted_as_new():
    # we did not see the notice: the card by TenderId returns the current state in full
    session = _FakeSession(raw_lookup=None, norm_lookup=None)
    adapter = _FakeAdapter([_correction_raw(enrich={"documents_index": []})])
    result = await _make_worker(session, adapter)._ingest(adapter, "fake", since=None)
    assert result.inserted == 1
    saved = json.loads(session.params_for("INSERT INTO tenders_raw")["raw_payload"])
    assert CORRECTION_KEY not in saved and saved[CORRECTIONS_SEEN_KEY] == [222]


# --- "not changed" probe ------------------------------------------------------ #
@pytest.mark.asyncio
async def test_probe_correction_seen_once_and_original_record_stays_unchanged():
    stored = {**_LIST_PART, CORRECTIONS_SEEN_KEY: [222], "_enrich": _OLD_ENRICH}
    session = _FakeSession(raw_lookup=SimpleNamespace(raw_payload=stored))
    worker = _make_worker(session, _FakeAdapter([]))
    probe = worker._list_part_unchanged

    seen = {**_LIST_PART, CORRECTION_KEY: {"notice_id": 222, "note": "x"}}
    new = {**_LIST_PART, CORRECTION_KEY: {"notice_id": 333, "note": "x"}}
    assert (
        await probe(session, "fake", "42", seen) is True
    )  # Correction already accepted
    assert await probe(session, "fake", "42", new) is False  # new one — we take it
    # the original notice record after the Correction is still "not changed"
    assert await probe(session, "fake", "42", dict(_LIST_PART)) is True


# --- document recheck: deadline/status do not roll back -------------------- #
_STALE = datetime(
    2026, 10, 20, 9, 0, tzinfo=timezone.utc
)  # deadline at the moment of intake (raw)
_MOVED = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)  # repoll moved deadline_at
_DOCS = SimpleNamespace(index=[{"name": "TZ.docx", "version": "v2"}], text="tekst")


async def _recheck(*, current, card, stale=_STALE, old_documents=None):
    """One recheck: normalize returns the OLD deadline from raw, in the DB — ``current``."""
    stale_norm = _norm_with(deadline_at=stale, documents=_DOCS.index)
    stored = {**_LIST_PART, "_enrich": _OLD_ENRICH}
    row = _existing(
        _norm_with(deadline_at=current, documents=old_documents or []),
        status="active",
    )
    session = _FakeSession(
        raw_lookup=SimpleNamespace(id="raw-1", raw_hash="old", raw_payload=stored),
        norm_lookup=row,
    )
    adapter = _FakeAdapter([], normalized_by_uid={"42": stale_norm})
    worker = _make_worker(session, adapter)
    updated = await worker._apply_documents_recheck(
        adapter,
        "fake",
        "42",
        "norm-1",
        _DOCS,
        datetime(2026, 10, 20, tzinfo=timezone.utc),
        card,
    )
    return session, updated


@pytest.mark.asyncio
async def test_recheck_does_not_roll_back_deadline_moved_by_repoll():
    session, updated = await _recheck(
        current=_MOVED, card={"deadline_at": None, "status": None}
    )
    assert updated
    assert session.params_for("UPDATE tenders_normalized")["deadline_at"] == _MOVED
    # deadline not changed → no "deadline" event
    assert all("deadline" not in json.loads(c["diff"]) for c in _changes(session))


@pytest.mark.asyncio
async def test_recheck_takes_fresh_card_deadline_over_stored():
    session, _ = await _recheck(
        current=_STALE, card={"deadline_at": _MOVED, "status": None}
    )
    assert session.params_for("UPDATE tenders_normalized")["deadline_at"] == _MOVED


@pytest.mark.asyncio
async def test_recheck_with_null_deadline_raw_keeps_current_deadline():
    session, _ = await _recheck(
        current=_MOVED, card={"deadline_at": None, "status": None}, stale=None
    )
    assert session.params_for("UPDATE tenders_normalized")["deadline_at"] == _MOVED


@pytest.mark.asyncio
async def test_first_recheck_without_baseline_creates_no_change_event():
    # no baseline of the list (documents=[]) → this is a snapshot, not a change
    session, updated = await _recheck(
        current=_MOVED, card={"deadline_at": None, "status": None}, old_documents=[]
    )
    assert updated
    assert _changes(session) == []
