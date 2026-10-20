"""The journal of sent daily summaries (migration 0010).

``summary_log`` — the idempotency of the daily summary worker
(:mod:`src.workers.summary`, a port of v6 ``daily_summary``): one row for
each sent summary for each tenant per calendar date (in the tz
of the tenant). UNIQUE ``(tenant_id, summary_date)`` guarantees "one summary a day
per tenant": the worker does claim-then-send through ``INSERT ... ON CONFLICT DO
NOTHING RETURNING id`` and sends only on a non-zero RETURNING.

``summary_date`` — the calendar date of the summary in the tenant's zone
(``COALESCE(tenants.tz, 'Europe/Belgrade')``), and not the server date in UTC.

tenant-scoped with a **direct** column ``tenant_id`` → the direct RLS policy
``tenant_isolation`` (like ``profiles``/``tender_user_status``/``reminder_log``,
DB_SCHEMA §4), set by migration 0010. The worker writes privileged (the owner
bypasses RLS); the policy — for the consistency of the schema.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class SummaryLog(Base):
    """A sent daily summary (one per pair tenant↔calendar date)."""

    __tablename__ = "summary_log"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    summary_date: Mapped[date] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "summary_date",
            name="summary_log_tenant_date_uniq",
        ),
    )
