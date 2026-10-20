"""The guard of the declaration of the capabilities of the platforms (the showcase ``/izvori``).

The showcase promises the client the fields of the card by ``SourceAdapter.capabilities``.
A promise without the code that executes it is a lie on the public page, therefore:

* each class with ``@register_source`` (incl. the sleeping adapters, which the registry does not load) declares ``capabilities`` — the check by the AST of the sources, without
  an import: the import of a sleeping adapter would register it for the whole process of tests;
* ``documents_text`` ⇔ the adapter really receives the documents (it overrides
  ``fetch_documents`` and did not switch it off);
* ``awards`` ⇔ the adapter has ``fetch_awards`` (it is called by ``awards_ingest``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.parsers.base import SourceAdapter
from src.parsers.capabilities import (
    SourceCapabilities,
    capabilities_by_key,
    matrix_rows,
    short_keys,
)
from src.parsers.registry import load_all_adapters

PARSERS_DIR = Path(__file__).resolve().parents[2] / "src" / "parsers"


def _implements_documents(cls: type[SourceAdapter]) -> bool:
    impl = getattr(cls, "fetch_documents", None)
    if impl is SourceAdapter.fetch_documents:
        return False
    # jnportal keeps the flag attribute ``fetch_documents = True`` over the base method.
    return bool(impl)


def _registered_classes_in_source() -> dict[str, set[str]]:
    """``{file: {names of the classes with @register_source}}`` over all the modules of src/parsers."""
    found: dict[str, set[str]] = {}
    for path in sorted(PARSERS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(d, ast.Name) and d.id == "register_source"
                for d in node.decorator_list
            ):
                found.setdefault(str(path), set()).add(node.name)
    return found


def _class_assigns_capabilities(tree: ast.Module, name: str) -> bool:
    """The class (or its base in the same module) assigns ``capabilities``."""
    classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    seen: set[str] = set()
    todo = [name]
    while todo:
        current = todo.pop()
        if current in seen or current not in classes:
            continue
        seen.add(current)
        node = classes[current]
        for stmt in node.body:
            targets: list[ast.expr] = []
            if isinstance(stmt, ast.Assign):
                targets = list(stmt.targets)
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets = [stmt.target]
            if any(isinstance(t, ast.Name) and t.id == "capabilities" for t in targets):
                return True
        todo.extend(b.id for b in node.bases if isinstance(b, ast.Name))
    return False


def test_every_register_source_class_declares_capabilities() -> None:
    registered = _registered_classes_in_source()
    # A literal from the fact: 5 modules with @register_source on 02.10.2026 (3 combat
    # + 2 sleeping VG-13). An empty scan = a broken guard, not clean code.
    assert len(registered) >= 5
    missing = [
        f"{Path(path).relative_to(PARSERS_DIR)}::{name}"
        for path, names in registered.items()
        for name in sorted(names)
        if not _class_assigns_capabilities(ast.parse(Path(path).read_text()), name)
    ]
    assert missing == []


def test_loaded_adapters_carry_capabilities_instance() -> None:
    registry = load_all_adapters()
    assert {
        "jnportal",
        "ted_balkan",
        "ted_eu",
        "bih_enabavke",
        "mod_nabavke",
        "ukck_nabavke",
    } <= set(registry)
    for key, cls in registry.items():
        assert isinstance(getattr(cls, "capabilities", None), SourceCapabilities), key
    assert set(capabilities_by_key()) == set(registry)


@pytest.mark.parametrize("key", sorted(load_all_adapters()))
def test_documents_text_matches_implementation(key: str) -> None:
    cls = load_all_adapters()[key]
    assert cls.capabilities.documents_text is _implements_documents(cls)


@pytest.mark.parametrize("key", sorted(load_all_adapters()))
def test_awards_matches_fetch_awards(key: str) -> None:
    cls = load_all_adapters()[key]
    assert cls.capabilities.awards is callable(getattr(cls, "fetch_awards", None))


def test_known_declarations_from_code_facts() -> None:
    """The expectations — literals from the code of the adapters, not from the same declaration."""
    caps = capabilities_by_key()
    jn = caps["jnportal"]
    # description of jnportal = the text of the documentation (adapter.normalize), the sum —
    # not always (65% fill rate in production, measurement 02.10.2026).
    assert (jn.documents_text, jn.notice_description, jn.amount) == (
        True,
        False,
        "if_stated",
    )
    # TED: the adapter’s base_url is the API, the client needs the site.
    assert caps["ted_balkan"].public_url == "https://src-ted.example.invalid"
    assert caps["ted_balkan"].documents_text is False
    # TED: description-proc for 7 of 10 tenders = the title (production 03.10.2026) →
    # a promise “if specified”, not an unconditional one.
    assert caps["ted_balkan"].notice_description == "if_stated"
    rows = dict((k, ok) for ok, k in matrix_rows(caps["ted_balkan"]))
    assert rows["cap_notice_description_if_stated"] is True
    assert "cap_notice_description" not in rows
    assert "cap_s_notice_description_if_stated" in short_keys(caps["ted_balkan"])
    # BiH (portal src-ejn.example.invalid, 19.10.2026): the card docs.src-ejn.example.invalid/…/<uuid>.html —
    # is public, a direct link exists (source_url).
    assert caps["bih_enabavke"].original_link is True
    # VG-13 (SRC-0c): there is no sum/CPV either in the list of the MO or in the UKC (they are in the PDF
    # “Poziv”); the link — the MO has a link to the card jn.php, the UKC has only the general page.
    for key in ("mod_nabavke", "ukck_nabavke"):
        c = caps[key]
        assert (c.amount, c.cpv, c.awards) == ("never", False, False)
    # The term: the MO has none (the PDF is behind bot protection); the UKC — from the text of the PDF “Poziva”, at
    # the scans of it are absent (20/26, 06/26 — 0 characters, measurement 06.10.2026) → “if specified”.
    assert caps["mod_nabavke"].deadline is False
    assert caps["ukck_nabavke"].deadline == "if_stated"
    rows = dict((k, ok) for ok, k in matrix_rows(caps["ukck_nabavke"]))
    assert rows["cap_deadline_if_stated"] is True
    assert "cap_deadline" not in rows
    assert "cap_s_deadline_if_stated" in short_keys(caps["ukck_nabavke"])
    assert "cap_s_deadline" not in short_keys(caps["ukck_nabavke"])
    assert caps["mod_nabavke"].original_link is True
    assert caps["ukck_nabavke"].original_link is False
    assert caps["ukck_nabavke"].public_url == "https://src-ukck.example.invalid/javne-nabavke/"
