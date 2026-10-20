"""SourceAdapter — the single interface of tender sources (ADR-021).

The conceptual contract — docs/ARCHITECTURE.md §10.1 (class attributes,
methods fetch_batch / build_source_uid / normalize / fetch_documents),
§10.2 (the source registry via @register_source / get_source).
Target normalization schema — docs/DB_SCHEMA.md §2.10 tenders_normalized.

The module makes no network requests and does not import the DB layer: ``normalize``
must be pure and testable with snapshots (ARCHITECTURE §10.1).
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.parsers.capabilities import SourceCapabilities


# --------------------------------------------------------------------------- #
#  Broken pass
# --------------------------------------------------------------------------- #
class FetchAborted(Exception):
    """``fetch_batch`` did not bring the pass to the end ⇒ the source cursor must NOT move.

    A silent ``return`` from ``fetch_batch`` is indistinguishable for ingest from “the source gave
    everything”: the pass is marked ``ok``, ``last_run_at = now`` — and the unread part will
    NEVER come back (#599). Therefore any failure (network, HTTP, an unexpected response,
    the markup is gone, robots.txt not read, bot protection) raises an exception;
    ingest counts the pass as broken (``failed=True``) and retries it from the previous
    ``since``. A separate type — for the name in the alert (``fail_reason``) and to
    distinguish a deliberate stop of the adapter from a bug in its code.
    """


# --------------------------------------------------------------------------- #
#  Data carriers
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RawTender:
    """Raw tender of the source before normalization.

    Stored in ``tenders_raw`` (DB_SCHEMA §2.9): ``payload`` → ``raw_payload``,
    ``country_code`` → ``country_code``. ``source_key`` — the adapter’s.
    """

    source_key: str
    payload: dict[str, Any]
    country_code: str | None = None
    fetched_at: datetime | None = None


@dataclass(slots=True)
class NormalizedTender:
    """Canonicalised record — fields per DB_SCHEMA §2.10 tenders_normalized.

    The fields are nullable where the schema says so; ``classifications`` / ``documents`` —
    JSONB-compatible python structures (list[dict]); ``regions`` — list[str].
    ``cluster_key`` is deliberately not filled by the adapter (NULL in v1) — the calculation of
    clusters is a separate step in Sprint-12 (src/dedup/, DB_SCHEMA §7).
    """

    source_key: str
    source_uid: str
    country_code: str | None = None
    source_url: str | None = None

    # Content
    title: str | None = None
    title_normalized: str | None = None
    description: str | None = None
    description_normalized: str | None = None

    # Customer
    customer_name: str | None = None
    customer_tax_id: str | None = None
    customer_region_code: str | None = None

    # Finance
    amount_original: float | None = None
    currency_original: str | None = None
    amount_eur: float | None = None
    is_amount_estimated: bool | None = None

    # Timing
    published_at: datetime | None = None
    deadline_at: datetime | None = None
    opening_at: datetime | None = None

    # Categorization
    classifications: list[dict[str, str]] = field(default_factory=list)
    procedure_type: str | None = None
    contract_type: str | None = None
    language: str | None = None
    regions: list[str] = field(default_factory=list)

    # Status
    status: str | None = None

    # Cluster (NULL in v1 — the calculation in Sprint-12, src/dedup/, DB_SCHEMA §7)
    cluster_key: str | None = None

    # Documents (metadata only, ADR-005 — we do not download files)
    documents: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
#  Adapter contract
# --------------------------------------------------------------------------- #
class SourceAdapter(abc.ABC):
    """Base class of a source adapter (ARCHITECTURE §10.1).

    Subclasses override the class attributes and implement 3 mandatory methods
    (``fetch_batch``, ``build_source_uid``, ``normalize``); ``fetch_documents``
    returns an empty list by default.
    """

    # --- class attributes (ARCHITECTURE §10.1) ---
    source_key: str
    source_name: str
    base_url: str
    country_codes: list[str]
    request_rate: float  # req/sec for the token bucket (ARCHITECTURE §10.3)
    default_polling_interval: int  # sec; override via sources.polling_interval_sec
    supported_languages: list[str]
    #: What the client gets from the source (:mod:`src.parsers.capabilities`) —
    #: the ``/izvori`` showcase and the profile form. There is deliberately no default value:
    #: an adapter without a declaration is caught by ``tests/parsers/test_source_capabilities.py``.
    capabilities: "SourceCapabilities"

    # --- enabling a source without a backfill (SRC-0a, owner decisions 30.09.2026) ---
    #: How much earlier than ``sources.enabled_at`` to start the FIRST pass. The cursor
    #: of the first pass = ``enabled_at - initial_lookback``; ``since=None``
    #: (“the whole archive”) is never passed by ingest. Greater than zero — only if
    #: the source publishes with a delay and without a margin would lose fresh notices.
    initial_lookback: timedelta = timedelta(0)
    #: Insert only the actual: a tender new to us with status
    #: closed/awarded/cancelled or with an expired deadline is not written at all (this is
    #: a skip BY POLICY, not a failure — the cursor may move).
    insert_only_actual: bool = True
    #: The alert threshold for “the source is silent” (hours); ``None`` — the common
    #: ``src.workers.health.silence_threshold_hours()``.
    silence_threshold_hours: float | None = None
    #: The source publishes new items in ONE nightly batch (jnportal, measured 06.08):
    #: only to it do the “night window missed” thresholds apply on
    #: ``/admin/health``; for the others — their ``silence_threshold_hours``.
    night_batch: bool = False
    #: The zone in which ``tenders_normalized.published_at`` means the “publication day”
    #: of the source (reconciliation M2): jnportal stores ``PublishDate`` as is (UTC), TED —
    #: midnight of the local date, converted to UTC (→ the day is counted in Belgrade time).
    reconcile_tz: str = "UTC"

    @abc.abstractmethod
    def fetch_batch(self, since: datetime | None) -> AsyncIterator[RawTender]:
        """Asynchronously yields raw tenders from the moment ``since``.

        Implemented as ``async def`` with ``yield`` (an async generator). The adapter
        paginates itself and decides where to stop. Ingest always passes a moment
        (``last_run_at`` or ``enabled_at - initial_lookback`` on the first
        pass, SRC-0a); ``None`` in the signature remained for manual calls and
        adapter tests, the worker does not send it. Each ``RawTender`` — with
        ``country_code``.

        ‼️ A normal return = “the pass is complete”, the cursor will move to ``now``. A failure —
        only by exception (:exc:`FetchAborted`), not by a silent ``return`` (#599).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def build_source_uid(self, raw: RawTender) -> str:
        """Stable ID of the tender in the source site.

        It does not change for the same tender even when the content changes —
        the key ``UNIQUE (source_key, source_uid)`` (ARCHITECTURE §10.5).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def normalize(self, raw: RawTender) -> NormalizedTender:
        """Raw object → ``NormalizedTender``. A pure function: no network and no DB."""
        raise NotImplementedError

    def customer_name_variants(self, payload: dict[str, Any]) -> list[str]:
        """All customer names from the raw payload (all languages of the source).

        For cross-source dedup (:mod:`src.dedup.cross_source`): a place name is
        searched by ALL variants, while ``customer_name`` stores one language (TED:
        “City of Šabac” in eng next to “Gradska uprava Grada Šapca”). By
        default empty — ``customer_name`` is enough. Pure: no network and no DB.
        """
        return []

    async def count_published_on(self, day: date) -> int | None:
        """How many notices the source ITSELF counted for the publication day ``day``.

        Reconciliation M2 (``src/workers/source_reconcile.py``): the number is compared with
        ``tenders_normalized`` for the same day. By default ``None`` — the source is not
        reconciled (a skip without an alert). The implementation counts with the SAME filter as
        ``fetch_batch`` (form types/countries), in one query without downloading the records;
        a failure — by exception, not by zero. A new source is enabled with this method
        (``docs/development/SOURCE_MONITORING_2026-10-15.md``, M5).
        """
        return None

    async def fetch_documents(self, tender: NormalizedTender) -> list[dict[str, Any]]:
        """Document metadata (konkursna dokumentacija).

        Empty by default. The file contents are not stored (ADR-005), only
        the metadata ``{name, url, type, size}``.
        """
        return []


# --------------------------------------------------------------------------- #
#  Registry (ARCHITECTURE §10.2)
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, type[SourceAdapter]] = {}


def register_source(cls: type[SourceAdapter]) -> type[SourceAdapter]:
    """Decorator-registry: registers the adapter by its ``source_key``.

    Applied at application startup (ARCHITECTURE §10.2).
    """
    key = getattr(cls, "source_key", None)
    if not key:
        raise ValueError(f"{cls.__name__} must define a non-empty source_key")
    if key in _REGISTRY and _REGISTRY[key] is not cls:
        raise ValueError(f"source_key {key!r} already registered")
    _REGISTRY[key] = cls
    return cls


def get_source(key: str) -> type[SourceAdapter]:
    """Adapter class by ``source_key``; KeyError if not registered."""
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise KeyError(f"no source adapter registered for {key!r}") from exc


def registered_sources() -> dict[str, type[SourceAdapter]]:
    """Snapshot of the registry (for diagnostics / tests)."""
    return dict(_REGISTRY)
