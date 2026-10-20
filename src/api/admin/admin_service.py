"""Admin cabinet service (EPIC 6).

``api``/``bot`` connect as role ``tendersrb_app`` (non-superuser, FORCE RLS —
VG-01/0021). Cross-tenant admin access (the operator sees ALL tenants) is
implemented through SECURITY DEFINER functions ``app_admin_*`` (migration 0025) —
they are created by the superuser owner and bypass FORCE RLS, not through an
“owner connection” (outdated description; with VG-01 RLS is forced for api too).
Explicit cross-tenant access is allowed only here — through the require_admin gate.

Logging of admin actions — through the logger AND the immutable journal
``admin_audit_log`` (VG-29.4): privileged mutations (grant/revoke/set_tier)
write an audit row in the SAME transaction as the mutation itself, through the
SECURITY DEFINER ``app_admin_write_audit`` (0030). The journal is global (not tenant-scoped);
the app role has no direct access to the table — only through ``app_admin_*_audit``.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from src.billing.service import activate_plan_manual, enqueue_decision_notification
from src.db.session import set_current_tenant
from src.domain.access import access_active as _access_active
from src.domain.access import clamp_extension_days
from src.domain.access import extend_until
from src.domain.access_grants import activate_pending_verification, extend_access
from src.domain.audit import write_audit
from src.domain.tiers import TIER_ORDER
from src.i18n_email import email_t, norm_locale
from src.plans.catalog import parse_upgrade_plan

logger = logging.getLogger(__name__)

# Page size for /admin/users
PAGE_SIZE = 20

# Upper bound of the window when a search/filter is active (filtering in Python, see
# list_users). Protection against unbounded selection; when the cap is hit we log —
# a “silent” truncation would read as “everything was found”.
FILTER_SCAN_CAP = 5000

# Sorting of /admin/users: whitelist of columns. Default direction on the first
# click: dates and profiles — descending (newest/largest on top), the rest
# — ascending. Table default is created desc (SQL order, no window).
SORT_KEYS: tuple[str, ...] = (
    "email",
    "tenant",
    "tier",
    "access_until",
    "profiles",
    "created",
)
SORT_FIRST_DIR: dict[str, str] = {
    "email": "asc",
    "tenant": "asc",
    "tier": "asc",
    "access_until": "desc",
    "profiles": "desc",
    "created": "desc",
}
DEFAULT_SORT: tuple[str, str] = ("created", "desc")

# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #

# Cross-tenant queries go through SECURITY DEFINER functions app_admin_*
# (migration 0025) — they bypass FORCE RLS (VG-01/0021), bodies 1:1 with the former
# raw SQL. tendersrb_app has no direct cross-tenant access to
# users/tenants/profiles/delivery_outbox — only EXECUTE on these functions.

_USERS_LIST_PAGED_SQL = text(
    "SELECT * FROM app_admin_list_users(CAST(:limit AS int), CAST(:offset AS int))"
)

_USERS_TOTAL_SQL = text("SELECT app_admin_users_total()")

_USER_DETAIL_SQL = text("SELECT * FROM app_admin_get_user(CAST(:uid AS uuid))")

_PROFILES_LIST_SQL = text("SELECT * FROM app_admin_list_profiles()")

# User link channels (user_channels, FORCE RLS → SECURITY DEFINER only,
# migration 0057). Filter by user_id — one tenant can have different users.
_USER_CHANNELS_SQL = text("SELECT * FROM app_admin_user_channels(CAST(:uid AS uuid))")

_GRANT_ACCESS_SQL = text(
    "SELECT app_admin_grant_access(CAST(:tid AS uuid), CAST(:until AS timestamptz))"
)

_REVOKE_ACCESS_SQL = text("SELECT app_admin_revoke_access(CAST(:tid AS uuid))")

_SET_TIER_SQL = text(
    "SELECT app_admin_set_tier(CAST(:tid AS uuid), CAST(:tier AS text))"
)

# Disabling/restoring a subscriber IN FULL (0037). Difference from revoke_access: that one
# moves only ``access_until``, while the bot keeps answering on the linked
# channel and magic-link login stays open. Here all three axes are switched off
# (access + channels + ``users.deleted_at``); there is no physical deletion.
_DISABLE_SUBSCRIBER_SQL = text(
    "SELECT app_admin_disable_subscriber(CAST(:tid AS uuid))"
)

_ENABLE_SUBSCRIBER_SQL = text(
    "SELECT app_admin_enable_subscriber(CAST(:tid AS uuid), CAST(:days AS int))"
)

# Audit of privileged actions (VG-29.4). Table admin_audit_log —
# global (not tenant-scoped), app-role access only through SECURITY DEFINER
# app_admin_write/list/total_audit (0030). Writing goes through the shared src.domain.audit
# (write_audit), so that the bot and the web take ONE path (see the import above).
_LIST_AUDIT_SQL = text(
    "SELECT * FROM app_admin_list_audit(CAST(:limit AS int), CAST(:offset AS int))"
)

_AUDIT_TOTAL_SQL = text("SELECT app_admin_audit_total()")

# Access/subscription requests (trial_requests, FORCE RLS 0012/0032). The operator
# sees pending requests of ALL tenants — cross-tenant, therefore through SECURITY DEFINER
# (0034): list/total/decision. A direct SELECT under the app role does NOT see other
# tenants (RLS is not weakened). The decision is a guarded UPDATE status='pending' inside the
# function (repeat → 0 rows → idempotency, as in bot/trial_service.decide).
_TRIAL_REQUESTS_LIST_SQL = text(
    "SELECT * FROM app_admin_list_trial_requests("
    "CAST(:limit AS int), CAST(:offset AS int))"
)

_TRIAL_REQUESTS_TOTAL_SQL = text("SELECT app_admin_trial_requests_total()")

_TRIAL_DECIDE_SQL = text(
    "SELECT * FROM app_admin_decide_trial_request("
    "CAST(:rid AS uuid), CAST(:status AS text), CAST(:days AS int), "
    "CAST(:comment AS text))"
)

# Correction of a request journal before the extension cap (approve_request). Under FORCE RLS —
# only after set_current_tenant to the tenant of the decided request (policy tenant_isolation,
# UPDATE grant from 0021); in the same transaction as the decision.
_TRIAL_REQUEST_DAYS_SQL = text(
    "UPDATE trial_requests SET days = CAST(:days AS int) "
    "WHERE id = CAST(:rid AS uuid)"
)

# Decision on a re-registration request (0049): a separate SECURITY DEFINER —
# the shared ``app_admin_decide_trial_request`` deliberately does not decide such requests.
_REREGISTER_DECIDE_SQL = text(
    "SELECT tenant_id, lang, email FROM app_admin_decide_reregister("
    "CAST(:rid AS uuid), CAST(:approve AS boolean), CAST(:comment AS text))"
)

# Global tender statistics (tenders_normalized without RLS — shared data,
# FORCE RLS does not apply to it, no bypass needed).
_TENDER_STATS_SQL = text("""
    SELECT
        COUNT(*)                                                           AS total_all_time,
        COUNT(*) FILTER (WHERE first_seen_at >= date_trunc('month', NOW())) AS total_month,
        COUNT(*) FILTER (WHERE first_seen_at >= date_trunc('day',   NOW())) AS total_day
    FROM tenders_normalized
