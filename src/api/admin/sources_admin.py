"""“Izvori” admin — management of sources (SRC-0d, owner decision 30.09 item 5).

Enabling a source is a BUTTON here, not a migration; a new adapter is registered
DISABLED (SRC-0a). The semantics of enabling/disabling are set by
:mod:`src.domain.sources` (``enable_source`` — a silent first pass, the archive is not
pulled; ``disable_source`` — only ``enabled=false``) — the enabling SQL is NOT
rewritten here.

``sources`` — a reference table without RLS (not tenant-scoped): direct SQL is allowed; the audit
goes through ``app_admin_write_audit`` (:func:`src.domain.audit.write_audit`),
in the SAME transaction as the mutation.

A source’s silence (banner and table row) is computed by the same predicate as the
worker alert (:mod:`src.domain.source_silence`), with the source’s threshold — the adapter
attribute ``silence_threshold_hours`` (otherwise the common one).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from src.domain.audit import write_audit
from src.domain.source_silence import is_silent, silence_hours
from src.domain.sources import disable_source, enable_source
from src.workers.health import silence_threshold_hours

logger = logging.getLogger(__name__)

_SOURCES_ADMIN_SQL = text("""
    SELECT s.source_key,
           s.display_name,
           s.country_codes,
           COALESCE(s.enabled, false) AS enabled,
           s.enabled_at,
           s.baseline_at,
           s.last_run_at,
           s.last_status,
           COALESCE(s.error_count, 0) AS error_count,
           COALESCE(t.cnt_24h, 0)     AS cnt_24h,
           COALESCE(t.cnt_7d, 0)      AS cnt_7d,
           COALESCE(t.cnt_total, 0)   AS cnt_total,
           t.last_at                  AS last_tender_at
    FROM sources s
    LEFT JOIN (
        SELECT source_key,
               COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '24 hours') AS cnt_24h,
               COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '7 days')   AS cnt_7d,
               COUNT(*)            AS cnt_total,
               MAX(first_seen_at)  AS last_at
        FROM tenders_normalized
        GROUP BY source_key
    ) t ON t.source_key = s.source_key
    ORDER BY s.source_key
    """)

# M2 reconciliation: the last reconciliation and the last MISMATCH for the source. Tolerance —
# mirror of src.domain.source_reconcile.is_mismatch (a test keeps them together).
_RECONCILE_SQL = text("""
    SELECT source_key, day, source_count, our_count, checked_at,
           (ABS(source_count - our_count)
              > GREATEST(2, 0.02 * source_count)) AS mismatch
    FROM source_reconcile
    ORDER BY source_key, day DESC
    """)

_SOURCE_ONE_SQL = text("""
    SELECT source_key, COALESCE(enabled, false) AS enabled, enabled_at, baseline_at
    FROM sources
    WHERE source_key = CAST(:source_key AS text)
    """)


def adapter_attrs() -> dict[str, dict[str, Any]]:
    """Attributes of registered adapters: ``{key: {threshold, night_batch}}``.

    The registry is loaded lazily and best-effort: an import failure of the adapters must not
    bring the admin down — then the common threshold and “no nightly batch” apply.
    """
    try:
        from src.parsers.registry import load_all_adapters

        registry = load_all_adapters()
    except Exception as exc:  # noqa: BLE001 — the admin lives without the registry
        logger.warning(
            "admin sources: adapter registry unavailable (%s)", type(exc).__name__
        )
        return {}
    return {
        key: {
            "threshold": getattr(cls, "silence_threshold_hours", None),
            "night_batch": bool(getattr(cls, "night_batch", False)),
        }
        for key, cls in registry.items()
    }


def source_threshold(attrs: dict[str, dict[str, Any]], key: str) -> float:
    """The source’s silence threshold: the adapter attribute, otherwise the common one."""
    value = (attrs.get(key) or {}).get("threshold")
    return float(value) if value is not None else silence_threshold_hours()


@dataclass
class SourceRow:
    source_key: str
    display_name: str
    country_codes: tuple[str, ...]
    enabled: bool
    enabled_at: datetime | None
    baseline_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    error_count: int
    cnt_24h: int
    cnt_7d: int
    cnt_total: int
    last_tender_at: datetime | None
    threshold_hours: float = 0.0
    night_batch: bool = False
    silent_hours: float | None = None
    silent: bool = False
    registered: bool = True
    #: Last reconciliation with the source (M2): ``{day, source_count, our_count, checked_at}``.
    reconcile_last: dict[str, Any] | None = None
    #: Last MISMATCH by day (of the same kind), ``None`` — there are no mismatches.
    reconcile_bad: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.source_key

    @property
    def baseline_done(self) -> bool:
        """The silent first pass is complete (``baseline_at`` was set by ingest)."""
        return self.baseline_at is not None


def _row(r, now: datetime, attrs: dict[str, dict[str, Any]]) -> SourceRow:
    key = str(r.source_key)
    threshold = source_threshold(attrs, key)
    hours = silence_hours(now, r.last_tender_at, r.enabled_at)
    enabled = bool(r.enabled)
    return SourceRow(
        source_key=key,
        display_name=str(r.display_name or ""),
        country_codes=tuple(str(c).strip() for c in (r.country_codes or ())),
        enabled=enabled,
        enabled_at=r.enabled_at,
        baseline_at=r.baseline_at,
        last_run_at=r.last_run_at,
        last_status=r.last_status,
        error_count=int(r.error_count or 0),
        cnt_24h=int(r.cnt_24h or 0),
        cnt_7d=int(r.cnt_7d or 0),
        cnt_total=int(r.cnt_total or 0),
        last_tender_at=r.last_tender_at,
        threshold_hours=threshold,
        night_batch=bool((attrs.get(key) or {}).get("night_batch", False)),
        silent_hours=hours,
        # Silence — only for an ENABLED source: a disabled one is silent on purpose.
        silent=enabled and is_silent(hours, threshold),
        registered=key in attrs,
    )


async def list_sources(db, *, now: datetime | None = None) -> list[SourceRow]:
    """All sources with inflow and silence state (for ``/admin/sources``)."""
    now = now or datetime.now(timezone.utc)
    attrs = adapter_attrs()
    rows = (await db.execute(_SOURCES_ADMIN_SQL)).all()
    sources = [_row(r, now, attrs) for r in rows]
    await _attach_reconcile(db, sources)
    return sources


async def _attach_reconcile(db, sources: list[SourceRow]) -> None:
    """The “reconciliation” column. The table may be absent (migration 0055 not deployed) —
    then the column is empty, the page lives on."""
    try:
        rows = (await db.execute(_RECONCILE_SQL)).all()
    except (
        Exception
    ) as exc:  # noqa: BLE001 — reconciliation must not bring the admin down
        logger.warning(
            "admin sources: reconciliation unavailable (%s)", type(exc).__name__
        )
        await db.rollback()
        return
    by_key = {s.source_key: s for s in sources}
    for r in rows:  # by source_key, day descending → the first row = the last day
        src = by_key.get(str(r.source_key))
        if src is None:
            continue
        item = {
            "day": r.day,
            "source_count": int(r.source_count),
            "our_count": int(r.our_count),
            "checked_at": r.checked_at,
        }
        if src.reconcile_last is None:
            src.reconcile_last = item
        if r.mismatch and src.reconcile_bad is None:
            src.reconcile_bad = item


async def silent_sources(db, *, now: datetime | None = None) -> list[SourceRow]:
    """Enabled sources silent for longer than their threshold — for the admin banner.

    A query failure does not bring the operator’s page down: the banner is simply not drawn.
    """
    try:
        return [s for s in await list_sources(db, now=now) if s.silent]
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin sources: silence check failed (%s)", type(exc).__name__)
        return []


@dataclass
class ToggleResult:
    ok: bool
    reason: str | None = None
    enabled: bool | None = None
    details: dict[str, Any] = field(default_factory=dict)


async def set_source_enabled(
    db,
    *,
    source_key: str,
    enable: bool,
    actor_email: str,
    ip: str | None = None,
    now: datetime | None = None,
) -> ToggleResult:
    """Enable/disable a source + an audit record (one transaction, one commit).

    Enabling — :func:`src.domain.sources.enable_source` (``enabled_at = now``,
    ``last_run_at``/``baseline_at`` reset → a silent first pass), disabling —
    :func:`src.domain.sources.disable_source` (only ``enabled=false``). A repeated
    button press in the same state is a no-op WITHOUT an audit record and WITHOUT a cursor reset
    (a double click on “Enable” would otherwise zero the ``baseline_at`` of a live source
    and make it silently swallow the stream a second time).
    """
    now = now or datetime.now(timezone.utc)
    before = (await db.execute(_SOURCE_ONE_SQL, {"source_key": source_key})).first()
    if before is None:
        return ToggleResult(ok=False, reason="not_found")
    if bool(before.enabled) == enable:
        return ToggleResult(ok=True, reason="unchanged", enabled=enable)
    if enable:
        await enable_source(db, source_key, now)
    else:
        await disable_source(db, source_key)
    details = {
        "source_key": source_key,
        "before_enabled": bool(before.enabled),
        "after_enabled": enable,
    }
    if enable:
        details["enabled_at"] = now.isoformat()
    await write_audit(
        db,
        actor=actor_email,
        action="source_enable" if enable else "source_disable",
        details=details,
        ip=ip,
    )
    await db.commit()
    logger.info(
        "admin_action=%s source_key=%s",
        "source_enable" if enable else "source_disable",
        source_key,
    )
    return ToggleResult(ok=True, enabled=enable, details=details)
