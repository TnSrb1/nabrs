"""channel_posts — the log of news already published to the Telegram channel @nabrs_news.

The publisher (``src/workers/news_autopost.py``) lives in the ``ingest`` worker: there is already
``ADMIN_BOT_TOKEN`` (@tendersrb_bot), and ``ingest`` itself connects as the superuser ``tendersrb``
(the table owner) — it needs no extra rights. ``tendersrb_app`` (bot/API) does not write the table: as in 0053,
from 0021 ``DEFAULT PRIVILEGES`` (SELECT/INSERT/UPDATE/DELETE) only ``SELECT`` remains.
The key — ``news_slug`` (natural PK = UNIQUE), no sequence.

The table is GLOBAL (RLS_MATRIX category category U+0432): neither ``tenant_id``, nor a policy, nor ``ENABLE``/``FORCE``.
``message_id`` NULL — the slug was “seeded” at the first run (the archive is not published to the channel).

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE channel_posts (
            news_slug text PRIMARY KEY,
            chat text NOT NULL,
            message_id bigint,
            posted_at timestamptz NOT NULL DEFAULT now()
        )
        """)
    op.execute("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON channel_posts FROM tendersrb_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS channel_posts")
