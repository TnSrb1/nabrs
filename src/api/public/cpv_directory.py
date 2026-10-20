"""The CPV directory for ``/cpv-kodovi``: the tree of sections → groups, and the labels by language.

The source is ``static/cpv_codes.json`` (9 454 codes, the same file the client-side search reads). A section = ``XX000000``, a group = ``XXX00000`` (the root of three digits). The label in the page language: sr — ``l_sr`` in Latin (``cyr_to_lat``), ru — ``l_ru``; if there is no translation — the English ``l`` with a mark (``fallback=True``). The client-side ``cpv_directory.js`` repeats THIS choice — change them together.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.api.cabinet.library_sectors import cpv_rows
from src.domain.text_norm import cyr_to_lat_strict


@dataclass(frozen=True)
class Label:
    text: str
    fallback: bool  # True — the English label instead of the page language


def label_of(row: dict, lang: str) -> Label:
    """The code’s label in the page language; no translation — EN with a mark."""
    if lang == "sr" and row.get("l_sr"):
        return Label(cyr_to_lat_strict(row["l_sr"]), False)
    if lang == "ru" and row.get("l_ru"):
        return Label(row["l_ru"], False)
    return Label(row.get("l", ""), lang != "en")


def is_division(code: str) -> bool:
    return code[2:] == "000000"


def is_group(code: str) -> bool:
    return code[3:] == "00000" and not is_division(code)


def tree(lang: str) -> list[dict]:
    """``[{code, label, fallback, groups: [{code3, label, fallback}]}]`` by code."""
    divisions: dict[str, dict] = {}
    groups: list[dict] = []
    for row in cpv_rows():
        code = row["c"]
        lab = label_of(row, lang)
        if is_division(code):
            divisions[code[:2]] = {
                "code": code[:2],
                "label": lab.text,
                "fallback": lab.fallback,
                "groups": [],
            }
        elif is_group(code):
            groups.append(
                {"code": code[:3], "label": lab.text, "fallback": lab.fallback}
            )
    for g in sorted(groups, key=lambda g: g["code"]):
        if g["code"][:2] in divisions:
            divisions[g["code"][:2]]["groups"].append(g)
    return [divisions[k] for k in sorted(divisions)]
