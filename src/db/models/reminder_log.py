"""The journal of sent deadline reminders (migration 0009).

``reminder_log`` — the idempotency of the reminder worker
(:mod:`src.workers.reminders`, a port of v6 ``reminders_loop``): one row for
each sent threshold for each pair (tenant, tender). UNIQUE
``(tenant_id, tender_id, threshold_days)`` guarantees “one threshold — once”:
the worker does claim-then-send through ``INSERT ... ON CONFLICT DO NOTHING
RETURNING id`` and sends only on a non-zero RETURNING.

The thresholds ``threshold_days`` — 5/3/1/0 days before ``tenders_normalized.deadline_at``.

tenant-scoped with a **direct** column ``tenant_id`` → the direct RLS policy
``tenant_isolation`` (like ``profiles``/``tender_user_status``, DB_SCHEMA §4),
set by migration 0009. The worker writes privileged (the owner bypasses RLS);
the policy — for the consistency of the schema.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class ReminderLog(Base):
    """A sent reminder about the deadline (one threshold per pair tenant↔tender)."""

    __tablename__ = "reminder_log"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    tender_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders_normalized.id", ondelete="CASCADE"),
        nullable=False,
    )
    threshold_days: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "tender_id",
            "threshold_days",
            name="reminder_log_tenant_tender_threshold_uniq",
        ),
    )
