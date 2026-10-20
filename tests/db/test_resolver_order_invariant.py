"""The guard: the resolve of “one row” must have an explicit ORDER BY. It works WITHOUT a DB.

``LIMIT 1`` without ``ORDER BY`` in PostgreSQL is a non-deterministic choice: the row
depends on the plan and the physical placement, and it changes silently. It has twice cost the product:

* ``app_resolve_user_for_auth`` led to a RANDOM tenant at login (04.08.2026);
  on 10.08.2026 two accounts of one email really lived in different tenants in production,
  so the choice was not theoretical;
* the same pattern went 1:1 into the new ``app_admin_tenant_notify_target`` (0041) —
  “we did not write it, we only moved it” does not cancel the defect.

We check the ACTUAL definition: one function may have several ``CREATE OR REPLACE`` in different revisions, the LAST one lives in the DB. Therefore we go through the files in the order of the numbers and keep the last definition of each name.
The ``downgrade()`` sections are skipped: there the earlier body is deliberately restored.

A guard WITHOUT a DB by the model of ``test_rls_force_invariant.py``: the RLS tests skip
without ``TEST_RLS_DATABASE_URL`` and were never run in the CI, while the invariant must
always work.
"""

from __future__ import annotations

import pathlib
import re

VERSIONS = pathlib.Path(__file__).resolve().parents[2] / "alembic" / "versions"

# ‼️ We take the body from the OPENING `AS $$` to the closing `$$`. The first version
# of the guard wrote `(?P<body>.*?)\$\$` from the argument parenthesis — the lazy quantifier
# stopped at the opening `$$`, the body did not get into the match at all, and the guard
# was ALWAYS green. Caught by a mutation (removed 0042 — the test had to turn red, but
# it passed); without the mutation check a false-green guard would have gone into main.
_FUNC_RE = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+(?P<name>\w+)\s*\((?P<args>[^)]*)\)"
    r"(?P<head>.*?)AS\s+\$\$(?P<body>.*?)\$\$",
    re.S | re.I,
)
_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+", re.I)


def _upgrade_section(source: str) -> str:
    """The text of the file up to ``def downgrade`` — in the downgrade the earlier bodies are legal."""
    idx = source.find("def downgrade")
    return source if idx < 0 else source[:idx]


def _latest_definitions() -> dict[str, str]:
    """{function name: the body of the last CREATE OR REPLACE in the order of the revisions}."""
    latest: dict[str, str] = {}
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        section = _upgrade_section(path.read_text(encoding="utf-8"))
        for m in _FUNC_RE.finditer(section):
            latest[m.group("name")] = m.group(0)
    return latest


def _limits_without_order(sql: str) -> list[str]:
    """Fragments of the form ``… LIMIT n`` whose SELECT has no ORDER BY.

    The boundary is taken by the nearest ``SELECT`` on the left: it opens the query or
    the subquery to which the LIMIT refers. A window of fixed length would lie on
    long bodies.
    """
    bad = []
    for m in _LIMIT_RE.finditer(sql):
        head = sql[: m.start()]
        select_at = head.upper().rfind("SELECT")
        scope = head[select_at:] if select_at >= 0 else head
        if "ORDER BY" not in scope.upper():
            bad.append(" ".join(scope.split())[-90:])
    return bad


def test_security_definer_resolvers_have_explicit_order():
    """Not a single live SECURITY DEFINER function selects a row without an order."""
    offenders: dict[str, list[str]] = {}
    for name, ddl in _latest_definitions().items():
        if "SECURITY DEFINER" not in ddl.upper():
            continue
        bad = _limits_without_order(ddl)
        if bad:
            offenders[name] = bad
    assert not offenders, (
        "LIMIT without ORDER BY in SECURITY DEFINER functions — a non-deterministic "
        f"choice of a row: {offenders}"
    )


def test_guard_itself_detects_a_missing_order_by():
    """A counter-fact: the guard catches the defect, rather than just turning green.

    Without this the test above cannot be told apart from “the regex finds nothing”.
    """
    broken = """
        CREATE OR REPLACE FUNCTION app_fake_resolver(p_email TEXT)
        RETURNS TABLE(id UUID)
        LANGUAGE SQL STABLE SECURITY DEFINER
        AS $$
          SELECT id FROM users WHERE email = p_email LIMIT 1
        $$
    """
    assert _limits_without_order(broken), "the guard did not see a LIMIT without ORDER BY"

    fixed = broken.replace("LIMIT 1", "ORDER BY created_at, id LIMIT 1")
    assert not _limits_without_order(fixed), "the guard falsely fires on an ORDER BY"
