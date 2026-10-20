"""The registry of source sources — the ``sources`` table as the single list (SRC-0a).

Owner decisions 30.09.2026: R1 — the list of sources is taken from the ``sources`` table
(the ``enabled`` flag), not from a hardcode of a single source; R4 — a new source does not
load its archive; R5 — a subscriber receives only what arrived AFTER their
subscription to the source. This module is a leaf: DB queries and pure functions, without
importing parsers (the cabinet and the bot must not pull in the adapters' network code).

‼️ THE SOURCE ENABLING CONTRACT (repeated by the enabling migrations SRC-0c and
the admin button SRC-0d — verbatim, without variations):

* :func:`enable_source` — ``enabled = true, enabled_at = now, last_run_at =
  NULL, baseline_at = NULL``. Any enabling, including a REPEATED one after
  disabling, goes through a SILENT first pass: the ingest cursor starts from
  ``enabled_at - adapter.initial_lookback`` (we do not pull the archive), and tenders
  seen before the end of the first pass (``first_seen_at <= baseline_at``) are
  sent to nobody (``STAGE_A_SQL``). ``baseline_at`` is set by ingest itself
  after the first successful pass. Re-enabling as a silent pass is the
  executor's default, owner did not decide it.
* :func:`disable_source` — ONLY ``enabled = false``. We do not touch the cursor and marks:
  disabling is a pause, not a reset; on enabling there is a reset anyway.

A source row appears DISABLED (ingest's autoseed for a
registered adapter without a row); only owner enables it (owner decision
30.09 item 5 — a button in the ``/admin/sources`` admin).

A profile's subscription is ``profiles.source_opt_in`` = ``{source_key: ISO moment}``,
maintained by ONE function :func:`apply_source_selection` in all writers of
``enabled_source_keys``.

The tier hook: ``sources.available_for_tiers IS NULL`` = available to all. There is NO tier
filter here (owner decision 30.09 item 1: all sources to all tiers;
do not mix the plan axes — ``.claude/rules/money-ai-tiers.md``).

The perimeter: ``sources`` is a reference table without RLS (not tenant-scoped), read by both the
role ``tendersrb_app`` (cabinet, bot) and the workers' superuser.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import text

__all__ = [
    "SourceInfo",
    "source_label_of",
    "source_label",
    "reset_source_labels_cache",
    "list_enabled_sources",
    "default_source_keys_for_new_profile",
    "validate_source_keys",
    "list_sources_for_selection",
    "enable_source",
    "disable_source",
    "apply_source_selection",
]

#: The country of “home” platforms: a new profile by default ticks only them
#: (owner decision 30.09 item 2 — a subscriber enables foreign ones themselves).
HOME_COUNTRY = "RS"


@dataclass(frozen=True)
class SourceInfo:
    """An enabled source — what the cabinet/bot sees."""

    source_key: str
    display_name: str
    country_codes: tuple[str, ...]
    available_for_tiers: tuple[str, ...] | None
    #: ``sources.enabled``. ``False`` — the source was disabled by the admin but is in
    #: the profile: shown with the note “privremeno isključen” (owner 30.09 evening
    #: item 3), it does not disappear from the profile.
    enabled: bool = True

    @property
    def label(self) -> str:
        """A human-readable name (the fallback is the key if the name is not filled in)."""
        return self.display_name or self.source_key


def source_label_of(display_name: str | None, source_key: str | None) -> str:
    """The source name for the customer: ``display_name``, the fallback is the key itself.

    The single rule for labelling a source (alerts, cards, the cabinet): the customer
    must not see the technical key ``jnportal`` while the source has a name.
    """
    return (display_name or "").strip() or (source_key or "").strip()


_ENABLED_SOURCES_SQL = text("""
    SELECT source_key, display_name, country_codes, available_for_tiers
    FROM sources
    WHERE enabled = true
    ORDER BY source_key
    """)

# Enabled + keys already in the profile (including those disabled by the admin) —
# the platform selection list in the cabinet form and in the bot (SRC-0b).
_SELECTION_SOURCES_SQL = text("""
    SELECT source_key, display_name, country_codes, available_for_tiers, enabled
    FROM sources
    WHERE enabled = true OR source_key = ANY(CAST(:keys AS text[]))
    ORDER BY source_key
    """)

_ENABLE_SQL = text("""
    UPDATE sources
    SET enabled = true,
        enabled_at = CAST(:now AS timestamptz),
        last_run_at = NULL,
        baseline_at = NULL
    WHERE source_key = CAST(:source_key AS text)
    """)

_DISABLE_SQL = text("""
    UPDATE sources
    SET enabled = false
    WHERE source_key = CAST(:source_key AS text)
    """)


def _codes(value: Any) -> tuple[str, ...]:
    # CHAR(2) from PG comes as a string without trailing spaces, but we normalize explicitly.
    return tuple(str(c).strip().upper() for c in (value or ()) if str(c).strip())


async def list_enabled_sources(session: Any) -> list[SourceInfo]:
    """Enabled sources by ``source_key`` (a deterministic order)."""
    rows = (await session.execute(_ENABLED_SOURCES_SQL)).all()
    return [
        SourceInfo(
            source_key=str(row.source_key),
            display_name=str(row.display_name or ""),
            country_codes=_codes(row.country_codes),
            available_for_tiers=(
                tuple(row.available_for_tiers)
                if row.available_for_tiers is not None
                else None
            ),
        )
        for row in rows
    ]


async def default_source_keys_for_new_profile(session: Any) -> list[str]:
    """The keys ticked on a NEW profile: enabled sources of exactly the country RS.

    ``country_codes = {'RS'}`` — only “home” sources; a multi-country
    source (TED, etc.) is enabled by the subscriber (owner 30.09 item 2). Existing
    profiles get nothing added — the function is called only on creation.
    """
    return [
        s.source_key
        for s in await list_enabled_sources(session)
        if s.country_codes == (HOME_COUNTRY,)
    ]


async def validate_source_keys(
    session: Any, keys: Iterable[str], *, keep: Iterable[str] = ()
) -> list[str]:
    """Filter the sent keys down to the permitted ones (order and duplicates — as in the form).

    Permitted: an enabled source OR a key already in the profile (``keep``) —
    a temporarily disabled source must not silently drop off the profile on
    an edit, otherwise the moment of subscription disappears with it. An unknown key
    (the form was forged by hand) is discarded.
    """
    allowed = {s.source_key for s in await list_enabled_sources(session)}
    allowed |= {str(k) for k in keep}
    out: list[str] = []
    for key in keys:
        key = str(key).strip()
        if key and key in allowed and key not in out:
            out.append(key)
    return out


async def list_sources_for_selection(
    session: Any, selected: Iterable[str] = ()
) -> list[SourceInfo]:
    """Sources for the subscriber to choose: enabled + already chosen (SRC-0b).

    The order is by ``source_key`` (deterministic: the bot addresses a button by index
    in this list). A chosen key disabled by the admin arrives with
    ``enabled=False`` — the UI marks it “privremeno isključen” and lets it be removed.
    A key that is not in the table at all is also returned (``enabled=False``,
    without a name): otherwise it would hang invisibly in the profile.
    """
    keys = [str(k) for k in selected or () if k]
    rows = (await session.execute(_SELECTION_SOURCES_SQL, {"keys": keys})).all()
    out = [
        SourceInfo(
            source_key=str(row.source_key),
            display_name=str(row.display_name or ""),
            country_codes=_codes(row.country_codes),
            available_for_tiers=(
                tuple(row.available_for_tiers)
                if row.available_for_tiers is not None
                else None
            ),
            enabled=bool(getattr(row, "enabled", True)),
        )
        for row in rows
    ]
    known = {s.source_key for s in out}
    for key in keys:
        if key not in known:
            known.add(key)
            out.append(SourceInfo(key, "", (), None, enabled=False))
    return sorted(out, key=lambda s: s.source_key)


async def enable_source(session: Any, source_key: str, now: datetime) -> None:
    """Enable a source — see “THE ENABLING CONTRACT” in the module docstring."""
    await session.execute(_ENABLE_SQL, {"source_key": source_key, "now": now})


async def disable_source(session: Any, source_key: str) -> None:
    """Disable a source: only ``enabled = false`` (a pause, not a reset)."""
    await session.execute(_DISABLE_SQL, {"source_key": source_key})


def apply_source_selection(
    profile: Any, new_keys: Sequence[str], now: datetime
) -> None:
    """Write the platform selection into the profile, maintaining ``source_opt_in``.

    * added keys get the subscription moment ``now``;
    * removed ones — the record is deleted (re-enabling = a new subscription);
    * remaining ones — NOT touched (otherwise editing the profile name would zero
      the subscription, and tenders between the old and the new moment would “vanish”).

    ``profile`` is an ORM ``Profile`` object (or anything with the attributes
    ``enabled_source_keys``/``source_opt_in``). The dict is assigned as a NEW
    object — otherwise SQLAlchemy will not see the JSONB change.
    """
    keys: list[str] = []
    for key in new_keys or ():
        key = str(key)
        if key and key not in keys:
            keys.append(key)
    old_keys = set(getattr(profile, "enabled_source_keys", None) or ())
    opt_in = dict(getattr(profile, "source_opt_in", None) or {})
    for key in list(opt_in):
        if key not in keys:
            del opt_in[key]
    for key in keys:
        if key not in old_keys:
            opt_in[key] = now.isoformat()
    profile.enabled_source_keys = keys
    profile.source_opt_in = opt_in


# --- Source label for rendering (public card, cabinet) -------------- #
# The reference table changes rarely (an admin button / a manual edit), so the map
# “key → display_name” is cached per process with a short TTL: the tender card is
# a hot public path, an extra query on every display is not needed.
_LABELS_TTL_SECONDS = 60.0
_labels_cache: tuple[float, dict[str, str]] | None = None

_ALL_LABELS_SQL = text("SELECT source_key, display_name FROM sources")


def reset_source_labels_cache() -> None:
    """Reset the label cache (tests; after editing ``sources`` in the admin)."""
    global _labels_cache
    _labels_cache = None


async def source_label(session: Any, source_key: str | None) -> str:
    """The source name by key: ``display_name`` from ``sources``, otherwise the key.

    ALL ``sources`` rows are taken (including disabled ones): the tender is already in the DB and
    must be labelled with its own name even if the source was temporarily disabled.
    An unknown key → the key itself; an empty one → an empty string.
    """
    global _labels_cache
    key = (source_key or "").strip()
    if not key:
        return ""
    now = time.monotonic()
    if _labels_cache is None or now - _labels_cache[0] > _LABELS_TTL_SECONDS:
        rows = (await session.execute(_ALL_LABELS_SQL)).all()
        _labels_cache = (
            now,
            {str(r.source_key): str(r.display_name or "") for r in rows},
        )
    return source_label_of(_labels_cache[1].get(key), key)
