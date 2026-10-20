"""foundational seed (RS country, classification systems, top-level regions)

Implements docs/DB_SCHEMA.md v1.1 §2.1 (RS), §2.2 (eu_cpv, unspsc) and the
3 top-level ISO 3166-2:RS regions (Track B brief STEP 3). Okruzi RS-01..RS-29
and opstine are deferred to Sprint-14.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-28

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    countries = sa.table(
        "countries",
        sa.column("code", sa.CHAR(2)),
        sa.column("default_locale", sa.Text),
        sa.column("default_currency", sa.CHAR(3)),
        sa.column("default_tz", sa.Text),
        sa.column("tax_id_required", sa.Boolean),
        sa.column("tax_id_format", sa.Text),
        sa.column("kyc_provider_key", sa.Text),
        sa.column("regulator_url", sa.Text),
        sa.column("support_email", sa.Text),
    )
    op.bulk_insert(countries, [
        {
            "code": "RS",
            "default_locale": "sr-Latn",
            "default_currency": "EUR",
            "default_tz": "Europe/Belgrade",
            "tax_id_required": True,
            "tax_id_format": r"^\d{9}$",
            "kyc_provider_key": "apr_rs",
            "regulator_url": "https://www.poverenik.rs/",
            "support_email": "support@nab.rs",
        },
    ])

    classification_systems = sa.table(
        "classification_systems",
        sa.column("code", sa.Text),
        sa.column("name", sa.Text),
        sa.column("scope_country_codes", postgresql.ARRAY(sa.CHAR(2))),
    )
    op.bulk_insert(classification_systems, [
        {"code": "eu_cpv",
         "name": "EU Common Procurement Vocabulary",
         "scope_country_codes": None},
        {"code": "unspsc",
         "name": "UN Standard Products and Services Code",
         "scope_country_codes": None},
    ])

    regions = sa.table(
        "regions",
        sa.column("code", sa.Text),
        sa.column("country_code", sa.CHAR(2)),
        sa.column("parent_code", sa.Text),
        sa.column("level", sa.Text),
        sa.column("name_sr_cyrl", sa.Text),
        sa.column("name_sr_latn", sa.Text),
        sa.column("name_en", sa.Text),
        sa.column("name_ru", sa.Text),
    )
    op.bulk_insert(regions, [
        {"code": "RS-00", "country_code": "RS", "parent_code": None,
         "level": "city",
         "name_sr_cyrl": "Београд", "name_sr_latn": "Beograd",
         "name_en": "Belgrade", "name_ru": "Белград"},
        {"code": "RS-VO", "country_code": "RS", "parent_code": None,
         "level": "autonomous_province",
         "name_sr_cyrl": "Војводина", "name_sr_latn": "Vojvodina",
         "name_en": "Vojvodina", "name_ru": "Воеводина"},
        {"code": "RS-KM", "country_code": "RS", "parent_code": None,
         "level": "autonomous_province",
         "name_sr_cyrl": "Косово и Метохија",
         "name_sr_latn": "Kosovo i Metohija",
         "name_en": "Kosovo and Metohija",
         "name_ru": "Косово и Метохия"},
    ])


def downgrade() -> None:
    op.execute("DELETE FROM regions WHERE code IN ('RS-00','RS-VO','RS-KM')")
    op.execute(
        "DELETE FROM classification_systems WHERE code IN ('eu_cpv','unspsc')"
    )
    op.execute("DELETE FROM countries WHERE code = 'RS'")
