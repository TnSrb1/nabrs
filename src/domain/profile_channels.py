"""Channel attached AFTER the template was created — to templates without channels.

Onboarding in ``/app`` leads "template → channel": a new client has no live channel yet, and the template is saved with ``delivery_channels=[]`` (the form prefill takes only channels that are live at creation time). Matching skips such a profile for delivery (``write_outbox``: only a ``web_only`` row for the feed) — tenders are not sent to Telegram/e-mail, even after a channel is connected.

Therefore a successful channel binding (Telegram — :func:`src.bots.telegram.bind_channel_by_token`, email — :func:`src.api.cabinet.channel_service.confirm_email`) calls ONE this function: the channel is appended to the tenant's templates that have no channels at all. A non-empty ``delivery_channels`` is not touched — it is the client's choice ("email only").

‼️ Lite — email only (:func:`src.api.tiers.gate_profile`, §3): Telegram is not appended to a Lite template, otherwise the setting would diverge from the form's gate.

The caller must set the tenant context (``set_current_tenant``/``repo.set_tenant``) BEFORE the call: ``profiles``/``tenants`` under role ``tendersrb_app`` are isolated by FORCE RLS; the commit is the caller's (one transaction with the binding itself).
"""

from __future__ import annotations

from sqlalchemy import text

from src.domain.tiers import effective_tier

_TENANT_TIER_SQL = text("""
    SELECT status, tier FROM tenants WHERE id = CAST(:tenant_id AS uuid)
    """)

# Deleted templates are not touched: neither the customer nor matching sees them.
_ADOPT_SQL = text("""
    UPDATE profiles
       SET delivery_channels = ARRAY[CAST(:channel AS text)],
           updated_at = now()
     WHERE tenant_id = CAST(:tenant_id AS uuid)
       AND deleted_at IS NULL
       AND COALESCE(cardinality(delivery_channels), 0) = 0
    RETURNING id
    """)


async def adopt_channel_into_empty_profiles(session, *, tenant_id, channel: str) -> int:
    """Appends ``channel`` to all of the tenant’s templates with an empty ``delivery_channels``.

    Returns the number of updated templates (0 — nothing to fix or Lite + non-email).
    """
    row = (
        await session.execute(_TENANT_TIER_SQL, {"tenant_id": str(tenant_id)})
    ).first()
    if row is None:
        return 0
    if channel != "email" and effective_tier(row.status, row.tier) == "lite":
        return 0
    result = await session.execute(
        _ADOPT_SQL, {"tenant_id": str(tenant_id), "channel": channel}
    )
    return len(result.fetchall())
