"""The banner “enable a source in all templates” and the honest figure “you have K of N”.

The subscription to a source is a tick IN EACH template (``profiles.enabled_source_keys``). A source that appeared later is not ticked in the old templates, and there is nobody to deliver to it: the client learns about it nowhere. The banner says so directly and gives one action — enable the source in all live templates.

Rules:

* nothing is switched on silently: the write happens only on a button press (or in the bot via a callback), through :func:`src.domain.sources.apply_source_selection`;
* a repeat press is idempotent: a template where the source is already ticked is NOT touched, and its ``source_opt_in`` (the subscription moment) is not overwritten;
* “seen / not now” — without a migration: a cookie with the moment of refusal. The banner is shown again only for sources enabled AFTER that moment.

The module is the cabinet's service layer: it reads ``sources`` (without RLS) and the tenant's templates (through :func:`profile_service.list_profiles`, which sets ``set_tenant`` itself). The pure functions (``plan_enable``, ``pending_sources``, ``coverage``) are separated from the DB, so that they can be checked without PG.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import text

from src.domain.sources import (
    SourceInfo,
    apply_source_selection,
    list_enabled_sources,
)
from src.parsers.capabilities import capabilities_by_key, short_keys

__all__ = [
    "SEEN_COOKIE",
    "SEEN_COOKIE_MAX_AGE",
    "PLAN_SOURCE_KEYS",
    "PendingSource",
    "parse_seen",
    "coverage",
    "is_live",
    "live_profiles",
    "pending_sources",
    "plan_enable",
    "add_source",
    "load_enabled_sources",
    "dashboard_banner",
    "enable_everywhere",
]

#: Cookie “seen / not now”: an ISO moment of refusal. No DB, like ``news_seen``.
SEEN_COOKIE = "sources_banner_seen"
SEEN_COOKIE_MAX_AGE = 365 * 24 * 3600

#: Sources that deliver procurement PLANS (not tender notices): in the banner they get
#: a “plan, not yet a tender” mark. Currently there are no such platforms — all the
#: connected sources publish notices; the set is kept ready in advance, so that
#: a plan source does not land in the banner without the mark.
PLAN_SOURCE_KEYS: frozenset[str] = frozenset()

#: Enabled sources with the moment of enabling (``sources.enabled_at``). Read
#: by the same query as the registry, but ``enabled_at`` in ``SourceInfo`` is not needed
#: by the rest of the code, hence a separate SELECT.
_ENABLED_AT_SQL = text("""
    SELECT source_key, enabled_at
    FROM sources
    WHERE enabled = true
    """)


@dataclass(frozen=True)
class PendingSource:
    """A source that the banner offers to enable in all templates."""

    source_key: str
    label: str
    #: Country codes (``RS``, ``BA``…): the template renders the country label from the catalog.
    countries: tuple[str, ...]
    #: Keys of the short capability labels (``cap_s_*``) — what the source gives.
    caps_short: tuple[str, ...]
    enabled_at: datetime | None
    #: How many live templates lack it.
    missing_in: int
    #: ``True`` — a procurement plan, not a tender (a mark in the banner).
    is_plan: bool
    #: ``True`` — among the templates where it is absent there is an “all tenders” one (match_all).
    has_match_all: bool


def parse_seen(value: str | None) -> datetime | None:
    """Parses the “seen” cookie: an ISO moment with a timezone; garbage → ``None``."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _keys(profile: Any) -> set[str]:
    return set(getattr(profile, "enabled_source_keys", None) or ())


def is_live(profile: Any) -> bool:
    """A working template (``is_active`` is true). ``NULL`` — disabled: matching takes ``is_active = true``.

    The banner neither touches nor counts a disabled template: neither in “K of N”, nor in “not enabled in M of N”, nor in the write “in all templates”.
    """
    return getattr(profile, "is_active", True) is True


def live_profiles(profiles: Iterable[Any]) -> list[Any]:
    """Only the working templates — the single selection by which the banner counts."""
    return [p for p in profiles if is_live(p)]


def coverage(profiles: Iterable[Any], enabled_keys: Iterable[str]) -> tuple[int, int]:
    """``(K, N)`` for the figure “you have K of N”.

    K — the union of ``enabled_source_keys`` of the live templates, intersected with the enabled sources (a source switched off by the admin or deleted, but sitting in a template, does not make K bigger than N). N — the number of enabled sources.
    """
    enabled = set(enabled_keys)
    used: set[str] = set()
    for profile in live_profiles(profiles):
        used |= _keys(profile)
    return len(used & enabled), len(enabled)


