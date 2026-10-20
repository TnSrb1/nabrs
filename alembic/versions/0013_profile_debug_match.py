"""profile_debug_match — debug mode flag for delivery (EPIC 2)

Additive change on top of 0012, without DROP/ALTER of existing objects.
The rollout on the live prod does NOT break existing rows (DEFAULT false).

1. ``profiles.debug_match`` BOOLEAN NOT NULL DEFAULT false — enables showing the match
   reason (triggered keywords and CPV codes) and the deep-link button
   “⚙️ Configure this template” in the tender card during delivery.

RED path: rollout by the owner by hand (NOT autodeploy).
  docker compose build migrate && alembic upgrade head

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column(
            "debug_match",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("profiles", "debug_match")
