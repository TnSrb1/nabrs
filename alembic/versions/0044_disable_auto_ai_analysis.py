"""Automatic AI analysis of the flow is off: ``profiles.ai_analysis_level = 'none'``.

Owner's decision of 15.08.2026. The product had TWO different AIs, and nobody deliberately ordered the second:

1. **On a button** — a verdict “take part / skip”, a document checklist, a request draft, a deep analysis.
   Started by a person, each with their own limit. This is the intended product; it remains untouched.
2. **Automatic analysis of the WHOLE flow** — the field “AI analysis level” in the profile form. If anything other than ``none`` was selected, routing set ``requires_ai`` on EVERY delivered tender (``matching.py``), and the model was called without a single click.

The cost of the second on live data (measurement 15.08): the median tender description —
**50,002 characters** (≈22k input tokens, confirmed by an actual analysis of
`bid` on haiku: 22,276), i.e. ≈$0.022 per tender with a flow of ~170 per day —
on the order of $110 per month for one profile in the “All tenders” mode. The client chose this
from a dropdown whose caption gave no sense of the scale.

Three profiles in production had the level chosen (`quick`) — and all their deliveries went to
``ai_rejected``, i.e. the subscribers received NEITHER tenders NOR analysis (see
0043 and PR #507).

The migration zeroes the axis for all profiles; the field stays in the schema — it is read by
``gate_profile`` and will still be useful if auto-analysis is brought back deliberately and with a finite limit. The form no longer sends this value, and
``parse_profile_form`` hard-writes ``'none'`` (a POST can be sent by hand).

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE profiles
           SET ai_analysis_level = 'none'
         WHERE COALESCE(ai_analysis_level, 'none') <> 'none'
        """)
    # The queue tail: rows already marked for auto-analysis, otherwise they would
    # hang in ai_pending forever — the worker will no longer pick them up by meaning, and
    # delivery takes nothing except pending/ai_done. We hand them to the subscriber without
    # analysis — the same principle as _degrade_to_plain (PR #507).
    op.execute("""
        UPDATE delivery_outbox
           SET status = 'pending', requires_ai = false
         WHERE status = 'ai_pending'
        """)


def downgrade() -> None:
    # The levels are not restored back: the earlier values were not saved, and
    # “set everyone to quick” would be worse than leaving them switched off.
    pass
