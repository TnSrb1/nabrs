"""AI pipeline models (ADR-024).

DB_SCHEMA.md v1.1 §2.16 tender_ai_analyses, §2.17 ai_usage.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class TenderAiAnalysis(Base):
    """Cached AI analysis (DB_SCHEMA §2.16).

    ``model`` stores the concrete Claude slug at call time (ADR-029).
    UNIQUE (tender_id, profile_id, level) -> cache reuse."""

    __tablename__ = "tender_ai_analyses"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tender_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenders_normalized.id")
    )
    profile_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id")
    )
    level: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    output: Mapped[dict | None] = mapped_column(JSONB)
    tokens_input: Mapped[int | None] = mapped_column(Integer)
    tokens_output: Mapped[int | None] = mapped_column(Integer)
    tokens_cached: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tender_id",
            "profile_id",
            "level",
            name="tender_ai_analyses_tender_profile_level_uniq",
        ),
    )


class AiUsage(Base):
    """Per-tenant per-month per-level AI accounting (DB_SCHEMA §2.17).

    DB_SCHEMA §2.17 lists no explicit PK; the natural grain is
    (tenant_id, period, level) -> used as composite PK (noted in PR body)."""

    __tablename__ = "ai_usage"

    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    period: Mapped[str] = mapped_column(CHAR(7))  # YYYY-MM
    level: Mapped[str] = mapped_column(Text)
    count: Mapped[int | None] = mapped_column(Integer)
    tokens_total: Mapped[int | None] = mapped_column(BigInteger)
    cost_usd_total: Mapped[float | None] = mapped_column(Numeric)

    __table_args__ = (PrimaryKeyConstraint("tenant_id", "period", "level"),)
