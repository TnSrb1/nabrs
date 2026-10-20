"""Cross-platform link confidence: ``tenders_normalized.canonical_confidence``.

The owner’s decision of 01.10.2026 — Serbia in TED + marking duplicates with jnportal
(``src/dedup/cross_source.py``, ``src/dedup/linker.py``). The link writes
``canonical_tender_id`` (the 0001 skeleton, still empty so far) and the VERDICT of the scoring:

* ``canonical_confidence`` — ``dup`` (≥4 points) | ``probable`` (2–3); NULL — the
  tender is not linked to anything. Suppression of the second delivery (behind a flag, off) applies ONLY at ``dup``; ``probable`` — only a marking.
* ``canonical_score`` — the points of the pair: the thresholds will be re-evaluated on them.

Additive and nullable, without DEFAULT → ``ADD COLUMN`` without a table rewrite.
No index is needed: the candidate selection goes through the existing
``tenders_normalized_deadline_at_idx``. No backfill (owner’s decision: do not touch existing
rows — an UPDATE wakes routing via a trigger).

The table is public (DB_SCHEMA §4, no RLS) — the policies do not change.

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-01
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK = "ck_tenders_normalized_canonical_confidence"


def upgrade() -> None:
    op.add_column(
        "tenders_normalized",
        sa.Column("canonical_confidence", sa.Text(), nullable=True),
    )
    op.add_column(
        "tenders_normalized",
        sa.Column("canonical_score", sa.SmallInteger(), nullable=True),
    )
    op.create_check_constraint(
        _CHECK,
        "tenders_normalized",
        "canonical_confidence IN ('dup', 'probable')",
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK, "tenders_normalized", type_="check")
    op.drop_column("tenders_normalized", "canonical_score")
    op.drop_column("tenders_normalized", "canonical_confidence")
