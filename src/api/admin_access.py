"""Single source of truth about who is a platform operator.

The admin gate (:func:`src.api.deps.require_admin`) and the flag for templates (the “Admin” link in the
cabinet header) must answer from the same allowlist, otherwise the link and the access would diverge.
Therefore parsing ``CABINET_ADMIN_EMAILS`` and the membership check live here, and are not
duplicated across the callers.

``users.role='admin'`` is a tenant-level role (the first user of EACH tenant receives it at
signup), so it is not suitable as a sign of an operator. A platform operator =
membership of the email in ``CABINET_ADMIN_EMAILS`` (ENV, CSV). An empty
allowlist → fail-closed: there are no operators at all.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from sqlalchemy import text

from src.api import security
from src.api.config import CabinetSettings
from src.db.session import set_current_tenant

_ADMIN_USER_SQL = text(
    "SELECT email FROM users WHERE id = CAST(:uid AS uuid) AND deleted_at IS NULL"
)


def parse_allowlist(settings: CabinetSettings) -> set[str]:
    """Normalized operator emails from ``CABINET_ADMIN_EMAILS`` (CSV)."""
    return {
        security.normalize_email(e)
        for e in settings.platform_admin_emails.split(",")
        if e.strip()
    }


async def is_platform_admin(
    session: Optional[dict],
    session_factory: Callable,
    settings: CabinetSettings,
) -> bool:
    """True — the session belongs to a platform operator from the allowlist.

    The session carries only uid/tid, so the email is resolved from the DB under the GUC of its
    tenant (ADR-002: ``set_tenant`` as the first action in the transaction).
    """
    if session is None:
        return False
    allowlist = parse_allowlist(settings)
    if not allowlist:
        return False
    async with session_factory() as db:
        await set_current_tenant(db, session["tid"])
        result = await db.execute(_ADMIN_USER_SQL, {"uid": session["uid"]})
        row = result.first()
    email = getattr(row, "email", None) if row is not None else None
    return email is not None and security.normalize_email(email) in allowlist