def pending_sources(
    entries: Sequence[tuple[SourceInfo, datetime | None]],
    profiles: Sequence[Any],
    seen_at: datetime | None,
) -> list[PendingSource]:
    """Sources for the banner: missing from at least one live template AND enabled after ``seen_at``.

    ``seen_at`` ``None`` (the banner was never closed) — we show everything that is missing. The most recently enabled source goes first (the banner shows the first one).
    """
    live = live_profiles(profiles)
    if not live:
        return []
    out: list[PendingSource] = []
    caps = capabilities_by_key()
    for source, enabled_at in entries:
        missing = [p for p in live if source.source_key not in _keys(p)]
        if not missing:
            continue
        # The “not now” refusal closes the sources enabled before it. A source
        # without an enabling moment is not shown after the refusal.
        if seen_at is not None and (enabled_at is None or enabled_at <= seen_at):
            continue
        out.append(
            PendingSource(
                source_key=source.source_key,
                label=source.label,
                countries=source.country_codes,
                caps_short=tuple(
                    short_keys(caps[source.source_key])
                    if source.source_key in caps
                    else []
                ),
                enabled_at=enabled_at,
                missing_in=len(missing),
                is_plan=source.source_key in PLAN_SOURCE_KEYS,
                has_match_all=any(
                    bool(getattr(p, "match_all", False)) for p in missing
                ),
            )
        )
    out.sort(
        key=lambda s: s.enabled_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return out


def add_source(profile: Any, source_key: str, now: datetime) -> bool:
    """Adds a source to ONE template with the subscription moment ``now``. ``True`` — changed.

    The single write for the banner (“in all templates”) and for the “+ source” button on the template card: the subscription moment is written by the same :func:`apply_source_selection` as the form and the creation. A template where the source is already ticked is NOT touched — its previous moment remains (otherwise a repeat press would move the subscription and “eat” the tenders between the old and the new moment).
    """
    current = list(getattr(profile, "enabled_source_keys", None) or [])
    if source_key in current:
        return False
    apply_source_selection(profile, current + [source_key], now)
    return True


def plan_enable(
    profiles: Sequence[Any],
    source_key: str,
    enabled_keys: Iterable[str],
    now: datetime,
) -> list[Any]:
    """Adds the source to every template where it is absent. Returns the changed templates.

    The source must be enabled in the registry (as in the form); otherwise nothing is changed. A template where the source is already ticked is skipped entirely — its ``source_opt_in`` (the subscription moment) stays the same. A disabled template (``is_active`` false) is not touched at all.
    """
    if source_key not in set(enabled_keys):
        return []
    changed: list[Any] = []
    for profile in live_profiles(profiles):
        if add_source(profile, source_key, now):
            changed.append(profile)
    return changed


async def load_enabled_sources(
    session: Any,
) -> list[tuple[SourceInfo, datetime | None]]:
    """Enabled sources with ``enabled_at`` (the same set as the registry ``list_enabled_sources``)."""
    enabled_at = {
        # getattr — as in the registry (``list_sources_for_selection``): the fake session
        # of the tests returns rows without the column; in production the column is always there.
        str(row.source_key): getattr(row, "enabled_at", None)
        for row in (await session.execute(_ENABLED_AT_SQL)).all()
    }
    return [
        (source, enabled_at.get(source.source_key))
        for source in await list_enabled_sources(session)
    ]


async def dashboard_banner(
    session: Any,
    profiles: Sequence[Any],
    seen_at: datetime | None,
    entries: Sequence[tuple[SourceInfo, datetime | None]] | None = None,
) -> PendingSource | None:
    """The most recent source for the dashboard banner, or ``None``."""
    if entries is None:
        entries = await load_enabled_sources(session)
    pending = pending_sources(entries, profiles, seen_at)
    return pending[0] if pending else None


async def enable_everywhere(
    session: Any, tenant_id: str, source_key: str, now: datetime
) -> tuple[int, int]:
    """Enables a source in all live templates of the tenant and commits.

    Returns ``(live templates, templates with the source)`` — for the phrase “enabled in K templates”. The count is taken BEFORE the commit: after commit the ORM attributes expire, and re-reading them in an async session does not work. The commit happens only if something changed.
    """
    # Lazy import: profile_service pulls in the channel code (email / SMTP). The bot imports
    # this module at start-up, and the env-contract test counts only module-level imports.
    from src.api.cabinet import profile_service as svc

    profiles = live_profiles(await svc.list_profiles(session, tenant_id))
    enabled = [s.source_key for s in await list_enabled_sources(session)]
    changed = plan_enable(profiles, source_key, enabled, now)
    with_key = sum(1 for p in profiles if source_key in _keys(p))
    if changed:
        await session.commit()
    return len(profiles), with_key
