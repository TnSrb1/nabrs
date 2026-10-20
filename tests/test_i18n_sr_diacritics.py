"""The guard of the Serbian diacritics: the sr branches of the catalogs are not typed in the ASCII “without diacritics”.

The incident (see TASK_sr_diacritics): the showcase had the right sr strings mixed with strings without diacritics («Sta je ukljuceno» instead of «Šta je uključeno», «Poredjenje planova» instead of «Poređenje planova») — almost all in ``PLANS`` of the catalog :mod:`src.api.public.i18n`. The cause — some strings were written/edited without regard to the diacritics; by eye it cannot be caught, because nearby in the file there are plenty of correct sr strings, and the parity test of the keys (all three languages are non-empty) does not look here.

The list of the sr strings for the check is not hard-coded from anywhere: it is derived by traversing the structure of each catalog (a node with the keys ``{"sr","ru","en"}`` is the language map, we take its ``sr`` branch and recursively collect ALL the string leaves under it, of any nesting of dict/list/tuple). The same way the tables in ``tests/db/test_rls_force_invariant.py`` are derived from the emitted SQL, rather than rewritten by hand.

``_FORBIDDEN`` — a dictionary of the frequent ASCII roots → the correct form (from the diagnosis of the task). It is a predicate, not a list of concrete defects: it catches both the already found and the future regressions of the same roots. The patterns are deliberately NARROWED where the ASCII form of the root is valid as a separate word (for example «vas» — the pronoun «vas» (you), not only the typo «vaš» = “your”; «cesta» — the existing word “doroga” (road), not only the typo «česta» = “frequent”) — otherwise the guard would catch the correct text.
"""

from __future__ import annotations

import re
from typing import Iterable

from src import i18n_common, i18n_email
from src.api.cabinet import i18n as cabinet_i18n
from src.api.public import i18n as public_i18n
from src.api.public import legal_texts
from src.bots import i18n as bots_i18n

_FORBIDDEN: list[tuple[str, str]] = [
    (r"\bSta\b", "Šta"),
    (r"\bjos\b", "još"),
    (r"\bvise\b", "više"),
    (r"\bmoze\b", "može"),
    (r"\bmozete\b", "možete"),
    (r"\bresenj\w*", "rešenj…"),
    (r"\bpoce\w*", "poče…"),
    (r"\bprace\w*", "praće…"),
    (r"\bukljuc\w*", "uključ…"),
    (r"\bucestv\w*", "učestv…"),
    (r"\bucesnik\w*", "učesnik…"),
    (r"\bnaruci\w*", "naruč…"),
    (
        r"\bobavesten\w*",
        "obavešten…",
    ),  # NOT "obavest" — "obavestićemo" is correct and without š
    (r"\bkljucn\w*", "ključn…"),
    (r"\breci\b", "reči"),
    (r"\bplacan\w*", "plaćan…"),
    (r"\bplaca\b", "plaća"),
    (r"\bplacen\w*", "plaćen…"),
    (r"\bcek-", "ček-"),
    (r"\bporedjenj\w*", "poređenj…"),
    (
        r"\bcesta pitanj\w*",
        "česta pitanj…",
    ),  # narrowed: “cesta” on its own = the valid word “doroga” (road)
    (r"\bmogucn\w*", "mogućn…"),
    (r"\botkazem\b", "otkažem"),
    (r"\bmesecn\w*", "mesečn…"),
    (r"\bpriblizan\b", "približan"),
    (r"\bvazi\b", "važi"),
    (r"\bvazan\b", "važan"),
    (r"\bobjasnjenj\w*", "objašnjenj…"),
    (r"\bspecificn\w*", "specifičn…"),
    (r"\bvec\b", "već"),
    (
        r"\bVas plan\b",
        "Vaš plan",
    ),  # narrowed: “vas” on its own = the valid pronoun “vas” (you)
    (r"\bgodisn\w*", "godišn…"),
]
_COMPILED = [(re.compile(pattern, re.IGNORECASE), hint) for pattern, hint in _FORBIDDEN]

_LANG_KEYS = {"sr", "ru", "en"}

CATALOG_MODULES = [
    ("src.api.public.i18n", public_i18n),
    ("src.api.cabinet.i18n", cabinet_i18n),
    ("src.bots.i18n", bots_i18n),
    ("src.api.public.legal_texts", legal_texts),
    # The neutral catalogs (the common lexicon bot↔cabinet↔delivery and the chrome of the letters): the sr strings
    # appear there by the same hand and in the same way as in the catalogs above.
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


def test_sr_catalogs_have_no_ascii_diacritic_typos():
    """The sr strings in all the catalogs are typed with diacritics (č ć š ž đ), not with ASCII substitutes."""
    violations = []
    for location, field_path, text in _all_sr_strings():
        for pattern, hint in _COMPILED:
            match = pattern.search(text)
            if match:
                violations.append(
                    f"{location}{field_path}: {match.group(0)!r} → {hint} "
                    f"(текст: {text!r})"
                )
    assert not violations, (
        "sr strings without the Serbian diacritics (the ASCII typos of the roots from TASK_sr_diacritics):\n"
        + "\n".join(violations)
    )
