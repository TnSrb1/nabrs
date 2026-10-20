"""Invariant: the operator path in the bot does not go over the tenant-scoped tables with raw SQL.

Why a separate guard (a regression of 06.08.2026). The bot runs under the role
``tendersrb_app`` (NOBYPASSRLS), ``session_scope()`` does NOT set the GUC ``app.current_tenant_id``, and the decision on a request is a CROSS-TENANT operation: it has no tenant of its own,
so there is no GUC to set. Any raw query to a table under FORCE RLS from there falls with ``UndefinedObjectError: unrecognized configuration parameter`` — that is, the buttons “3/15/30 days / reject” and the ``/trials`` command did not work a single time since the launch. The defect survived both the code review and the tests: the mocks of ``AsyncSession`` execute ANY SQL, and the RLS integration (``tests/db/test_rls_isolation.py``) skips in the CI without ``TEST_RLS_DATABASE_URL``.
Therefore the check is static, without a DB: it works on every PR.

What we check and why exactly so:

* ``trial_requests`` and the WRITE to ``tenants`` are forbidden in the whole ``src/bots/``:
  both operations are by meaning cross-tenant (the operator decides, not the owner of the data) and must go through SECURITY DEFINER.
* the READ of ``tenants``/``profiles`` in the other bot services is NOT flagged: there the tenant is one’s own and the GUC is set (``status_service``, ``telegram.py`` — see ``set_current_tenant``). A ban on “any SQL over a tenant-scoped table” would give false positives and would be disabled by the first PR.
* For the cross-tenant module ``trial_service`` the rule is stricter: EACH of its SQL lines must call an ``app_*`` function, and there must be no tables at all in the position of ``FROM/JOIN/UPDATE/INSERT INTO/DELETE FROM``.

The SQL is taken from the AST (the arguments of ``text(...)``/``execute(...)`` and the constants ``*_SQL``), not by grepping the file: the docstrings and the comments discuss these same tables in words, and a text grep would turn red on the prose.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

BOTS_DIR = Path(__file__).resolve().parents[2] / "src" / "bots"

# A write to tenants and any mention of trial_requests — only via SECURITY
# DEFINER (0025/0034/0035/0041). The other tables are read under the GUC (see the docstring).
_FORBIDDEN_ANYWHERE = (
    re.compile(r"\btrial_requests\b", re.IGNORECASE),
    re.compile(r"\b(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+tenants\b", re.IGNORECASE),
)

# A table in the position of the source/target: everything that is not an app_* function is forbidden
# for the cross-tenant module.
_TABLE_POSITION = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+(\w+)", re.IGNORECASE
)

_CROSS_TENANT_MODULE = "trial_service.py"


def _literal(node: ast.AST) -> str | None:
    """A string literal of the node: an ordinary string or an f-string (a placeholder → ``?``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(" ? ")
        return "".join(parts)
    return None


def _callee(node: ast.expr) -> str:
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")


def sql_strings(source: str) -> list[str]:
    """The SQL strings of the module: the arguments of ``text()``/``execute()`` and the constants ``*_SQL``."""
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node.func) in {"text", "execute"}:
            found.extend(s for s in map(_literal, node.args) if s)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(n.upper().endswith("_SQL") for n in names):
                if (s := _literal(node.value)) is not None:
                    found.append(s)
    return found


def _bot_modules() -> list[Path]:
    return sorted(p for p in BOTS_DIR.glob("*.py") if p.name != "__init__.py")


def test_scanner_sees_sql_at_all():
    """A meta-check: the scanner really finds the SQL in src/bots.

    Without it the guard would silently turn green if the extraction broke (for example, the SQL
    moved to another helper) — and the hole would come back unnoticed.
    """
    total = sum(len(sql_strings(p.read_text())) for p in _bot_modules())
    assert total > 10, f"the scanner found only {total} SQL rows in src/bots — it is broken"


def test_scanner_flags_synthetic_raw_sql():
    """A built-in mutation: on knowingly raw SQL the scanner must turn red."""
    bad = """
from sqlalchemy import text
_BAD_SQL = text("UPDATE tenants SET access_until = now() WHERE id = :tid")
_ALSO_BAD = text("SELECT * FROM trial_requests WHERE status = 'pending'")
"""
    strings = sql_strings(bad)
    assert len(strings) == 2
    assert [s for s in strings if any(rx.search(s) for rx in _FORBIDDEN_ANYWHERE)] == (
        strings
    )
    # A docstring/comment about the same tables is not seen by the scanner — otherwise the guard
    # would turn red on the prose and it would be disabled.
    prose = '"""Правит trial_requests и делает UPDATE tenants."""\nX = 1\n'
    assert sql_strings(prose) == []


@pytest.mark.parametrize("path", _bot_modules(), ids=lambda p: p.name)
def test_no_raw_cross_tenant_sql_in_bots(path: Path):
    """Neither trial_requests, nor a write to tenants by raw SQL anywhere in src/bots."""
    for sql in sql_strings(path.read_text()):
        flat = " ".join(sql.split())
        for rx in _FORBIDDEN_ANYWHERE:
            assert not rx.search(flat), (
                f"{path.name}: raw cross-tenant SQL under FORCE RLS "
                f"(will fail with UndefinedObjectError under the bot role): {flat[:120]}"
            )


def test_cross_tenant_module_uses_only_security_definer_functions():
    """In ``trial_service`` EVERY query is a call of an ``app_*`` function, without tables.

    The module goes over a foreign tenant: there is no GUC to set for it, therefore its SQL has nowhere to get tables from — only SECURITY DEFINER (0034/0035/0041 and the reusable app_billing_*/app_admin_write_audit).
    """
    path = BOTS_DIR / _CROSS_TENANT_MODULE
    statements = sql_strings(path.read_text())
    assert statements, f"{_CROSS_TENANT_MODULE}: no SQL found — the scanner is broken"
    for sql in statements:
        flat = " ".join(sql.split())
        assert re.search(
            r"\bapp_(?:admin|billing|resolve)_\w+\s*\(", flat
        ), f"{_CROSS_TENANT_MODULE}: a query bypassing SECURITY DEFINER: {flat[:120]}"
        tables = [t for t in _TABLE_POSITION.findall(flat) if not t.startswith("app_")]
        assert not tables, (
            f"{_CROSS_TENANT_MODULE}: direct access to the tables {tables} "
            f"under FORCE RLS: {flat[:120]}"
        )
