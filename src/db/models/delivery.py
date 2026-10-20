"""Delivery outbox models (outbox pattern, ADR-025).

DB_SCHEMA.md v1.1 §2.14 delivery_outbox, §2.15 delivery_attempts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class DeliveryOutbox(Base):
    """Delivery queue, status machine (DB_SCHEMA §2.14).

    UNIQUE (profile_id, tender_id, channel) -> §3 / migration."""

    __tablename__ = "delivery_outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    profile_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id")
    )
    tender_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenders_normalized.id")
    )
    channel: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    requires_ai: Mapped[bool | None] = mapped_column()
    ai_analysis_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tender_ai_analyses.id")
    )
    match_kind: Mapped[str | None] = mapped_column(Text)
    match_score: Mapped[float | None] = mapped_column(Numeric)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class DeliveryAttempt(Base):
    """Per-attempt delivery log (DB_SCHEMA §2.15)."""

    __tablename__ = "delivery_attempts"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    outbox_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("delivery_outbox.id")
    )
    attempted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    response_code: Mapped[int | None] = mapped_column(Integer)
    error_text: Mapped[str | None] = mapped_column(Text)
