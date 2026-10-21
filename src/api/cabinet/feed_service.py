"""Delivery feed + burning deadlines + visible AI actions (VG-28.3b-BE).

Two blocks:
- :func:`get_feed_items` — the latest N CURRENT matching tenders of the tenant
  (``delivery_outbox`` in ``FEED_VISIBLE_STATUSES``: sent, cut by the
  daily limit, and still on the way — the limit cuts only Telegram/email, the site
  shows everything), with per-item flags of really existing
  AI artifacts (see :class:`AiActions`). Dedup by (tender_id, profile_id):
  one profile with several delivery channels (email+telegram) writes
  a SEPARATE outbox row per channel (``write_outbox``, UNIQUE (profile_id,
  tender_id, channel)) — without dedup the same tender would take
  two places in the feed instead of one.
- :func:`get_hot_deadlines` — tenders delivered to the tenant with a deadline not yet
  expired and within the nearest ``window_days`` days, dedup by tender.
- The dashboard cockpit (the last section of the file): :func:`build_work_summary`,
  :func:`build_calendar_events`, :func:`build_calendar_month` — PURE functions
  over the data already read, without their own SQL.

AI artifacts in ``tender_ai_analyses`` (UNIQUE tender_id, profile_id, level):
- the ``level`` of delivery (quick/standard) — already bound to the row through
  ``delivery_outbox.ai_analysis_id`` (routing, see src/workers/ai.py); L3 deep
  is not created in auto-routing (only on demand, profile_id=NULL).
- the ``level`` of the bot’s on-demand actions (bid/checklist/draft) — always
  per (tender_id, profile_id), profile_id is NOT NULL (see
  src/bots/bid_verdict_service.py, compliance_checklist_service.py,
  draft_service.py). Therefore for each feed item (tender_id + profile_id are known)
  their presence can be honestly checked by a direct JOIN — there are no fictitious actions here.

All functions take an AsyncSession — they are tested on a mock session (ADR-013).
Tenant queries: repo.set_tenant + an explicit WHERE p.tenant_id (double
isolation, as in stats_service.py). Timezone-aware (UTC), like get_days_left.
"""

from __future__ import annotations

import calendar as _calendar
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.api import repo
from src.api.cabinet import preview_service
from src.db.models.tender_status import STATUS_LABELS, STATUSES
from src.dedup.also_on import also_on_count_sql
from src.domain.actuality import sql_no_deadline_fresh_tn
from src.domain.tz import belgrade_days_within_sql, calendar_days_until

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #

# The common source of the feed (dedup by (tender_id, profile_id), the freshest sent).
# The dashboard (:func:`get_feed_items`, LIMIT) and the web archive (:func:`get_feed_page`,
# LIMIT/OFFSET) read ONE selection — WHERE/dedup/ORDER do not diverge: pagination
# must return the same rows as the dashboard, only deeper. The trailing
# LIMIT/OFFSET is appended in the queries themselves, the body is common.
#
# The feed is WORKING, not an archive: see :data:`_CURRENT_ONLY_PREDICATE` and
# :data:`_PROFILE_FILTER_PREDICATE` — both predicates live as separate constants
# and are substituted BOTH into the body AND into the COUNT (:data:`_FEED_COUNT_SQL`). If they diverged —
# `total` and the pagination would show one thing, and the pages would return another.

# “Current” = the tender can still be applied to: the deadline is in the future OR there is no deadline
# and the tender is younger than 30 days from publication (owner 06.10.2026,
# src/domain/actuality.py). Before this decision, “no deadline” meant “current forever” — and
# 6351 of the 6529 “current” feed pairs were older than 30 days (prod, 06.10).
# An expired one leaves the working feed. `now()` — the server time of the DB (UTC), as for
# the burning deadlines: one time for all the feed selections.
#
# ``:actual_only`` (owner 22.09) — the switch “all / only current” on
# /app/feed: a bind parameter, not a second SQL text — the predicate stays ONE and
# the same in the body and in the COUNT (see the class at the top of the file); it is just that at
# ``actual_only=false`` the first branch of the OR makes the whole expression true, and
# the expired ones stop being cut. The dashboard teaser (:func:`get_feed_items`)
# always sends ``actual_only=true`` — there is no switch there and there will not be one (Step 3).
_CURRENT_ONLY_PREDICATE = (
    "(CAST(:actual_only AS boolean) = false"
    f" OR {sql_no_deadline_fresh_tn('tn')} OR tn.deadline_at >= now())"
)

# The filter “which profile the match is with”. A NULL parameter = show all profiles,
# therefore :profile_id is ALWAYS bound (on the dashboard too). The filter works
# INSIDE the tenant: the predicate `p.tenant_id` is not removed next to it, therefore substituting
# someone else’s profile_id into the query string is pointless — no rows will come back.
_PROFILE_FILTER_PREDICATE = (
    "(CAST(:profile_id AS uuid) IS NULL OR o.profile_id = CAST(:profile_id AS uuid))"
)


# What counts as “a match the client sees” (owner 16.10.2026). The daily limit
# cuts only sending to Telegram/email, the site shows ALL matches of the template:
# ``sent`` + ``suppressed`` (cut by the limit) + the whole path to sending (the row is already
# in the queue — the tender is the same). The terminal “did not arrive due to an error”
# (``failed``, ``ai_rejected``) are NOT included — these are not matches but failures. The set mirrors
# ``matching.CLUSTER_DELIVERED_STATUSES`` + ``suppressed`` (a guard in the tests).
FEED_VISIBLE_STATUSES: tuple[str, ...] = (
    "sent",
    "suppressed",
    "delivering",
    "pending",
    "ai_pending",
    "analyzing",
    "ai_done",
    # Cabinet as the third channel (owner 18.10): match without a live Telegram/e-mail.
    "web_only",
)
_STATUS_IN = "o.status IN (" + ", ".join(f"'{s}'" for s in FEED_VISIBLE_STATUSES) + ")"
# The moment of the match for sorting/date: sent; otherwise — cut; otherwise —
# the row was created (suppressed/pending have no sent_at).
_EVENT_TS = "COALESCE(o.sent_at, o.failed_at, o.created_at)"
# One tender = one card: from the per-channel rows we pick the “best” — sent,
# then on the way, then cut (cut only if NO channel sent it).
_STATUS_RANK = "CASE o.status WHEN 'sent' THEN 0 WHEN 'suppressed' THEN 2 ELSE 1 END"


