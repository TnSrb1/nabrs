"""Dashboard statistics of the cabinet (Track A).

Three blocks:
- :func:`get_system_stats` — global tender counters (tenders_normalized, no tenant filter, owner role, set_tenant is NOT called).
- :func:`get_tenant_delivery_stats` — how many tenders were delivered to the tenant (delivery_outbox status='sent'), broken down by profiles including soft-deleted ones.
- :func:`get_days_left` — how many days are left until the access ends.

All functions take an AsyncSession — they are tested on a mock session (ADR-013). Tenant queries: repo.set_tenant + an explicit WHERE tenant_id (double isolation, as in trial_service.py). Global counters — without set_tenant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import text

from src.api import repo
from src.domain.live_tender import (
    REGULAR_SINCE,
    live_tender_predicate,
    regular_tender_predicate,
)

# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #

# The counters are computed ONLY over live tenders published since the start of regular
# collection (owner 10.10 and 14.10.2026, ANALYTICS_PRODUCT R1). The predicate and the date are shared with
# the showcase /statistika — src/domain/live_tender.py. ``archive`` — as before, everything
# that is not “live” (the old portal).
_LIVE_TENDER = regular_tender_predicate()
_LIVE_ONLY = live_tender_predicate()

_SYSTEM_STATS_SQL = text(f"""
    SELECT
        COUNT(*) FILTER (WHERE {_LIVE_TENDER})                                    AS all_time,
        COUNT(*) FILTER (WHERE {_LIVE_TENDER}
                           AND first_seen_at >= date_trunc('month', now()))       AS this_month,
        COUNT(*) FILTER (WHERE {_LIVE_TENDER}
                           AND first_seen_at >= date_trunc('day', now()))         AS today,
        COUNT(*) - COUNT(*) FILTER (WHERE {_LIVE_ONLY})                           AS archive
    FROM tenders_normalized
""")

_TENANT_DELIVERY_SQL = text("""
    SELECT
        p.id::text                            AS profile_id,
        COALESCE(p.name, 'Без названия')      AS profile_name,
        COUNT(o.id)                           AS delivered
    FROM delivery_outbox o
    JOIN profiles p ON p.id = o.profile_id
    WHERE p.tenant_id = CAST(:tid AS uuid)
      AND o.status = 'sent'
    GROUP BY p.id, p.name
    ORDER BY delivered DESC
""")

_ACCESS_UNTIL_SQL = text("""
    SELECT access_until FROM tenants WHERE id = CAST(:tid AS uuid)
""")

# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SystemStats:
    """Global counters of LIVE tenders from the source.

    ``since`` — the start of regular collection (``REGULAR_SINCE``, Europe/Belgrade): “16 047 tenders since 02.06.2026”. ``None`` if there are no tenders in the population.
    """

    all_time: int
    this_month: int
    today: int
    since: date | None
    # Archive (old portal, no amounts) — a separate line (owner 10.10.2026).
    archive: int = 0


@dataclass(frozen=True)
class ProfileDelivery:
    """Deliveries for one profile."""

    profile_id: str
    profile_name: str
    count: int


@dataclass(frozen=True)
class TenantStats:
    """Deliveries to the tenant: total + broken down by profiles."""

    total: int
    by_profile: list[ProfileDelivery] = field(default_factory=list)


@dataclass(frozen=True)
class AccessDays:
    """Subscription term: a human-readable label, the number of days and the end date itself.

    ``until`` — the very ``tenants.access_until`` from which ``days`` are computed: the cockpit calendar shows the “subscription expires” event by DATE, and taking it in a second query would be a second axis for one fact (they would diverge — the badge would say one thing, the calendar another). ``None`` = unlimited, exactly as ``days``.
    """

    label: str
    days: int | None  # None = unlimited
    until: datetime | None = None


# --------------------------------------------------------------------------- #
# Service functions
# --------------------------------------------------------------------------- #


async def get_system_stats(db) -> SystemStats:
    """Global tender counters (owner role, without set_tenant).

    Counters across all tenants at once — the source’s public data.
    """
    row = (await db.execute(_SYSTEM_STATS_SQL)).first()
    if row is None:
        return SystemStats(0, 0, 0, None, 0)
    all_time = int(row.all_time or 0)
    since = REGULAR_SINCE if all_time else None
    return SystemStats(
        all_time=all_time,
        this_month=int(row.this_month or 0),
        today=int(row.today or 0),
        since=since,
        archive=int(getattr(row, "archive", 0) or 0),
    )


async def get_tenant_delivery_stats(db, tenant_id: str) -> TenantStats:
    """Deliveries to the tenant: in total and by profiles (including soft-deleted profiles).

    Requires repo.set_tenant + an explicit WHERE tenant_id (double isolation).
    """
    await repo.set_tenant(db, tenant_id)
    rows = (await db.execute(_TENANT_DELIVERY_SQL, {"tid": str(tenant_id)})).all()
    by_profile = [
        ProfileDelivery(
            profile_id=row.profile_id,
            profile_name=row.profile_name,
            count=int(row.delivered or 0),
        )
        for row in rows
    ]
    total = sum(p.count for p in by_profile)
    return TenantStats(total=total, by_profile=by_profile)


async def get_days_left(db, tenant_id: str) -> AccessDays:
    """Days until the subscription ends.

    NULL = unlimited → label='unlimited', days=None.
    <0 → label='access expired', days=<negative>.
    """
    await repo.set_tenant(db, tenant_id)
    row = (await db.execute(_ACCESS_UNTIL_SQL, {"tid": str(tenant_id)})).first()
    access_until = row.access_until if row is not None else None

    if access_until is None:
        return AccessDays(label="бессрочно", days=None)

    now = datetime.now(timezone.utc)
    delta = access_until - now
    days = math.ceil(delta.total_seconds() / 86400)

    if days <= 0:
        return AccessDays(label="доступ истёк", days=days, until=access_until)
    return AccessDays(label=f"{days} дн.", days=days, until=access_until)
