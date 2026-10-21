"""Service of the cabinet workflow / retention (VG-17): folders, notes, watching.

It gives the subscriber a way to organize the work and not miss a deadline:

* **Folders** (``tender_folders`` / ``folder_items``, migration 0029) — group tenders (“my tenders”).
* **Notes** (``tender_notes``) — one free-text note per tender.
* **Funnel** — reuses the existing ``tender_user_status`` (0008), the same table the bot writes to (:mod:`src.bots.status_service`). There is NO separate table / status logic for the web; the list of stages — only from ``STATUSES``:
    - ``set_watch`` — the “follow” toggle (``watching``↔``new``), protects the “rich” stages;
    - ``set_status`` — an explicit change to any of the 7 stages (VG-17w, parity with the bot).
  The deadline reminders are already sent by the worker :mod:`src.workers.reminders` (thresholds 5/3/1/0 for ``active`` / ``watching``), therefore marking the stage from the cabinet turns on the Telegram reminders by itself — no changes in the worker are needed.

RLS contract (VG-01): all functions run under the role ``tendersrb_app`` (FORCE RLS), therefore ``repo.set_tenant`` is MANDATORY as the first action (otherwise ``UndefinedObjectError`` / 0 rows fail-closed) + an explicit ``WHERE tenant_id`` as an extra layer. asyncpg in ``text()`` — only ``CAST(:p AS uuid)``. A cross-tenant access is needed nowhere → no SECURITY DEFINER is created.

Tested on a mock session (ADR-013), without network and a real DB; the RLS isolation — a regression under the app role in tests/db/test_rls_isolation.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.api import repo
from src.api.cabinet.feed_service import WORK_STATUSES
from src.db.models.tender_status import STATUS_LABELS, STATUSES

# “Follow” from the cabinet = the watching status of the tender_user_status funnel (0008). active
# is set by the bot (👀 / ✅) — it also counts as observed (reminders go to both),
# but the web toggle touches ONLY watching↔new, so as not to overwrite the funnel.
_WEB_WATCH_STATUS = "watching"

# The stages by which :mod:`src.workers.reminders` sends the deadline reminders —
# a MIRROR of its ``_WATCH_STATUSES``, not a second copy of the meaning. It is needed only for the
# label in the UI (“reminders come for these stages”); the worker itself reads its own
# constant. A divergence is guarded by tests/test_status_funnel_parity.py.
REMINDER_STATUSES: tuple[str, ...] = ("watching", "active")

# The funnel board /app/my (portal, Stage 2) splits the stages into “in work” and “outcomes”.
# “In work” — a MIRROR of :data:`src.api.cabinet.feed_service.WORK_STATUSES`, by which
# the cockpit counts “N tenders in work”: if the lists diverge, the dashboard
# would count one thing and the board would show another. One predicate per meaning (the project rules), rather than
# a second copy of the literal. The outcomes are derived by subtraction, so a new stage in
# ``STATUSES`` lands on the board by itself (in the archive), rather than vanishing silently.
BOARD_WORK_STATUSES: tuple[str, ...] = WORK_STATUSES
BOARD_OUTCOME_STATUSES: tuple[str, ...] = tuple(
    s for s in STATUSES if s != "new" and s not in BOARD_WORK_STATUSES
)

# “Mine” tender of the funnel = any stage except the default new (= “the subscriber has marked something”).
# ONE predicate per meaning: the page /app/my and the CSV export must show one set (the project rules),
# so the list is derived from STATUSES and not written as a literal;
# a stage that is added will be picked up by both places themselves.
_MY_STATUS_IN = "(" + ", ".join(f"'{s}'" for s in STATUSES if s != "new") + ")"

# --------------------------------------------------------------------------- #
# Folders
# --------------------------------------------------------------------------- #

_LIST_FOLDERS_SQL = text("""
    SELECT f.id::text AS id,
           f.name     AS name,
           COUNT(fi.id) AS item_count
    FROM tender_folders f
    LEFT JOIN folder_items fi
           ON fi.folder_id = f.id
          AND fi.tenant_id = CAST(:tid AS uuid)
    WHERE f.tenant_id = CAST(:tid AS uuid)
    GROUP BY f.id, f.name
    ORDER BY f.name
    """)

_INSERT_FOLDER_SQL = text("""
    INSERT INTO tender_folders (tenant_id, name)
    VALUES (CAST(:tid AS uuid), :name)
    RETURNING id::text
    """)

_DELETE_FOLDER_SQL = text("""
    DELETE FROM tender_folders
    WHERE id = CAST(:fid AS uuid) AND tenant_id = CAST(:tid AS uuid)
    """)

# Folder ownership (RLS-scoped): a check before the insert into folder_items — the FK check
# in PG bypasses RLS, so a foreign folder_id could “stick” to one’s own row.
_FOLDER_OWNED_SQL = text("""
    SELECT 1 FROM tender_folders
    WHERE id = CAST(:fid AS uuid) AND tenant_id = CAST(:tid AS uuid)
    """)

_ADD_ITEM_SQL = text("""
    INSERT INTO folder_items (tenant_id, folder_id, tender_id)
    VALUES (CAST(:tid AS uuid), CAST(:fid AS uuid), CAST(:tender AS uuid))
    ON CONFLICT (folder_id, tender_id) DO NOTHING
    """)

_REMOVE_ITEM_SQL = text("""
    DELETE FROM folder_items
    WHERE tenant_id = CAST(:tid AS uuid)
      AND folder_id = CAST(:fid AS uuid)
      AND tender_id = CAST(:tender AS uuid)
    """)

# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #

_UPSERT_NOTE_SQL = text("""
    INSERT INTO tender_notes (tenant_id, tender_id, body)
    VALUES (CAST(:tid AS uuid), CAST(:tender AS uuid), :body)
    ON CONFLICT (tenant_id, tender_id)
    DO UPDATE SET body = EXCLUDED.body, updated_at = now()
    """)

_DELETE_NOTE_SQL = text("""
    DELETE FROM tender_notes
    WHERE tenant_id = CAST(:tid AS uuid) AND tender_id = CAST(:tender AS uuid)
    """)

_GET_NOTE_SQL = text("""
    SELECT body FROM tender_notes
    WHERE tenant_id = CAST(:tid AS uuid) AND tender_id = CAST(:tender AS uuid)
    """)

_ALL_NOTED_BODY_SQL = text("""
    SELECT tender_id::text, body FROM tender_notes
    WHERE tenant_id = CAST(:tid AS uuid)
    """)

# --------------------------------------------------------------------------- #
# Watching (tender_user_status, 0008)
# --------------------------------------------------------------------------- #

# Enable watching: upsert watching, but do NOT downgrade the rich funnel statuses
# (active / submitted / won / lost) — their WHERE filters them out (only new / skipped are updated).
_WATCH_ON_SQL = text("""
    INSERT INTO tender_user_status (tenant_id, tender_id, status)
    VALUES (CAST(:tid AS uuid), CAST(:tender AS uuid), 'watching')
    ON CONFLICT (tenant_id, tender_id) DO UPDATE
        SET status = 'watching', updated_at = now()
        WHERE tender_user_status.status IN ('new', 'skipped')
    """)

# Disable: only an explicit watching → new (active / the funnel are not touched).
_WATCH_OFF_SQL = text("""
    UPDATE tender_user_status
    SET status = 'new', updated_at = now()
    WHERE tenant_id = CAST(:tid AS uuid)
      AND tender_id = CAST(:tender AS uuid)
      AND status = 'watching'
    """)

# An explicit stage change from the cabinet (VG-17w): we write EXACTLY what the subscriber chose —
# unlike the watch toggle above, which protects the “rich” stages. The same table as the bot’s →
# an active set here is immediately visible to the candidate selection of the reminders worker,
# without a single line of new code in it.
# The value — only via a bind (:status is validated against STATUSES before the call).
_SET_STATUS_SQL = text("""
    INSERT INTO tender_user_status (tenant_id, tender_id, status)
    VALUES (CAST(:tid AS uuid), CAST(:tender AS uuid), :status)
    ON CONFLICT (tenant_id, tender_id) DO UPDATE
        SET status = EXCLUDED.status, updated_at = now()
    """)

# --------------------------------------------------------------------------- #
# State of one tender (the card page)
# --------------------------------------------------------------------------- #

_STATE_WATCH_SQL = text("""
    SELECT status FROM tender_user_status
    WHERE tenant_id = CAST(:tid AS uuid) AND tender_id = CAST(:tender AS uuid)
    """)

_STATE_FOLDERS_SQL = text("""
    SELECT folder_id::text FROM folder_items
    WHERE tenant_id = CAST(:tid AS uuid) AND tender_id = CAST(:tender AS uuid)
    """)

# --------------------------------------------------------------------------- #
# Bulk state for the feed (without the tender_ids parameter — the tenant’s sets are small;
# the intersection with the feed is done in Python, we avoid a bind array).
# --------------------------------------------------------------------------- #

# The funnel stage of each marked tender: the feed needs both the watched flag (the "Following" button)
# and the current stage (the switcher), so we take the status, not only the fact of watching
# — one selection instead of two.
_ALL_STATUS_SQL = text("""
    SELECT tender_id::text AS tender_id, status AS status
    FROM tender_user_status
    WHERE tenant_id = CAST(:tid AS uuid)
    """)

_ALL_FOLDER_ITEMS_SQL = text("""
    SELECT tender_id::text AS tender_id, folder_id::text AS folder_id
    FROM folder_items
    WHERE tenant_id = CAST(:tid AS uuid)
    """)

# --------------------------------------------------------------------------- #
# “My tenders”: folders with tenders + the watch list by deadline
# --------------------------------------------------------------------------- #

# The title-translation toggle (_card_title.html) needs a profile_id — the rows of
# “my tenders” do not carry it themselves (tender_user_status / folder_items
# are tender-centric, without a profile). We resolve it via delivery_outbox: the freshest
# delivery of THIS tender whose profiles.tenant_id is the request’s tenant (delivery_
# outbox does not carry its own tenant_id — the project rules). A correlated subquery degrades itself to NULL
# both when there are no matching rows and when RLS has hidden them (protection in
# two layers, as in feed_service._FEED_SELECT_BODY) — there is nothing to break.
_PROFILE_RESOLVE_SQL = """(
               SELECT o.profile_id::text
               FROM delivery_outbox o
               JOIN profiles p ON p.id = o.profile_id
               WHERE o.tender_id = t.id
                 AND p.tenant_id = CAST(:tid AS uuid)
               ORDER BY o.sent_at DESC NULLS LAST, o.id DESC
               LIMIT 1
           )"""

_FOLDER_TENDERS_SQL = text(f"""
    SELECT f.id::text        AS folder_id,
           f.name            AS folder_name,
           t.id::text        AS tender_id,
           t.title           AS title,
           t.customer_name   AS customer_name,
           t.deadline_at     AS deadline_at,
           t.source_key AS source_key,
           t.source_url      AS tender_url,
           NULLIF(src.display_name, '') AS source_label,
           fi.created_at     AS added_at,
           COALESCE(tus.status, 'new') AS status,
           {_PROFILE_RESOLVE_SQL} AS profile_id
    FROM tender_folders f
    LEFT JOIN folder_items fi
           ON fi.folder_id = f.id AND fi.tenant_id = CAST(:tid AS uuid)
    LEFT JOIN tenders_normalized t ON t.id = fi.tender_id
    LEFT JOIN sources src ON src.source_key = t.source_key
    LEFT JOIN tender_user_status tus
           ON tus.tender_id = fi.tender_id AND tus.tenant_id = CAST(:tid AS uuid)
    WHERE f.tenant_id = CAST(:tid AS uuid)
    ORDER BY f.name, t.deadline_at NULLS LAST
    """)

# The funnel tenders for /app/my — the same predicate `_MY_STATUS_IN` as in the export
# below. Before VG-17w it was ('watching','active'): while the web could only do these
# two stages, “mine” and “observed” coincided. Now all 7 are reachable from the cabinet, and
# a tender marked “🏆 Won” must stay on the page rather than vanish.
_MY_TENDERS_SQL = text(f"""
    SELECT t.id::text      AS tender_id,
           t.title         AS title,
           t.customer_name AS customer_name,
           t.deadline_at   AS deadline_at,
           t.source_key AS source_key,
           t.source_url    AS tender_url,
           NULLIF(src.display_name, '') AS source_label,
           tus.status      AS status,
           {_PROFILE_RESOLVE_SQL} AS profile_id
    FROM tender_user_status tus
    JOIN tenders_normalized t ON t.id = tus.tender_id
    LEFT JOIN sources src ON src.source_key = t.source_key
    WHERE tus.tenant_id = CAST(:tid AS uuid)
      AND tus.status IN {_MY_STATUS_IN}
    ORDER BY t.deadline_at NULLS LAST
    """)

# The deadline calendar (.ics): only “following” / “active” with a deadline that has not passed.
# A subset of _MY_STATUS_IN: submitted / won / lost / skipped are not turned into deadline reminders —
# the submission is already made or the decision taken, so there is no point in a deadline reminder.
_CALENDAR_SQL = text("""
    SELECT t.id::text      AS tender_id,
           t.title         AS title,
           t.customer_name AS customer_name,
           t.deadline_at   AS deadline_at,
           t.source_key    AS source_key
    FROM tender_user_status tus
    JOIN tenders_normalized t ON t.id = tus.tender_id
    WHERE tus.tenant_id = CAST(:tid AS uuid)
      AND tus.status IN ('watching', 'active')
      AND t.deadline_at > :now
    ORDER BY t.deadline_at, t.id
    """)

# --------------------------------------------------------------------------- #
# CSV export of “My tenders” (F2)
# --------------------------------------------------------------------------- #

# The set of export rows = exactly what /app/my shows: tenders in folders
# ∪ tenders marked in the funnel (_MY_STATUS_IN). The tenant scope is carried by tender_user_status /
# folder_items / tender_folders (RLS + an explicit WHERE); tenders_normalized —
# a shared reference, which by itself does not know the tenant, so the selection is driven FROM the
# tenant-scoped tables, not the other way round.
_EXPORT_ROWS_SQL = text(f"""
    WITH mine AS (
        SELECT tender_id FROM tender_user_status
        WHERE tenant_id = CAST(:tid AS uuid)
          AND status IN {_MY_STATUS_IN}
        UNION
        SELECT tender_id FROM folder_items
        WHERE tenant_id = CAST(:tid AS uuid)
    )
    SELECT t.id::text          AS tender_id,
           t.title             AS title,
           t.customer_name     AS customer_name,
           t.amount_original   AS amount,
           t.currency_original AS currency,
           t.deadline_at       AS deadline_at,
           t.source_key AS source_key,
           t.source_url        AS tender_url,
           tus.status          AS user_status,
           string_agg(DISTINCT f.name, ', ') AS folder_names
    FROM mine m
    JOIN tenders_normalized t ON t.id = m.tender_id
    LEFT JOIN tender_user_status tus
           ON tus.tender_id = t.id AND tus.tenant_id = CAST(:tid AS uuid)
    LEFT JOIN folder_items fi
           ON fi.tender_id = t.id AND fi.tenant_id = CAST(:tid AS uuid)
    LEFT JOIN tender_folders f
           ON f.id = fi.folder_id AND f.tenant_id = CAST(:tid AS uuid)
    GROUP BY t.id, t.title, t.customer_name, t.amount_original,
             t.currency_original, t.deadline_at, t.source_url, tus.status
    ORDER BY t.deadline_at NULLS LAST, t.title
    """)


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FolderSummary:
    """A folder + the number of tenders in it (for the dropdown and the list)."""

    id: str
    name: str
    item_count: int


@dataclass(frozen=True)
class TenderState:
    """The state of a tender for the buttons “to folder” / “note” / “follow” / stages.

    ``status`` — the current funnel stage (``STATUSES``); a tender without a row in ``tender_user_status`` is ``new``, as is the column’s server default.
    """

    is_watched: bool
    note: str | None
    folder_ids: frozenset[str]
    status: str = "new"

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, STATUS_LABELS["new"])


@dataclass(frozen=True)
class FeedState:
    """Bulk state for the feed: funnel stages + the note and folder maps."""

    watched: frozenset[str]
    notes: dict[str, str]
    folders_by_tender: dict[str, frozenset[str]]
    statuses: dict[str, str] = field(default_factory=dict)

    def for_tender(self, tender_id: str) -> TenderState:
        return TenderState(
            is_watched=tender_id in self.watched,
            note=self.notes.get(tender_id),
            folder_ids=self.folders_by_tender.get(tender_id, frozenset()),
            status=self.statuses.get(tender_id, "new"),
        )


@dataclass(frozen=True)
class MyTenderRow:
    """The tender row in the “my tenders” section."""

    tender_id: str
    title: str
    customer_name: str | None
    deadline_at: datetime | None
    tender_url: str | None
    note: str | None = None
    status: str = "new"
    # Resolved via delivery_outbox (see _PROFILE_RESOLVE_SQL) — the title-translation toggle
    # (_card_title.html) needs a profile_id, which the row of "my
    # tenders" does not have of its own. None → the row draws an external link without the toggle
    # (an honest link to the portal is better than a 404 — the project rules).
    profile_id: str | None = None
    # The name of the source (sources.display_name); None — the name is not filled in.
    source_label: str | None = None
    #: The source — a deadline without an hour at CFKU (see ``deadline_time`` in capabilities).
    source_key: str | None = None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, STATUS_LABELS["new"])


@dataclass
class FolderView:
    """A folder + its tenders (for the “My tenders” page)."""

    id: str
    name: str
    tenders: list[MyTenderRow] = field(default_factory=list)


@dataclass(frozen=True)
class StageGroup:
    """A funnel stage + its tenders (grouping on /app/my)."""

    status: str
    label: str
    tenders: list[MyTenderRow]

    @property
    def reminds(self) -> bool:
        """Whether the reminders worker sends reminders for this stage."""
        return self.status in REMINDER_STATUSES


@dataclass(frozen=True)
class MyTenders:
    """The aggregate of the “My tenders” page: folders + funnel.

    ``funnel`` — a flat list of marked tenders (any stage except ``new``) by deadline; ``stages`` — the same, grouped by stage in funnel order. The groups are derivatives of ``funnel``, not a second store: if they diverged, the page would show one thing and the CSV export (the same predicate) another.
    """

    folders: list[FolderView]
    funnel: list[MyTenderRow]

    @property
    def stages(self) -> list[StageGroup]:
        by_status: dict[str, list[MyTenderRow]] = {}
        for row in self.funnel:
            by_status.setdefault(row.status, []).append(row)
        return [
            StageGroup(status=s, label=STATUS_LABELS[s], tenders=by_status[s])
            for s in STATUSES
            if by_status.get(s)
        ]


@dataclass(frozen=True)
class FunnelBoard:
    """The funnel “My tenders” as a board: the “in work” columns + the collapsed outcomes.

    A presentation slice over :class:`MyTenders` — there is no second store here; everything is computed from the already loaded ``funnel``. The “in work” columns ALWAYS come, even empty: the funnel board must show the process itself (researching → participating → submitted), otherwise an empty stage looks as if it does not exist.
    """

    work: list[StageGroup]
    outcomes: list[StageGroup]

    @property
    def work_total(self) -> int:
        return sum(len(g.tenders) for g in self.work)

    @property
    def outcome_total(self) -> int:
        return sum(len(g.tenders) for g in self.outcomes)

    @property
    def is_empty(self) -> bool:
        return not (self.work_total or self.outcome_total)


def build_funnel_board(data: MyTenders, active_stage: str | None = None) -> FunnelBoard:
    """Builds the board from the page’s aggregate — a pure function, no DB.

    ``active_stage`` (the ``?stage=`` filter already applied to ``data``) narrows the board to one column: showing three empty “in work” columns next to it, when the subscriber is explicitly looking at “🏆 Won”, is noise. A stage outside the funnel (``new``, garbage in the query) gives an empty board, not an exception: the page renders “there are no tenders at this stage” for such a request.
    """
    by_status: dict[str, list[MyTenderRow]] = {}
    for row in data.funnel:
        by_status.setdefault(row.status, []).append(row)

    def _group(status: str) -> StageGroup:
        return StageGroup(
            status=status,
            label=STATUS_LABELS[status],
            tenders=by_status.get(status, []),
        )

    if active_stage is not None:
        # An empty board, not a column with a zero: on a filtered stage with no
        # tenders it is more honest for the page to say “empty here, show all”.
        if not by_status.get(active_stage):
            return FunnelBoard(work=[], outcomes=[])
        return FunnelBoard(
            work=[_group(active_stage)] if active_stage in BOARD_WORK_STATUSES else [],
            outcomes=(
                [_group(active_stage)] if active_stage in BOARD_OUTCOME_STATUSES else []
            ),
        )
    return FunnelBoard(
        work=[_group(s) for s in BOARD_WORK_STATUSES],
        # Outcomes — only non-empty ones: the archive need not list what did not happen.
        outcomes=[_group(s) for s in BOARD_OUTCOME_STATUSES if by_status.get(s)],
    )


@dataclass(frozen=True)
class MyTenderExportRow:
    """A row of the CSV export “My tenders” (F2).

    ``user_status`` — the raw funnel status of ``tender_user_status`` (or ``None`` if the tender got into the export only via a folder); the human-readable label is given by :mod:`src.api.cabinet.my_tenders_export`.
    """

    tender_id: str
    title: str
    customer_name: str | None
    amount: Decimal | None
    currency: str | None
    deadline_at: datetime | None
    tender_url: str | None
    user_status: str | None
    folder_names: str | None
    #: The source — a deadline without an hour at CFKU (see ``deadline_time`` in capabilities).
    source_key: str | None = None


@dataclass(frozen=True)
class FolderResult:
    """The result of creating a folder."""

    ok: bool
    folder_id: str | None = None
    reason: str | None = None


# --------------------------------------------------------------------------- #
# Folders — operations
# --------------------------------------------------------------------------- #


async def list_folders(db, tenant_id: str) -> list[FolderSummary]:
    """All the tenant’s folders + the number of tenders (sorted by name)."""
    await repo.set_tenant(db, tenant_id)
    rows = (await db.execute(_LIST_FOLDERS_SQL, {"tid": str(tenant_id)})).all()
    return [FolderSummary(r.id, r.name, int(r.item_count)) for r in rows]


async def create_folder(db, *, tenant_id: str, name: str) -> FolderResult:
    """Creates a folder. An empty name → ``reason='blank'``; a duplicate → ``'duplicate'``."""
    clean = (name or "").strip()
    if not clean:
        return FolderResult(False, reason="blank")
    await repo.set_tenant(db, tenant_id)
    try:
        fid = (
            await db.execute(_INSERT_FOLDER_SQL, {"tid": str(tenant_id), "name": clean})
        ).scalar_one()
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return FolderResult(False, reason="duplicate")
    return FolderResult(True, folder_id=str(fid))


async def delete_folder(db, *, tenant_id: str, folder_id: str) -> None:
    """Deletes the tenant’s folder (folder_items go by ON DELETE CASCADE)."""
    await repo.set_tenant(db, tenant_id)
    await db.execute(_DELETE_FOLDER_SQL, {"tid": str(tenant_id), "fid": str(folder_id)})
    await db.commit()


async def add_to_folder(db, *, tenant_id: str, folder_id: str, tender_id: str) -> bool:
    """Puts a tender into a folder. Returns ``False`` if the folder is not the tenant’s."""
    await repo.set_tenant(db, tenant_id)
    owned = (
        await db.execute(
            _FOLDER_OWNED_SQL, {"tid": str(tenant_id), "fid": str(folder_id)}
        )
    ).first()
    if owned is None:
        return False
    await db.execute(
        _ADD_ITEM_SQL,
        {"tid": str(tenant_id), "fid": str(folder_id), "tender": str(tender_id)},
    )
    await db.commit()
    return True


async def remove_from_folder(
    db, *, tenant_id: str, folder_id: str, tender_id: str
) -> None:
    """Removes a tender from a folder."""
    await repo.set_tenant(db, tenant_id)
    await db.execute(
        _REMOVE_ITEM_SQL,
        {"tid": str(tenant_id), "fid": str(folder_id), "tender": str(tender_id)},
    )
    await db.commit()


# --------------------------------------------------------------------------- #
# Notes — operations
# --------------------------------------------------------------------------- #


async def save_note(db, *, tenant_id: str, tender_id: str, body: str) -> str | None:
    """Saves a note. Empty text → deletes it. Returns the resulting text."""
    clean = (body or "").strip()
    await repo.set_tenant(db, tenant_id)
    if not clean:
        await db.execute(
            _DELETE_NOTE_SQL, {"tid": str(tenant_id), "tender": str(tender_id)}
        )
        await db.commit()
        return None
    await db.execute(
        _UPSERT_NOTE_SQL,
        {"tid": str(tenant_id), "tender": str(tender_id), "body": clean},
    )
    await db.commit()
    return clean


async def get_note(db, *, tenant_id: str, tender_id: str) -> str | None:
    """The current note for the tender (or ``None``)."""
    await repo.set_tenant(db, tenant_id)
    row = (
        await db.execute(
            _GET_NOTE_SQL, {"tid": str(tenant_id), "tender": str(tender_id)}
        )
    ).first()
    return row.body if row is not None else None


# --------------------------------------------------------------------------- #
# Watching — operations
# --------------------------------------------------------------------------- #


async def set_watch(db, *, tenant_id: str, tender_id: str, watch: bool) -> bool:
    """Turns watching on / off (watching↔new). Returns the resulting state."""
    await repo.set_tenant(db, tenant_id)
    sql = _WATCH_ON_SQL if watch else _WATCH_OFF_SQL
    await db.execute(sql, {"tid": str(tenant_id), "tender": str(tender_id)})
    await db.commit()
    return watch


async def set_status(db, *, tenant_id: str, tender_id: str, status: str) -> bool:
    """Sets any funnel stage from ``STATUSES``. ``False`` — the value was rejected.

    Parity with the bot (VG-17w): we write to the same ``tender_user_status`` as :mod:`src.bots.status_service`, so an ``active`` / ``watching`` set here is picked up by the reminders worker, and the bot shows the same stage.

    A garbage value is rejected BEFORE the write (``STATUSES`` is the only source of the list): the table’s CHECK constraint would catch it with an exception at commit, but a refusal at the entrance is more honest than a 500 and does not waste the transaction.
    """
    if status not in STATUSES:
        return False
    await repo.set_tenant(db, tenant_id)
    await db.execute(
        _SET_STATUS_SQL,
        {"tid": str(tenant_id), "tender": str(tender_id), "status": status},
    )
    await db.commit()
    return True


# --------------------------------------------------------------------------- #
# State — reading
# --------------------------------------------------------------------------- #


async def get_tender_state(db, *, tenant_id: str, tender_id: str) -> TenderState:
    """The state of one tender (for the card): watch + note + folders."""
    await repo.set_tenant(db, tenant_id)
    watch_row = (
        await db.execute(
            _STATE_WATCH_SQL, {"tid": str(tenant_id), "tender": str(tender_id)}
        )
    ).first()
    note_row = (
        await db.execute(
            _GET_NOTE_SQL, {"tid": str(tenant_id), "tender": str(tender_id)}
        )
    ).first()
    folder_rows = (
        await db.execute(
            _STATE_FOLDERS_SQL, {"tid": str(tenant_id), "tender": str(tender_id)}
        )
    ).all()
    status = getattr(watch_row, "status", None) if watch_row is not None else None
    return TenderState(
        is_watched=status == _WEB_WATCH_STATUS,
        note=note_row.body if note_row is not None else None,
        folder_ids=frozenset(r.folder_id for r in folder_rows),
        status=status or "new",
    )


async def load_feed_state(db, tenant_id: str) -> FeedState:
    """Bulk state for the feed: funnel stages, notes, folder membership."""
    await repo.set_tenant(db, tenant_id)
    status_rows = (await db.execute(_ALL_STATUS_SQL, {"tid": str(tenant_id)})).all()
    notes = (await db.execute(_ALL_NOTED_BODY_SQL, {"tid": str(tenant_id)})).all()
    items = (await db.execute(_ALL_FOLDER_ITEMS_SQL, {"tid": str(tenant_id)})).all()
    folders_by_tender: dict[str, set[str]] = {}
    for r in items:
        folders_by_tender.setdefault(r.tender_id, set()).add(r.folder_id)
    statuses = {r.tender_id: r.status for r in status_rows}
    return FeedState(
        watched=frozenset(
            tid for tid, st in statuses.items() if st == _WEB_WATCH_STATUS
        ),
        notes={r[0]: r.body for r in notes},
        folders_by_tender={k: frozenset(v) for k, v in folders_by_tender.items()},
        statuses=statuses,
    )


# --------------------------------------------------------------------------- #
# The “My tenders” aggregate of the page
# --------------------------------------------------------------------------- #


async def get_my_tenders(db, tenant_id: str) -> MyTenders:
    """Folders with tenders + the funnel (by deadline) for the “My tenders” page."""
    await repo.set_tenant(db, tenant_id)
    folder_rows = (await db.execute(_FOLDER_TENDERS_SQL, {"tid": str(tenant_id)})).all()
    funnel_rows = (await db.execute(_MY_TENDERS_SQL, {"tid": str(tenant_id)})).all()
    notes = {
        r[0]: r.body
        for r in (await db.execute(_ALL_NOTED_BODY_SQL, {"tid": str(tenant_id)})).all()
    }

    folders: list[FolderView] = []
    by_id: dict[str, FolderView] = {}
    for r in folder_rows:
        view = by_id.get(r.folder_id)
        if view is None:
            view = FolderView(id=r.folder_id, name=r.folder_name)
            by_id[r.folder_id] = view
            folders.append(view)
        # LEFT JOIN: an empty folder gives a row with tender_id = NULL — skip it.
        if r.tender_id is not None:
            view.tenders.append(
                MyTenderRow(
                    tender_id=r.tender_id,
                    title=r.title or "Без названия",
                    customer_name=r.customer_name,
                    deadline_at=r.deadline_at,
                    source_key=getattr(r, "source_key", None),
                    tender_url=r.tender_url,
                    note=notes.get(r.tender_id),
                    status=r.status,
                    profile_id=r.profile_id,
                    source_label=getattr(r, "source_label", None),
                )
            )

    funnel = [
        MyTenderRow(
            tender_id=r.tender_id,
            title=r.title or "Без названия",
            customer_name=r.customer_name,
            deadline_at=r.deadline_at,
            source_key=getattr(r, "source_key", None),
            tender_url=r.tender_url,
            note=notes.get(r.tender_id),
            status=r.status,
            profile_id=r.profile_id,
            source_label=getattr(r, "source_label", None),
        )
        for r in funnel_rows
    ]
    return MyTenders(folders=folders, funnel=funnel)


def filter_by_stage(data: MyTenders, stage: str | None) -> MyTenders:
    """Leaves on the page only the tenders of the stage ``stage`` (the folders too).

    The filter is a pure function over the already loaded aggregate: the tenant’s sets are small (as in ``load_feed_state``), a second SQL with a dynamic WHERE is not worth it. An unknown stage and ``None`` → the aggregate unchanged: garbage in the query string must not show the subscriber an empty page.
    """
    if stage not in STATUSES:
        return data
    return MyTenders(
        folders=[
            FolderView(
                id=f.id,
                name=f.name,
                tenders=[t for t in f.tenders if t.status == stage],
            )
            for f in data.folders
        ],
        funnel=[r for r in data.funnel if r.status == stage],
    )


async def get_calendar_deadlines(db, tenant_id: str, now: datetime) -> list:
    """Rows ``(tender_id, title, customer_name, deadline_at, source_key)`` for .ics.

    Tenant scope — ``set_tenant`` FIRST as an action; the selection goes from the tenant-scoped ``tender_user_status``, so other tenants' tenders cannot get into the file.
    """
    await repo.set_tenant(db, tenant_id)
    return list(
        (await db.execute(_CALENDAR_SQL, {"tid": str(tenant_id), "now": now})).all()
    )


async def get_my_tenders_export(db, tenant_id: str) -> list[MyTenderExportRow]:
    """A flat list of “my tenders” for the CSV export (F2).

    One row per tender (folders collapsed into ``folder_names``), sorted by deadline, as on the page. An empty result is a valid state (the tenant has neither folders nor watching); the caller gives a CSV of headers only.
    """
    await repo.set_tenant(db, tenant_id)
    rows = (await db.execute(_EXPORT_ROWS_SQL, {"tid": str(tenant_id)})).all()
    return [
        MyTenderExportRow(
            tender_id=r.tender_id,
            title=r.title or "Без названия",
            customer_name=r.customer_name,
            amount=r.amount,
            currency=r.currency,
            deadline_at=r.deadline_at,
            source_key=getattr(r, "source_key", None),
            tender_url=r.tender_url,
            user_status=r.user_status,
            folder_names=r.folder_names,
        )
        for r in rows
    ]
