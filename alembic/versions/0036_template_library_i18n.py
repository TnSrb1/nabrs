"""template_library.i18n — translations of the template showcase (sr/en)

The template library is the onboarding tool “take a ready-made profile”: the first thing a new client
opens. Names, descriptions and groups of 16 public templates exist ONLY in Russian (seed 0016),
so a Serbian client reads the showcase in someone else’s language: a render check of ``/library`` under
session ``locale=sr`` gave 34 Russian letters (the metric: Russian-only letters U+044B U+044D U+0449 U+044A U+0451 U+0439) AFTER the vertical packages had already
become localized (#443).

Column ``i18n jsonb NULL`` — translations of the record fields:

    {“sr”: {"name": …, "description": …, "activity_kind": …},
     “en”: {"name": …, "description": …, "activity_kind": …}}

Why jsonb and not columns ``name_sr``/``name_en``/…: three fields, three languages — nine columns against
one, and adding a language would again require a migration. Russian stays in the original
``name``/``description``/``activity_kind`` — the contract of the existing reads (admin, EPIC 5) does not
change, ``i18n`` only complements it.

User templates (EPIC 5, user suggestions) remain with ``i18n IS NULL`` — their text is written by a person,
it must not be machine-translated; in that case the render shows the original (fail-open, like
``okrug_label``/``status_label``).

The sr translations are in Serbian CYRILLIC: the same page renders the vertical packages
(``vertical_packages``) in Cyrillic too, and mixing scripts in one list would read worse than the
mismatch with the Latin cabinet chrome.
‼️ Not written by a native speaker — treat as a draft until proofread (like the other sr strings,
see STATE “Native-speaker proofreading”).

The table is not under FORCE RLS (0016); the additive ADD COLUMN does not affect the policies.

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-25
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Template ids are deterministic by the seed 0016 — we key the translations by them, not by
# the name text: the text can be edited by the operator via the admin, the id cannot.
_TRANSLATIONS: dict[str, dict[str, dict[str, str]]] = {
    "00000016-0001-0000-0000-000000000001": {
        "sr": {
            "name": "Грађевински радови и адаптације",
            "description": "Општи грађевински радови, реконструкција, адаптација објеката",
            "activity_kind": "Грађевина",
        },
        "en": {
            "name": "Building construction and renovation",
            "description": "General construction works, reconstruction, renovation",
            "activity_kind": "Construction",
        },
    },
    "00000016-0002-0000-0000-000000000001": {
        "sr": {
            "name": "Путеви, асфалт и инфраструктура",
            "description": "Изградња и одржавање путева, асфалтирање, инфраструктура",
            "activity_kind": "Грађевина",
        },
        "en": {
            "name": "Roads, asphalt and infrastructure",
            "description": "Road construction and maintenance, asphalting, infrastructure",
            "activity_kind": "Construction",
        },
    },
    "00000016-0003-0000-0000-000000000001": {
        "sr": {
            "name": "Водовод и санитарије",
            "description": "Водоинсталатерски радови, водовод, канализација",
            "activity_kind": "Грађевина",
        },
        "en": {
            "name": "Plumbing and water supply",
            "description": "Plumbing works, water supply, sewerage",
            "activity_kind": "Construction",
        },
    },
    "00000016-0004-0000-0000-000000000001": {
        "sr": {
            "name": "Софтвер и лиценце",
            "description": "Набавка софтвера, лиценци, информационих система",
            "activity_kind": "ИТ и технологије",
        },
        "en": {
            "name": "Software and licences",
            "description": "Software, licences, information systems",
            "activity_kind": "IT and technology",
        },
    },
    "00000016-0005-0000-0000-000000000001": {
        "sr": {
            "name": "ИТ услуге и развој",
            "description": "Развој софтвера, ИТ консалтинг, подршка системима",
            "activity_kind": "ИТ и технологије",
        },
        "en": {
            "name": "IT services and development",
            "description": "Software development, IT consulting, systems support",
            "activity_kind": "IT and technology",
        },
    },
    "00000016-0006-0000-0000-000000000001": {
        "sr": {
            "name": "Рачунарска опрема",
            "description": "Набавка рачунара, штампача, сервера",
            "activity_kind": "ИТ и технологије",
        },
        "en": {
            "name": "Computers and office equipment",
            "description": "Supply of computers, printers, servers",
            "activity_kind": "IT and technology",
        },
    },
    "00000016-0007-0000-0000-000000000001": {
        "sr": {
            "name": "Обезбеђење и видео-надзор",
            "description": "Услуге обезбеђења, видео-надзор, аларм",
            "activity_kind": "Безбедност",
        },
        "en": {
            "name": "Security and video surveillance",
            "description": "Security services, video surveillance, alarms",
            "activity_kind": "Security",
        },
    },
    "00000016-0008-0000-0000-000000000001": {
        "sr": {
            "name": "Медицинска опрема",
            "description": "Набавка медицинске и лабораторијске опреме",
            "activity_kind": "Медицина и фармација",
        },
        "en": {
            "name": "Medical equipment",
            "description": "Supply of medical and laboratory equipment",
            "activity_kind": "Medicine and pharmacy",
        },
    },
    "00000016-0009-0000-0000-000000000001": {
        "sr": {
            "name": "Лекови и фармација",
            "description": "Набавка лекова и медицинског потрошног материјала",
            "activity_kind": "Медицина и фармација",
        },
        "en": {
            "name": "Medicines and pharmaceuticals",
            "description": "Procurement of medicines and medical consumables",
            "activity_kind": "Medicine and pharmacy",
        },
    },
    "00000016-000a-0000-0000-000000000001": {
        "sr": {
            "name": "Возила и транспорт",
            "description": "Набавка возила, специјалне механизације, транспортне услуге",
            "activity_kind": "Транспорт",
        },
        "en": {
            "name": "Vehicles and transport",
            "description": "Supply of vehicles, special machinery, transport services",
            "activity_kind": "Transport",
        },
    },
    "00000016-000b-0000-0000-000000000001": {
        "sr": {
            "name": "Намештај и опремање канцеларија",
            "description": "Набавка намештаја, канцеларијских столица, ормара",
            "activity_kind": "Намештај и ентеријер",
        },
        "en": {
            "name": "Furniture and office equipment",
            "description": "Supply of furniture, office chairs, cabinets",
            "activity_kind": "Furniture and interior",
        },
    },
    "00000016-000c-0000-0000-000000000001": {
        "sr": {
            "name": "Прехрамбени производи",
            "description": "Набавка хране за установе, кухиње и болнице",
            "activity_kind": "Прехрамбени производи",
        },
        "en": {
            "name": "Food products",
            "description": "Food supply for institutions, canteens, hospitals",
            "activity_kind": "Food products",
        },
    },
    "00000016-000d-0000-0000-000000000001": {
        "sr": {
            "name": "Канцеларијски материјал",
            "description": "Папир, оловке, тонери, канцеларијски прибор",
            "activity_kind": "Канцеларија и потрошни материјал",
        },
        "en": {
            "name": "Office supplies",
            "description": "Paper, pens, toner, office accessories",
            "activity_kind": "Office supplies",
        },
    },
    "00000016-000e-0000-0000-000000000001": {
        "sr": {
            "name": "Електроопрема и монтажа",
            "description": "Електроинсталациони материјал, каблови, монтажа електромреже",
            "activity_kind": "Електрика",
        },
        "en": {
            "name": "Electrical equipment and installation",
            "description": "Electrical fittings, cables, power network installation",
            "activity_kind": "Electrical",
        },
    },
    "00000016-000f-0000-0000-000000000001": {
        "sr": {
            "name": "Чишћење и одржавање хигијене",
            "description": "Услуге чишћења, одржавање објеката, одвоз смећа",
            "activity_kind": "Чишћење и одржавање",
        },
        "en": {
            "name": "Cleaning services",
            "description": "Cleaning services, facility maintenance, waste removal",
            "activity_kind": "Cleaning",
        },
    },
    "00000016-0010-0000-0000-000000000001": {
        "sr": {
            "name": "Комуникације и телекомуникације",
            "description": "Телефонија, интернет, мрежна опрема, телеком услуге",
            "activity_kind": "Комуникације и телеком",
        },
        "en": {
            "name": "Communications and telecom",
            "description": "Telephony, internet, network equipment, telecom services",
            "activity_kind": "Telecom",
        },
    },
}


def upgrade() -> None:
    op.add_column(
        "template_library",
        sa.Column("i18n", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # The translations are keyed by the seed ids. Records that are absent (foreign DB / partial seed)
    # simply are not updated — an UPDATE on a non-existent id is not an error.
    conn = op.get_bind()
    for tpl_id, payload in _TRANSLATIONS.items():
        conn.execute(
            sa.text(
                "UPDATE template_library SET i18n = CAST(:payload AS jsonb) "
                "WHERE id = CAST(:tpl_id AS uuid)"
            ),
            {"payload": json.dumps(payload, ensure_ascii=False), "tpl_id": tpl_id},
        )


def downgrade() -> None:
    op.drop_column("template_library", "i18n")
