"""Cleaning tender text for SEO surfaces (title/meta/JSON-LD).

The June 2026 Excel backfill left the literal ``_x000D_`` (CR) inside some
``title``/``customer_name`` (2943 of 45010 rows of that month, 0 in July—September
— ingest is already clean). We clean AT RENDER: ``UPDATE tenders_normalized``
is forbidden — the trigger ``tenders_normalized_notify`` would wake a routing replay and
send stale items out to customers (CLAUDE.md).
"""

from __future__ import annotations

import re

_ARTIFACT_RE = re.compile(r"_x000D_")
_WS_RE = re.compile(r"\s+")


def clean_seo_text(value: str | None) -> str:
    """``None``/empty → ``""``; otherwise removes ``_x000D_`` and collapses whitespace."""
    if not value:
        return ""
    cleaned = _ARTIFACT_RE.sub(" ", value)
    return _WS_RE.sub(" ", cleaned).strip()
