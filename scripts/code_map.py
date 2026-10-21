#!/usr/bin/env python3
"""Generator of docs/CODE_MAP.md — the map of the repository modules.

The map is DERIVED: everything except the section “Where to look” is extracted from the code
(AST parsing of docstrings, routes, tables; a walk of the directories). If the map
and the code disagree, the CODE is right — regenerate:

    python3 scripts/code_map.py

Why: an agent (and a person) needs an address “file:line”, not a retelling. The map
answers the question “where does X live” and gives predicates for search, not descriptions.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "CODE_MAP.md"

BIG_FILE_KB = 25  # the “edit point-by-point” threshold from the project rules
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".pytest_cache", ".mypy_cache"}

# The section that is NOT derived from the code: the search predicates. It lives here so that
# regeneration does not lose it.
WHERE_TO_LOOK = """\
| Question | Predicate (not a retelling) |
| --- | --- |
| Where is an interface string? | `grep -rn "<string>" src/api/*/i18n.py src/i18n_common.py src/i18n_email.py` |
| Where is an entity rendered? | `grep -rln "<partial>.html" src/api/templates/` — edit ALL the points, not one |
| Who touches the table? | `grep -rn "<table>" src/ --include=*.py` + `grep -rn "<table>" alembic/versions/` |
| Does the table have RLS? | `grep -rn "FORCE ROW LEVEL SECURITY" alembic/versions/ \\| grep <table>` |
| Where is the tier/limit gate? | `src/ai/usage.py` (`_TIER_LIMITS`), `src/plans/catalog.py`, `src/api/deps.py` |
| Where is access granted? | `src/domain/access.py`, `src/domain/access_grants.py` — the single point |
| Which route serves the screen? | `grep -rn "<template>.html" src/api/` |
| Where is the CSS class defined? | `grep -n "<class>" src/api/static/app.css` (there is NO Tailwind build) |
| What does migration N change? | `alembic/versions/` — the file by number; heads: `alembic heads` (in the development clone) |
| A cross-tenant operation? | `grep -rn "app_admin_\\|app_resolve_" src/ alembic/versions/` — only SECURITY DEFINER |
"""


@dataclass
class ModuleInfo:
    path: str
    loc: int
    doc: str
    tables: list[str] = field(default_factory=list)
    routes: list[tuple[str, str, int]] = field(
        default_factory=list
    )  # method, path, line
    prefixes: list[str] = field(default_factory=list)


def first_doc_line(node: ast.Module) -> str:
    doc = ast.get_docstring(node)
    if not doc:
        return ""
    line = doc.strip().splitlines()[0].strip()
    return line.rstrip(".")


def scan_module(path: Path) -> ModuleInfo:
    src = path.read_text(encoding="utf-8", errors="replace")
    rel = str(path.relative_to(ROOT))
    loc = src.count("\n") + 1
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ModuleInfo(path=rel, loc=loc, doc="(not parsed)")

    info = ModuleInfo(path=rel, loc=loc, doc=first_doc_line(tree))

    for node in ast.walk(tree):
        # __tablename__ = "..."
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__tablename__":
                    if isinstance(node.value, ast.Constant) and isinstance(
                        node.value.value, str
                    ):
                        info.tables.append(node.value.value)
        # APIRouter(prefix="...")
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "APIRouter":
                for kw in node.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        info.prefixes.append(str(kw.value.value))
        # @router.get(...) / @app.post(...)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    if dec.func.attr in {
                        "get",
                        "post",
                        "put",
                        "patch",
                        "delete",
                        "head",
                    }:
                        obj = dec.func.value
                        if getattr(obj, "id", "") in {"router", "app"}:
                            path = ""
                            if dec.args and isinstance(dec.args[0], ast.Constant):
                                path = str(dec.args[0].value)
                            info.routes.append(
                                (dec.func.attr.upper(), path, node.lineno)
                            )
    return info


def walk_py(base: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(base.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def package_of(rel: str) -> str:
    parts = Path(rel).parts
    if len(parts) <= 2:
        return parts[0]  # src/x.py → src
    return "/".join(parts[:2]) if len(parts) == 3 else "/".join(parts[:3])


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "?"


def tables_from_sources() -> dict[str, str]:
    """Table → the file where it is created. THREE sources, not one.

    Migrations (``op.create_table`` and raw ``CREATE TABLE``), the code of ``src/`` and
    the one-off scripts ``scripts/``: the service table ``deadline_tz_backfill`` lives in production,
    but is described neither by a model nor by a migration — the backfill script creates it.
    Scanning one source means creating a blind spot.
    """
    import re

    out: dict[str, str] = {}
    pat_op = re.compile(r"""op\.create_table\(\s*["']([a-z0-9_]+)["']""", re.I)
    pat_sql = re.compile(
        r"""CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["']?(?:public\.)?([a-z0-9_]+)""",
        re.I,
    )
    bases = [ROOT / "alembic" / "versions", ROOT / "src", ROOT / "scripts"]
    files: list[Path] = []
    for base in bases:
        if not base.exists():
            continue
        files += [p for p in sorted(base.rglob("*.py")) if "__pycache__" not in p.parts]
        files += sorted(base.rglob("*.sql"))
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for pat in (pat_op, pat_sql):
            for name in pat.findall(text):
                out.setdefault(name, str(f.relative_to(ROOT)))
    return out


def includes(template: Path, partial_name: str) -> bool:
    """Whether the template INCLUDES the partial (and does not merely mention it in a comment).

    A partial’s name in the comment `{# … _feed_card.html … #}` gave a false
    “included in” and mutual references A↔B. We count only Jinja directives.
    """
    text = template.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if partial_name not in line:
            continue
        if any(
            kw in line for kw in ("{% include", "{% import", "{% from", "{% extends")
        ):
            return True
    return False


def rendered_from_python(partial_name: str) -> list[str]:
    """Where the partial is rendered FROM THE CODE (`get_template`, `TemplateResponse`).

    The second render source: `_kw_forms.html` is not included in any template, but is
    returned as an HTMX fragment from a route. Counting only Jinja — means declaring a live partial dead.
    """
    out: list[str] = []
    for p in sorted((ROOT / "src").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if partial_name in line and (
                "get_template" in line or "TemplateResponse" in line
            ):
                out.append(f"{p.relative_to(ROOT)}:{i}")
    return out


def fmt_kb(path: Path) -> int:
    return round(path.stat().st_size / 1024)


def build() -> str:
    modules = [scan_module(p) for p in walk_py(ROOT / "src")]
    by_pkg: dict[str, list[ModuleInfo]] = {}
    for m in modules:
        by_pkg.setdefault(package_of(m.path), []).append(m)

    total_loc = sum(m.loc for m in modules)
    sha = git("rev-parse", "--short", "HEAD")
    sha_date = git("log", "-1", "--format=%cs")

    lines: list[str] = []
    add = lines.append

    add("# CODE_MAP — module map")
    add("")
    add(
        f"**Derived document.** Generated from the code at `{sha}` ({sha_date}). "
        "Regeneration: `python3 scripts/code_map.py`."
    )
    add(
        "If the map and the code disagree, **the code is right** — do not edit the map by hand, "
        "except the section “Where to look” (it lives in `scripts/code_map.py`)."
    )
    add("")
    loc_str = f"{total_loc:,}".replace(",", " ")
    add(f"Volume: **{len(modules)} modules** in `src/`, **{loc_str} lines** of Python.")
    add("")

    # ── Check against the facts ───────────────────────────────────────────────────
    add("## Checking the map against fact")
    add("")
    add(
        "The map is a hypothesis, production is a fact. Both checks are read-only, both are done from "
        "the development clone:"
    )
    add("")
    add("```")
    # /openapi.json over HTTP is closed without API_DOCS_ENABLED (#607) — the schema is taken
    # in-process from app.openapi(), it is available regardless of the flag.
    add("# routes: the number of paths in the live app’s schema (compare with the “Routes” section)")
    add("docker compose -f deploy/docker-compose.yml exec -T api python -c \\")
    add("  \"from src.api.main import app; print(len(app.openapi()['paths']))\"")
    add("")
    add("# tables: the list from the live DB (compare with the “DB tables” section)")
    add("docker compose -f deploy/docker-compose.yml exec -T postgres bash -lc \\")
    add("  'psql -U $POSTGRES_USER -d $POSTGRES_DB -c \\\"\\\\dt\\\"'")
    add("```")
    add("")
    add(
        "As of 10.08.2026 checked: **72 of 72** paths matched in both directions; "
        "**41 of 42** tables (the divergence — the service table `alembic_version`)."
    )
    add("")

    # ── Where to look ────────────────────────────────────────────────────────
    add("## Where to look (predicates)")
    add("")
    add(WHERE_TO_LOOK)

    # ── Entry points ───────────────────────────────────────────────────────
    add("## Entry points")
    add("")
    add("| What | File | How it is launched |")
    add("| --- | --- | --- |")
    entry_rows = [
        ("HTTP API + cabinet + showcase", "src/api/main.py", "uvicorn, service `api`"),
        ("Telegram bot", "src/bots/telegram.py", "service `bot`"),
        ("Migrations", "alembic/", "`docker compose run --rm migrate`"),
    ]
    for what, f, how in entry_rows:
        exists = "" if (ROOT / f.rstrip("/")).exists() else " ⚠️ none"
        add(f"| {what} | `{f}`{exists} | {how} |")
    workers = sorted(
        p.relative_to(ROOT)
        for p in (ROOT / "src" / "workers").glob("*.py")
        if p.name != "__init__.py"
    )
    add(f"| Workers ({len(workers)}) | `src/workers/` | separate compose services |")
    add("")

    # ── Packages ────────────────────────────────────────────────────────────
    add("## Packages of `src/`")
    add("")
    for pkg in sorted(by_pkg):
        mods = sorted(by_pkg[pkg], key=lambda m: -m.loc)
        pkg_loc = sum(m.loc for m in mods)
        add(f"### `{pkg}/` — {len(mods)} modules, {pkg_loc} lines")
        add("")
        add("| Module | Lines | Purpose (docstring) |")
        add("| --- | ---: | --- |")
        for m in mods:
            name = Path(m.path).name
            doc = m.doc or "—"
            extra = []
            if m.prefixes:
                extra.append("prefix " + ", ".join(f"`{p}`" for p in m.prefixes))
            if m.routes:
                extra.append(f"{len(m.routes)} route(s)")
            if m.tables:
                extra.append("tables: " + ", ".join(f"`{t}`" for t in m.tables))
            if extra:
                doc = f"{doc} · {' · '.join(extra)}" if m.doc else " · ".join(extra)
            add(f"| `{name}` | {m.loc} | {doc} |")
        add("")

    # ── Tables ───────────────────────────────────────────────────────────
    tables: list[tuple[str, str]] = []
    for m in modules:
        for t in m.tables:
            tables.append((t, m.path))
    created = tables_from_sources()
    if tables or created:
        model_of = dict(tables)
        add("## DB tables")
        add("")
        add(
            "There are THREE sources: models (`__tablename__`), migrations and one-off scripts. "
            "A table without a model exists only in SQL — find its references "
            "by the predicate `text(`, not via the ORM."
        )
        add("")
        add(
            "Check of the map against the LIVE database (the map is a hypothesis, the database is a fact):\n"
            "```\n"
            "docker compose -f deploy/docker-compose.yml exec -T postgres bash -lc \\\n"
            "  'psql -U $POSTGRES_USER -d $POSTGRES_DB -tAc \"select table_name "
            "from information_schema.tables where table_schema='public' "
            "and table_type='BASE TABLE' order by 1\\\"'\n"
            "```"
        )
        add("")
        add("| Table | Model | Created in |")
        add("| --- | --- | --- |")
        for t in sorted(set(model_of) | set(created)):
            mod = f"`{model_of[t]}`" if t in model_of else "— (SQL only)"
            src_f = f"`{created[t]}`" if t in created else "—"
            add(f"| `{t}` | {mod} | {src_f} |")
        add("")

    # ── Routes ─────────────────────────────────────────────────────────────
    routed = [m for m in modules if m.routes]
    if routed:
        total_routes = sum(len(m.routes) for m in routed)
        add("## Routes")
        add("")
        add(
            f"**{total_routes} routes** in {len(routed)} modules. The path = the `prefix` "
            "of the router + the path of the decorator (`include_router` in `src/api/main.py` goes "
            "WITHOUT `prefix=`), i.e. the full URL."
        )
        add("")
        add("| Method | Path | Address |")
        add("| --- | --- | --- |")
        flat = []
        for m in routed:
            pref = m.prefixes[0] if m.prefixes else ""
            for meth, path, line in m.routes:
                full = (pref + path) or "/"
                flat.append((full, meth, m.path, line))
        for path, meth, f, line in sorted(flat):
            add(f"| {meth} | `{path}` | `{f}:{line}` |")
        add("")

    # ── Large files ─────────────────────────────────────────────────────
    big = []
    for p in walk_py(ROOT / "src"):
        kb = fmt_kb(p)
        if kb >= BIG_FILE_KB:
            big.append((kb, str(p.relative_to(ROOT))))
    for p in sorted((ROOT / "src" / "api" / "templates").rglob("*.html")):
        kb = fmt_kb(p)
        if kb >= BIG_FILE_KB:
            big.append((kb, str(p.relative_to(ROOT))))
    css = ROOT / "src" / "api" / "static" / "app.css"
    if css.exists():
        big.append((fmt_kb(css), str(css.relative_to(ROOT))))
    if big:
        add(f"## Large files (≥{BIG_FILE_KB} KB) — edit point by point")
        add("")
        add("| File | KB |")
        add("| --- | ---: |")
        for kb, p in sorted(big, reverse=True):
            add(f"| `{p}` | {kb} |")
        add("")

    # ── Templates ───────────────────────────────────────────────────────────
    tpl_dir = ROOT / "src" / "api" / "templates"
    if tpl_dir.exists():
        tpls = sorted(tpl_dir.rglob("*.html"))
        partials = [t for t in tpls if t.name.startswith("_")]
        add("## Templates")
        add("")
        add(
            f"`src/api/templates/` — **{len(tpls)} files**, of which "
            f"**{len(partials)} partials** (`_*.html`)."
        )
        add("")
        add(
            "‼️ A feature visible in several blocks is fixed by a predicate over ALL "
            'render points: `grep -rln "_<partial>.html" src/api/templates/`.'
        )
        add("")
        add(
            "«—» = neither an inclusion nor a render from the code: a candidate for a "
            "dead file, check before editing."
        )
        add("")
        add("| Partial | Included in | Rendered from code |")
        add("| --- | --- | --- |")
        for t in partials:
            name = t.name
            users = [
                str(o.relative_to(tpl_dir))
                for o in tpls
                if o != t and includes(o, name)
            ]
            py = rendered_from_python(name)
            col_tpl = ", ".join(f"`{u}`" for u in users) if users else "—"
            col_py = ", ".join(f"`{u}`" for u in py) if py else "—"
            add(f"| `{name}` | {col_tpl} | {col_py} |")
        add("")

    # ── Migrations ──────────────────────────────────────────────────────────
    mig_dir = ROOT / "alembic" / "versions"
    if mig_dir.exists():
        migs = sorted(mig_dir.glob("*.py"))
        add("## Migrations")
        add("")
        add(
            f"`alembic/versions/` — **{len(migs)} files**. The latest: "
            + ", ".join(f"`{m.name}`" for m in migs[-3:])
        )
        add("")
        add(
            "‼️ Check the number against `origin/main` AND against the open branches `claude/*` "
            "before the push: two branches with one number give TWO heads and a failure "
            "of the rollout on production. `alembic heads` — only in the development clone."
        )
        add("")

    # ── Tests ─────────────────────────────────────────────────────────────
    tests_dir = ROOT / "tests"
    if tests_dir.exists():
        add("## Tests")
        add("")
        add("| Directory | Files | What it covers |")
        add("| --- | ---: | --- |")
        groups: dict[str, int] = {}
        for p in sorted(tests_dir.rglob("test_*.py")):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            rel = p.relative_to(tests_dir)
            key = rel.parts[0] if len(rel.parts) > 1 else "(root)"
            groups[key] = groups.get(key, 0) + 1
        hints = {
            "db": "RLS contour, the invariant FORCE RLS",
            "quality": "mypy ratchet, coverage of CSS classes by the templates",
            "api": "routes, templates, render i18n",
            "workers": "delivery, reminders, ingest",
        }
        for key in sorted(groups):
            add(f"| `tests/{key}` | {groups[key]} | {hints.get(key, '')} |")
        add("")
        add(
            "‼️ In the second clone the run is INCOMPLETE (21 routes instead of hundreds, `alembic` does not "
            "import) — the deciding `gate.sh` is only in the development clone."
        )
        add("")

    add("---")
    add("")
    add(
        "The code invariants are enforced by the tests."
    )
    add("")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/code_map.py",
        description=(
            "Generator of docs/CODE_MAP.md — the map of the repository modules. "
            "The document is derived: it is not edited by hand, but regenerated."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT,
        help=f"path for writing the map (default {OUT.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "do not write: compare the file (see --out) with the freshly collected map, "
            "return code 1 if it is stale"
        ),
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="print the map to stdout instead of writing to the file",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    text = build()

    if args.stdout:
        print(text)
        return 0

    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if current != text:
            print(
                f"STALE: {args.out} diverges from the freshly collected map — "
                "regenerate: python3 scripts/code_map.py"
            )
            return 1
        print(f"OK: {args.out} is up to date")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"OK: {args.out} — {len(text.splitlines())} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
