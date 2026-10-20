"""An index ``tenders_normalized (source_key, first_seen_at)`` for the ``/izvori`` showcase.

``src/api/public/sources_page.py`` for each enabled source computes ``min(first_seen_at) WHERE source_key = …`` (“collecting since”). The existing index
``(source_key, source_uid)`` does not contain ``first_seen_at`` → two seq scans over
~55.7K rows, EXPLAIN on production 137 ms (docs/reports/TESTING_small-three.md).
With the new index ``min()`` the planner takes the first record of the index.

Additive, a plain ``CREATE INDEX`` (not CONCURRENTLY): 55K rows are built in
fractions of a second, only writes to ``tenders_normalized`` are blocked during that time (reads go through). The table is public, no RLS. ``downgrade`` = ``DROP INDEX``.

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-03
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "tenders_normalized_source_key_first_seen_at_idx"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} "
        "ON tenders_normalized (source_key, first_seen_at)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
