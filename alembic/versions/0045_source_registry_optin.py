"""Source registry: the moment a source is enabled and the moment a profile subscribes.

Owner's decisions of 30.09.2026 (tmp/TASK_src0a.md, R1/R4/R5):

* **R4 — no backfill.** A new source loads only the current data, not its own archive. Before this revision
  ``sources.last_run_at IS NULL`` meant ``since=None`` for the ingest — “pull everything”. Now the cursor of the first pass
  is computed from ``sources.enabled_at`` (the moment of enabling).
* **R4 — silent first pass.** ``sources.baseline_at`` — the moment the first pass of the source finished. Tenders seen
  BEFORE it (``first_seen_at <= baseline_at``) are never sent: otherwise the first pass of a new source would have gone
  to all profiles with its key “everything at once”.
* **R5 — only after subscription.** ``profiles.source_opt_in`` —
  ``{source_key: moment the profile enabled the source}``. A tender is delivered to a
  profile only if it arrived AFTER this moment (fallback — ``profiles.created_at``). Without the cut-off an UPDATE of an old open tender
  went to a profile created later (measurement 30.09: 2 outbox rows out of 3165 over
  30 days, both ``update``).

The backfill preserves the behavior of the live contour:

* enabled sources (in production — only jnportal): ``enabled_at =
  COALESCE(last_run_at, now())``, ``baseline_at = '1970-01-01'`` — the cursor is the same,
  the silent-pass cut-off cuts nothing;
* profiles: for each key from ``enabled_source_keys`` — the profile's ``created_at``. We write into
  ``profiles`` (there are no NOTIFY triggers on it — checked with ``\\d
  profiles`` on production 30.09), we do not touch ``tenders_normalized`` — routing is not woken.

``profiles`` is already tenant-scoped with the policy + ENABLE + FORCE RLS (0021/0032);
there are no new tables.

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sources", sa.Column("enabled_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column(
        "sources", sa.Column("baseline_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.execute("""
        UPDATE sources
           SET enabled_at = COALESCE(last_run_at, now()),
               baseline_at = TIMESTAMPTZ '1970-01-01 00:00:00+00'
         WHERE enabled = true
        """)

    op.add_column(
        "profiles",
        sa.Column(
            "source_opt_in",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute("""
        UPDATE profiles p
           SET source_opt_in = (
                 SELECT COALESCE(jsonb_object_agg(k, to_jsonb(p.created_at)),
                                 '{}'::jsonb)
                   FROM unnest(p.enabled_source_keys) AS k
               )
         WHERE cardinality(COALESCE(p.enabled_source_keys, '{}')) > 0
        """)


def downgrade() -> None:
    op.drop_column("profiles", "source_opt_in")
    op.drop_column("sources", "baseline_at")
    op.drop_column("sources", "enabled_at")