def _feed_select_body(extra_where: str = "") -> str:
    """The feed selection body; ``extra_where`` — ready AND-conditions of the filters.

    Without filters (the dashboard teaser) — exactly the former text. The filter conditions stand
    NEXT TO ``p.tenant_id``, not instead of it (the tenant isolation is not removed).
    """
    return f"""
    SELECT tender_id, profile_id, sent_at, title, customer_name, deadline_at,
           source_key, tender_url, source_label, profile_name, ai_level, delivered_ai_level,
           has_bid_verdict, has_checklist, has_draft, outbox_id,
           amount_eur, published_at, canonical_confidence, also_on_n, capped, web_only
    FROM (
        SELECT DISTINCT ON (o.tender_id, o.profile_id)
            o.tender_id::text          AS tender_id,
            o.profile_id::text         AS profile_id,
            {_EVENT_TS}                AS sent_at,
            (o.status = 'suppressed')  AS capped,
            (o.status = 'web_only')    AS web_only,
            o.id                       AS outbox_id,
            tn.title                   AS title,
            tn.customer_name           AS customer_name,
            tn.deadline_at             AS deadline_at,
            tn.source_key              AS source_key,
            tn.amount_eur              AS amount_eur,
            tn.published_at            AS published_at,
            tn.source_url              AS tender_url,
            NULLIF(src.display_name, '') AS source_label,
            tn.canonical_confidence    AS canonical_confidence,
            {also_on_count_sql('tn')} AS also_on_n,
            p.name                     AS profile_name,
            p.ai_analysis_level        AS ai_level,
            delivered.level            AS delivered_ai_level,
            (bid.id IS NOT NULL)       AS has_bid_verdict,
            (checklist.id IS NOT NULL) AS has_checklist,
            (draft.id IS NOT NULL)     AS has_draft
        FROM delivery_outbox o
        JOIN tenders_normalized tn ON tn.id = o.tender_id
        JOIN profiles p ON p.id = o.profile_id
        LEFT JOIN sources src ON src.source_key = tn.source_key
        LEFT JOIN tender_ai_analyses delivered
               ON delivered.id = o.ai_analysis_id
        LEFT JOIN tender_ai_analyses bid
               ON bid.tender_id = o.tender_id
              AND bid.profile_id = o.profile_id
              AND bid.level = 'bid'
        LEFT JOIN tender_ai_analyses checklist
               ON checklist.tender_id = o.tender_id
              AND checklist.profile_id = o.profile_id
              AND checklist.level = 'checklist'
        LEFT JOIN tender_ai_analyses draft
               ON draft.tender_id = o.tender_id
              AND draft.profile_id = o.profile_id
              AND draft.level = 'draft'
        WHERE p.tenant_id = CAST(:tid AS uuid)
          AND {_STATUS_IN}
          AND {_CURRENT_ONLY_PREDICATE}
          AND {_PROFILE_FILTER_PREDICATE}{extra_where}
        ORDER BY o.tender_id, o.profile_id, {_STATUS_RANK}, {_EVENT_TS} DESC NULLS LAST, o.id DESC
    ) dedup
"""


_FEED_SELECT_BODY = _feed_select_body()

# “By date received” (default, dashboard teaser) — the freshly delivered on top,
# as before. This is the DELIVERY date (``sent_at``), not the tender’s submission deadline — see
# :data:`_FEED_ORDER_BY_DEADLINE` for the second meaning.
_FEED_ORDER_BY_DATE = "    ORDER BY sent_at DESC NULLS LAST, outbox_id DESC\n"

# “By template” (owner 22.09, only the web archive /app/feed): the profile name is primary,
# inside a profile — the fresh ones on top. ``outbox_id DESC`` — the trailing tie-breaker:
# without it rows with an equal (profile_name, sent_at) would not have a stable
# order between requests for the same page, and the pagination would “drift”
# (the same reason as ``_list_profiles`` in the bot — the determinism entirely in the
# ORDER BY, not in the application).
_FEED_ORDER_BY_PROFILE = (
    "    ORDER BY profile_name, sent_at DESC NULLS LAST, outbox_id DESC\n"
)

# “By submission deadline” (owner 22.09, only the web archive /app/feed): the nearest deadline
# first — the question “what is on fire” is solved by sorting, not only by the badge.
# ``NULLS LAST`` — a tender without a known deadline cannot "stand first by a deadline
# that does not exist". ``outbox_id DESC`` — the same determinism tie-breaker
# as the sorting by template: without it rows with an equal ``deadline_at``
# (including all the NULLs at once) would not have a stable order between requests
# of one page, and the pagination would “drift”.
_FEED_ORDER_BY_DEADLINE = "    ORDER BY deadline_at ASC NULLS LAST, outbox_id DESC\n"

# “By amount” (only the filtered feed): ``NULLS LAST`` in both directions — a tender
# without an amount is neither “the cheapest” nor “the most expensive”; the tie-breaker is the same.
_FEED_ORDER_BY_AMOUNT_DESC = "    ORDER BY amount_eur DESC NULLS LAST, outbox_id DESC\n"
_FEED_ORDER_BY_AMOUNT_ASC = "    ORDER BY amount_eur ASC NULLS LAST, outbox_id DESC\n"

#: The sort key → a FIXED ORDER BY (the value from the query does not go into the SQL).
_FEED_ORDER_BY = {
    "date": _FEED_ORDER_BY_DATE,
    "deadline": _FEED_ORDER_BY_DEADLINE,
    "profile": _FEED_ORDER_BY_PROFILE,
    "amount_desc": _FEED_ORDER_BY_AMOUNT_DESC,
    "amount_asc": _FEED_ORDER_BY_AMOUNT_ASC,
}

_FEED_ITEMS_SQL = text(_FEED_SELECT_BODY + _FEED_ORDER_BY_DATE + "    LIMIT :limit\n")
# The dashboard teaser “Deadline soon”: the same corpus, ordered by submission deadline.
_FEED_ITEMS_DEADLINE_SQL = text(
    _FEED_SELECT_BODY + _FEED_ORDER_BY_DEADLINE + "    LIMIT :limit\n"
)

