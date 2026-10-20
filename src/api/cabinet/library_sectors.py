"""One showcase of templates by industry (owner complaint 01.10.2026: “all over the place, in one heap”).

Before this module, /library showed two lists in a row — 7 vertical packages and 16 ``template_library`` templates in 11 ``activity_kind`` groups (8 groups with a single template each) — and the groups were sorted by the RUSSIAN column, while the headers were shown translated, so in sr the order looked random.

Here — a dictionary “industry → packages + activity kinds” (data-as-code, no migration) with an EXPLICIT order. An industry shows the package first (“the whole industry”), then the narrow templates by descending “≈N for 30 days” figure. The order does not depend on the interface language: the keys are the package slug and the ORIGINAL (Russian) ``activity_kind`` column, not its translation.

The industry order is by stream volume: production measurement 01.10.2026 (card figures for 30 days, package + template sum; ``docs/reports/library_showcase_probe.py``): IT ≈166, construction ≈126, medicine ≈75, goods supply ≈63, energy ≈41, cleaning ≈19, security ≈14.

An activity kind that is not in the dictionary (a user template of EPIK 5 with ``activity_kind IS NULL``, or a new kind from the admin) falls into “Other” at the end — the template is not lost from the showcase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from src.domain.text_norm import fold

#: Target industry for activity kinds outside the dictionary.
OTHER = "other"


@dataclass(frozen=True)
class Sector:
    slug: str  # anchor #sector-<slug> and i18n key lib_sector_<slug>
    packages: tuple[str, ...]  # vertical_packages slugs, in display order
    kinds: tuple[str, ...]  # ORIGINAL (ru) values of template_library.activity_kind


# Order = the industry order on the showcase (see the module docstring).
# A group with a single template is never separate: “Telecom” lives in IT,
# “Electrical” lives in energy, and transport / furniture / stationery / food are one
# industry “goods supply”.
SECTORS: tuple[Sector, ...] = (
    Sector("it", ("it", "remote"), ("IT и технологии", "Связь и телеком")),
    Sector("construction", ("gradjevina",), ("Строительство",)),
    Sector("medical", ("medoprema",), ("Медицина и фармацевтика",)),
    Sector(
        "supplies",
        (),
        (
            "Транспорт",
            "Продукты питания",
            "Канцелярия и расходники",
            "Мебель и интерьер",
        ),
    ),
    Sector("energy", ("elenergija",), ("Электрика",)),
    Sector("cleaning", ("ciscenje",), ("Клининг и уборка",)),
    Sector("security", ("obezbedjenje",), ("Безопасность",)),
)

_SECTOR_OF_KIND: dict[str, str] = {k: s.slug for s in SECTORS for k in s.kinds}
_SECTOR_OF_PACKAGE: dict[str, str] = {p: s.slug for s in SECTORS for p in s.packages}


def sector_of_kind(activity_kind: str | None) -> str:
    """The industry of a template by the ORIGINAL ``activity_kind``; outside the dictionary — :data:`OTHER`."""
    return _SECTOR_OF_KIND.get(activity_kind or "", OTHER)


def sector_of_package(slug: str) -> str:
    """The industry of a package; a package outside the dictionary — :data:`OTHER` (not lost)."""
    return _SECTOR_OF_PACKAGE.get(slug, OTHER)


def card_terms(keywords_include: Any) -> list[str]:
    """Flat list of OR-group terms without duplicates (card tags)."""
    out: list[str] = []
    for group in keywords_include or []:
        for term in (group or {}).get("all_of") or []:
            if term not in out:
                out.append(term)
    return out


def _sort_count(card: dict) -> int:
    count = card.get("count")
    return count if isinstance(count, int) else -1


def matches_query(card: dict, q: str) -> bool:
    """Showcase search: substring of the folded query in the name / description / words.

    Folding is the shared :func:`src.domain.text_norm.fold` (Cyrillic/Latin, case, diacritics), so “ciscenje” and “čišćenje” (and their Cyrillic Serbian spelling) find the same thing.
    """
    needle = fold(q).strip()
    if not needle:
        return True
    hay = " ".join(
        [card.get("name") or "", card.get("description") or ""]
        + list(card.get("terms") or [])
    )
    return needle in fold(hay)


def build_sectors(
    packages: Iterable[dict],
    templates: Iterable[dict],
    *,
    q: str = "",
) -> list[dict]:
    """Groups cards by industry in the order of :data:`SECTORS`.

    ``packages`` — dicts of ``vertical_packages.display_packages`` (+ ``count``); ``templates`` — ``template_library_service.localized_view`` (+ ``count``, ``source_kind`` — the original ru ``activity_kind``). Each card gets ``kind`` (``package``/``template``), ``key`` (slug/id) and ``terms``. Within an industry: packages in dictionary order, then templates by descending figure (no figure — at the end), ties by id: the order does not depend on the interface language. Empty industries (after the search ``q``) are not returned.
    """
    buckets: dict[str, dict[str, list[dict]]] = {
        s.slug: {"packages": [], "templates": []} for s in SECTORS
    }
    buckets[OTHER] = {"packages": [], "templates": []}

    pkg_rank = {p: i for s in SECTORS for i, p in enumerate(s.packages)}
    for pkg in packages:
        card = {
            **pkg,
            "kind": "package",
            "key": pkg["slug"],
            "terms": list(pkg.get("keywords") or []),
        }
        if matches_query(card, q):
            buckets[sector_of_package(pkg["slug"])]["packages"].append(card)
    for tpl in templates:
        card = {
            **tpl,
            "kind": "template",
            "key": str(tpl["id"]),
            "terms": card_terms(tpl.get("keywords_include")),
        }
        if matches_query(card, q):
            buckets[sector_of_kind(tpl.get("source_kind"))]["templates"].append(card)

    out: list[dict] = []
    for slug in [s.slug for s in SECTORS] + [OTHER]:
        pkgs = sorted(
            buckets[slug]["packages"], key=lambda c: pkg_rank.get(c["key"], 99)
        )
        tpls = sorted(
            buckets[slug]["templates"], key=lambda c: (-_sort_count(c), c["key"])
        )
        if pkgs or tpls:
            out.append({"slug": slug, "packages": pkgs, "templates": tpls})
    return out


# --------------------------------------------------------------------------- #
# Names of CPV codes for the template composition page
# --------------------------------------------------------------------------- #

_CPV_JSON = Path(__file__).resolve().parents[1] / "static" / "cpv_codes.json"

# The name field in the reference by interface language; then fallbacks. The reference
# is the same as the CPV widget of the form (`static/cpv_codes.json`, 9454 codes):
# `l` — EN for all, `l_sr` — for 1306, `l_ru` — for 304.
_LABEL_FIELDS: dict[str, tuple[str, ...]] = {
    "sr": ("l_sr", "l"),
    "ru": ("l_ru", "l_sr", "l"),
    "en": ("l", "l_sr"),
}


@lru_cache(maxsize=1)
def _cpv_index() -> dict[str, dict]:
    try:
        rows = json.loads(_CPV_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {r["c"]: r for r in rows if isinstance(r, dict) and r.get("c")}


def cpv_rows() -> list[dict]:
    """The whole CPV reference (``{c, lv, p?, l, l_sr?, l_ru?}``), in file order."""
    return list(_cpv_index().values())


def cpv_label(code: str, lang: str) -> str | None:
    """Name of the CPV code in the interface language (where available), otherwise a fallback; none — None."""
    row = _cpv_index().get(code)
    if row is None:
        return None
    for field in _LABEL_FIELDS.get(lang, ("l",)):
        if row.get(field):
            return row[field]
    return None
