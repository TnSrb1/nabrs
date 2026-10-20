"""profile_keyword_dict — toggle for keyword dictionary expansion (EPIC 3)

Adds ``profiles.keyword_dict`` BOOLEAN NOT NULL DEFAULT false.
When enabled, routing (stage_b_filter) applies expand_term (keyword_expand.py)
to each position of the AND group of include and to exclude stop-words.

Additive change on top of 0014; does not break existing rows (DEFAULT false).

RED path: rollout by the owner by hand (NOT autodeploy).
  docker compose build migrate && alembic upgrade head

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column(
            "keyword_dict",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("profiles", "keyword_dict")