_FEED_PAGE_SQL = text(
    _FEED_SELECT_BODY + _FEED_ORDER_BY_DATE + "    LIMIT :limit OFFSET :offset\n"
)

_FEED_PAGE_SQL_BY_PROFILE = text(
    _FEED_SELECT_BODY + _FEED_ORDER_BY_PROFILE + "    LIMIT :limit OFFSET :offset\n"
)

_FEED_PAGE_SQL_BY_DEADLINE = text(
    _FEED_SELECT_BODY + _FEED_ORDER_BY_DEADLINE + "    LIMIT :limit OFFSET :offset\n"
)


# The COUNT for the archive pagination — a MIRROR of the feed row set: the same JOIN/WHERE
# (including BOTH predicate constants above) and the same dedup by (tender_id,
# profile_id). The INNER JOIN tenders_normalized/profiles (as in the body above) — a tender
# without a normalized row does not get into the feed, and must not get into the counter either,
# otherwise total would diverge from the pages.
def _feed_count_sql(extra_where: str = ""):
    return text(f"""
    SELECT COUNT(*) FROM (
        SELECT DISTINCT o.tender_id, o.profile_id
        FROM delivery_outbox o
        JOIN tenders_normalized tn ON tn.id = o.tender_id
        JOIN profiles p ON p.id = o.profile_id
        WHERE p.tenant_id = CAST(:tid AS uuid)
          AND {_STATUS_IN}
          AND {_CURRENT_ONLY_PREDICATE}
          AND {_PROFILE_FILTER_PREDICATE}{extra_where}
    ) c
""")


_FEED_COUNT_SQL = _feed_count_sql()

#: The time limit of the feed query WITH FILTERS (ms); as for the portal (#568): a search
#: by substring in the description/customer on a large stream of the tenant must not hang
#: the connection. A timeout — not a 500, but the hint “refine the query”.
FEED_STATEMENT_TIMEOUT_MS = 3000
_QUERY_CANCELED = "57014"


#: Allowed feed sorts (the first is the default). ORDER BY — from ``_FEED_ORDER_BY``.
FEED_SORT_KEYS: tuple[str, ...] = (
    "date",
    "deadline",
    "amount_desc",
    "amount_asc",
    "profile",
)


def feed_filter_sql(f) -> tuple[str, dict]:
    """The feed filter conditions → ``(" AND …", bind parameters)`` for the internal WHERE.

    ``f`` — ``PortalFilters`` (``src.api.public.portal_filters``): q/customer/
    source/amount/publication — the common :func:`repo.filter_clauses` (the same code
    as the portal’s), region and submission deadline — their own. ``profile_id``/``actual`` do not
    enter here: they have their own predicates (``_PROFILE_FILTER_PREDICATE``,
    ``_CURRENT_ONLY_PREDICATE``). Values — bind parameters with ``CAST``.
    """
    if f is None:
        return "", {}
    clauses, params = repo.filter_clauses(f, alias="tn.")
    if f.region:
        clauses.append("tn.regions @> ARRAY[CAST(:region AS text)]")
        params["region"] = f.region
    if f.deadline_to is not None:
        clauses.append("tn.deadline_at < CAST(:deadline_to AS date) + 1")
        params["deadline_to"] = f.deadline_to
    if not clauses:
        return "", {}
    return "".join(f"\n          AND {c}" for c in clauses), params


# The page size of the web archive of matches (sample: admin_service.PAGE_SIZE).
FEED_PAGE_SIZE = 20

# The window “burns” on /app/feed — exactly the one on which the deadline badge stops
# being colored (`badge-new` at ≤7 days, further on — a gray date). One number for
# one meaning: if it diverges from the template, the card will land in the “Deadline soon” section
# with a gray badge “until 12.08” and vice versa.
FEED_URGENT_WINDOW_DAYS = 7

_HOT_DEADLINES_SQL = text(f"""
    SELECT tender_id, profile_id, title, customer_name, deadline_at, source_key, tender_url
    FROM (
        SELECT DISTINCT ON (tn.id)
            tn.id::text        AS tender_id,
            o.profile_id::text AS profile_id,
            tn.title           AS title,
            tn.customer_name   AS customer_name,
            tn.deadline_at     AS deadline_at,
            tn.source_key      AS source_key,
            tn.source_url      AS tender_url
        FROM delivery_outbox o
        JOIN tenders_normalized tn ON tn.id = o.tender_id
        JOIN profiles p ON p.id = o.profile_id
        WHERE p.tenant_id = CAST(:tid AS uuid)
          AND {_STATUS_IN}
          AND tn.deadline_at IS NOT NULL
          AND tn.deadline_at > now()
          AND {belgrade_days_within_sql("tn.deadline_at", ":window_days")}
        ORDER BY tn.id, tn.deadline_at ASC
    ) dedup
    ORDER BY deadline_at ASC
    LIMIT :limit
""")

# Number of the "Upcoming deadlines" tile: THE SAME predicate as ``_HOT_DEADLINES_SQL``
# (delivered, open, deadline ≤ N calendar days), but COUNT without LIMIT — the tile
# must not depend on how many rows fit into the list. ``skip`` — tenders
# already in "my" working stages: :func:`urgent_events` counts them separately,
# and one tender must not be counted twice.
_URGENT_COUNT_SQL = text(f"""
    SELECT count(DISTINCT tn.id)
    FROM delivery_outbox o
    JOIN tenders_normalized tn ON tn.id = o.tender_id
    JOIN profiles p ON p.id = o.profile_id
    WHERE p.tenant_id = CAST(:tid AS uuid)
      AND {_STATUS_IN}
      AND tn.deadline_at IS NOT NULL
      AND tn.deadline_at > now()
      AND {belgrade_days_within_sql("tn.deadline_at", ":window_days")}
      AND NOT (tn.id = ANY(CAST(:skip AS uuid[])))
""")

# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AiActions:
    """AI artifacts that really exist for the pair (tender_id, profile_id).

    ``delivered_analysis_level`` — the analysis level bound to the delivery itself
    (quick/standard; None, if no auto-analysis was done). The other
    three fields are on-demand actions from the Telegram buttons (the bot, out of scope of this PR).
    """

    delivered_analysis_level: str | None
    has_bid_verdict: bool
    has_checklist: bool
    has_draft: bool


