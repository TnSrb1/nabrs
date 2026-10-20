"""The guard: the chrome does not call the client by the internal name of a tier instead of the showcase one.

The incident (TASK_sr_proofread_plan_names, block A): 9 gate lines said “available from the Starter plan” — such a plan cannot be bought, `starter` is legacy and hidden from the showcase (see :mod:`src.plans.catalog`, the comment at ``CATALOG``). Another 2 lines showed the client the internal name ``Lite`` instead of the showcase ``Free``. The defect was not in the gating (who can do what) — the user got exactly what the tier entitles, — but in the TEXT explaining the boundary: it called to a non-existent product.

The list of the internal names is NOT hard-coded: it is the difference between the internal tiers (:data:`src.domain.tiers.TIER_ORDER` — the SSOT, the same list is used by ``PROFILE_LIMITS``/``_TIER_LIMITS``) and the public slugs of the showcase (:data:`src.plans.catalog.CATALOG`, the field ``key``). A new internal tier without a public equivalent will appear (or the showcase will lose a slug) — the test itself will pick up the new name and turn red if it surfaces in the text of the chrome.

All the language strings (sr/ru/en) in the chrome catalogs addressed to the client are checked: the cabinet, the public showcase, the common lexicon bot↔cabinet, the letters. The admin (operator-facing) is out of scope — as everywhere in the i18n of this repository.
"""

from __future__ import annotations

import re
from typing import Iterable

from src import i18n_common, i18n_email
from src.api.cabinet import i18n as cabinet_i18n
from src.api.public import i18n as public_i18n
from src.domain.tiers import TIER_ORDER
from src.plans.catalog import CATALOG

CATALOG_MODULES = [
    ("src.api.cabinet.i18n", cabinet_i18n),
    ("src.api.public.i18n", public_i18n),
    ("src.i18n_common", i18n_common),
    ("src.i18n_email", i18n_email),
]


def _internal_only_tier_names() -> set[str]:
    """The internal tiers that have NO public slug on the showcase."""
    public_keys = {p.key for p in CATALOG}
    return {tier for tier in TIER_ORDER if tier not in public_keys}


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


def _catalog_dicts(module: object) -> Iterable[tuple[str, dict]]:
    for name, value in vars(module).items():
        if name.isupper() and isinstance(value, dict):
            yield name, value


def _all_strings() -> list[tuple[str, str, str]]:
    """(location, path to the field, text) for each string in all the catalogs."""
    out: list[tuple[str, str, str]] = []
    for mod_path, module in CATALOG_MODULES:
        for catalog_name, catalog in _catalog_dicts(module):
            for field_path, text in _collect_leaf_strings(catalog, ""):
                out.append((f"{mod_path}.{catalog_name}", field_path, text))
    return out


def test_internal_tier_diff_is_not_empty():
    """Sanity: the difference of internal/public must not suddenly become empty."""
    assert _internal_only_tier_names() == {"lite", "starter"}, (
        "the difference TIER_ORDER \\ CATALOG.key changed — update the docstring of the test "
        "and re-check block A (the list of the tiers without a public slug)"
    )


def test_string_scan_is_not_empty():
    """The sanity check of the traversal: it does not let the guard silently check emptiness."""
    strings = _all_strings()
    assert len(strings) > 500, (
        f"the traversal of the chrome catalogs found suspiciously few strings ({len(strings)}) — "
        "it looks like the traversal broke and the test below checks nothing"
    )


def test_chrome_catalogs_never_mention_internal_tier_names():
    """No string of the chrome names the internal tier name to the client."""
    internal_names = _internal_only_tier_names()
    patterns = [
        (re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE), name)
        for name in internal_names
    ]
    violations = []
    for location, field_path, text in _all_strings():
        for pattern, name in patterns:
            if pattern.search(text):
                violations.append(
                    f"{location}{field_path} упоминает {name!r}: {text!r}"
                )
    assert not violations, (
        "the chrome names the internal tier name to the client (there is no such one on sale, "
        "see src/plans/catalog.py):\n" + "\n".join(violations)
    )
