"""The single point of EXTENDING a tenant’s access by N days (migration 0041).

Moved into a neutral layer (like :mod:`src.domain.audit`) so that ONE path is
available both to the web (``api/admin/admin_service.approve_request``), and to billing
(``billing/service.activate_plan_manual``), and to the bot
(``bots/trial_service.decide``) — without a ``bot → api layer`` dependency and without
three copies of one meaning.

The perimeter: the callers go under the role ``tendersrb_app`` (FORCE RLS) and decide
the fate of SOMEONE ELSE’S tenant (the operator owner), so the write goes only via SECURITY
DEFINER ``app_admin_extend_access`` (0041). The function ITSELF computes the extension —
the base taken is the greater of the dates (the current ``access_until`` / ``now()``), see
:func:`src.domain.access.extend_until_sql`; there is no need to read the current term in Python
(and no means: someone else’s ``tenants`` is not visible under the app role).

It does NOT commit: the caller writes the decision/audit in THE SAME transaction and commits
once.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

_EXTEND_ACCESS_SQL = text(
    "SELECT app_admin_extend_access(CAST(:tid AS uuid), CAST(:days AS int))"
)


# Approval by the operator of a tenant stuck in pending_verification (a sign-up
# of medium/high risk): extending ``access_until`` is not enough — delivery is cut by the
# ``status`` axis (matching.STAGE_A_SQL: status IN ('trial','active')), and the Pro rights
# are given by effective_tier on status='trial'. Therefore in the same transaction the status
# becomes 'trial' with a term = access_until (as for a low-risk sign-up).
# Guarded: other statuses (active/deleted/…) are NOT touched; without a live term — neither.
_ACTIVATE_PENDING_SQL = text(
    "UPDATE tenants SET status = 'trial', trial_until = access_until, "
    "updated_at = now() "
    "WHERE id = CAST(:tid AS uuid) AND status = 'pending_verification' "
    "AND access_until > now()"
)


async def activate_pending_verification(db, *, tenant_id: str) -> bool:
    """``pending_verification`` → ``trial`` after a term is granted. True — the status was changed.

    Call AFTER extending ``access_until`` in the same transaction. The tenant GUC
    is set here (FORCE RLS on ``tenants``).
    """
    from src.db.session import set_current_tenant

    await set_current_tenant(db, str(tenant_id))
    res = await db.execute(_ACTIVATE_PENDING_SQL, {"tid": str(tenant_id)})
    return bool(getattr(res, "rowcount", 0))


async def extend_access(db, *, tenant_id: str, days: int) -> datetime:
    """Extend a tenant’s access by ``days`` days. Returns the NEW ``access_until``.

    An extension does not reduce access: if the current term is further than ``now()``,
    the count goes from it (the invariant of :func:`src.domain.access.extend_until`).

    ``LookupError`` — the tenant does not exist (the UPDATE touched no rows, the function
    returned NULL). It must not stay silent here: the caller would substitute ``None`` into the
    subscription term and the notification text, while access was in fact not granted.
    """
    until = (
        await db.execute(_EXTEND_ACCESS_SQL, {"tid": str(tenant_id), "days": days})
    ).scalar()
    if until is None:
        raise LookupError(f"extend_access: tenant {tenant_id} not found")
    # Approval by ANY path (bot, web, billing) lifts pending_verification: without
    # this the tenant is “approved”, has a term, but gets no delivery (matching: only trial/active).
    # Idempotent: WHERE status='pending_verification' — a repeat changes nothing.
    await activate_pending_verification(db, tenant_id=tenant_id)
    return until