@dataclass(frozen=True)
class FeedItem:
    """One delivery feed item."""

    tender_id: str
    title: str
    customer_name: str | None
    deadline_at: datetime | None
    tender_url: str | None
    profile_id: str
    profile_name: str
    ai_level: str | None
    sent_at: datetime | None
    ai_actions: AiActions
    # The name of the source (sources.display_name); None — the name is not filled in.
    source_label: str | None = None
    # The source — it decides whether to show the deadline hour (deadline_time).
    source_key: str | None = None
    # The mark “the same tender on other sources” (src.dedup.also_on, 0047).
    canonical_confidence: str | None = None
    also_on_n: int = 0
    # Cut by the daily limit: NO channel sent it (on the site the tender is complete).
    capped: bool = False
    # Cabinet row without a live channel (#820): match found, nothing sent.
    web_only: bool = False


@dataclass(frozen=True)
class FeedGroups:
    """The archive page, split by urgency (purely presentational).

    ``urgent`` — matches for which one can still apply and the deadline is already
    visible (≤ :data:`FEED_URGENT_WINDOW_DAYS` days); ``rest`` — everything else:
    distant deadlines and tenders without a deadline. Expired ones are no longer in the working feed
    (``_CURRENT_ONLY_PREDICATE``), but the function is PURE and stays robust to them:
    an expired deadline does not get into “burning” here either.
    """

    urgent: list[FeedItem] = field(default_factory=list)
    rest: list[FeedItem] = field(default_factory=list)


@dataclass(frozen=True)
class RegionHiddenNotice:
    """An honest banner “hidden by the district filter” for the feed/archive.

    ``hidden`` — ≈ how many suitable tenders were cut off by the district filter (their
    ``regions`` are not resolved), an aggregate over the tenant’s active profiles with a non-empty
    district filter. ``profile_id`` — where the “Configure the district filter” link leads
    (the first profile with a district). Computed by REUSING ``preview_service`` —
    a mirror of the delivery predicate ``STAGE_A_SQL`` (``PREVIEW_WHERE_A``), NOT a new
    predicate.
    """

    hidden: int
    profile_id: str


@dataclass(frozen=True)
class HotDeadline:
    """A burning deadline: a delivered tender whose submission deadline is expiring soon."""

    tender_id: str
    title: str
    customer_name: str | None
    deadline_at: datetime
    tender_url: str | None
    days_left: int
    # ‼️ Needed for the card link (/app/tenders/{id}?profile_id=…): without it
    # the route returns 404. Incident 25.07: profile_id was added to the SQL, but NOT here —
    # getattr returned None, and all 100 rows of the agenda led to a 404.
    profile_id: str | None = None
    #: The source — see :attr:`FeedItem.source_key` (a deadline without the hour for CFKU).
    source_key: str | None = None


# --------------------------------------------------------------------------- #
# Service functions
# --------------------------------------------------------------------------- #


async def get_feed_items(
    db, tenant_id: str, limit: int = 10, sort: str = "date"
) -> list[FeedItem]:
    """The last ``limit`` delivered CURRENT tenders of the tenant, fresh ones on top.

    The dashboard teaser reads the same body as the archive, therefore “current only”
    applies here too: showing on the cockpit a tender that can no longer be applied to —
    is a promise of work that does not exist. The teaser has no profile filter
    (``:profile_id`` = NULL), but the parameter is bound anyway — the body is common.

    ``sort=“deadline”`` — the teaser “Deadline soon” (the nearest submission deadline first,
    among the current ones); any other value — “fresh on top”.

    Requires repo.set_tenant + an explicit WHERE p.tenant_id (double isolation).
    """
    await repo.set_tenant(db, tenant_id)
    rows = (
        await db.execute(
            _FEED_ITEMS_DEADLINE_SQL if sort == "deadline" else _FEED_ITEMS_SQL,
            {
                "tid": str(tenant_id),
                "limit": limit,
                "profile_id": None,
                "actual_only": True,
            },
        )
    ).all()
    return [_row_to_feed_item(row) for row in rows]


def _row_to_feed_item(row) -> FeedItem:
    """Mapping of a feed selection row to :class:`FeedItem` (common for all paths)."""
    return FeedItem(
        tender_id=row.tender_id,
        title=row.title or "Без названия",
        customer_name=row.customer_name,
        deadline_at=row.deadline_at,
        tender_url=row.tender_url,
        profile_id=row.profile_id,
        profile_name=row.profile_name or "Без названия",
        ai_level=row.ai_level,
        sent_at=row.sent_at,
        source_label=getattr(row, "source_label", None),
        source_key=getattr(row, "source_key", None),
        canonical_confidence=getattr(row, "canonical_confidence", None),
        also_on_n=int(getattr(row, "also_on_n", 0) or 0),
        capped=bool(getattr(row, "capped", False)),
        web_only=bool(getattr(row, "web_only", False)),
        ai_actions=AiActions(
            delivered_analysis_level=row.delivered_ai_level,
            has_bid_verdict=bool(row.has_bid_verdict),
            has_checklist=bool(row.has_checklist),
            has_draft=bool(row.has_draft),
        ),
    )


