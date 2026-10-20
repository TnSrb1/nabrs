"""The service of the tender status funnel for the Telegram bot (a port of v6 tracker_bot).

Writes the per-tenant status of the tender into ``tender_user_status`` (migration 0008): by
``chat_id`` Telegram resolves ``tenant_id``, makes an UPSERT of the status. The funnel of 7
values — the constant ``STATUSES`` of the model :mod:`src.db.models.tender_status`.

RLS: the table is under the DIRECT policy ``tenant_isolation`` (``tenant_id =
current_setting('app.current_tenant_id')::uuid``, like ``profiles``). Before the
write we set ``app.current_tenant_id`` with the transaction-level
``set_config(..., is_local=true)`` — the same contract as the cabinet
(:func:`src.api.repo.set_tenant`). ``set_config`` and the UPSERT go in ONE
transaction (one ``session`` until ``commit``), therefore the policy lets the write through
even under a restricted role.

The functions take an ``AsyncSession`` — they are tested on a mock session (ADR-013), without
the network and a real DB.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from src.api import repo
from src.db.models.tender_status import STATUSES
from src.db.session import set_current_tenant
from src.domain.access import is_frozen
from src.domain.tiers import effective_tier
from src.plans.catalog import tier_to_plan_key

# Bootstrap: resolve tenant_id via the SECURITY DEFINER function (bypasses RLS before
# the GUC is set). The function was created by migration 0021.
_RESOLVE_TENANT_SQL = text("SELECT app_resolve_tenant_id(:eid, 'telegram')")

# UPSERT of the status: one row per pair (tenant_id, tender_id) — the UNIQUE index
# ``tender_user_status_tenant_tender_uniq`` (migration 0008). On a repeat
# we update the status and the timestamp (as v6 handle_callback does).
_UPSERT_SQL = text("""
    INSERT INTO tender_user_status (tenant_id, tender_id, status)
    VALUES (:tenant_id, :tender_id, :status)
    ON CONFLICT (tenant_id, tender_id)
    DO UPDATE SET status = excluded.status, updated_at = now()
    """)

# The current status of the funnel (no row → there was no status).
_SELECT_STATUS_SQL = text("""
    SELECT status FROM tender_user_status
    WHERE tenant_id = :tenant_id AND tender_id = :tender_id
    ORDER BY updated_at DESC
    LIMIT 1
    """)

_DELETE_STATUS_SQL = text("""
    DELETE FROM tender_user_status
    WHERE tenant_id = :tenant_id AND tender_id = :tender_id
    """)

# A light SELECT of access_until for the freeze gate of the funnel buttons (FIX-SG-04).
# The same column and the same parameter type (str tenant_id) as in
# ``deep_analysis_service._LOAD_TENANT_TIER_SQL`` — a proven pattern.
_LOAD_ACCESS_SQL = text("""
    SELECT access_until FROM tenants WHERE id = :tenant_id LIMIT 1
    """)

# The plan + the access term for the /status command (SITE-28). The same column/pattern as
# ``deep_analysis_service._LOAD_TENANT_TIER_SQL`` — a verified SELECT over
# ``tenants``. No write is made.
# ‼️ The account email is taken by the same query, not a second one: /status must
# say WHOSE account this is (owner, 06.09.2026) — one chat_id may be bound to
# several accounts, and without the email the subscriber does not know whose status they see.
# ORDER BY created_at — a deterministic choice of the tenant’s owner (LIMIT 1 without
# ORDER BY gives a random row).
_LOAD_TIER_ACCESS_SQL = text("""
    SELECT t.tier,
           t.status,
           t.access_until,
           (SELECT u.email
              FROM users u
             WHERE u.tenant_id = t.id
               AND u.deleted_at IS NULL
               AND u.email IS NOT NULL
               AND u.email <> ''
             ORDER BY u.created_at
             LIMIT 1) AS email
    FROM tenants t
    WHERE t.id = :tenant_id
    LIMIT 1
    """)

# The human-readable names of the plans for /status (SITE-28).
_TIER_LABELS: dict[str, str] = {
    "free": "Free",
    "trial": "Триал",
    "pro": "Pro",
    "business": "Business",
    "enterprise": "Enterprise",
}


async def resolve_tenant_id(session, chat_id) -> str | None:
    """The ``tenant_id`` by the Telegram ``chat_id`` via the SECURITY DEFINER function.

    Calls ``app_resolve_tenant_id`` (migration 0021), which runs with superuser rights →
    bypasses FORCE RLS on ``users`` before the GUC is set. On success it immediately sets
    ``app.current_tenant_id`` transactionally (SET LOCAL), so that all subsequent accesses to the tenant tables
    in the same session pass the RLS.

    Returns ``None`` if there is no binding (the user did not do /start).
    """
    result = await session.execute(_RESOLVE_TENANT_SQL, {"eid": str(chat_id)})
    tenant_id = result.scalar()
    if tenant_id is None:
        return None
    tid_str = str(tenant_id)
    await set_current_tenant(session, tid_str)
    return tid_str


async def is_tenant_frozen(session, tenant_id) -> bool:
    """Whether the tenant is frozen (access expired) — the gate of the funnel buttons ``st:`` (FIX-SG-04).

    A light SELECT of ``access_until`` by ``tenant_id``; the decision — through the SSOT predicate
    :func:`src.domain.access.is_frozen`. No write is made (no ``commit``).
    ``None`` ``access_until`` or the absence of a row → NOT frozen (unlimited
    access): we issue a refusal only when the term is set and expired.
    """
    row = (
        await session.execute(_LOAD_ACCESS_SQL, {"tenant_id": str(tenant_id)})
    ).first()
    if row is None:
        return False
    return is_frozen(row.access_until, datetime.now(timezone.utc))


async def get_status_summary(session, tenant_id) -> dict:
    """The subscription summary of the tenant for the /status command (SITE-28).

    One light SELECT ``tier, access_until`` by ``tenant_id`` (no write is made).
    The freeze/activity — through the SSOT predicate :func:`src.domain.access.is_frozen`
    (the ``access_until`` axis; the same boundary as for the funnel and the deep analysis).

    Returns a dict:
      - ``tier`` (str): the tier code from the DB (``free`` if the row/value is absent);
      - ``status`` (str | None): the tenant’s status (``trial`` → the label “Trial”);
      - ``tier_label`` (str): the name by the EFFECTIVE tier (not by the raw ``tier``);
      - ``access_until`` (datetime | None): the access term (``None`` — unlimited);
      - ``frozen`` (bool): the access is set and expired (``<= now``);
      - ``days_left`` (int | None): whole days until the end of the access (``None`` —
        unlimited; ``0`` — it expires in less than a day or has already expired);
      - ``email`` (str | None): the address of the tenant’s account (whom the access belongs to).
    """
    now = datetime.now(timezone.utc)
    row = (
        await session.execute(_LOAD_TIER_ACCESS_SQL, {"tenant_id": str(tenant_id)})
    ).first()
    tier = str(row.tier) if row is not None and row.tier else "free"
    status = str(row.status) if row is not None and row.status else None
    # ‼️ The label — by the EFFECTIVE tier (as the site and the gates): on trial tier='lite'
    # is not shown, trial — “Trial” (trial = the rights of Pro, ARCHITECTURE §4).
    is_trial = (status or "").strip().lower() == "trial"
    tier_label = (
        _TIER_LABELS["trial"]
        if is_trial
        else _TIER_LABELS.get(tier_to_plan_key(effective_tier(status, tier)), tier)
    )
    access_until = getattr(row, "access_until", None) if row is not None else None
    frozen = is_frozen(access_until, now)
    days_left: int | None = None
    if access_until is not None:
        remaining = access_until - now
        days_left = remaining.days if remaining.days > 0 else 0
    return {
        "tier": tier,
        "status": status,
        "tier_label": tier_label,
        "access_until": access_until,
        "frozen": frozen,
        "days_left": days_left,
        "email": getattr(row, "email", None) if row is not None else None,
    }


async def set_tender_status(
    session, *, tenant_id: str, tender_id: str, status: str
) -> None:
    """UPSERT of the funnel status. Before the write — SET LOCAL ``app.current_tenant_id``.

    ``repo.set_tenant`` (``set_config(..., is_local=true)``) and the UPSERT go in one
    transaction (one ``session`` until ``commit``), therefore the direct RLS policy
    ``tenant_isolation`` lets the write through.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown funnel status: {status!r}")
    await repo.set_tenant(session, tenant_id)  # SET LOCAL app.current_tenant_id (RLS)
    await session.execute(
        _UPSERT_SQL,
        {"tenant_id": str(tenant_id), "tender_id": str(tender_id), "status": status},
    )
    await session.commit()


async def get_tender_status(session, *, tenant_id: str, tender_id: str) -> str | None:
    """The current funnel status of the tender for the tenant; ``None`` — there is no status (row).

    Reads under RLS (SET LOCAL ``app.current_tenant_id`` before the SELECT). No write is
    made.
    """
    await repo.set_tenant(session, tenant_id)
    row = (
        await session.execute(
            _SELECT_STATUS_SQL,
            {"tenant_id": str(tenant_id), "tender_id": str(tender_id)},
        )
    ).first()
    return str(row.status) if row is not None and row.status else None


async def clear_tender_status(session, *, tenant_id: str, tender_id: str) -> None:
    """Removes the funnel status (deletes the row) — returns the tender to “no status”."""
    await repo.set_tenant(session, tenant_id)
    await session.execute(
        _DELETE_STATUS_SQL,
        {"tenant_id": str(tenant_id), "tender_id": str(tender_id)},
    )
    await session.commit()
