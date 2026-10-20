"""The guard: each term of the onboarding showcase has Serbian forms in the dictionary.

Why (a real defect). The showcase promises the new client a ready search profile:
the templates of the library (the seed ``0016_template_library``) and the vertical packages
(:mod:`src.api.cabinet.vertical_packages`). The keywords there are in Russian; the matching of stage B finds tenders only by expanding each word with the Serbian forms
from ``keyword_dict.json`` (see :func:`src.workers.keyword_expand.expand_term`).
If there are no forms for a term — the Russian word folds into Latin, which is absent in the Serbian haystack, and the profile finds ZERO. Thus “Medical equipment” and “Food products” gave emptiness on the very first day of the client.

Why a predicate, not a list. The terms are derived FROM the sources of the showcase themselves (PACKAGES + the seed 0016), rather than being rewritten by hand: if a new template/package with an uncovered word is added, the test turns red by itself, without an edit of the list (the pitfall of handwritten enumerations — like the lost ``trial_requests`` in 0021).

The test works WITHOUT a DB (the model — ``tests/db/test_rls_force_invariant.py``): the seed is read as text and parsed via ``ast`` (bypassing alembic/PostgreSQL), the packages are imported as data-as-code, the coverage is checked by the same ``expand_term`` as the production matching — one predicate per meaning, not a copy.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from src.api.cabinet import vertical_packages as vp
from src.workers.keyword_expand import expand_term

REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED = REPO_ROOT / "alembic/versions/0016_template_library.py"


def _terms_from_groups(groups) -> set[str]:
    """A flat set of the terms from the list of OR-groups ``{"all_of": [...]}``."""
    out: set[str] = set()
    for group in groups or []:
        for term in (group or {}).get("all_of") or []:
            if isinstance(term, str) and term.strip():
                out.append(term) if False else out.add(term.strip())
    return out


def _seed_template_terms() -> dict[str, set[str]]:
    """The terms of the seed templates 0016 → {name of the template: the set of terms}.

    We read the seed as text and parse ``ast``: each template is a dict literal with the keys ``name`` and ``keywords_include`` inside ``op.bulk_insert``. So the list of templates is derived from the source, not hard-coded (otherwise it would go stale when a template is added).
    """
    tree = ast.parse(_SEED.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            k.value: v
            for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant)
        }
        if "name" not in keys or "keywords_include" not in keys:
            continue
        name = keys["name"]
        if not isinstance(name, ast.Constant):
            continue
        groups = ast.literal_eval(keys["keywords_include"])
        out[name.value] = _terms_from_groups(groups)
    return out


def _package_terms() -> dict[str, set[str]]:
    """The terms of the vertical packages → {slug: the set of terms}."""
    return {p.slug: _terms_from_groups(p.keywords_include) for p in vp.PACKAGES}


def _all_showcase_terms() -> set[str]:
    terms: set[str] = set()
    for group in (*_seed_template_terms().values(), *_package_terms().values()):
        terms |= group
    return terms


def test_seed_and_packages_parsed_nonempty():
    """The predicate really pulled the sources — otherwise the coverage checks nothing.

    If the parse of the seed breaks (op.bulk_insert moved, the dict form changed), the set of terms collapses and the main test passes in vain. We nail the lower bound: 16 public templates and 6 packages as of the time of writing.
    """
    templates = _seed_template_terms()
    packages = _package_terms()
    assert len(templates) >= 16, (
        f"{len(templates)} templates were parsed from the seed 0016 (< 16) — "
        "it looks like the form of the seed changed and the predicate went blind"
    )
    assert len(packages) >= 6, f"{len(packages)} packages recognized (< 6)"
    assert all(
        terms for terms in templates.values()
    ), "the template has an empty keywords_include after the parse — the predicate is broken"
    assert len(_all_showcase_terms()) >= 30


def test_every_showcase_term_has_serbian_forms():
    """Each term of the showcase has at least one Serbian form via ``expand_term``.

    ``expand_term(t)`` returns ``[t] + sr_forms``; length 1 = the word is not in the dictionary or its forms are empty → the production matching looks for the Russian word as it is and finds zero in the Serbian haystack. This is exactly the defect “the profile is promised, but is certain not to work”.
    """
    # The decision of the owner 30.09.2026: the terms of the packages are Serbian stems in Latin, they
    # are matched directly (the fold of text_norm on both sides) and do not
    # require a dictionary. We check only the CYRILLIC ones (the Russian seeds of 0016 before the application of
    # scripts/sr_first_templates.py): for them the dictionary is still mandatory.
    uncovered = sorted(
        t
        for t in _all_showcase_terms()
        if re.search("[А-Яа-яЁё]", t) and len(expand_term(t)) <= 1
    )
    assert not uncovered, (
        "showcase onboarding terms without Serbian forms in keyword_dict.json: "
        f"{uncovered}. The Russian word will fold into Latin, which is absent in the Serbian "
        "haystack → the profile will find 0 tenders. Add forms (in Cyrillic) to "
        "src/api/static/keyword_dict.json — pick the forms from the real data."
    )
