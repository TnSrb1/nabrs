"""Foundational reference tables (multi-country foundations, ADR-033).

DB_SCHEMA.md v1.1 §2.1 countries, §2.2 classification_systems,
§2.12 classifications, §2.13 regions.

These are global reference tables (not tenant-scoped, no RLS — see §4).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR, ForeignKey, PrimaryKeyConstraint, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class Country(Base):
    """ISO 3166-1 alpha-2 registry of supported countries (DB_SCHEMA §2.1).

    ``name`` — the human-readable name of the country (one value, in ru), the source
    for ``{{country_name}}`` in the AI prompt (ARCHITECTURE §14.3, ADR-024).
    The column was added by migration 0005 (Q-28 / CHAT18 F-08); the multilingual localization of the name — later,
    when a non-RU target_language appears.
    """

    __tablename__ = "countries"

    code: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    default_locale: Mapped[str] = mapped_column(Text, nullable=False)
    default_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    default_tz: Mapped[str] = mapped_column(Text, nullable=False)
    tax_id_required: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true")
    )
    tax_id_format: Mapped[str | None] = mapped_column(Text)
    kyc_provider_key: Mapped[str | None] = mapped_column(Text)
    regulator_url: Mapped[str | None] = mapped_column(Text)
    support_email: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class ClassificationSystem(Base):
    """Registry of procurement classification systems (DB_SCHEMA §2.2)."""

    __tablename__ = "classification_systems"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scope_country_codes: Mapped[list[str] | None] = mapped_column(ARRAY(CHAR(2)))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class Classification(Base):
    """Localized classification dictionary (DB_SCHEMA §2.12).

    Composite PK ``(system_code, code)``.
    """

    __tablename__ = "classifications"

    system_code: Mapped[str] = mapped_column(
        Text, ForeignKey("classification_systems.code"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    parent_code: Mapped[str | None] = mapped_column(Text)
    label_en: Mapped[str | None] = mapped_column(Text)
    label_sr_cyrl: Mapped[str | None] = mapped_column(Text)
    label_sr_latn: Mapped[str | None] = mapped_column(Text)
    label_ru: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (PrimaryKeyConstraint("system_code", "code"),)


class Region(Base):
    """ISO 3166-2 subdivision dictionary (DB_SCHEMA §2.13).

    ``level`` is free TEXT without a CHECK constraint; ``city`` /
    ``autonomous_province`` / ``okrug`` / ``opstina`` are all valid.
    """

    __tablename__ = "regions"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    country_code: Mapped[str] = mapped_column(
        CHAR(2),
        ForeignKey("countries.code"),
        nullable=False,
        server_default=text("'RS'"),
    )
    parent_code: Mapped[str | None] = mapped_column(Text)
    name_sr_cyrl: Mapped[str | None] = mapped_column(Text)
    name_sr_latn: Mapped[str | None] = mapped_column(Text)
    name_en: Mapped[str | None] = mapped_column(Text)
    name_ru: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str | None] = mapped_column(Text)
