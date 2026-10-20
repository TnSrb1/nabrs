#!/usr/bin/env python3
"""Filling in ``l_sr`` in ``cpv_codes.json`` from the portal’s labels (SELECT only).

The source — ``tenders_normalized.classifications`` of jnportal tenders: there lie the
official Serbian (Cyrillic) CPV labels, which the portal gives together with the code
(``{"scheme":"CPV","code":..,"label":..}``). For each code that has no ``l_sr`` in
``cpv_codes.json`` we take the most frequent Cyrillic label
(on divergence — ``ORDER BY count DESC, label``, see ``pick_labels``).

The lv1 sections that are absent from the portal’s data are translated by hand
(``LV1_TRANSLATIONS``) following the example of the already filled lv1 labels. For lv1 without a label
in the source we do NOT translate — the EN fallback ``l`` remains.

We write only ``l_sr`` (Cyrillic; we do not touch ``l`` and ``c``). Idempotent.

Run on production (read-only, RUNBOOK §4), from the root of the clone:

    python3 scripts/cpv_sr_from_portal.py --fetch

or with a ready TSV from psql (``code<TAB>label<TAB>freq``):

    python3 scripts/cpv_sr_from_portal.py < rows.tsv

The flag ``--dry-run`` — only printing of the statistics, no writing of the JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

# A standalone script: the neighbouring module is taken directly, without the ``scripts`` package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cpv_aggregate_sr import _read_tsv, pick_labels  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_CODES = _ROOT / "src" / "api" / "static" / "cpv_codes.json"

_CYR = re.compile("[Ѐ-ӿ]")

# A SELECT read-only: jnportal tenders with CPV labels. The same selection
# as in the docstring of cpv_aggregate_sr.py, with a source filter.
_SQL = """
WITH c AS (
  SELECT jsonb_array_elements(classifications) AS cl
  FROM tenders_normalized
  WHERE split_part(source_key,':',1)='jnportal' AND jsonb_array_length(classifications) > 0
)
SELECT cl->>'code', cl->>'label', count(*)
FROM c WHERE cl->>'scheme'='CPV' AND coalesce(cl->>'label','')<>''
GROUP BY 1,2 ORDER BY 1;
"""

# lv1 sections without a label in the portal’s data. Translation into Serbian (Cyrillic) following
# the example of the already filled lv1 in the same reference; we do not introduce new terms.
# Check: the sub-code labels of the portal (classifications of jnportal) — 14700000 “Precious metals”,
# 19500000 “Gumeni i plastični materijali”, 51500000/51700000/51800000 “Usluge instaliranja …”.
LV1_TRANSLATIONS: dict[str, str] = {
    "14000000": "Рударски производи, прости метали и сродни производи",
    "19000000": "Кожа и текстилне тканине, гумени и пластични материјали",
    "41000000": "Прикупљена и пречишћена вода",
    "51000000": "Услуге инсталирања (осим софтвера)",
    "70000000": "Услуге у вези с некретнинама",
    "75000000": "Услуге државне управе, одбране и социјалног осигурања",
    "76000000": "Услуге у индустрији нафте и гаса",
    "79000000": "Пословне услуге: право, маркетинг, саветовање, запошљавање, штампање и обезбеђење",
}


def fill_missing(
    records: list[dict],
    portal: dict[str, str],
    lv1_translations: dict[str, str],
) -> tuple[list[dict], Counter, list[str]]:
    """Return the new records with ``l_sr`` and the statistics.

    Priority order: the portal label → the translation of lv1 → no ``l_sr``.
    An already filled ``l_sr`` is not overwritten. Returns
    ``(records, a counter of the filled by lv, the list of lv1 without a label)``.
    """
    filled: Counter = Counter()
    lv1_left: list[str] = []
    out: list[dict] = []
    for rec in records:
        new = dict(rec)
        code = new.get("c")
        if not new.get("l_sr"):
            sr = portal.get(code) or (
                lv1_translations.get(code) if new.get("lv") == 1 else None
            )
            if sr:
                # l_sr goes before the English l (the key order as in the file)
                ordered: dict = {}
                for k, v in new.items():
                    if k == "l":
                        ordered["l_sr"] = sr
                    ordered[k] = v
                ordered.setdefault("l_sr", sr)
                new = ordered
                filled[new["lv"]] += 1
        if new.get("lv") == 1 and not new.get("l_sr"):
            lv1_left.append(code)
        out.append(new)
    return out, filled, lv1_left


def portal_snapshot(tsv_lines) -> dict[str, str]:
    """TSV ``code<TAB>label<TAB>freq`` → ``{code: the most frequent Cyrillic label}``."""
    rows = [r for r in _read_tsv(tsv_lines) if _CYR.search(r[1])]
    return pick_labels(rows)


def _fetch_tsv() -> list[str]:
    """Run ``_SQL`` in the production DB container; timeout — 5 min, no writing."""
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "deploy-postgres-1",
            "psql",
            "-U",
            "tendersrb",
            "-d",
            "tendersrb",
            "-t",
            "-A",
            "-F\t",
            "-c",
            _SQL,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return proc.stdout.splitlines()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--fetch", action="store_true", help="take the data from the production DB (SELECT)"
    )
    ap.add_argument("--dry-run", action="store_true", help="do not write the JSON")
    args = ap.parse_args()

    lines = _fetch_tsv() if args.fetch else sys.stdin.read().splitlines()
    portal = portal_snapshot(lines)
    records = json.loads(_CODES.read_text(encoding="utf-8"))
    enriched, filled, lv1_left = fill_missing(records, portal, LV1_TRANSLATIONS)

    total = sum(filled.values())
    print(f"[cpv_sr_from_portal] portal labels: {len(portal)}; filled: {total}")
    for lv in sorted(filled):
        print(f"  lv{lv}: +{filled[lv]}")
    print(f"[cpv_sr_from_portal] lv1 without l_sr: {len(lv1_left)} {lv1_left}")
    if args.dry_run:
        return
    _CODES.write_text(
        json.dumps(enriched, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
