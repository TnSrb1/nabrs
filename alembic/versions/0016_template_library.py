"""template_library — library of ready-made search templates (EPIC 4)

Creates the table ``template_library`` and fills it with 14 public templates
(construction, IT, security, medicine, transport, etc. — Serbian market topics).

The fields ``status`` / ``is_public`` / ``created_by_user_id`` are laid down now
for EPIC 5 (user suggestions + moderation queue); the EPIC 5 flow is NOT implemented
in this migration.

RED path: rollout by the owner by hand together with 0015.
  docker compose build migrate && alembic upgrade head

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-14
"""

from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "template_library",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("activity_kind", sa.Text(), nullable=True),
        sa.Column("keywords_include", postgresql.JSONB(), nullable=True),
        sa.Column("classifications", postgresql.JSONB(), nullable=True),
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'approved'"),
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "country_code",
            sa.CHAR(2),
            sa.ForeignKey("countries.code"),
            nullable=False,
            server_default=sa.text("'RS'"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    tbl = sa.table(
        "template_library",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.Text),
        sa.column("description", sa.Text),
        sa.column("activity_kind", sa.Text),
        sa.column("keywords_include", postgresql.JSONB),
        sa.column("classifications", postgresql.JSONB),
        sa.column("is_public", sa.Boolean),
        sa.column("status", sa.Text),
        sa.column("created_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.column("country_code", sa.CHAR(2)),
    )

    op.bulk_insert(
        tbl,
        [
            # --- Construction and real estate ---
            {
                "id": uuid.UUID("00000016-0001-0000-0000-000000000001"),
                "name": "Строительство и ремонт зданий",
                "description": "Общестроительные работы, реконструкция, ремонт объектов",
                "activity_kind": "Строительство",
                "keywords_include": [
                    {"all_of": ["строительство"]},
                    {"all_of": ["ремонт", "здание"]},
                    {"all_of": ["реконструкция"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "45000000"},
                        {"system_code": "eu_cpv", "code": "45210000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            {
                "id": uuid.UUID("00000016-0002-0000-0000-000000000001"),
                "name": "Дороги, асфальт и инфраструктура",
                "description": "Строительство и ремонт дорог, асфальтирование, коммуникации",
                "activity_kind": "Строительство",
                "keywords_include": [
                    {"all_of": ["дорога"]},
                    {"all_of": ["асфальт"]},
                    {"all_of": ["инфраструктура"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "45233000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            {
                "id": uuid.UUID("00000016-0003-0000-0000-000000000001"),
                "name": "Сантехника и водоснабжение",
                "description": "Сантехнические работы, водопровод, канализация",
                "activity_kind": "Строительство",
                "keywords_include": [
                    {"all_of": ["водоснабжение"]},
                    {"all_of": ["канализация"]},
                    {"all_of": ["сантехника"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "45330000"},
                        {"system_code": "eu_cpv", "code": "44163000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            # --- IT and technology ---
            {
                "id": uuid.UUID("00000016-0004-0000-0000-000000000001"),
                "name": "Программное обеспечение и лицензии",
                "description": "Поставка ПО, лицензии, информационные системы",
                "activity_kind": "IT и технологии",
                "keywords_include": [
                    {"all_of": ["программное"]},
                    {"all_of": ["лицензия"]},
                    {"all_of": ["информационная система"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "48000000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            {
                "id": uuid.UUID("00000016-0005-0000-0000-000000000001"),
                "name": "IT-услуги и разработка",
                "description": "Разработка ПО, IT-консалтинг, поддержка систем",
                "activity_kind": "IT и технологии",
                "keywords_include": [
                    {"all_of": ["разработка"]},
                    {"all_of": ["интернет"]},
                    {"all_of": ["сеть"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "72000000"},
                        {"system_code": "eu_cpv", "code": "72200000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            {
                "id": uuid.UUID("00000016-0006-0000-0000-000000000001"),
                "name": "Компьютеры и оргтехника",
                "description": "Поставка компьютеров, принтеров, серверов",
                "activity_kind": "IT и технологии",
                "keywords_include": [
                    {"all_of": ["компьютер"]},
                    {"all_of": ["сервер"]},
                    {"all_of": ["принтер"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "30000000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            # --- Security ---
            {
                "id": uuid.UUID("00000016-0007-0000-0000-000000000001"),
                "name": "Охрана и видеонаблюдение",
                "description": "Охранные услуги, системы видеонаблюдения, сигнализация",
                "activity_kind": "Безопасность",
                "keywords_include": [
                    {"all_of": ["охрана"]},
                    {"all_of": ["видеонаблюдение"]},
                    {"all_of": ["сигнализация"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "79710000"},
                        {"system_code": "eu_cpv", "code": "35125000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            # --- Medicine ---
            {
                "id": uuid.UUID("00000016-0008-0000-0000-000000000001"),
                "name": "Медицинское оборудование",
                "description": "Поставка медтехники, лабораторного оборудования",
                "activity_kind": "Медицина и фармацевтика",
                "keywords_include": [
                    {"all_of": ["медицинское", "оборудование"]},
                    {"all_of": ["лабораторное"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "33100000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            {
                "id": uuid.UUID("00000016-0009-0000-0000-000000000001"),
                "name": "Лекарства и фармацевтика",
                "description": "Закупка медикаментов, расходных медматериалов",
                "activity_kind": "Медицина и фармацевтика",
                "keywords_include": [
                    {"all_of": ["лекарство"]},
                    {"all_of": ["фармацевтика"]},
                    {"all_of": ["медикамент"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "33600000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            # --- Transport ---
            {
                "id": uuid.UUID("00000016-000a-0000-0000-000000000001"),
                "name": "Транспорт и автомобили",
                "description": "Поставка автотранспорта, спецтехники, транспортные услуги",
                "activity_kind": "Транспорт",
                "keywords_include": [
                    {"all_of": ["транспорт"]},
                    {"all_of": ["автомобиль"]},
                    {"all_of": ["автобус"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "34100000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            # --- Furniture and interior ---
            {
                "id": uuid.UUID("00000016-000b-0000-0000-000000000001"),
                "name": "Мебель и офисное оснащение",
                "description": "Поставка мебели, офисных кресел, шкафов",
                "activity_kind": "Мебель и интерьер",
                "keywords_include": [
                    {"all_of": ["мебель"]},
                    {"all_of": ["стол", "кресло"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "39100000"},
                        {"system_code": "eu_cpv", "code": "39130000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            # --- Food products ---
            {
                "id": uuid.UUID("00000016-000c-0000-0000-000000000001"),
                "name": "Продукты питания",
                "description": "Поставка продовольствия для учреждений, столовых, больниц",
                "activity_kind": "Продукты питания",
                "keywords_include": [
                    {"all_of": ["продукт", "питание"]},
                    {"all_of": ["продовольствие"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "15000000"},
                        {"system_code": "eu_cpv", "code": "15800000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            # --- Stationery ---
            {
                "id": uuid.UUID("00000016-000d-0000-0000-000000000001"),
                "name": "Канцелярские товары и расходники",
                "description": "Бумага, ручки, картриджи, офисные принадлежности",
                "activity_kind": "Канцелярия и расходники",
                "keywords_include": [
                    {"all_of": ["канцелярия"]},
                    {"all_of": ["бумага"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "30190000"},
                        {"system_code": "eu_cpv", "code": "30192000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            # --- Electrical ---
            {
                "id": uuid.UUID("00000016-000e-0000-0000-000000000001"),
                "name": "Электрооборудование и монтаж",
                "description": "Электроустановочные изделия, кабели, монтаж электросети",
                "activity_kind": "Электрика",
                "keywords_include": [
                    {"all_of": ["электрооборудование"]},
                    {"all_of": ["монтаж", "электро"]},
                    {"all_of": ["кабель"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "31000000"},
                        {"system_code": "eu_cpv", "code": "31200000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            # --- Cleaning ---
            {
                "id": uuid.UUID("00000016-000f-0000-0000-000000000001"),
                "name": "Клининг и уборка помещений",
                "description": "Услуги уборки, клининг объектов, вывоз мусора",
                "activity_kind": "Клининг и уборка",
                "keywords_include": [
                    {"all_of": ["уборка"]},
                    {"all_of": ["клининг"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "90910000"},
                        {"system_code": "eu_cpv", "code": "90911000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
            # --- Telecom ---
            {
                "id": uuid.UUID("00000016-0010-0000-0000-000000000001"),
                "name": "Связь и телекоммуникации",
                "description": "Телефония, интернет, сетевое оборудование, телеком-услуги",
                "activity_kind": "Связь и телеком",
                "keywords_include": [
                    {"all_of": ["телефония"]},
                    {"all_of": ["телекоммуникация"]},
                    {"all_of": ["связь"]},
                ],
                "classifications": {
                    "include": [
                        {"system_code": "eu_cpv", "code": "32000000"},
                        {"system_code": "eu_cpv", "code": "32520000"},
                    ],
                    "exclude": [],
                },
                "is_public": True,
                "status": "approved",
                "created_by_user_id": None,
                "country_code": "RS",
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("template_library")
