"""Read-only view of the tender’s AI artifacts in the web cabinet (web-ai-1, step 1).

“Value up to the web”: we show in the cabinet the artifacts ALREADY generated (the bid verdict / checklist / draft / deep analysis of the specification), which until now were visible only in the Telegram bot. ONLY reading of what exists — WITHOUT generation (``run_*`` / AI calls), WITHOUT usage / limits, WITHOUT ``chat_id``. ``tid`` is taken from the web session (NOT chat_id).

The storage is ``tender_ai_analyses`` (UNIQUE tender_id, profile_id, level; the JSON of the result in the ``output`` column). RLS policy 0003: ``profile_id IS NULL OR profile_id IN (the tenant’s profiles)`` — therefore by its own ``profile_id`` the tenant sees its profile artifacts (bid / checklist / draft / standard), while the rows with ``profile_id IS NULL`` (L1 quick / L3 deep on-demand) are shared per tender (not sensitive, see the comment of migration 0003). A foreign ``profile_id`` gives 0 rows under RLS, fail-closed; additionally we check the profile’s ownership by the tenant explicitly (route → 404).

Tested on a mock session (ADR-013); the live RLS regression under the app role — tests/db/test_rls_isolation.py (``test_tender_ai_analyses_isolation_under_app_role``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from src.api import repo
from src.dedup.also_on import also_on_count_sql
from src.domain.ids import canonical_uuid

# --------------------------------------------------------------------------- #
# SQL (asyncpg in text(): only CAST(:p AS type))
# --------------------------------------------------------------------------- #

_TENDER_META_SQL = text(f"""
    SELECT tn.title, tn.description, tn.customer_name, tn.deadline_at,
           tn.source_key, tn.source_url, NULLIF(src.display_name, '') AS source_label,
           tn.canonical_confidence, {also_on_count_sql('tn')} AS also_on_n
    FROM tenders_normalized tn
    LEFT JOIN sources src ON src.source_key = tn.source_key
    WHERE tn.id = CAST(:tender_id AS uuid)
    LIMIT 1
""")

_PROFILE_SQL = text("""
    SELECT name
    FROM profiles
    WHERE id = CAST(:profile_id AS uuid)
      AND tenant_id = CAST(:tid AS uuid)
      AND deleted_at IS NULL
    LIMIT 1
""")

# The profile artifacts of the pair (tender, profile) + the shared-per-tender rows
# (profile_id IS NULL — L1 quick / L3 deep on-demand). ORDER BY by
# (profile_id IS NULL): the profile-specific row of a level takes priority over the
# shared one, if both happen to be present.
_ANALYSES_SQL = text("""
    SELECT level, output
    FROM tender_ai_analyses
    WHERE tender_id = CAST(:tender_id AS uuid)
      AND (profile_id = CAST(:profile_id AS uuid) OR profile_id IS NULL)
    ORDER BY (profile_id IS NULL)
""")


# --------------------------------------------------------------------------- #
# Dataclass
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TenderAnalyses:
    """The AI artifacts of the tender for a specific profile (a read-only slice).

    ``outputs`` — ``level -> output`` (JSON as stored). The properties below are the named sections of the detail page; ``deep_analysis`` takes the best of the available specification-analysis levels (deep → standard → quick).
    """

    tender_id: str
    profile_id: str
    profile_name: str
    title: str
    customer_name: str | None
    deadline_at: datetime | None
    source_url: str | None
    outputs: dict[str, dict]
    source_key: str | None = None
    # The original from the source (sr) — as stored. The translation (VG-26) is laid
    # ON TOP in the route; these fields are not substituted: the “Original” toggle shows them.
    description: str | None = None
    # The name of the source (sources.display_name); None — the name is not filled in.
    source_label: str | None = None
    # The mark “the same tender on other sources” (src.dedup.also_on, 0047).
    canonical_confidence: str | None = None
    also_on_n: int = 0

    @property
    def bid(self) -> dict | None:
        return self.outputs.get("bid")

    @property
    def checklist(self) -> dict | None:
        return self.outputs.get("checklist")

    @property
    def draft(self) -> dict | None:
        return self.outputs.get("draft")

    @property
    def deep_analysis(self) -> dict | None:
        for level in ("deep", "standard", "quick"):
            out = self.outputs.get(level)
            if out:
                return out
        return None

    @property
    def has_deep(self) -> bool:
        """There is a deep one (standard / quick do not count): the deep button is needed while it is absent."""
        return bool(self.outputs.get("deep"))

    @property
    def has_any(self) -> bool:
        return bool(self.bid or self.checklist or self.draft or self.deep_analysis)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _coerce_output(raw: Any) -> dict | None:
    """JSONB → dict. asyncpg returns a dict, but we play safe with a string (as
    ``_load_cached_output`` in bid_verdict_service)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


async def get_tender_analyses(
    db, *, tid: str, tender_id: str, profile_id: str
) -> TenderAnalyses | None:
    """Read-only artifacts of the tender ``tender_id`` for the profile ``profile_id``.

    ``set_tenant(tid)`` — FIRST as the transaction’s action (RLS, otherwise 0 rows fail-closed / UndefinedObjectError). Returns ``None`` if the tender is not found or the profile does not belong to the tenant (route → 404). No generation / usage / limits — only the display of what is already stored.
    """
    # profile_id from ?query: garbage / urn → None (route 404); in SQL — only the canonical form (#632).
    profile_canon = canonical_uuid(profile_id)
    if profile_canon is None:
        return None
    profile_id = profile_canon
    await repo.set_tenant(db, tid)

    profile_row = (
        await db.execute(_PROFILE_SQL, {"profile_id": profile_id, "tid": str(tid)})
    ).first()
    if profile_row is None:
        return None

    tender_row = (await db.execute(_TENDER_META_SQL, {"tender_id": tender_id})).first()
    if tender_row is None:
        return None

    rows = (
        await db.execute(
            _ANALYSES_SQL, {"tender_id": tender_id, "profile_id": profile_id}
        )
    ).all()
    outputs: dict[str, dict] = {}
    for row in rows:
        level = row.level
        out = _coerce_output(row.output)
        if level and out is not None and level not in outputs:
            outputs[level] = out

    return TenderAnalyses(
        tender_id=str(tender_id),
        profile_id=str(profile_id),
        profile_name=str(profile_row.name) if profile_row.name else "Без названия",
        title=str(tender_row.title) if tender_row.title else "Без названия",
        customer_name=tender_row.customer_name,
        deadline_at=tender_row.deadline_at,
        source_key=getattr(tender_row, "source_key", None),
        source_url=tender_row.source_url,
        outputs=outputs,
        description=tender_row.description,
        source_label=getattr(tender_row, "source_label", None),
        canonical_confidence=getattr(tender_row, "canonical_confidence", None),
        also_on_n=int(getattr(tender_row, "also_on_n", 0) or 0),
    )
