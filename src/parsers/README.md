# src/parsers — Source adapters (Python contract)

Interface contract for tender-platform adapters. Concept —
the earlier architecture notes (not published) §10 (ADR-021). Target schema — `docs/DB_SCHEMA.md`
§2.9 (`tenders_raw`) and §2.10 (`tenders_normalized`). ARCHITECTURE §10.1
states this file lands in Sprint-12 with the concrete signatures — here they
are.

Every platform is a subclass of `SourceAdapter` (`src/parsers/base.py`),
registered with `@register_source` and looked up with `get_source(key)`.

## Class attributes

```python
source_key: str                 # machine id, e.g. "jnportal"
source_name: str                # human-readable platform name
base_url: str
country_codes: list[str]        # ["RS"] for jnportal; [] / multi for INT
request_rate: float             # req/sec for the Redis token bucket
default_polling_interval: int   # seconds; overridable via sources.polling_interval_sec
supported_languages: list[str]
# SRC-0a (defaults in base.py):
initial_lookback: timedelta = timedelta(0)   # first pass cursor = enabled_at - this
insert_only_actual: bool = True              # skip NEW closed/expired tenders on insert
silence_threshold_hours: float | None = None # per-source silence alert; None = global
```

## Methods

```python
async def fetch_batch(self, since: datetime | None = None) -> AsyncIterator[RawTender]:
    """Yield raw tenders published since `since`. The ingest worker ALWAYS
    passes a moment (last_run_at, or enabled_at - initial_lookback on the
    first pass) — never None (SRC-0a: no archive backfill). Paginates
    internally, fills country_code. The only IO method."""

def build_source_uid(self, raw: RawTender) -> str:
    """Stable per-platform id. Key for UNIQUE(source_key, source_uid)."""

def normalize(self, raw: RawTender) -> NormalizedTender:
    """Pure transform raw -> NormalizedTender. NO network, NO DB
    (so it is snapshot-testable — ADR-013)."""

async def fetch_documents(self, tender: NormalizedTender) -> list[dict]:
    """Optional konkursna dokumentacija metadata. Default: [] .
    Files themselves are never stored — ADR-005."""
```

## Dataclasses

`RawTender` — one raw platform record:

```python
source_key: str
country_code: str | None
payload: dict[str, Any]   # verbatim row/object -> tenders_raw.raw_payload
fetched_at: datetime
source_url: str | None = None
```

`NormalizedTender` — canonicalized tender; fields mirror
`tenders_normalized` (§2.10) minus DB-managed columns (`id`, FKs,
`description_embedding`, the `is_archived` GENERATED column, audit
timestamps). `classifications` is a list of `{"system_code", "code"}` dicts;
`regions` a list of ISO 3166-2 codes; `cluster_key` is `None` in v1.

## Registry (ARCHITECTURE §10.2)

```python
@register_source
class MyAdapter(SourceAdapter):
    source_key = "my_source"
    ...

adapter = get_source("my_source")   # instantiates the registered class
```

Importing the adapter module runs the decorator. Since SRC-0a the package
`__init__.py` imports NO adapter: the explicit list lives in
`src/parsers/registry.py::load_all_adapters()` (called by `ingest.main()`).
The registry answers "is there code for this platform"; whether to ingest it
is decided by the `sources` table (below).

## How to enable a source (SRC-0a, owner decisions 30.09.2026)

Registry → table → flags. Loads exactly `sources.enabled = true` ∩ registry,
re-read on every pass (without a restart).

1. **Code.** An adapter + one import line in `src/parsers/registry.py`.
   At ingest startup the `sources` row is auto-seeded (`display_name`, `base_url`,
   `country_codes`, `polling_interval_sec` — from the class attributes) —
   **DISABLED** (`enabled=false`). An existing row is not touched.
2. **Enabling — only the owner** (the `/admin/sources` button, SRC-0d) through
   `src/domain/sources.py::enable_source`: `enabled=true, enabled_at=now,
   last_run_at=NULL, baseline_at=NULL`. Any enabling, including a repeated one, —
   starts from scratch. Disabling (`disable_source`) — only `enabled=false`.
3. **First pass — no archive and silent.** The cursor = `enabled_at -
   initial_lookback` (default 0: only what was published from the moment of
   enabling). With `insert_only_actual=True` a new tender with status
   `closed/awarded/cancelled` or an expired deadline is not written at all (a skip BY POLICY, the cursor moves — this is not a failure). After the first successful pass
   ingest sets `baseline_at`; tenders with `first_seen_at <= baseline_at` are not
   sent to anybody by `STAGE_A_SQL`.
4. **Subscriber — only new ones.** A profile receives a source’s tender only if it came AFTER the source was enabled in the profile (`profiles.source_opt_in`,
   fallback `created_at`); written by one function `apply_source_selection`.
   A new profile by default ticks the enabled sources with
   `country_codes = {RS}`; foreign subscribers enable them themselves.
5. **Silence** is measured per each enabled source
   (the adapter’s `silence_threshold_hours` or the common threshold).
