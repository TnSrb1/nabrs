"""Background workers of Tender Bot Serbia.

In phase 1 the workers of the core MVP pipeline live here:

- :mod:`src.workers.matching` — two-stage matching of a tender against profiles
  (stage A — SQL pre-filter §6.1, stage B — text comparison §6.2),
  plus the write to ``delivery_outbox`` (§6.4) and a stub of stage C (§6.3).
- :mod:`src.workers.routing` — the LISTEN/NOTIFY worker: listens to the channel
  ``tender_changed`` (the trigger ``notify_tender_change`` from migration 0001,
  DB_SCHEMA §5.1), on each notification runs the matching and writes the outbox.
- :mod:`src.workers.delivery` — the polling delivery worker: picks up ready rows of
  ``delivery_outbox`` (``pending``/``ai_done``) and sends them to the channels through
  the adapters :mod:`src.workers.adapters` (Telegram active; Viber/Email — stubs;
  ARCHITECTURE §8, §15, ADR-025).

The AI worker (Sprint-18) and the parser worker are separate modules in this same package;
they will appear in their sprints (ARCHITECTURE §9).
"""

from __future__ import annotations
