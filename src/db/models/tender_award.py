"""ORM model TenderAward — the award decisions of tenders (F03 eNabavke).

DB_SCHEMA.md §2.25 tender_awards (migration 0018).
Source: DocumentTypeId=11 (F03 regular) + 204 (F03 for framework/DSN).
Without RLS (the award is public data).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


class TenderAward(Base):
    """One record of an award decision for a tender (or for a lot of a tender)."""

    __tablename__ = "tender_awards"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tender_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders_normalized.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_uid: Mapped[str] = mapped_column(Text, nullable=False)
    document_type_id: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    lot_number: Mapped[str | None] = mapped_column(Text)
    lot_title: Mapped[str | None] = mapped_column(Text)

    winner_name: Mapped[str | None] = mapped_column(Text)
    winner_pib: Mapped[str | None] = mapped_column(Text)
    customer_pib: Mapped[str | None] = mapped_column(Text)
    customer_name: Mapped[str | None] = mapped_column(Text)

    decision_date: Mapped[date | None] = mapped_column(Date)
    award_amount_rsd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    award_amount_vat_rsd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    award_amount_eur: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency_id: Mapped[int | None] = mapped_column(SmallInteger)
    contract_date: Mapped[date | None] = mapped_column(Date)

    is_framework: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=func.cast(False, Boolean)
    )
    duration_months: Mapped[int | None] = mapped_column(Integer)
    has_changed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=func.cast(False, Boolean)
    )

    raw: Mapped[dict | None] = mapped_column(JSONB)
    captured_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    tender: Mapped["TenderNormalized"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "TenderNormalized",
        foreign_keys=[tender_id],
    )