""")

# Number of tenders delivered to a specific tenant (including soft-deleted profiles).
_USER_DELIVERED_SQL = text("SELECT app_admin_user_delivered_count(CAST(:tid AS uuid))")

# Ingest health (VG-29.2). ONLY non-RLS tables: tenders_normalized and
# tender_awards — public data (RLS_MATRIX category U+0432), FORCE RLS does not
# apply to them, no cross-tenant bypass needed. Freshness of inflow = MAX of the
# ingest time; volume = COUNT over windows (pattern _TENDER_STATS_SQL, FILTER).
_INGEST_TENDERS_SQL = text("""
    SELECT
        MAX(first_seen_at)                                                    AS last_at,
        COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '1 hour')    AS cnt_1h,
        COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '24 hours')  AS cnt_24h,
        COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '7 days')    AS cnt_7d
    FROM tenders_normalized
""")

# tender_awards.created_at — the moment of ingest of the award record (server_default
# now(), see model TenderAward). A table without RLS (awards are public data).
_INGEST_AWARDS_SQL = text("""
    SELECT
        MAX(created_at)                                                    AS last_at,
        COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours')  AS cnt_24h,
        COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')    AS cnt_7d
    FROM tender_awards
""")

# Freshness thresholds of tender inflow are TIED TO THE SOURCE’S RHYTHM, not to flat
# hours (measurement 06.08 09:57 UTC, tenders_normalized.first_seen_at over 14 days):
# the eNabavke portal publishes new procurements in ONE nightly batch 22:00–00:59
# UTC (1591 of 1614 records in this window), during the day inflow is almost zero. A healthy
# ingest therefore gives a gap of up to ~24h EVERY day — the observed maximum over 30
# days is 24.3h (n=3556). A flat “6h” threshold would fire every day after ~06:00
# UTC on a completely healthy stream. The thresholds below measure "whether the expected
# nightly window was missed", not hours since midnight: urgent — one window missed (margin
# 3.7h over the observed maximum), error — two in a row missed.
_TENDER_MISSED_NIGHT_BATCH_URGENT_HOURS = 28
_TENDER_MISSED_NIGHT_BATCH_ERROR_HOURS = 52

# Delivery/queue health (VG-29.3). delivery_outbox (JOIN profiles.tenant_id,
# RLS_MATRIX category U+0431) and the reminder logs reminder_log /
# subscription_reminder_log (direct tenant_id, category U+0430) — ALL under FORCE RLS
# (0021). The operator looks across all tenants, so the cross-tenant bypass goes through
# SECURITY DEFINER functions app_admin_delivery_backlog /
# app_admin_reminder_freshness (migration 0026) — they return ONLY aggregates
# (COUNT/MAX), without per-row tenant data / PII. A direct SELECT from the service
# under tendersrb_app would fail closed (UndefinedObjectError without GUC).
_DELIVERY_BACKLOG_SQL = text("SELECT * FROM app_admin_delivery_backlog()")

_REMINDER_FRESHNESS_SQL = text("SELECT * FROM app_admin_reminder_freshness()")

# “Queue stuck” threshold (hours): there are unsent items (pending_total > 0), but
# the last successful delivery is older than the threshold (or there was none at all) → “error”.
# There are delivery failures in the last day (failed_24h > 0) → “urgent”. Otherwise → “success”.
_DELIVERY_STALE_ERROR_HOURS = 6


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #


@dataclass
class UserRow:
    id: str
    email: str | None
    created_at: datetime | None
    tenant_id: str
    tier: str | None
    access_until: datetime | None
    profile_count: int
    deleted_at: datetime | None = None

    @property
    def is_disabled(self) -> bool:
        """Fully disabled (0037: access + channels + login) — not the same as access_active."""
        return self.deleted_at is not None

    @property
    def access_active(self) -> bool:
        # SSOT predicate (FIX-SG-01): no behavior change.
        return _access_active(self.access_until, datetime.now(timezone.utc))

    @property
    def days_left(self) -> int | None:
        """Days until the subscription ends (ceiling).

        Returns None for unlimited access (access_until IS NULL).
        A negative value or 0 — if access has expired.
        """
        if self.access_until is None:
            return None
        delta = self.access_until - datetime.now(timezone.utc)
        return math.ceil(delta.total_seconds() / 86400)


@dataclass
class ProfileRow:
    id: str
    name: str | None
    tenant_id: str
    is_active: bool
    owner_email: str | None
    match_7d: int
    # Template connection date (profiles.created_at, UTC) and template delivery channels.
    # Empty delivery_channels = no deliveries for the template.
    created_at: datetime | None = None
    delivery_channels: list[str] = field(default_factory=list)


@dataclass
class UserChannelRow:
    """User link channel (user_channels) — for the subscriber card."""

    channel: str | None
    status: str | None
    linked_at: datetime | None


@dataclass
class PlatformStats:
    """Platform-wide statistics for the admin dashboard."""

    tenders_all_time: int
    tenders_month: int
    tenders_day: int
    users_total: int


@dataclass
class AuditRow:
    """One row of the admin journal (VG-29.4) for the /admin/audit page."""

    id: int
    at: datetime | None
    actor_email: str
    action: str
    target_tenant_id: str | None
    target_user_id: str | None
    details: dict[str, Any]
    ip: str | None


# Page size for /admin/audit
AUDIT_PAGE_SIZE = 50


@dataclass
class TrialRequestRow:
    """One pending request for the /admin/requests page (VG sub-approval).

    ``plan`` — public slug of the plan from ``comment='upgrade_request:<plan>'`` (None
    for an ordinary test access). ``days`` — previously requested/default period
    (usually None for pending). Cross-tenant list — through SECURITY DEFINER.
    """

    id: str
    tenant_id: str
    email: str | None
    plan: str | None
    requested_at: datetime | None
    days: int | None


# Page size for /admin/requests
REQUESTS_PAGE_SIZE = 20


def _humanize_age(dt: datetime | None) -> str:
    """“How long ago”: just now / N min / N h / N d ago."""
    if dt is None:
        return "—"
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:
        return "только что"
    if secs < 60:
        return "только что"
    if secs < 3600:
        return f"{int(secs // 60)} мин назад"
    if secs < 86400:
        return f"{int(secs // 3600)} ч назад"
    return f"{int(secs // 86400)} дн назад"


@dataclass
class IngestHealth:
    """Ingest stream health (VG-29.2): freshness and volume of inflow from the
    NON-RLS tables tenders_normalized / tender_awards. Read-only, no cross-tenant
    bypasses — the operator sees whether the tender/award stream is “alive” without SSH."""

    last_tender_at: datetime | None
    tenders_1h: int
    tenders_24h: int
    tenders_7d: int
    last_award_at: datetime | None
    awards_24h: int
    awards_7d: int
    # Inflow breakdown by ENABLED sources (SRC-0d). Empty — the former
    # behavior: one stream, “nightly batch” thresholds on the shared last_tender_at.
    sources: list[Any] = field(default_factory=list)

    @property
    def tender_age_hours(self) -> float | None:
        """Hours since the last tender arrived (None — no data at all)."""
        if self.last_tender_at is None:
            return None
        return max(
            0.0,
            (datetime.now(timezone.utc) - self.last_tender_at).total_seconds() / 3600,
        )

    @property
    def tender_status(self) -> str:
        """Tender inflow freshness status: 'success' | 'urgent' | 'error'.

        The threshold measures the SOURCE'S RHYTHM, not absolute hours (VG-29.2, measurement 06.08):
        the eNabavke portal publishes new procurements in one nightly batch 22:00–00:59 UTC
        (1591 of 1614 first_seen_at records over 14 days), during the day inflow is almost
        zero — a healthy ingest gives a gap of up to ~24h EVERY day (observed maximum over
        30 days — 24.3h, n=3556). Therefore: no new tenders for
        more than 28h (one nightly window missed, margin 3.7h over the maximum) →
        urgent; more than 52h (two windows in a row missed) or no data at all →
        error (matches the badges badge-success/urgent/error).

        SRC-0d: when broken down by source, the “nightly batch” thresholds apply only to
        sources with the adapter attribute ``night_batch`` (jnportal); the others are
        measured with their own silence threshold (``error`` if they are silent). The result — the worst
        of the statuses of the enabled sources."""
        if self.sources:
            worst = "success"
            for src in self.sources:
                status = src.status
                if status == "error":
                    return "error"
                if status == "urgent":
                    worst = "urgent"
            return worst
        hours = self.tender_age_hours
        if hours is None or hours > _TENDER_MISSED_NIGHT_BATCH_ERROR_HOURS:
            return "error"
        if hours > _TENDER_MISSED_NIGHT_BATCH_URGENT_HOURS:
            return "urgent"
        return "success"

    @property
    def tender_age_label(self) -> str:
        return _humanize_age(self.last_tender_at)

    @property
    def award_age_label(self) -> str:
        return _humanize_age(self.last_award_at)

    @property
    def is_empty(self) -> bool:
        """There is no ingest data at all (no tenders, no awards) — show the empty state."""
        return self.last_tender_at is None and self.last_award_at is None


@dataclass
class DeliveryHealth:
    """Delivery/queue health (VG-29.3): delivery_outbox backlog + freshness of the
    reminder workers. Cross-tenant aggregates go through SECURITY DEFINER
    (0026, FORCE RLS bypass) — the operator sees whether the delivery queue got stuck and
    whether the background processes are alive, without SSH. Only COUNT/MAX, no tenant rows."""

    pending_total: int
    failed_24h: int
    sent_1h: int
    sent_24h: int
    last_sent_at: datetime | None
    last_deadline_reminder_at: datetime | None
    last_subscription_reminder_at: datetime | None

    @property
    def delivery_age_hours(self) -> float | None:
        """Hours since the last successful delivery (None — there were no deliveries at all)."""
        if self.last_sent_at is None:
            return None
        return max(
            0.0,
            (datetime.now(timezone.utc) - self.last_sent_at).total_seconds() / 3600,
        )

    @property
    def backlog_status(self) -> str:
        """Delivery queue status: 'success' | 'urgent' | 'error'.

        Threshold (VG-29.3): there are unsent items (pending_total > 0), but the last
        successful delivery is older than _DELIVERY_STALE_ERROR_HOURS or there was none
        at all → 'error' (the queue is stuck). Otherwise there are failures
        in the last day (failed_24h > 0) → 'urgent'. Otherwise → 'success' (matches the
        badges badge-success/urgent/error)."""
        age = self.delivery_age_hours
        if self.pending_total > 0 and (
            age is None or age > _DELIVERY_STALE_ERROR_HOURS
        ):
            return "error"
        if self.failed_24h > 0:
            return "urgent"
        return "success"

    @property
    def delivery_age_label(self) -> str:
        return _humanize_age(self.last_sent_at)

    @property
    def deadline_reminder_label(self) -> str:
        return _humanize_age(self.last_deadline_reminder_at)

    @property
    def subscription_reminder_label(self) -> str:
        return _humanize_age(self.last_subscription_reminder_at)

    @property
    def is_empty(self) -> bool:
        """No deliveries, no queue, no reminders — show the empty state."""
        return (
            self.pending_total == 0
            and self.failed_24h == 0
            and self.last_sent_at is None
            and self.last_deadline_reminder_at is None
            and self.last_subscription_reminder_at is None
        )


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def _row_to_user(r) -> UserRow:
    return UserRow(
        id=str(r.id),
        email=r.email,
        created_at=r.created_at,
        tenant_id=str(r.tenant_id),
        tier=r.tier,
        access_until=r.access_until,
        profile_count=r.profile_count or 0,
        deleted_at=getattr(r, "deleted_at", None),
    )


def _user_matches(r, *, search: str, tier: str) -> bool:
    """Whether a row matches the search (substring by email/tenant) and the plan filter.

    ``search``/``tier`` are already normalized (strip+lower). An empty criterion does not
    narrow. The match mirrors the columns of the /admin/users table (email + tenant).
    """
    if tier and (r.tier or "").lower() != tier:
        return False
    if search:
        haystack = f"{r.email or ''} {r.tenant_id or ''}".lower()
        if search not in haystack:
            return False
    return True


def normalize_sort(sort: str | None, direction: str | None) -> tuple[str, str]:
    """Sort whitelist: garbage in ``sort`` → default ``created desc``.

    Garbage in ``direction`` with a valid column → its first direction (without 422).
    """
    key = (sort or "").strip().lower()
    if key not in SORT_KEYS:
        return DEFAULT_SORT
    d = (direction or "").strip().lower()
    if d not in ("asc", "desc"):
        d = SORT_FIRST_DIR[key]
    return key, d


def _sort_value(r, key: str):
    """Column value for sorting; ``None`` = NULL (at the end for either dir)."""
    if key == "email":
        return r.email.lower() if r.email else None
    if key == "tenant":
        return str(r.tenant_id) if r.tenant_id else None
    if key == "tier":
        t = (r.tier or "").lower()
        return (
            TIER_ORDER.index(t) if t in TIER_ORDER else None
        )  # order of plans, not alphabetical
    if key == "access_until":
        return r.access_until
    if key == "profiles":
        return r.profile_count or 0
    return r.created_at  # created


def _sort_rows(rows: list, key: str, direction: str) -> list:
    """Sorting in Python. NULL — always at the end (both for asc and desc).

    Stable: on equal values the input order is kept (created desc from SQL).
    """
    keyed = [(_sort_value(r, key), r) for r in rows]
    present = [(v, r) for v, r in keyed if v is not None]
    absent = [r for v, r in keyed if v is None]
    present.sort(key=lambda vr: vr[0], reverse=(direction == "desc"))
    return [r for _, r in present] + absent


async def list_users(
    db,
    *,
    page: int = 1,
    page_size: int = PAGE_SIZE,
    search: str | None = None,
    tier: str | None = None,
    sort: str | None = None,
    direction: str | None = None,
) -> tuple[list[UserRow], int]:
    """Paginated list of users with search/filter (read-only).

    Returns (list of users on the page, total count after the filter). Default order: ORDER BY created_at DESC; ``sort``/``direction``
    — whitelist ``normalize_sort``, NULL at the end for both directions.

    Search (email/tenant, substring) and the plan filter are applied in Python:
    SECURITY DEFINER ``app_admin_list_users(limit, offset)`` does not
    accept filters, and migrations are not touched in this task. Without filters — the former
    path with pagination in SQL (cheap). With a filter — we fetch a window of rows (cap
    ``FILTER_SCAN_CAP``) and narrow/paginate in Python, so that the search covers all
    users, not only the current page.
    """
    page = max(1, page)
    search_norm = (search or "").strip().lower()
    tier_norm = (tier or "").strip().lower()
    if tier_norm and tier_norm not in TIER_ORDER:
        tier_norm = ""  # unknown plan in the query is ignored (without 422)
    sort_key, sort_dir = normalize_sort(sort, direction)

    # Without filters and without a non-default sort — pagination in SQL (former behavior).
    if not search_norm and not tier_norm and (sort_key, sort_dir) == DEFAULT_SORT:
        offset = (page - 1) * page_size
        rows = (
            await db.execute(
                _USERS_LIST_PAGED_SQL, {"limit": page_size, "offset": offset}
            )
        ).all()
        total: int = (await db.execute(_USERS_TOTAL_SQL)).scalar_one() or 0
        return [_row_to_user(r) for r in rows], total

    # With a filter — a row window + filtering/pagination in Python.
    scan = (
        await db.execute(_USERS_LIST_PAGED_SQL, {"limit": FILTER_SCAN_CAP, "offset": 0})
    ).all()
    if len(scan) >= FILTER_SCAN_CAP:
        logger.warning(
            "admin list_users: the filtered selection hit the cap=%d — search results "
            "may be incomplete",
            FILTER_SCAN_CAP,
        )
    matched = [r for r in scan if _user_matches(r, search=search_norm, tier=tier_norm)]
    matched = _sort_rows(matched, sort_key, sort_dir)
    filtered_total = len(matched)
    offset = (page - 1) * page_size
    page_rows = matched[offset : offset + page_size]
    return [_row_to_user(r) for r in page_rows], filtered_total


async def get_user(db, uid: str) -> UserRow | None:
    row = (await db.execute(_USER_DETAIL_SQL, {"uid": uid})).first()
    if row is None:
        return None
    return _row_to_user(row)


async def list_profiles(db, tenant_id: str | None = None) -> list[ProfileRow]:
    """Profiles of all tenants (SECURITY DEFINER) or of one — if ``tenant_id`` is set.

    Filtering in Python, not in a new SQL function: the same selection as on /admin/templates.
    """
    rows = (await db.execute(_PROFILES_LIST_SQL)).all()
    return [
        ProfileRow(
            id=str(r.id),
            name=r.name,
            tenant_id=str(r.tenant_id),
            is_active=bool(r.is_active),
            owner_email=r.owner_email,
            match_7d=r.match_7d or 0,
            created_at=r.created_at,
            delivery_channels=list(r.delivery_channels or []),
        )
        for r in rows
        if tenant_id is None or str(r.tenant_id) == str(tenant_id)
    ]


async def list_user_channels(db, uid: str) -> list[UserChannelRow]:
    """User link channels (SECURITY DEFINER app_admin_user_channels)."""
    rows = (await db.execute(_USER_CHANNELS_SQL, {"uid": uid})).all()
    return [
        UserChannelRow(channel=r.channel, status=r.status, linked_at=r.linked_at)
        for r in rows
    ]


async def get_platform_stats(db) -> PlatformStats:
    """Global statistics of tenders and users for the dashboard.

    tenders_normalized — without RLS (shared table); users_total — through
    SECURITY DEFINER app_admin_users_total (cross-tenant, FORCE RLS).
    """
    row = (await db.execute(_TENDER_STATS_SQL)).first()
    users_total: int = (await db.execute(_USERS_TOTAL_SQL)).scalar_one() or 0
    return PlatformStats(
        tenders_all_time=row.total_all_time or 0,
        tenders_month=row.total_month or 0,
        tenders_day=row.total_day or 0,
        users_total=users_total,
    )


def night_batch_status(hours: float | None) -> str:
    """Freshness status of a source with a nightly batch: thresholds 28h/52h (measurement 06.08)."""
    if hours is None or hours > _TENDER_MISSED_NIGHT_BATCH_ERROR_HOURS:
        return "error"
    if hours > _TENDER_MISSED_NIGHT_BATCH_URGENT_HOURS:
        return "urgent"
    return "success"


@dataclass
class SourceInflow:
    """Inflow of one enabled source for ``/admin/health``."""

    source_key: str
    label: str
    night_batch: bool
    silent: bool
    silent_hours: float | None
    last_tender_at: datetime | None
    tenders_24h: int
    tenders_7d: int

    @property
    def status(self) -> str:
        if self.night_batch:
            return night_batch_status(self.silent_hours)
        return "error" if self.silent else "success"

    @property
    def age_label(self) -> str:
        return _humanize_age(self.last_tender_at)


async def get_ingest_health(db) -> IngestHealth:
    """Freshness and volume of inflow from NON-RLS tables (VG-29.2).

    tenders_normalized / tender_awards — public (RLS_MATRIX category U+0432), FORCE
    RLS does not apply to them, no cross-tenant bypass needed. No
    app_admin_*/SECURITY DEFINER, no RLS tables (worker/queue health
    = a separate step 29.3)."""
    t = (await db.execute(_INGEST_TENDERS_SQL)).first()
    a = (await db.execute(_INGEST_AWARDS_SQL)).first()
    from src.api.admin import sources_admin

    inflow = [
        SourceInflow(
            source_key=r.source_key,
            label=r.label,
            night_batch=r.night_batch,
            silent=r.silent,
            silent_hours=r.silent_hours,
            last_tender_at=r.last_tender_at,
            tenders_24h=r.cnt_24h,
            tenders_7d=r.cnt_7d,
        )
        for r in await sources_admin.list_sources(db)
        if r.enabled
    ]
    return IngestHealth(
        last_tender_at=t.last_at if t else None,
        tenders_1h=(t.cnt_1h if t else 0) or 0,
        tenders_24h=(t.cnt_24h if t else 0) or 0,
        tenders_7d=(t.cnt_7d if t else 0) or 0,
        last_award_at=a.last_at if a else None,
        awards_24h=(a.cnt_24h if a else 0) or 0,
        awards_7d=(a.cnt_7d if a else 0) or 0,
        sources=inflow,
    )


async def get_delivery_health(db) -> DeliveryHealth:
    """Delivery queue backlog and freshness of the reminder workers (VG-29.3).

    delivery_outbox / reminder_log / subscription_reminder_log — ALL under FORCE
    RLS (0021). Cross-tenant aggregates are taken through SECURITY DEFINER functions
    app_admin_delivery_backlog / app_admin_reminder_freshness (0026) — they
    bypass FORCE RLS and return only COUNT/MAX (without tenant rows). A direct
    SELECT under tendersrb_app would fail closed."""
    b = (await db.execute(_DELIVERY_BACKLOG_SQL)).first()
    r = (await db.execute(_REMINDER_FRESHNESS_SQL)).first()
    return DeliveryHealth(
        pending_total=(b.pending_total if b else 0) or 0,
        failed_24h=(b.failed_24h if b else 0) or 0,
        sent_1h=(b.sent_1h if b else 0) or 0,
        sent_24h=(b.sent_24h if b else 0) or 0,
        last_sent_at=b.last_sent_at if b else None,
        last_deadline_reminder_at=r.last_deadline_reminder_at if r else None,
        last_subscription_reminder_at=r.last_subscription_reminder_at if r else None,
    )


async def get_user_delivered_count(db, tenant_id: str) -> int:
    """Number of tenders delivered to a tenant (delivery_outbox.status='sent').

    Includes deliveries through soft-deleted profiles — historical data is kept.
    """
    result = (await db.execute(_USER_DELIVERED_SQL, {"tid": tenant_id})).scalar_one()
    return result or 0


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


async def list_audit(
    db, *, page: int = 1, page_size: int = AUDIT_PAGE_SIZE
) -> tuple[list[AuditRow], int]:
    """Paginated admin journal (newest on top). (rows, total)."""
    page = max(1, page)
    offset = (page - 1) * page_size
    rows = (
        await db.execute(_LIST_AUDIT_SQL, {"limit": page_size, "offset": offset})
    ).all()
    total: int = (await db.execute(_AUDIT_TOTAL_SQL)).scalar_one() or 0
    return (
        [
            AuditRow(
                id=int(r.id),
                at=r.at,
                actor_email=r.actor_email,
                action=r.action,
                target_tenant_id=(
                    str(r.target_tenant_id) if r.target_tenant_id else None
                ),
                target_user_id=str(r.target_user_id) if r.target_user_id else None,
                details=_coerce_details(r.details),
                ip=r.ip,
            )
            for r in rows
        ],
        total,
    )


def _coerce_details(value: Any) -> dict[str, Any]:
    """jsonb from a raw text() SELECT is returned by asyncpg as str — we parse it into a dict.

    Defensively: if the driver has already returned a dict — as is; for any other form — {}.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def grant_access(
    db,
    *,
    admin_uid: str,
    tenant_id: str,
    days: int,
    before_until: datetime | None,
    actor_email: str | None = None,
    target_user: str | None = None,
    ip: str | None = None,
) -> None:
    """“Extend access” by ``days`` — an EXTENSION, not setting the period.

    The new period is computed by the SSOT helper :func:`src.domain.access.extend_until` from
    the LARGER of the dates (the current ``before_until`` / ``now``), so the button
    cannot silently cut paid access (incident 04.08.2026: −11 days with an
    active subscription). Shortening — only through an explicit ``revoke_access``.

    ``before_until`` is a REQUIRED argument (no default value): it is both the base of the extension and
    ``before_access_until`` in the audit. The caller takes it from the same tenant selection that draws the screen
    (``get_user`` → ``app_admin_get_user``); an argument with a default value would silently return
    the defect — forgetting to pass it would turn the extension back into a plain setting.
    """
    until = extend_until(before_until, days, datetime.now(timezone.utc))
    await db.execute(_GRANT_ACCESS_SQL, {"tid": tenant_id, "until": until})
    await activate_pending_verification(db, tenant_id=tenant_id)
    if actor_email is not None:
        await write_audit(
            db,
            actor=actor_email,
            action="grant_access",
            target_tenant=tenant_id,
            target_user=target_user,
            details={
                "days": days,
                "before_access_until": _iso(before_until),
                "after_access_until": until.isoformat(),
            },
            ip=ip,
        )
    await db.commit()
    logger.info(
        "admin_action=grant_access admin_uid=%s tenant_id=%s days=%d before=%s "
        "until=%s",
        admin_uid,
        tenant_id,
        days,
        _iso(before_until),
        until.isoformat(),
    )


