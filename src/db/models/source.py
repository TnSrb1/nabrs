"""Tender source runtime config.

DB_SCHEMA.md v1.1 §2.8 sources.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR, Integer, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class Source(Base):
    """Runtime config of a tender platform (DB_SCHEMA §2.8)."""

    __tablename__ = "sources"

    source_key: Mapped[str] = mapped_column(Text, primary_key=True)
    country_codes: Mapped[list[str] | None] = mapped_column(
        ARRAY(CHAR(2)), server_default=text("ARRAY['RS']::CHAR(2)[]")
    )
    display_name: Mapped[str | None] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool | None] = mapped_column()
    polling_interval_sec: Mapped[int | None] = mapped_column(Integer)
    last_run_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_status: Mapped[str | None] = mapped_column(Text)
    error_count: Mapped[int | None] = mapped_column(Integer)
    available_for_tiers: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # 0045 (SRC-0a): the moment of switching on — the cursor of the first pass of the ingest
    # (without the backfill of the archive); the moment of the end of the first (quiet) pass — tenders
    # seen before it, are not sent out. The contract — src/domain/sources.py.
    enabled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    baseline_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
