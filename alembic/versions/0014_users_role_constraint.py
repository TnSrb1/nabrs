"""users_role_constraint — user role flag (EPIC 6: admin cabinet)

The column ``users.role`` has existed since 0001 as a nullable Text without CHECK.
This migration:

1. backfill: ``NULL → 'user'`` for all existing rows.
2. NOT NULL + DEFAULT 'user' — new rows get the role “user” automatically.
3. CHECK ``role IN ('user', 'admin')`` — only allowed values.

The first administrator is assigned manually by the owner via SQL:
  UPDATE users SET role = 'admin' WHERE email = '...';

RED path: rollout by the owner by hand.
  docker compose build migrate && alembic upgrade head

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Backfill: existing NULL → 'user'.
    op.execute("UPDATE users SET role = 'user' WHERE role IS NULL")

    # 2. NOT NULL + DEFAULT 'user'.
    op.alter_column(
        "users",
        "role",
        existing_type=sa.Text(),
        nullable=False,
        server_default=sa.text("'user'"),
    )

    # 3. CHECK constraint.
    op.create_check_constraint(
        "users_role_check",
        "users",
        "role IN ('user', 'admin')",
    )


def downgrade() -> None:
    op.drop_constraint("users_role_check", "users", type_="check")
    op.alter_column(
        "users",
        "role",
        existing_type=sa.Text(),
        nullable=True,
        server_default=None,
    )
