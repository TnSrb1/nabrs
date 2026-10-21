# v2026.10-snapshot

Published 21 October 2026. A snapshot of the private TenderSRB repository (see [`SHOWCASE_SOURCE`](SHOWCASE_SOURCE) for the exact source commit); no git history, refreshed by export.

## What is in this snapshot

- **The application as it runs in production:** web app and public catalogue (`src/api/`), Telegram bot (`src/bots/`), the worker processes (`src/workers/`), the AI layer (`src/ai/`), the database layer and 58 migrations (`src/db/`, `alembic/`).
- **Documentation written for readers:**
  - [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): bird's-eye view, code map and eleven invariants, each tied to the code and the test that hold it;
  - [`docs/AI_PIPELINE.md`](docs/AI_PIPELINE.md): the AI path of a tender, from documents to the usage ledger;
  - [`docs/AGENTIC_DELIVERY.md`](docs/AGENTIC_DELIVERY.md): how the product is built with coordinator and worker agents under a human owner;
  - [`docs/rules/`](docs/rules/): the written rules for the database, money and AI layers, and interface languages.
- **Decision records:** 27 ADRs in [`docs/DECISIONS.md`](docs/DECISIONS.md). The ones behind the architecture document: [ADR-001](docs/DECISIONS.md) (one shared bot, many tenants), [ADR-021](docs/DECISIONS.md) (source adapter contract), [ADR-022](docs/DECISIONS.md) (two-level tender model), [ADR-023](docs/DECISIONS.md) (profile and two-stage routing), [ADR-024](docs/DECISIONS.md) (three AI levels, caching, plan gate), [ADR-025](docs/DECISIONS.md) (outbox delivery).
- **Visuals:** animated architecture, AI pipeline and delivery diagrams; product and AI-analysis screenshots.
- **Tests:** the suite runs with `python3 -m pytest -q`; tests that need unpublished files are skipped by a generated `conftest.py`.

## What is not in this snapshot

- Connectors to procurement portals (the adapter contract and a demo adapter are included).
- Deployment files, server runbooks, operational state and internal working notes.
- Seven decision records that contain operational or connector detail.
- Some internal rule files that are specific to the interface layer, matching and the test suite.

## Changes since the previous snapshot

- Added the architecture document with invariants.
- Added `docs/rules/` (three rule files).
- README: three badges instead of thirteen, the AI verdict screenshot at the top, a Limitations section.

## Licence

Proprietary, published for reading and evaluation; see [`LICENSE`](LICENSE).