async def get_feed_page(
    db,
    tenant_id: str,
    *,
    page: int = 1,
    page_size: int = FEED_PAGE_SIZE,
    profile_id: str | None = None,
    sort: str = "date",
    actual_only: bool = True,
    filters=None,
) -> list[FeedItem]:
    """One page of the working feed of matches (LIMIT/OFFSET over the feed selection).

    The source is common with :func:`get_feed_items` (``_FEED_SELECT_BODY``): the same
    WHERE/dedup, only the LIMIT/OFFSET window and (only here) the sorting differ.
    ``profile_id`` (None — all profiles) narrows the selection to one profile of the tenant
    and must be passed to :func:`count_feed_total` with THE SAME value — otherwise
    ``total`` would count one set, and the pages would return another.
    ``sort=“profile”``/``sort=“deadline”`` — the web archive /app/feed, owner 22.09
    (``_FEED_ORDER_BY_PROFILE``/``_FEED_ORDER_BY_DEADLINE``); any other
    value — the default “by date received”, as before.
    ``actual_only`` (owner 22.09) — a bind parameter in :data:`_CURRENT_ONLY_PREDICATE`;
    True (default, the former behavior) cuts expired ones, False shows
    everything. Must be passed to :func:`count_feed_total` with THE SAME value for the
    same reason as ``profile_id``. Requires repo.set_tenant + an explicit
    WHERE p.tenant_id (double isolation).
    """
    page = max(1, page)
    offset = (page - 1) * page_size
    await repo.set_tenant(db, tenant_id)
    extra_where, extra_params = feed_filter_sql(filters)
    if extra_where or sort in ("amount_desc", "amount_asc"):
        order_by = _FEED_ORDER_BY.get(sort, _FEED_ORDER_BY_DATE)
        stmt = text(
            _feed_select_body(extra_where)
            + order_by
            + "    LIMIT :limit OFFSET :offset\n"
        )
    elif sort == "profile":
        stmt = _FEED_PAGE_SQL_BY_PROFILE
    elif sort == "deadline":
        stmt = _FEED_PAGE_SQL_BY_DEADLINE
    else:
        stmt = _FEED_PAGE_SQL
    rows = (
        await db.execute(
            stmt,
            {
                "tid": str(tenant_id),
                "limit": page_size,
                "offset": offset,
                "profile_id": profile_id or None,
                "actual_only": actual_only,
                **extra_params,
            },
        )
    ).all()
    return [_row_to_feed_item(row) for row in rows]


async def get_group_carry(
    db,
    tenant_id: str,
    *,
    page: int,
    page_size: int = FEED_PAGE_SIZE,
    profile_id: str | None = None,
    sort: str = "profile",
    actual_only: bool = True,
    filters=None,
) -> str | None:
    """``profile_id`` of the row standing BEFORE the page ``page`` (None — there is no such one).

    Needed by the group header “By template”: if the page starts from the middle of a
    group, the header is marked “continuation”. We take exactly one row of the same
    selection and the same ORDER BY: a window of 1 row at position ``(page-1)*page_size-1``
    — the same :func:`get_feed_page`, without its own SQL.
    """
    if page <= 1:
        return None
    prev = await get_feed_page(
        db,
        tenant_id,
        page=(page - 1) * page_size,
        page_size=1,
        profile_id=profile_id,
        sort=sort,
        actual_only=actual_only,
        filters=filters,
    )
    return prev[0].profile_id if prev else None


def split_feed_by_urgency(
    items, *, window_days: int = FEED_URGENT_WINDOW_DAYS
) -> FeedGroups:
    """Splits an ALREADY obtained archive page into “burning” and the rest.

    A PURE function over the list from :func:`get_feed_page`: no SQL of its own, no
    second query, no re-sorting. The selection order (``sent_at DESC``) is kept
    INSIDE each group — the page shows exactly the same rows in the
    same order, just with a separator. That is precisely why the pagination stays
    honest: grouping cannot drag a row between pages.

    The urgency is computed by the same predicate that draws the badge
    (:func:`templating._days_left`), not by its own arithmetic — otherwise the section
    “Deadline soon” and the badge color would diverge (the project rules: one predicate per
    meaning). An expired deadline does NOT get into “burning”: one can no longer apply,
    and the emphasis on it is a false alarm.

    The import of ``templating`` is lazy: its module level creates
    ``Jinja2Templates`` (reading the template directory), while ``feed_service``
    is also imported by the workers, which do not need templates.
    """
    from src.api.templating import _days_left

    urgent: list[FeedItem] = []
    rest: list[FeedItem] = []
    for item in items:
        left = _days_left(getattr(item, "deadline_at", None))
        (urgent if left is not None and 0 <= left <= window_days else rest).append(item)
    return FeedGroups(urgent=urgent, rest=rest)


async def count_feed_total(
    db,
    tenant_id: str,
    *,
    profile_id: str | None = None,
    actual_only: bool = True,
    filters=None,
) -> int:
    """The total matches of the tenant under the current filter (for pagination).

    The mirror of the feed row set (``_FEED_COUNT_SQL``): the same JOIN/WHERE — both
    predicates (``_CURRENT_ONLY_PREDICATE``, ``_PROFILE_FILTER_PREDICATE``) and the
    dedup by (tender_id, profile_id). ``profile_id``/``actual_only`` must match
    what went into :func:`get_feed_page` — otherwise ``total`` would count
    one set, and the pages would return another. Requires repo.set_tenant (RLS) as the first
    action.
    """
    await repo.set_tenant(db, tenant_id)
    extra_where, extra_params = feed_filter_sql(filters)
    total = (
        await db.execute(
            _feed_count_sql(extra_where) if extra_where else _FEED_COUNT_SQL,
            {
                "tid": str(tenant_id),
                "profile_id": profile_id or None,
                "actual_only": actual_only,
                **extra_params,
            },
        )
    ).scalar_one()
    return total or 0


def _is_query_canceled(exc: DBAPIError) -> bool:
    orig = getattr(exc, "orig", None)
    return (
        getattr(orig, "sqlstate", None) == _QUERY_CANCELED
        or type(orig).__name__ == "QueryCanceledError"
    )


async def get_feed_listing(
    db, tenant_id: str, f, *, page: int = 1, page_size: int = FEED_PAGE_SIZE
) -> tuple[list[FeedItem], int, bool]:
    """A feed page + the counter under ``statement_timeout`` → ``(items, total, timed_out)``.

    ``f`` — ``PortalFilters`` (the profile/actuality/sorting are taken from it).
    ``set_tenant`` — as the FIRST action (RLS). A timeout (SQLSTATE 57014) is not a 500: the
    transaction after it is aborted, therefore a rollback, and the caller must set the tenant
    again before the next query (the rollback resets it too). The count
    is skipped when the first page is incomplete: then total = len(items).
    We do not write PII (the query text) into the log — only the exception type.
    """
    await repo.set_tenant(db, tenant_id)
    profile_id = f.profile_id or None
    try:
        await db.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(FEED_STATEMENT_TIMEOUT_MS)},
        )
        items = await get_feed_page(
            db,
            tenant_id,
            page=page,
            page_size=page_size,
            profile_id=profile_id,
            sort=f.sort,
            actual_only=f.actual,
            filters=f,
        )
        if page == 1 and len(items) < page_size:
            total = len(items)
        else:
            total = await count_feed_total(
                db, tenant_id, profile_id=profile_id, actual_only=f.actual, filters=f
            )
    except DBAPIError as exc:
        if not _is_query_canceled(exc):
            raise
        logger.warning("feed query timeout: %s", type(exc.orig or exc).__name__)
        await db.rollback()
        return [], 0, True
    return items, total, False


