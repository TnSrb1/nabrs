"""The user’s funnel of tender statuses within a tenant (migration 0008).

``tender_user_status`` — the status of a tender in the funnel of a SPECIFIC tenant (the per-tenant
mark of the subscriber), in contrast to ``tenders_normalized.status`` (the status of the tender
itself from the source: active/awarded/cancelled/closed). One status per pair
(tenant_id, tender_id) — the UNIQUE index ``tender_user_status_tenant_tender_uniq``
(created by migration 0008). The foundation for the buttons/summary/reminders; the handlers and
the workers — in separate PRs.

The funnel of 7 values (a port of v6 ``STATUS_LABELS``):
    new       — 🆕 New (the default, before the user’s action)
    watching  — 👀 Studying
    active    — ✅ Participating
    submitted — 📨 Bid submitted
    won       — 🏆 Won
    lost      — 😔 Lost
    skipped   — ❌ Skipped

tenant-scoped with a **direct** column ``tenant_id`` → the direct RLS policy
``tenant_isolation`` (like ``profiles``, DB_SCHEMA §4), set by migration 0008.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

# The funnel of statuses (a port of v6 STATUS_LABELS); the order = the order of the funnel.
# The source of truth for the CHECK of migration 0008 and for the UI labels.
STATUSES: tuple[str, ...] = (
    "new",
    "watching",
    "active",
    "submitted",
    "won",
    "lost",
    "skipped",
)

# The stage labels for the UI. They live next to STATUSES: the bot (STATUS_LABELS in
# src/bots/telegram.py) and the web cabinet show ONE funnel, and the labels must
# match — otherwise the same row of tender_user_status is named differently in two
# places. The copy in the bot is guarded by tests/test_status_funnel_parity.py.
STATUS_LABELS: dict[str, str] = {
    "new": "🆕 Новый",
    "watching": "👀 Изучаю",
    "active": "✅ Участвую",
    "submitted": "📨 Заявка подана",
    "won": "🏆 Выиграли",
    "lost": "😔 Проиграли",
    "skipped": "❌ Пропущен",
}


class TenderUserStatus(Base):
    """The status of the tender in the tenant’s funnel, the default ``new`` (migration 0008)."""

    __tablename__ = "tender_user_status"

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
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'new'")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('new','watching','active','submitted','won','lost','skipped')",
            name="tender_user_status_status_check",
        ),
    )
