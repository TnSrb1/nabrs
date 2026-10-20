"""seed jnportal source + 29 RS okrugs (ISO 3166-2:RS)

Track D Sprint-14. INSERT the jnportal `sources` row and the 29 districts
RS-01..RS-29. Top-level RS-00 / RS-VO / RS-KM were already seeded in 0002 —
not re-inserted here.

District codes / names / parents come from the official ISO 3166-2:RS list
(Wikipedia ISO 3166-2:RS, cross-checked against ISO 3166-2 Newsletter II-1
2010-02-19), NOT from memory. `name_ru` is intentionally left NULL: the ISO
source carries only sr/en, and the brief asked only for sr_cyrl / sr_latn /
en — Russian names are deferred rather than fabricated.

Parent rule (ISO 3166-2:RS digit ranges):
  RS-01..RS-07 -> RS-VO (Vojvodina)
  RS-08..RS-24 -> NULL  (Central Serbia, not in an autonomous province)
  RS-25..RS-29 -> RS-KM (Kosovo-Metohija)

Opstine (~150) are deferred — out of scope for this PR.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-28

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (code, name_sr_cyrl, name_sr_latn, name_en, parent_code)
_OKRUZI = [
    ("RS-01", "Севернобачки округ", "Severnobački okrug", "North Bačka", "RS-VO"),
    ("RS-02", "Средњобанатски округ", "Srednjebanatski okrug", "Central Banat", "RS-VO"),
    ("RS-03", "Севернобанатски округ", "Severnobanatski okrug", "North Banat", "RS-VO"),
    ("RS-04", "Јужнобанатски округ", "Južnobanatski okrug", "South Banat", "RS-VO"),
    ("RS-05", "Западнобачки округ", "Zapadnobački okrug", "West Bačka", "RS-VO"),
    ("RS-06", "Јужнобачки округ", "Južnobački okrug", "South Bačka", "RS-VO"),
    ("RS-07", "Сремски округ", "Sremski okrug", "Srem", "RS-VO"),
    ("RS-08", "Мачвански округ", "Mačvanski okrug", "Mačva", None),
    ("RS-09", "Колубарски округ", "Kolubarski okrug", "Kolubara", None),
    ("RS-10", "Подунавски округ", "Podunavski okrug", "Podunavlje", None),
    ("RS-11", "Браничевски округ", "Braničevski okrug", "Braničevo", None),
    ("RS-12", "Шумадијски округ", "Šumadijski okrug", "Šumadija", None),
    ("RS-13", "Поморавски округ", "Pomoravski okrug", "Pomoravlje", None),
    ("RS-14", "Борски округ", "Borski okrug", "Bor", None),
    ("RS-15", "Зајечарски округ", "Zaječarski okrug", "Zaječar", None),
    ("RS-16", "Златиборски округ", "Zlatiborski okrug", "Zlatibor", None),
    ("RS-17", "Моравички округ", "Moravički okrug", "Moravica", None),
    ("RS-18", "Рашки округ", "Raški okrug", "Raška", None),
    ("RS-19", "Расински округ", "Rasinski okrug", "Rasina", None),
    ("RS-20", "Нишавски округ", "Nišavski okrug", "Nišava", None),
    ("RS-21", "Топлички округ", "Toplički okrug", "Toplica", None),
    ("RS-22", "Пиротски округ", "Pirotski okrug", "Pirot", None),
    ("RS-23", "Јабланички округ", "Jablanički okrug", "Jablanica", None),
    ("RS-24", "Пчињски округ", "Pčinjski okrug", "Pčinja", None),
    ("RS-25", "Косовски округ", "Kosovski okrug", "Kosovo", "RS-KM"),
    ("RS-26", "Пећки округ", "Pećki okrug", "Peć", "RS-KM"),
    ("RS-27", "Призренски округ", "Prizrenski okrug", "Prizren", "RS-KM"),
    ("RS-28", "Косовско-митровачки округ", "Kosovsko-Mitrovački okrug", "Kosovo-Mitrovica", "RS-KM"),
    ("RS-29", "Косовско-поморавски округ", "Kosovsko-Pomoravski okrug", "Kosovo-Pomoravlje", "RS-KM"),
]


def upgrade() -> None:
    sources = sa.table(
        "sources",
        sa.column("source_key", sa.Text),
        sa.column("country_codes", postgresql.ARRAY(sa.CHAR(2))),
        sa.column("display_name", sa.Text),
        sa.column("base_url", sa.Text),
        sa.column("enabled", sa.Boolean),
        sa.column("polling_interval_sec", sa.Integer),
    )
    op.bulk_insert(sources, [
        {
            "source_key": "jnportal",
            "country_codes": ["RS"],
            "display_name": "Portal javnih nabavki (UJN)",
            "base_url": "https://jnportal.src-ujn.example.invalid",
            "enabled": True,
            "polling_interval_sec": 1800,
        },
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
        {
            "code": code,
            "country_code": "RS",
            "parent_code": parent,
            "level": "okrug",
            "name_sr_cyrl": cyrl,
            "name_sr_latn": latn,
            "name_en": en,
            "name_ru": None,
        }
        for (code, cyrl, latn, en, parent) in _OKRUZI
    ])


def downgrade() -> None:
    codes = ", ".join(f"'RS-{i:02d}'" for i in range(1, 30))
    op.execute(f"DELETE FROM regions WHERE code IN ({codes})")
    op.execute("DELETE FROM sources WHERE source_key = 'jnportal'")