async def get_region_hidden_notice(
    db, tenant_id: str, profiles
) -> RegionHiddenNotice | None:
    """≈ how many suitable tenders are hidden from the tenant by the district filter (the banner).

    A subscriber with a chosen district silently does not see the tenders whose district is not
    resolved (the delivery predicate ``STAGE_A_SQL`` cuts them off). We count “hidden”
    ONLY for active profiles with a non-empty ``regions`` — by REUSING
    :func:`preview_service.preview_count` (``hidden_by_region``), the same mirror
    predicate ``PREVIEW_WHERE_A``, not a new one. The aggregate is the sum over such profiles
    (on prod usually 1 profile/tenant → an exact number; with several — an honest
    estimate from above, therefore “≈” in the text). The link leads to the first profile with a district.

    Returns ``None`` when the banner need not be shown: no active profile has set
    a district filter, or nothing is hidden (``hidden == 0``).

    Performance invariant: for profiles WITHOUT a district filter
    ``preview_count`` is NOT called at all (not a single SQL). For a profile with a district —
    the same 2 queries (the 30-day window) as the form’s preview; with N profiles with
    a district — 2·N queries per page load. RLS: ``set_tenant`` first.
    """
    await repo.set_tenant(db, tenant_id)
    scoped = [
        p
        for p in profiles
        if getattr(p, "is_active", None) and (getattr(p, "regions", None) or [])
    ]
    if not scoped:
        return None
    total_hidden = 0
    for p in scoped:
        payload = preview_service.payload_from_profile(p)
        result = await preview_service.preview_count(db, payload)
        total_hidden += result.hidden_by_region or 0
    if total_hidden <= 0:
        return None
    return RegionHiddenNotice(hidden=total_hidden, profile_id=str(scoped[0].id))


async def get_calendar_deadlines(db, tenant_id: str) -> list[HotDeadline]:
    """Deadlines of delivered tenders on the cockpit calendar horizon.

    THIS IS THE SAME predicate as "hot deadlines" (:func:`get_hot_deadlines`),
    only with the calendar window instead of the weekly one: the calendar and the "requires
    action" block must show one set of tenders, only at a different
    depth. A separate SQL here would split them — a tender would land in one block and
    disappear from the other.

    The :data:`CALENDAR_DEADLINE_LIMIT` applies ONLY to the 8–30 day tail:
    the nearest :data:`URGENT_HORIZON_DAYS` days are returned in full. Otherwise a slice of the 50
    nearest gave the "Upcoming deadlines" tile 50 instead of 519 (#810). RLS:
    ``set_tenant`` inside (as the first action).
    """
    rows = await get_hot_deadlines(
        db, tenant_id, limit=None, window_days=CALENDAR_HORIZON_DAYS
    )
    near = [d for d in rows if d.days_left <= URGENT_HORIZON_DAYS]
    far = [d for d in rows if d.days_left > URGENT_HORIZON_DAYS]
    return near + far[:CALENDAR_DEADLINE_LIMIT]


async def count_urgent_deadlines(db, tenant_id: str, my_tenders) -> int:
    """Count of "upcoming deadlines" (≤ :data:`URGENT_HORIZON_DAYS` days) for the tile.

    Matches in meaning ``len(urgent_events(...))``: the "my" part (working stages
    with the nearest deadline) — from the already-read funnel; the delivered part — COUNT
    with the same predicate without a limit; tenders of working stages are excluded from it,
    as in :func:`build_calendar_events` (one tender — one event). RLS:
    ``set_tenant`` as the first action.
    """
    await repo.set_tenant(db, tenant_id)
    work_ids = [str(row.tender_id) for row in my_tenders if row.status in WORK_STATUSES]
    action_n = len(
        urgent_events(build_calendar_events(my_tenders=my_tenders), URGENT_HORIZON_DAYS)
    )
    delivered_n = (
        await db.execute(
            _URGENT_COUNT_SQL,
            {
                "tid": str(tenant_id),
                "window_days": URGENT_HORIZON_DAYS,
                "skip": work_ids,
            },
        )
    ).scalar_one()
    return action_n + delivered_n


async def get_hot_deadlines(
    db, tenant_id: str, limit: int | None = 5, window_days: int = 7
) -> list[HotDeadline]:
    """Tenders delivered to the tenant with a deadline ≤ ``window_days`` days, deduplicated by tender.

    ``limit=None`` — no limit (``LIMIT NULL`` in PG). Requires repo.set_tenant +
    an explicit WHERE p.tenant_id (double isolation).
    """
    await repo.set_tenant(db, tenant_id)
    rows = (
        await db.execute(
            _HOT_DEADLINES_SQL,
            {"tid": str(tenant_id), "limit": limit, "window_days": window_days},
        )
    ).all()
    now = datetime.now(timezone.utc)
    result = []
    for row in rows:
        deadline = row.deadline_at
        days_left = max(0, calendar_days_until(deadline, now))
        result.append(
            HotDeadline(
                tender_id=row.tender_id,
                title=row.title or "Без названия",
                customer_name=row.customer_name,
                deadline_at=deadline,
                tender_url=row.tender_url,
                days_left=days_left,
                profile_id=getattr(row, "profile_id", None),
                source_key=getattr(row, "source_key", None),
            )
        )
    return result


# --------------------------------------------------------------------------- #
# The dashboard cockpit: the “in work” summary + the calendar of deadlines and events
#
# Everything below is PURE functions over the data already read (delivery deadlines,
# the “my tenders” funnel, the subscription end date). No SQL of its own:
# the sources are read by the existing services under RLS, and the cockpit only lays
# them out by dates. Therefore it is tested without a DB and does not multiply queries on the dashboard.
# --------------------------------------------------------------------------- #

# How many deliveries the dashboard teaser shows (the full feed — on /app/feed).
DASHBOARD_TEASER_LIMIT = 5

