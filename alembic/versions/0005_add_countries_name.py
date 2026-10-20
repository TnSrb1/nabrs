"""add countries.name (human-readable, for {{country_name}} in the AI prompt)

Q-28 / CHAT18 F-08. ARCHITECTURE §14.3 takes {{country_name}} from
countries.name, but the column did not exist (DB_SCHEMA §2.1, geo.py model and
migrations 0001/0002 — no name). Owner decision: add the column and fill it
with one Russian value (the prompt is Russian-language, profiles.target_language
defaults to 'ru'). Multilingual localization — later, when a non-RU
target_language appears.

Two-step add: the column is added nullable, filled for the existing RS row
(seed from 0002), then changed to NOT NULL.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-28

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("countries", sa.Column("name", sa.Text(), nullable=True))
    op.execute("UPDATE countries SET name = 'Сербия' WHERE code = 'RS'")
    op.alter_column("countries", "name", nullable=False)


def downgrade() -> None:
    op.drop_column("countries", "name")