async def revoke_access(
    db,
    *,
    admin_uid: str,
    tenant_id: str,
    actor_email: str | None = None,
    target_user: str | None = None,
    ip: str | None = None,
    before_until: datetime | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(_REVOKE_ACCESS_SQL, {"tid": tenant_id})
    if actor_email is not None:
        await write_audit(
            db,
            actor=actor_email,
            action="revoke_access",
            target_tenant=tenant_id,
            target_user=target_user,
            details={
                "before_access_until": _iso(before_until),
                "after_access_until": now.isoformat(),
            },
            ip=ip,
        )
    await db.commit()
    logger.info(
        "admin_action=revoke_access admin_uid=%s tenant_id=%s",
        admin_uid,
        tenant_id,
    )


async def set_tier(
    db,
    *,
    admin_uid: str,
    tenant_id: str,
    tier: str,
    actor_email: str | None = None,
    target_user: str | None = None,
    ip: str | None = None,
    before_tier: str | None = None,
) -> None:
    # Canon validation (R8) BEFORE the write: ck_tenants_tier (0027) would reject a non-canon
    # value with a raw DB exception at commit. We check here so that we give a clear
    # error. The router (routes_admin.admin_set_tier) validates the form separately —
    # this is defense-in-depth for direct calls to the service.
    if tier not in TIER_ORDER:
        raise ValueError(
            f"Недопустимый тариф {tier!r}: ожидается один из {', '.join(TIER_ORDER)}."
        )
    await db.execute(_SET_TIER_SQL, {"tid": tenant_id, "tier": tier})
    if actor_email is not None:
        await write_audit(
            db,
            actor=actor_email,
            action="set_tier",
            target_tenant=tenant_id,
            target_user=target_user,
            details={"before_tier": before_tier, "after_tier": tier},
            ip=ip,
        )
    await db.commit()
    logger.info(
        "admin_action=set_tier admin_uid=%s tenant_id=%s tier=%s",
        admin_uid,
        tenant_id,
        tier,
    )


async def disable_subscriber(
    db,
    *,
    admin_uid: str,
    tenant_id: str,
    actor_email: str | None = None,
    target_user: str | None = None,
    ip: str | None = None,
    before_until: datetime | None = None,
) -> None:
    """Disables a subscriber in full: access + channels + login (0037).

    Soft and reversible (:func:`enable_subscriber`): rows are not deleted, audit and
    history remain. Difference from :func:`revoke_access` — that one touches only
    ``tenants.access_until``, after which the bot still answers on the linked
    channel, and the subscriber gets a new magic-link and enters the cabinet.
    """
    await db.execute(_DISABLE_SUBSCRIBER_SQL, {"tid": tenant_id})
    if actor_email is not None:
        await write_audit(
            db,
            actor=actor_email,
            action="disable_subscriber",
            target_tenant=tenant_id,
            target_user=target_user,
            details={
                "before_access_until": _iso(before_until),
                "channels": "disabled",
                "users_deleted_at": "set",
            },
            ip=ip,
        )
    await db.commit()
    logger.info(
        "admin_action=disable_subscriber admin_uid=%s tenant_id=%s",
        admin_uid,
        tenant_id,
    )


async def enable_subscriber(
    db,
    *,
    admin_uid: str,
    tenant_id: str,
    days: int = 0,
    actor_email: str | None = None,
    target_user: str | None = None,
    ip: str | None = None,
    before_until: datetime | None = None,
) -> None:
    """Restores a disabled subscriber (0037): login, channels, and with ``days>0`` the period.

    ``days=0`` — restore login and channels without touching ``access_until``: the period is
    issued by the operator separately (:func:`grant_access`), otherwise one button would silently
    decide two different questions.
    """
    await db.execute(_ENABLE_SUBSCRIBER_SQL, {"tid": tenant_id, "days": days})
    if actor_email is not None:
        await write_audit(
            db,
            actor=actor_email,
            action="enable_subscriber",
            target_tenant=tenant_id,
            target_user=target_user,
            details={
                "before_access_until": _iso(before_until),
                "days": days,
                "channels": "restored",
                "users_deleted_at": "cleared",
            },
            ip=ip,
        )
    await db.commit()
    logger.info(
        "admin_action=enable_subscriber admin_uid=%s tenant_id=%s days=%d",
        admin_uid,
        tenant_id,
        days,
    )


# --------------------------------------------------------------------------- #
# Access/subscription requests (web list of pending + approve/reject)
# --------------------------------------------------------------------------- #


async def list_trial_requests(
    db, *, page: int = 1, page_size: int = REQUESTS_PAGE_SIZE
) -> tuple[list[TrialRequestRow], int]:
    """Paginated list of pending requests (newest at the bottom — ORDER BY age).

    Cross-tenant through SECURITY DEFINER app_admin_list_trial_requests (0034);
    trial_requests RLS is NOT weakened. (rows, total).
    """
    page = max(1, page)
    offset = (page - 1) * page_size
    rows = (
        await db.execute(
            _TRIAL_REQUESTS_LIST_SQL, {"limit": page_size, "offset": offset}
        )
    ).all()
    total: int = (await db.execute(_TRIAL_REQUESTS_TOTAL_SQL)).scalar_one() or 0
    return (
        [
            TrialRequestRow(
                id=str(r.id),
                tenant_id=str(r.tenant_id),
                email=r.email,
                plan=r.plan,
                requested_at=r.requested_at,
                days=r.days,
            )
            for r in rows
        ],
        total,
    )


async def _decide_reregister(
    db,
    *,
    request_id: str,
    approve: bool,
    comment: str | None,
    email_sender,
    settings,
    actor_email: str | None,
    ip: str | None,
) -> str | None:
    """Decision on a re-registration request; ``None`` — the request is not such.

    Approval lifts the address block (once, in the DB) and sends the person an email with a
    link to registration; rejection — a rejection email (as for an ordinary access request).
    Audit — without the address and without the IP: the journal is immutable. The address lives only in the request
    and is wiped by the function itself at the moment of the decision; the email goes out AFTER the commit.
    """
    row = (
        await db.execute(
            _REREGISTER_DECIDE_SQL,
            {"rid": request_id, "approve": approve, "comment": comment},
        )
    ).first()
    if row is None:
        return None
    await write_audit(
        db,
        actor=actor_email or "admin",
        action="approve_reregister" if approve else "reject_reregister",
        target_tenant=str(row.tenant_id),
        details={"request_id": request_id},
    )
    await db.commit()
    if email_sender is not None and settings is not None and row.email:
        lang = norm_locale(row.lang)
        if approve:
            subject = email_t(lang, "rereg_approved_subject")
            body = email_t(lang, "rereg_approved_body").format(
                link=settings.cabinet_base_url.rstrip("/") + "/signup"
            )
        else:
            subject = email_t(lang, "rereg_rejected_subject")
            body = email_t(lang, "rereg_rejected_body")
        if comment:
            body = f"{body}\n\n{comment}"
        try:
            await email_sender.send(to=row.email, subject=subject, text_body=body)
        except Exception as exc:  # noqa: BLE001 — the decision is already in the DB; no PII in the log
            logger.warning(
                "reregister: decision email not sent (%s)", type(exc).__name__
            )
    logger.info(
        "admin_action=%s_reregister tenant_id=%s",
        "approve" if approve else "reject",
        row.tenant_id,
    )
    return "reregister_approved" if approve else "reregister_rejected"


async def approve_request(
    db,
    *,
    admin_uid: str,
    request_id: str,
    days: int,
    comment: str | None = None,
    actor_email: str | None = None,
    ip: str | None = None,
    email_sender=None,
    settings=None,
) -> str:
    """Approve a request for ``days`` days (cross-tenant). Returns the outcome.

    A re-registration request (0049) is decided by a separate DB function and does not
    extend access (``days`` is ignored): outcome ``reregister_approved``.

    Idempotent: guarded UPDATE ``status='pending'`` inside SECURITY DEFINER
    (repeat → 0 rows → ``already``, no second grant). An upgrade request
    (``upgrade_request:<plan>``) is issued by the SINGLE billing path
    (:func:`activate_plan_manual`: ``manual`` subscription + tier from the catalog +
    access); an ordinary test access — only an extension of ``access_until`` (tier is
    not touched). The audit of the subscription decision — in the same transaction (0034 + 0030).

    ``comment`` — an optional operator comment (written to
    ``trial_requests.decision_comment``, shown to the subscriber in the email and the cabinet).

    Outcomes: ``approved`` / ``approved_subscription`` / ``reregister_approved`` /
    ``already``.
    """
    decided = (
        await db.execute(
            _TRIAL_DECIDE_SQL,
            {
                "rid": request_id,
                "status": "approved",
                "days": days,
                "comment": comment,
            },
        )
    ).first()
    if decided is None:
        # The shared DB function does not decide re-registration requests (0049) — it
        # goes to its own; if not found there either — it is already decided.
        rereg = await _decide_reregister(
            db,
            request_id=request_id,
            approve=True,
            comment=comment,
            email_sender=email_sender,
            settings=settings,
            actor_email=actor_email,
            ip=ip,
        )
        return rereg or "already"
    tenant_id = str(decided.tenant_id)
    plan = parse_upgrade_plan(decided.comment)
    # A free extension — no more than 30 days (owner 10.10), whatever the operator chose.
    # We know the cap only from the comment, and it comes FROM decide (RETURNING) — therefore
    # we fix the request journal right here, in this transaction: otherwise trial_requests.days
    # would show the operator’s decision (90), while 30 was granted to the client.
    capped = clamp_extension_days(decided.comment, days)
    if capped != days:
        await set_current_tenant(db, tenant_id)
        await db.execute(_TRIAL_REQUEST_DAYS_SQL, {"rid": request_id, "days": capped})
    days = capped

    # Reliable reply to the subscriber about the decision (email + cabinet), independent of Telegram.
    # A single enqueue helper (the bot’s decide calls the same one), in the SAME transaction —
    # idempotent by (provider, event_id): a repeated approve does not get here
    # (already), and a direct repeat of enqueue is absorbed by ON CONFLICT.
    await enqueue_decision_notification(
        db, request_id=request_id, tenant_id=tenant_id, approve=True
    )

    if plan is not None:
        act = await activate_plan_manual(
            db, tenant_id=tenant_id, plan_key=plan.key, days=days
        )
        if actor_email is not None:
            await write_audit(
                db,
                actor=actor_email,
                action="approve_subscription",
                target_tenant=tenant_id,
                details={
                    "request_id": request_id,
                    "plan": act.plan_key,
                    "tier": act.tier,
                    "days": days,
                    "until": act.until.isoformat(),
                    "provider": "manual",
                },
                ip=ip,
            )
        await db.commit()
        logger.info(
            "admin_action=approve_subscription admin_uid=%s tenant_id=%s "
            "plan=%s days=%d",
            admin_uid,
            tenant_id,
            act.plan_key,
            days,
        )
        return "approved_subscription"

    # An ordinary test access — only an EXTENSION of access (tier is not touched).
    # The base of the count is the larger of the dates (the current period / now), it is computed by
    # SECURITY DEFINER app_admin_extend_access (0041): under the role tendersrb_app
    # (FORCE RLS) there is nothing in Python to read another tenant’s tenants with — we only have
    # the tenant_id of the decided request, while 0025 returns the period only by user_id. Before 0041
    # here stood now() + days, and approving N days for a tenant with a longer
    # access silently cut its period. Invariant — src.domain.access.extend_until.
    until = await extend_access(db, tenant_id=tenant_id, days=days)
    await db.commit()
    logger.info(
        "admin_action=approve_trial admin_uid=%s tenant_id=%s days=%d until=%s",
        admin_uid,
        tenant_id,
        days,
        until.isoformat(),
    )
    return "approved"


async def reject_request(
    db,
    *,
    admin_uid: str,
    request_id: str,
    comment: str | None = None,
    actor_email: str | None = None,
    ip: str | None = None,
    email_sender=None,
    settings=None,
) -> str:
    """Reject a request (cross-tenant). Idempotent (repeat → ``already``).

    A subscription request (``upgrade_request:``) is written to the audit; access/plan are NOT
    touched. ``comment`` — an optional reason for rejection (written to
    ``decision_comment``, shown to the subscriber in the email and the cabinet). Outcomes:
    ``rejected`` / ``reregister_rejected`` / ``already``.
    """
    decided = (
        await db.execute(
            _TRIAL_DECIDE_SQL,
            {
                "rid": request_id,
                "status": "rejected",
                "days": None,
                "comment": comment,
            },
        )
    ).first()
    if decided is None:
        # The shared DB function does not decide re-registration requests (0049) — it
        # goes to its own; if not found there either — it is already decided.
        rereg = await _decide_reregister(
            db,
            request_id=request_id,
            approve=False,
            comment=comment,
            email_sender=email_sender,
            settings=settings,
            actor_email=actor_email,
            ip=ip,
        )
        return rereg or "already"
    tenant_id = str(decided.tenant_id)
    plan = parse_upgrade_plan(decided.comment)

    # Reliable reply to the subscriber about the rejection (email + cabinet), independent of Telegram.
    # A single enqueue helper, in the SAME transaction, idempotent by (provider,
    # event_id). The rejection comment will go into the email (see billing_notifications).
    await enqueue_decision_notification(
        db, request_id=request_id, tenant_id=tenant_id, approve=False
    )

    if plan is not None and actor_email is not None:
        await write_audit(
            db,
            actor=actor_email,
            action="reject_subscription",
            target_tenant=tenant_id,
            details={"request_id": request_id, "plan": plan.key},
            ip=ip,
        )
    await db.commit()
    logger.info(
        "admin_action=reject_request admin_uid=%s tenant_id=%s",
        admin_uid,
        tenant_id,
    )
    return "rejected"