# Teaser modes (?teaser=): a whitelist, the first is the default.
TEASER_MODES = ("new", "deadline")


def normalize_teaser_mode(value: str | None) -> str:
    """An unknown/empty value of ``?teaser=`` → the default ``new``."""
    return value if value in TEASER_MODES else TEASER_MODES[0]


# The calendar horizon: the agenda and the dots on the month grid are taken from one window.
CALENDAR_HORIZON_DAYS = 30
CALENDAR_DEADLINE_LIMIT = 50
# "Upcoming deadlines" (tile, ROKOVI agenda for 7 days): calendar days.
URGENT_HORIZON_DAYS = 7

# How many agenda items are visible WITHOUT expanding. A limit of the DISPLAY, not of the selection:
# events from the 9th onward remain in the markup (and in the link with the month grid), but
# lie under “Show N more”.
#
# Measurement 04.08.2026 on the live prod (a tenant with a stream, 51 events in the agenda and 50 in
# “do now”): an agenda row takes 138 px on desktop 1280×900 and 277 px
# on mobile 390×844 (the tender title wraps onto 2–3 lines). Hence the
# number: 8 rows — is ~1.1 thousand px on desktop (as much as the month grid
# next to it takes, the columns do not drift apart) and ~2.2 thousand px on mobile.
# 10 rows would already give 2.8 thousand px on mobile — three screens for the agenda alone.
AGENDA_PREVIEW_LIMIT = 8

# “In work” = the funnel stages where the subscriber still does something: studies,
# takes part, has applied. The terminal (won/lost/skipped) and the default new are
# not work. The list is derived FROM ``STATUSES`` (see the guard in the tests): a stage
# added to the model — the test catches the divergence, not the prod.
WORK_STATUSES: tuple[str, ...] = tuple(
    s for s in STATUSES if s in ("watching", "active", "submitted")
)

EVENT_DEADLINE = "deadline"
EVENT_ACTION = "action"
EVENT_SUBSCRIPTION = "subscription"

_EVENT_ICONS = {
    EVENT_DEADLINE: "⏰",
    EVENT_ACTION: "📝",
    EVENT_SUBSCRIPTION: "🔔",
}


def _tender_url(tender_id, profile_id) -> str | None:
    """The internal link to the tender card (there are the translation, AI and the external link).

    ‼️ Without ``profile_id`` we do NOT build the internal link: the card route without it
    returns 404 (incident 25.07 — 100 agenda rows led nowhere). We return
    ``None``, and the caller falls back to the former external address: going to the portal is worse
    than staying in the product, but incomparably better than a 404.
    """
    if not tender_id or not profile_id:
        return None
    return f"/app/tenders/{tender_id}?profile_id={profile_id}"


@dataclass(frozen=True)
class CalendarEvent:
    """One event of the cockpit calendar.

    ``kind`` — ``deadline`` (the deadline of a delivered tender), ``action``
    (the tender in the subscriber’s work) or ``subscription`` (the access expires).

    ``title``/``subtitle`` — ready CONTENT (the tender name, the authority): we do not localize it
    (the owner’s decision “as is”). The chrome labels of the event —
    ``title_key``/``subtitle_key``: the service hands out the catalog KEY, and the template
    assembles the string by locale (the two i18n contours — the project rules). Thus on the English
    cockpit there is no Russian “Subscription expires” and no Russian funnel statuses.
    """

    kind: str
    title: str
    at: datetime
    days_left: int
    # The identifiers are needed by the agenda row for the translation toggle (the htmx fragment
    # /app/tenders/{id}/title). Without them the button is not drawn — a subscription event,
    # for example, has nothing to translate.
    tender_id: str | None = None
    profile_id: str | None = None
    # ‼️ The link leads INTO the product — to the tender card, where there are the translation
    # toggle, the AI artifacts and the “open on the portal” button. Previously
    # source_url stood here, and a click from “My tasks”/the calendar led to eNabavke: there was
    # no translation in the dashboard AT ALL and nowhere to reach it (owner defect 25.07).
    url: str | None = None
    #: The source of the deadline — we show the hour only if the source provides it.
    source_key: str | None = None
    subtitle: str | None = None
    title_key: str | None = None
    subtitle_key: str | None = None

    @property
    def icon(self) -> str:
        return _EVENT_ICONS.get(self.kind, "•")

    @property
    def day(self) -> date:
        return self.at.date()


@dataclass(frozen=True)
class CalendarDay:
    """A cell of the month grid."""

    day: date
    in_month: bool
    is_today: bool
    kinds: tuple[str, ...] = ()
    count: int = 0

    @property
    def has_events(self) -> bool:
        return self.count > 0


@dataclass(frozen=True)
class CalendarMonth:
    """The grid of the current month with day markers where there are events.

    ``month``/``year`` — numbers: the month name and the day-of-week labels are assembled by the presentation layer per locale from the
    catalog ``cabinet.i18n`` (keys ``mon_1..mon_12``/``wd_1..wd_7``), not by the service — otherwise the English
    cockpit would keep the Russian days and month.
    """

    month: int
    year: int
    weeks: list[list[CalendarDay]] = field(default_factory=list)


@dataclass(frozen=True)
class WorkBucket:
    """A funnel stage + how many tenders are at it (the “in work” breakdown)."""

    status: str
    label: str
    count: int


@dataclass(frozen=True)
class WorkSummary:
    """How many tenders the subscriber has in work, broken down by stages."""

    total: int
    buckets: list[WorkBucket] = field(default_factory=list)


