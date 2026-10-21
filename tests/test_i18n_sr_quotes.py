"""The guard of the Serbian quotes: the sr strings close with „…“, not with the English „…”.

The incident (TASK_sr_proofread_plan_names, block B1): the Serbian pair of quotes — ``„…“`` (the opening one at the bottom, the closing one at the top). In the catalog of the cabinet it was so, and 11 sr strings in ``src/api/public/i18n.py`` opened correctly (``„``), but closed with the English ``”`` — a mix of two conventions in one text.

The list of the sr strings, as in :mod:`tests.test_i18n_sr_diacritics`, is derived by traversing the structure of each catalog (the language map ``{"sr","ru","en"}`` → the ``sr`` branch → all the string leaves of any nesting), rather than being hard-coded.
"""

from __future__ import annotations

from typing import Iterable

from src import i18n_common, i18n_email
from src.api.cabinet import i18n as cabinet_i18n
from src.api.public import i18n as public_i18n
from src.api.public import legal_texts
from src.bots import i18n as bots_i18n

_LANG_KEYS = {"sr", "ru", "en"}

CATALOG_MODULES = [
    ("src.api.public.i18n", public_i18n),
    ("src.api.cabinet.i18n", cabinet_i18n),
    ("src.bots.i18n", bots_i18n),
    ("src.api.public.legal_texts", legal_texts),
    ("src.i18n_common", i18n_common),
    ("src.i18n_email", i18n_email),
]


def _collect_leaf_strings(node: object, path: str) -> list[tuple[str, str]]:
    """All the string leaves under the node, regardless of the depth/type of the container."""
    if isinstance(node, str):
        return [(path, node)]
    if isinstance(node, dict):
        out: list[tuple[str, str]] = []
        for key, value in node.items():
            out.extend(_collect_leaf_strings(value, f"{path}.{key}"))
        return out
    if isinstance(node, (list, tuple)):
        out = []
        for i, item in enumerate(node):
            out.extend(_collect_leaf_strings(item, f"{path}[{i}]"))
        return out
    return []


def _sr_strings(node: object, path: str = "") -> list[tuple[str, str]]:
    """Recursively looks for the language maps ({"sr","ru","en"}) and extracts the sr branch."""
    results: list[tuple[str, str]] = []
    if isinstance(node, dict):
        if _LANG_KEYS <= node.keys():
            results.extend(_collect_leaf_strings(node.get("sr"), f"{path}.sr"))
        else:
            for key, value in node.items():
                results.extend(_sr_strings(value, f"{path}.{key}"))
    elif isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            results.extend(_sr_strings(item, f"{path}[{i}]"))
    return results


def _catalog_dicts(module: object) -> Iterable[tuple[str, dict]]:
    for name, value in vars(module).items():
        if name.isupper() and isinstance(value, dict):
            yield name, value


def _all_sr_strings() -> list[tuple[str, str, str]]:
    """(location, path to the field, text) for each sr string in all the catalogs."""
    out: list[tuple[str, str, str]] = []
    for mod_path, module in CATALOG_MODULES:
        for catalog_name, catalog in _catalog_dicts(module):
            for field_path, text in _sr_strings(catalog):
                out.append((f"{mod_path}.{catalog_name}", field_path, text))
    return out


def test_sr_catalog_scan_is_not_empty():
    """The sanity check of the traversal itself: it does not let the guard silently check emptiness."""
    strings = _all_sr_strings()
    assert len(strings) > 500, (
        f"the traversal of the sr branches found suspiciously few strings ({len(strings)}) — "
        "it looks like the traversal of the catalogs broke and the test below checks nothing"
    )


def test_sr_strings_never_use_english_closing_quote():
    """the sr strings do not contain the English closing quote ”."""
    violations = [
        f"{location}{field_path}: {text!r}"
        for location, field_path, text in _all_sr_strings()
        if "”" in text
    ]
    assert not violations, (
        "the sr string closes with the English quote ” instead of the Serbian “:\n"
        + "\n".join(violations)
    )


def test_sr_quote_pairs_are_balanced():
    """The number of opening „ equals the number of closing “ in all the sr strings."""
    opens = 0
    closes = 0
    for _location, _field_path, text in _all_sr_strings():
        opens += text.count("„")
        closes += text.count("“")
    assert opens == closes, (
        f"the sr quotes are not balanced: „ occurs {opens} time(s), "
        f"“ — {closes} time(s)"
    )
