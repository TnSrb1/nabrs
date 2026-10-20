"""The journal of the incidents “the delivery channel dropped” (migration 0031).

``channel_dead_notice_log`` — the idempotency of the notification worker
(:mod:`src.workers.channel_dead_notify`): one row per ONE incident, that is,
per the continuous period while the tenant’s channel is not alive.

The incident is open while ``resolved_at IS NULL``. The partial UNIQUE index
``channel_dead_notice_log_open_incident_uniq`` on ``(tenant_id, channel) WHERE
resolved_at IS NULL`` keeps at most one open incident per pair: the worker
does claim-then-send through ``INSERT ... ON CONFLICT ... DO NOTHING RETURNING
id`` and sends the email only on a non-zero RETURNING. The channel has recovered → the worker
sets ``resolved_at`` → the next drop opens a NEW incident and
the email goes out again.

The partial index is declared in migration 0031 (``postgresql_where``), and not here:
the model describes the shape of the table, the metadata is not used for DDL
(``create_all`` is not called in production).

tenant-scoped with a **direct** column ``tenant_id`` → the direct RLS policy
``tenant_isolation`` (like ``reminder_log``/``subscription_reminder_log``),
set by migration 0031. The worker writes privileged (the owner bypasses RLS);
the policy — for the consistency of the schema.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class ChannelDeadNoticeLog(Base):
    """An incident “the channel is not alive”, for which an email was sent to the subscriber."""

    __tablename__ = "channel_dead_notice_log"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    notified_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