def _as_utc(moment: datetime) -> datetime:
    """A naive datetime is treated as UTC (as ``templating._days_left``)."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def _days_until(moment: datetime, now: datetime) -> int:
    """Calendar days (Belgrade time) until ``moment``, as for the burning deadlines and the
    card (:func:`src.domain.tz.calendar_days_until`); the past → ``0``."""
    return max(0, calendar_days_until(_as_utc(moment), now))


def build_work_summary(my_tenders) -> WorkSummary:
    """The summary “in work N” by the tenant’s funnel (``tender_user_status``).

    Counts ONLY the stages of :data:`WORK_STATUSES`; the breakdown order is the funnel order. Empty
    stages do not get into the breakdown, but ``total`` is the sum over all the
    work stages, not over the shown ones (otherwise the number would diverge from the chips).
    """
    counts: dict[str, int] = {}
    for row in my_tenders:
        status = getattr(row, "status", "new")
        if status in WORK_STATUSES:
            counts[status] = counts.get(status, 0) + 1
    buckets = [
        WorkBucket(status=s, label=STATUS_LABELS[s], count=counts[s])
        for s in WORK_STATUSES
        if counts.get(s)
    ]
    return WorkSummary(total=sum(counts.values()), buckets=buckets)


def build_calendar_events(
    *,
    deadlines=(),
    my_tenders=(),
    access_until: datetime | None = None,
    now: datetime | None = None,
    horizon_days: int = CALENDAR_HORIZON_DAYS,
) -> list[CalendarEvent]:
    """Collects the calendar events from the already read sources, by date.

    Three sources:
    - ``my_tenders`` — the “my tenders” funnel (stages :data:`WORK_STATUSES`)
      → ``action``: on such a tender the subscriber is already doing something;
    - ``deadlines`` — the deadlines of delivered tenders → ``deadline``;
    - ``access_until`` — the end of the subscription → ``subscription``, and ONLY when the
      period is finite: a non-expiring access has no event (``None`` = unlimited,
      see :class:`stats_service.AccessDays`).

    One tender = one event: if it is in work, ``action`` wins — otherwise the
    subscriber would see the same tender in the calendar twice. Only the future within
    ``horizon_days`` gets into the window.
    """
    now = now or datetime.now(timezone.utc)
    events: list[CalendarEvent] = []
    seen_tenders: set[str] = set()

    def _in_window(moment: datetime) -> bool:
        # Window — calendar days in Belgrade time (as SQL ``belgrade_days_within_sql``
        # and badges), not 24-hour days: otherwise in the morning a deadline "day+N, 23:00"
        # fell out of the N-day window.
        moment = _as_utc(moment)
        return moment > now and calendar_days_until(moment, now) <= horizon_days

    for row in my_tenders:
        deadline = getattr(row, "deadline_at", None)
        if getattr(row, "status", "new") not in WORK_STATUSES or deadline is None:
            continue
        if not _in_window(deadline):
            continue
        seen_tenders.add(str(getattr(row, "tender_id", "")))
        events.append(
            CalendarEvent(
                kind=EVENT_ACTION,
                title=getattr(row, "title", None) or "Без названия",
                tender_id=str(getattr(row, "tender_id", "") or "") or None,
                profile_id=str(getattr(row, "profile_id", "") or "") or None,
                at=_as_utc(deadline),
                days_left=_days_until(deadline, now),
                source_key=getattr(row, "source_key", None),
                url=(
                    _tender_url(
                        getattr(row, "tender_id", None),
                        getattr(row, "profile_id", None),
                    )
                    or getattr(row, "tender_url", None)
                    or "/app/my"
                ),
                # The funnel stage — common vocabulary bot↔cabinet: we hand out the COMMON KEY
                # (``status_<...>``), the locale is substituted by the template, not by the service.
                subtitle_key="status_" + getattr(row, "status", "new"),
            )
        )

    for item in deadlines:
        if str(getattr(item, "tender_id", "")) in seen_tenders:
            continue
        deadline = getattr(item, "deadline_at", None)
        if deadline is None or not _in_window(deadline):
            continue
        events.append(
            CalendarEvent(
                kind=EVENT_DEADLINE,
                title=getattr(item, "title", None) or "Без названия",
                tender_id=str(getattr(item, "tender_id", "") or "") or None,
                profile_id=str(getattr(item, "profile_id", "") or "") or None,
                at=_as_utc(deadline),
                days_left=_days_until(deadline, now),
                source_key=getattr(item, "source_key", None),
                url=(
                    _tender_url(
                        getattr(item, "tender_id", None),
                        getattr(item, "profile_id", None),
                    )
                    or getattr(item, "tender_url", None)
                ),
                subtitle=getattr(item, "customer_name", None),
            )
        )

    if access_until is not None and _in_window(access_until):
        events.append(
            CalendarEvent(
                kind=EVENT_SUBSCRIPTION,
                # The chrome of the event — catalog keys (the heading and the caption), the string
                # is assembled by the template per locale. ``title`` is empty: there is no content here.
                title="",
                at=_as_utc(access_until),
                days_left=_days_until(access_until, now),
                url="/cabinet/account#pretplata",
                title_key="cal_sub_title",
                subtitle_key="cal_sub_hint",
            )
        )

    events.sort(key=lambda e: (e.at, e.title))
    return events


def urgent_events(events, window_days: int = 7) -> list[CalendarEvent]:
    """Events of the block “do now”: the nearest ``window_days`` days.

    This is a DERIVATIVE of the calendar, not a second selection: the block "needs
    action" and the calendar must talk about the same tenders — if the
    selections diverge, a burning deadline would be in one block and vanish from the other
    (the project rules: one predicate per meaning). The end of the subscription does not get here —
    it has its own emphasis in the hero strip, and it is not a “tender action”.
    """
    return [
        e
        for e in events
        if e.kind in (EVENT_ACTION, EVENT_DEADLINE) and e.days_left <= window_days
    ]


def build_calendar_month(events=(), now: datetime | None = None) -> CalendarMonth:
    """The grid of the current month with day markers where there are events.

    The week starts on Monday; the days of neighbouring months in the grid are marked
    ``in_month=False`` (drawn muted, not as empty holes). Events outside the month give no
    dots — they are visible in the agenda, whose horizon is wider.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    kinds_by_day: dict[date, list[str]] = {}
    for event in events:
        kinds_by_day.setdefault(event.day, []).append(event.kind)
    weeks = [
        [
            CalendarDay(
                day=day,
                in_month=(day.month == today.month and day.year == today.year),
                is_today=(day == today),
                # dict.fromkeys — unique types IN THE ORDER of appearance: there are as many dots
                # on a day as there are DIFFERENT event types, not events.
                kinds=tuple(dict.fromkeys(kinds_by_day.get(day, ()))),
                count=len(kinds_by_day.get(day, ())),
            )
            for day in week
        ]
        for week in _calendar.Calendar(firstweekday=0).monthdatescalendar(
            today.year, today.month
        )
    ]
    return CalendarMonth(month=today.month, year=today.year, weeks=weeks)
