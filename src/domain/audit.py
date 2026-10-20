"""A neutral layer for writing to the immutable admin log (``admin_audit_log``).

Moved out of ``src.api.admin.admin_service`` so that ONE audit write path is
available both to the web (``/admin/*``), and to the bot (decisions on requests in
``src.bots.trial_service``) — without a ``bot→api layer`` dependency (the same technique
as ``src.domain.tiers``). Duplicating the audit write SQL in two modules
is not allowed (SSOT: one predicate per meaning).

The log is global (not tenant-scoped), the app role has no direct access to the table —
writes only via SECURITY DEFINER ``app_admin_write_audit`` (0030). The jsonb
details are serialized in Python (``json.dumps``) and passed as ``CAST(:details AS
jsonb)`` — a TypeDecorator does NOT apply to raw SQL. uuid/text NULL pass through
normally (``CAST(NULL AS uuid) = NULL``).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

_WRITE_AUDIT_SQL = text(
    "SELECT app_admin_write_audit("
    "CAST(:actor AS text), CAST(:action AS text), "
    "CAST(:target_tenant AS uuid), CAST(:target_user AS uuid), "
    "CAST(:details AS jsonb), CAST(:ip AS text))"
)


async def write_audit(
    db,
    *,
    actor: str,
    action: str,
    target_tenant: str | None = None,
    target_user: str | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Writing a row of the admin log via SECURITY DEFINER ``app_admin_write_audit``.

    It does NOT commit — the caller writes the audit in THE SAME transaction as the mutation and
    commits once (consistency of the audit with the change).
    """
    await db.execute(
        _WRITE_AUDIT_SQL,
        {
            "actor": actor,
            "action": action,
            "target_tenant": target_tenant,
            "target_user": target_user,
            "details": json.dumps(details or {}),
            "ip": ip,
        },
    )
