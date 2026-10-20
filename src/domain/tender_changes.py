"""Tender changes (World Model): a structural "was → became" diff and its rendering.

Pure functions with no network or DB. Used by three layers:

- ingest (``src/workers/ingest.py``) — computes the diff on a card content-UPDATE,
  on a deadline change found by re-polling, and on a documents re-check;
- the notifier (``src/workers/tender_change_notifier.py``) — merges the accumulated
  tender events into one and renders the message for the subscriber;
- tests.

Event format (JSON, stored in ``tender_changes.diff``)::

    {"deadline": {"old": "2026-10-20T09:00:00+00:00", "new": "…"},
     "amount":   {"old": "100000.00", "new": "120000.00", "currency": "RSD"},
     "files":    {"added": ["a.pdf"], "changed": ["b.docx"], "removed": ["c.xls"]},
     "note":     "Naručilac produžava rok …"}

An empty event (no key filled in) is NOT written — a "change without changes"
is of no use to the subscriber. File names and the ``note`` text are source DATA
and are not translated; labels come from the ``i18n_common``/``i18n_email`` catalogs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from src.domain.tz import to_local

#: Marker key in the payload of a correction record (Form 14): set by the adapter, removed by ingest
#: BEFORE hashing and writing to ``tenders_raw`` (the marker never reaches the DB).
CORRECTION_KEY = "_correction"
#: Key in the stored ``tenders_raw.raw_payload``: Ids of corrections already accepted.
CORRECTIONS_SEEN_KEY = "_corrections"
#: How many file names to show on one message line (the rest — "+N").
MAX_FILE_NAMES = 5
#: Length cap for the customer's note (Telegram limits the whole message to 4096).
MAX_NOTE_CHARS = 400


def _utc_iso(dt: Any) -> str | None:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _entries(index: Any) -> dict[str, dict[str, Any]]:
    """File list → ``{name: entry}``. A non-list / entries without a name are skipped."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(index, list):
        return out
    for item in index:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = item
    return out


