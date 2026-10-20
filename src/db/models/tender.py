"""Two-tier tender models + canonical clustering.

DB_SCHEMA.md v1.1 §2.9 tenders_raw, §2.10 tenders_normalized,
§2.11 canonical_tenders.

These are NOT tenant-scoped (public data, no RLS — see §4).
``country_code`` on raw/normalized is plain CHAR(2) (no FK to countries per
§2.9/§2.10 — INT tenders may carry codes outside the registry or NULL).
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    Boolean,
    ForeignKey,
    Numeric,
    SmallInteger,
    Text,
    TypeDecorator,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

# dict[str, Any] — see the explanation in tenant.py: without an annotation the type dict[str, object]
# does not fit any overload of mapped_column. The fix is typical, of the types, not of the values.
_UUID_PK: dict[str, Any] = dict(primary_key=True, server_default=func.gen_random_uuid())

# Money: numeric WITHOUT scale stores the binary float “tail” of a division
# (38461.54 → 38461.5400000000008731…). We quantize the bind parameter to Decimal to
# 2 digits — a clean 38461.54 lands in the DB (and in the bot reading from the DB). The DDL
# of the column stays NUMERIC → NO migration needed; the reading — as with Numeric (Decimal).
_MONEY_QUANT = Decimal("0.01")


class QuantizedNumeric(TypeDecorator):
    """Numeric, quantizing the written value to 2 digits (ROUND_HALF_UP).

    ``round(x, 2)`` on a float still gives a float tail; ``Decimal.quantize`` to
    ``0.01`` gives exactly 2 digits. None → None; accepts float/int/Decimal/str.
    The DDL of the column is NUMERIC (without scale), as it was → no ALTER migration is needed.
    The reading of the value does not change (as with Numeric — Decimal).
    """

    impl = Numeric
    cache_ok = True

    def process_bind_param(self, value: object, dialect: object) -> Decimal | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        return value.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


class TenderRaw(Base):
    """Verbatim platform payload, kept forever (DB_SCHEMA §2.9).

    UNIQUE (source_key, source_uid) -> via index in §3 / migration."""

    __tablename__ = "tenders_raw"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    country_code: Mapped[str | None] = mapped_column(CHAR(2))
    source_key: Mapped[str | None] = mapped_column(Text)
    source_uid: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    raw_hash: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class CanonicalTender(Base):
    """Cluster of cross-platform duplicates (DB_SCHEMA §2.11, ADR-022)."""

    __tablename__ = "canonical_tenders"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    cluster_key: Mapped[str | None] = mapped_column(Text)
    primary_tender_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        # circular FK <-> tenders_normalized; created via ALTER (use_alter)
        ForeignKey(
            "tenders_normalized.id",
            use_alter=True,
            name="canonical_tenders_primary_tender_id_fkey",
        ),
    )


class TenderNormalized(Base):
    """Canonicalized per-platform tender, main routing/AI table
    (DB_SCHEMA §2.10). UNIQUE (source_key, source_uid) -> §3 / migration."""

    __tablename__ = "tenders_normalized"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    country_code: Mapped[str | None] = mapped_column(CHAR(2))
    tenders_raw_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenders_raw.id")
    )
    canonical_tender_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("canonical_tenders.id")
    )
    # The verdict of the cross-platform link (0047, src/dedup/cross_source.py):
    # 'dup' | 'probable' | NULL (not linked) and the scores of the pair.
    canonical_confidence: Mapped[str | None] = mapped_column(Text)
    canonical_score: Mapped[int | None] = mapped_column(SmallInteger)
    cluster_key: Mapped[str | None] = mapped_column(CHAR(40))
    source_key: Mapped[str | None] = mapped_column(Text)
    source_uid: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)

    # Content
    title: Mapped[str | None] = mapped_column(Text)
    title_normalized: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    description_normalized: Mapped[str | None] = mapped_column(Text)
    description_embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))

    # Customer
    customer_name: Mapped[str | None] = mapped_column(Text)
    customer_tax_id: Mapped[str | None] = mapped_column(Text)
    customer_region_code: Mapped[str | None] = mapped_column(Text)

    # Finance — QuantizedNumeric: the write is quantized to 2 digits (cosmetics,
    # cures the float tail); the DDL stays NUMERIC (no migration needed).
    amount_original: Mapped[float | None] = mapped_column(QuantizedNumeric)
    currency_original: Mapped[str | None] = mapped_column(CHAR(3))
    amount_eur: Mapped[float | None] = mapped_column(QuantizedNumeric)
    is_amount_estimated: Mapped[bool | None] = mapped_column(Boolean)

    # Timing
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    opening_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Categorization
    classifications: Mapped[dict | None] = mapped_column(JSONB)
    procedure_type: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    regions: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    # Status
    status: Mapped[str | None] = mapped_column(Text)

    # Documents
    documents: Mapped[dict | None] = mapped_column(JSONB)

    # Audit
    first_seen_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
