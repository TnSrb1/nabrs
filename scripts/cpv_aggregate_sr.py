#!/usr/bin/env python3
"""Aggregator of the Serbian CPV names from ingest → a snapshot ``code → sr label``.

The original language of the CPV reference on eNabavke is **Serbian** (Cyrillic).
The Serbian word forms already lie in ``tenders_normalized.classifications`` of real
tenders: the parser ``src/parsers/jnportal/adapter.py::_parse_cpv`` puts there
``{"scheme":"CPV","code":..,"label":<Serbian>}``. This script collects from them
DISTINCT ``(code → sr label)`` and writes a snapshot with which ``cpv_enrich.py``
enriches the static reference ``src/api/static/cpv_codes.json`` with the field
``l_sr`` (see TASK t2b).

The script is **one-off**, NOT in the image (``scripts/`` is not copied into the container). The language
axis: CPV labels are always the original of the reference (sr); this is not the interface language.

Run on production (the data is there only), read-only (RUNBOOK §4):

    docker exec deploy-postgres-1 psql -U tendersrb -d tendersrb -t -A -F$'\\t' -c "
      WITH c AS (
        SELECT jsonb_array_elements(classifications) AS cl
        FROM tenders_normalized WHERE jsonb_array_length(classifications) > 0
      )
      SELECT cl->>'code', cl->>'label', count(*)
      FROM c WHERE cl->>'scheme'='CPV' AND coalesce(cl->>'label','')<>''
      GROUP BY 1,2 ORDER BY 1;" \\
      | python3 scripts/cpv_aggregate_sr.py > scripts/data/cpv_sr_labels.json

The parsing/dedup logic is pure functions (``parse_cpv_line``, ``pick_labels``):
they go under the unit test ``tests/scripts/test_cpv_aggregate.py`` without a DB.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from typing import Iterable, Iterator

# The same format as on the eNabavke card: '<8 digits> - <name>'. We keep
# our own copy (the script is standalone, it does not pull src.parsers into the image), but the meaning
# mirrors adapter._parse_cpv: 8 digits, the separator ' - ', a non-empty label.
_CPV_LINE_RE = re.compile(r"^\s*(\d{8})\s*-\s*(.+?)\s*$")


def parse_cpv_line(value: str) -> tuple[str, str] | None:
    """'<8 digits> - <Serbian name>' → (code, label) or None.

    The fallback path for sources where the CPV came as a raw string, not as a
    structure. An empty label / not 8 digits / no separator → None (a guard against
    inventing, as in adapter._parse_cpv).
    """
    if not isinstance(value, str):
        return None
    m = _CPV_LINE_RE.match(value)
    if not m:
        return None
    label = m.group(2).strip()
    if not label:
        return None
    return m.group(1), label


def pick_labels(rows: Iterable[tuple[str, str, int]]) -> dict[str, str]:
    """``(code, label, freq)`` → ``{code: label}``, the label = the most frequent one.

    One code may occur with diverging forms (typos of the source,
    edits of the cards). We take the most frequent; at equal frequency — the lexicographically
    smaller one (determinism: the snapshot must be stable between runs).
    """
    by_code: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for code, label, freq in rows:
        code = (code or "").strip()
        label = (label or "").strip()
        if not code or not label:
            continue
        by_code[code][label] += int(freq)
    out: dict[str, str] = {}
    for code, variants in by_code.items():
        # max by (frequency, reverse lexicographic order) → the frequent one, then the smaller.
        best = max(
            variants.items(), key=lambda kv: (kv[1], tuple(-ord(c) for c in kv[0]))
        )
        out[code] = best[0]
    return out


def _read_tsv(stream: Iterable[str]) -> Iterator[tuple[str, str, int]]:
    """Lines ``code<TAB>label<TAB>freq`` from psql (-t -A -F$'\\t')."""
    for line in stream:
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        code, label, freq_s = parts
        try:
            freq = int(freq_s)
        except ValueError:
            continue
        yield code, label, freq


def main() -> None:
    snapshot = pick_labels(_read_tsv(sys.stdin))
    snapshot = {k: snapshot[k] for k in sorted(snapshot)}
    json.dump(snapshot, sys.stdout, ensure_ascii=False, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    print(f"[cpv_aggregate_sr] {len(snapshot)} codes", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    main()