def _file_changed(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Did the file with the same name change?

    sha256 on both sides → IT decides (the "Izmenjen" (changed) status without a byte change
    is not a change). No sha256 (file was not downloaded: slot/budget) → the version or the
    source status.
    """
    o_sha, n_sha = old.get("sha256"), new.get("sha256")
    if o_sha and n_sha:
        return o_sha != n_sha
    return (old.get("version"), old.get("doc_status")) != (
        new.get("version"),
        new.get("doc_status"),
    )


def diff_documents(old_index: Any, new_index: Any) -> dict[str, list[str]]:
    """Diff of file lists: ``{"added": […], "changed": […], "removed": […]}``.

    An empty OLD list = no baseline (tender accepted before the file list existed,
    #840) → empty diff: "everything added" would be a false message to the subscriber.
    """
    old, new = _entries(old_index), _entries(new_index)
    if not old:
        return {"added": [], "changed": [], "removed": []}
    return {
        "added": sorted(n for n in new if n not in old),
        "changed": sorted(n for n in new if n in old and _file_changed(old[n], new[n])),
        "removed": sorted(n for n in old if n not in new),
    }


def build_change(
    old: dict[str, Any], new: dict[str, Any], *, note: str | None = None
) -> dict[str, Any] | None:
    """Diff of two sets of tender content fields → an event or ``None``.

    ``old``/``new`` — dicts with keys ``deadline_at``, ``amount_original``,
    ``currency_original``, ``documents`` (as in ``tenders_normalized``). Deadline:
    missing in the new one (``None``) is NOT a change — "did not arrive" ≠ "removed"
    (same rule as ``repoll_decision``).
    """
    change: dict[str, Any] = {}

    o_dl, n_dl = _utc_iso(old.get("deadline_at")), _utc_iso(new.get("deadline_at"))
    if n_dl is not None and o_dl != n_dl:
        change["deadline"] = {"old": o_dl, "new": n_dl}

    o_amt, n_amt = _dec(old.get("amount_original")), _dec(new.get("amount_original"))
    if n_amt is not None and o_amt != n_amt:
        change["amount"] = {
            "old": None if o_amt is None else str(o_amt),
            "new": str(n_amt),
            "currency": new.get("currency_original") or old.get("currency_original"),
        }

    files = diff_documents(old.get("documents"), new.get("documents"))
    if any(files.values()):
        change["files"] = files

    if note and change:
        # The customer's note is not an event by itself: without
        # a structural change (deadline/amount/files) there is nothing to compare it with.
        change["note"] = note.strip()[:MAX_NOTE_CHARS]
    return change or None


def merge_changes(changes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Merge a tender's events (in order of occurrence) into one "was → became".

    Deadline: "was" is taken from the FIRST event, "became" from the LAST; back to
    the original — no change. Amount — the same. Files — union of the lists;
    a file added and then removed drops out of the message.
    """
    merged: dict[str, Any] = {}
    for key in ("deadline", "amount"):
        seq = [c[key] for c in changes if key in c]
        if seq and seq[0].get("old") != seq[-1].get("new"):
            merged[key] = {**seq[-1], "old": seq[0].get("old")}

    added: set[str] = set()
    changed: set[str] = set()
    removed: set[str] = set()
    for c in changes:
        f = c.get("files") or {}
        added |= set(f.get("added") or [])
        changed |= set(f.get("changed") or [])
        removed |= set(f.get("removed") or [])
    both = added & removed
    added, removed = added - both, removed - both
    changed -= added
    if added or changed or removed:
        merged["files"] = {
            "added": sorted(added),
            "changed": sorted(changed),
            "removed": sorted(removed),
        }

    notes = [c["note"] for c in changes if c.get("note")]
    if notes and merged:
        merged["note"] = notes[-1]
    return merged or None


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    local = to_local(datetime.fromisoformat(iso))
    return local.strftime("%d.%m.%Y %H:%M")


def _fmt_amount(value: str | None, currency: str | None) -> str:
    if value is None:
        return "—"
    try:
        text = f"{Decimal(value):,.2f}".replace(",", " ")
    except (InvalidOperation, ValueError):
        text = value
    return f"{text} {currency}".strip() if currency else text


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_FILE_NAMES])
    extra = len(names) - MAX_FILE_NAMES
    return f"{shown} (+{extra})" if extra > 0 else shown


def render_lines(
    change: dict[str, Any], locale: str | None, t: Callable[[str | None, str], str]
) -> list[str]:
    """Message lines (no emoji or markup) in the ``locale`` language.

    ``t`` is ``common_t`` for Telegram or ``email_t`` for email: each channel has its
    own language, so the caller passes THAT catalog and THAT language of the channel.
    Values (dates, amounts, file names, note) are substituted into the lines
    as data.
    """
    lines: list[str] = []
    if dl := change.get("deadline"):
        lines.append(
            t(locale, "tc_deadline").format(
                old=_fmt_dt(dl.get("old")), new=_fmt_dt(dl.get("new"))
            )
        )
    if amt := change.get("amount"):
        lines.append(
            t(locale, "tc_amount").format(
                old=_fmt_amount(amt.get("old"), amt.get("currency")),
                new=_fmt_amount(amt.get("new"), amt.get("currency")),
            )
        )
    files = change.get("files") or {}
    for key, label in (
        ("changed", "tc_files_changed"),
        ("added", "tc_files_added"),
        ("removed", "tc_files_removed"),
    ):
        if files.get(key):
            lines.append(t(locale, label).format(names=_names(files[key])))
    if note := change.get("note"):
        lines.append(t(locale, "tc_note").format(text=note))
    if change.get("files", {}).get("changed") or change.get("files", {}).get("added"):
        lines.append(t(locale, "tc_ai_stale"))
    return lines
