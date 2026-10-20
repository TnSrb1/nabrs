#!/usr/bin/env python3
"""Enrichment of the static reference ``cpv_codes.json`` with the Serbian original + ru.

Input:
  * ``src/api/static/cpv_codes.json`` — the full EU CPV classifier (9454
    items, labels only the English ``l``).
  * ``scripts/data/cpv_sr_labels.json`` — a snapshot ``code → sr label`` from ingest
    (``cpv_aggregate_sr.py``). Serbian is the original language of the reference.
  * ``scripts/data/cpv_sr_ru.json`` — ``sr label → ru`` (an offline translation, shown
    next to the original on the ru site; we do NOT call a model at render time).

Result of the record (key order): ``{c, lv, p?, l_sr?, l, l_ru?}``.
  * ``l_sr`` — the Serbian original (primary in the UI, always, for the covered codes).
  * ``l``   — English (already was; the translation for the en site, the translation fallback).
  * ``l_ru`` — the Russian translation (secondary on the ru site; where absent the widget falls
    back to ``l``).

Idempotent: a repeated run overwrites ``l_sr``/``l_ru`` from the sources.
The display/search is on the ``cpv_widget.js`` side. The axis: labels = the original of the reference
(sr), the widget wrapper = the interface language. The pure function ``enrich`` is under test.

Run:  python3 scripts/cpv_enrich.py
"""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CODES = _ROOT / "src" / "api" / "static" / "cpv_codes.json"
_SR = _ROOT / "scripts" / "data" / "cpv_sr_labels.json"
_SR_RU = _ROOT / "scripts" / "data" / "cpv_sr_ru.json"

# The key order in the resulting record (readability of the diff + the primacy of l_sr).
_KEY_ORDER = ("c", "lv", "p", "l_sr", "l", "l_ru")


def enrich(
    records: list[dict],
    sr_map: dict[str, str],
    ru_map: dict[str, str],
) -> list[dict]:
    """Return a new list of records with the fields ``l_sr``/``l_ru``.

    ``l_sr`` — from ``sr_map`` by the code; ``l_ru`` — from ``ru_map`` by the Serbian label
    (the translation is bound to the original, not to the code — so the dictionary is reused and
    the translations of identical labels are not duplicated). Missing fields are not written —
    the widget falls back to ``l`` (English). Other keys of the record are preserved.
    """
    out: list[dict] = []
    for rec in records:
        new = dict(rec)
        code = new.get("c")
        sr = sr_map.get(code)
        if sr:
            new["l_sr"] = sr
            ru = ru_map.get(sr)
            if ru:
                new["l_ru"] = ru
            else:
                new.pop("l_ru", None)
        else:
            # a code without sr in the ingest: only the English l remains (a documented fallback)
            new.pop("l_sr", None)
            new.pop("l_ru", None)
        ordered = {k: new[k] for k in _KEY_ORDER if k in new}
        for k, v in new.items():  # for unexpected keys — do not lose them
            if k not in ordered:
                ordered[k] = v
        out.append(ordered)
    return out


def main() -> None:
    records = json.loads(_CODES.read_text(encoding="utf-8"))
    sr_map = json.loads(_SR.read_text(encoding="utf-8"))
    ru_map = json.loads(_SR_RU.read_text(encoding="utf-8"))
    enriched = enrich(records, sr_map, ru_map)
    _CODES.write_text(
        json.dumps(enriched, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    covered = sum(1 for r in enriched if r.get("l_sr"))
    ru = sum(1 for r in enriched if r.get("l_ru"))
    print(
        f"[cpv_enrich] {len(enriched)} records; l_sr={covered} "
        f"({covered * 100 // len(enriched)}%); l_ru={ru}"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
