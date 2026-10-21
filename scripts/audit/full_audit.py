#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TenderSRB — Comprehensive project audit (full_audit.py)
=====================================================

An end-to-end check of the WHOLE project, as if it were run by a staff of domain
experts. Eighteen independent “teams” (modules), each a separate direction: security, dependencies, code quality, tests, DB/migrations,
performance, DevOps/infrastructure, architecture (multi-tenant/
multi-country), HTTP layer (FastAPI), Telegram bot, observability,
AI pipeline, data collection, configuration/environment, localization (sr/ru/en),
privacy/compliance (Serbian jurisdiction, GDPR/ZZPL), context ledger,
git hygiene.

PRINCIPLES (strict):
  • HONESTY. If a tool/resource is unavailable — status SKIP (loud),
    NEVER presented as PASS. The results of linters/tests are not invented.
  • NO CRASHES. A failure of one module does not fail the audit: we write an INFRA flag and move on.
  • TWO MODES:
      static (default) — only static analysis of the repository.
                         Safe in CI: does not touch docker, the live DB, the network.
      full                  — on the VPS: + docker compose config, the live DB head via
                            `docker exec deploy-postgres-1 psql`, container checks,
                            the AUTHORITATIVE CVE audit by `pip freeze` of the live container.
    git checks and external linters (ruff/black/bandit/pip-audit/…) are run in
    BOTH modes, if the binary is installed; otherwise — SKIP. For a one-line run
    with the real linters — `scripts/audit/run_full.sh`.
  • HEAVY TESTS (the pytest run with coverage) — only under the --run-tests flag.
    By default only a quick collect-only is done. If pytest is NOT installed —
    an honest SKIP (not a false HIGH).

RUNNING:
  python3 full_audit.py                         # static, report to ./audit_report_<date>.md
  python3 full_audit.py --mode full             # on the VPS, under `sudo -u claude`
  python3 full_audit.py --root /home/<user>/tendersrb --mode full --run-tests
  python3 full_audit.py --list                  # list of modules
  python3 full_audit.py --only SEC,DB,INFRA     # a subset
  python3 full_audit.py --json audit.json       # additionally machine-readable JSON

EXIT CODE (gate for CI):
  0 — no findings above the --fail-on threshold
  1 — there are MEDIUM-level findings (if the threshold == medium)
  2 — there are CRITICAL/HIGH (by default --fail-on high)

No dependencies for the core (stdlib 3.12 only). External auditors are picked up
by presence in PATH: ruff, black, mypy, pip-audit, bandit, gitleaks, hadolint,
radon, vulture, docker, git, caddy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

# --------------------------------------------------------------------------- #
#  Constants specific to TenderSRB
# --------------------------------------------------------------------------- #

REQUIRED_PINS = {"ruff": "0.15.16", "black": "26.5.1"}  # CI pins (the project state log)

# AI tier slugs expected in infra/config.yaml
EXPECTED_AI_TIERS = ("cheap", "balanced", "premium")

# Containers from which we take `pip freeze` for the authoritative CVE audit
# (the ground truth of the actually deployed versions). Iterated in order.
DEPLOY_PY_CONTAINERS = (
    "deploy-bot-1",
    "deploy-ai-1",
    "deploy-ingest-1",
    "deploy-web-1",
    "deploy-api-1",
    "deploy-worker-1",
)

# RED paths (rollout by hand by the owner) — for the inventory in the report
RED_PATH_PATTERNS = (
    "alembic/",
    "src/api/security.py",
    "cabinet/auth_service.py",
    "cabinet/routes_auth.py",
    "billing",
    "payment",
    "paddle",
    "docs/DECISIONS.md",
    "docs/adr/",
    "infra/secrets",
    "infra/config.yaml",
    "deploy/",
    "docker-compose",
    "Caddyfile",
    ".github/workflows/",
    "site/sr",
    "site/ru",
    "site/en",
)

# Source directories over which we run greps
SRC_DIRS = ("src",)
# Text extensions that we read as code/config
TEXT_EXTS = {
    ".py",
    ".pyi",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".txt",
    ".html",
    ".j2",
    ".jinja",
    ".jinja2",
    ".sql",
    ".env",
    ".sh",
    ".conf",
    ".Caddyfile",
}
# Directories that we do NOT walk in the static scan
SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
}

LARGE_FILE_LINES = 1500  # the threshold of a “large” file (drift risk on edits)
LARGE_TRACKED_BYTES = 1_000_000  # 1 MB — the threshold of a large binary in git
STATE_STALE_DAYS = 14  # the “staleness” threshold of the project state log
EXTERNAL_TIMEOUT = 240  # sec per external tool


# --------------------------------------------------------------------------- #
#  The finding model
# --------------------------------------------------------------------------- #


class Severity(IntEnum):
    OK = 0
    INFO = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    CRITICAL = 5
    SKIP = 6  # outside the severity scale — “not verified”

    @property
    def label(self) -> str:
        return {
            Severity.OK: "OK",
            Severity.INFO: "INFO",
            Severity.LOW: "LOW",
            Severity.MEDIUM: "MEDIUM",
            Severity.HIGH: "HIGH",
            Severity.CRITICAL: "CRITICAL",
            Severity.SKIP: "SKIP",
        }[self]

    @property
    def mark(self) -> str:
        return {
            Severity.OK: "[ OK ]",
            Severity.INFO: "[INFO]",
            Severity.LOW: "[LOW ]",
            Severity.MEDIUM: "[MED ]",
            Severity.HIGH: "[HIGH]",
            Severity.CRITICAL: "[CRIT]",
            Severity.SKIP: "[SKIP]",
        }[self]


@dataclass
class Finding:
    module: str
    severity: Severity
    title: str
    detail: str = ""
    evidence: str = ""
    fix: str = ""


@dataclass
class ModuleResult:
    mid: str
    name: str
    findings: list[Finding] = field(default_factory=list)
    crashed: Optional[str] = None


# --------------------------------------------------------------------------- #
#  Context and helpers
# --------------------------------------------------------------------------- #


@dataclass
class RunResult:
    code: int = 0
    out: str = ""
    err: str = ""
    skipped: Optional[str] = None  # the SKIP reason if the tool is missing/timed out

    @property
    def ok(self) -> bool:
        return self.skipped is None and self.code == 0


class Ctx:
    """The general context: repo root, mode, cache of the file list, command runner."""

    def __init__(self, root: Path, mode: str, run_tests: bool):
        self.root = root
        self.mode = mode  # 'static' | 'full'
        self.run_tests = run_tests
        self.has_git = shutil.which("git") is not None and (root / ".git").exists()
        self._text_files: Optional[list[Path]] = None

    # ---- mode ----
    @property
    def is_full(self) -> bool:
        return self.mode == "full"

    # ---- file system ----
    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def read(self, rel: str) -> Optional[str]:
        p = self.root / rel
        if not p.is_file():
            return None
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def text_files(self) -> list[Path]:
        """All text files of the repo (cache), excluding junk directories."""
        if self._text_files is not None:
            return self._text_files
        acc: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix in TEXT_EXTS or fn == "Caddyfile" or fn == "Dockerfile":
                    acc.append(p)
        self._text_files = acc
        return acc

    def files_under(
        self, subdirs: Iterable[str], exts: Optional[set[str]] = None
    ) -> Iterator[Path]:
        for sub in subdirs:
            base = self.root / sub
            if not base.exists():
                continue
            for p in self.text_files():
                try:
                    p.relative_to(base)
                except ValueError:
                    continue
                if exts is None or p.suffix in exts:
                    yield p

    def grep(
        self,
        pattern: str,
        subdirs: Iterable[str] = SRC_DIRS,
        exts: Optional[set[str]] = None,
        flags: int = re.IGNORECASE,
        limit: int = 50,
    ) -> list[tuple[Path, int, str]]:
        rx = re.compile(pattern, flags)
        hits: list[tuple[Path, int, str]] = []
        for p in self.files_under(subdirs, exts):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append((p, i, line.strip()[:200]))
                    if len(hits) >= limit:
                        return hits
        return hits

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)

    # ---- external commands ----
    def run(
        self,
        cmd: list[str],
        cwd: Optional[Path] = None,
        timeout: int = EXTERNAL_TIMEOUT,
        env: Optional[dict[str, str]] = None,
    ) -> RunResult:
        if shutil.which(cmd[0]) is None:
            return RunResult(skipped=f"binary '{cmd[0]}' not found in PATH")
        try:
            cp = subprocess.run(
                cmd,
                cwd=str(cwd or self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            return RunResult(code=cp.returncode, out=cp.stdout, err=cp.stderr)
        except subprocess.TimeoutExpired:
            return RunResult(skipped=f"timeout {timeout}s: {' '.join(cmd[:3])}")
        except OSError as e:  # noqa: BLE001
            return RunResult(skipped=f"failed to start {cmd[0]}: {e}")

    def docker_exec_psql(self, sql: str) -> RunResult:
        """Full mode only. A live request to the DB on the VPS."""
        if not self.is_full:
            return RunResult(skipped="static mode — the live DB is not queried")
        return self.run(
            [
                "docker",
                "exec",
                "deploy-postgres-1",
                "psql",
                "-U",
                "tendersrb",
                "-d",
                "tendersrb",
                "-tA",
                "-c",
                sql,
            ],
            timeout=30,
        )


class Probe:
    """Accumulator of a module’s findings + short severity helpers."""

    def __init__(self, ctx: Ctx, mid: str):
        self.ctx = ctx
        self.mid = mid
        self.F: list[Finding] = []

    def add(
        self,
        sev: Severity,
        title: str,
        detail: str = "",
        evidence: str = "",
        fix: str = "",
    ):
        self.F.append(Finding(self.mid, sev, title, detail, evidence, fix))

    def crit(self, *a, **k):
        self.add(Severity.CRITICAL, *a, **k)

    def high(self, *a, **k):
        self.add(Severity.HIGH, *a, **k)

    def med(self, *a, **k):
        self.add(Severity.MEDIUM, *a, **k)

    def low(self, *a, **k):
        self.add(Severity.LOW, *a, **k)

    def info(self, *a, **k):
        self.add(Severity.INFO, *a, **k)

    def ok(self, *a, **k):
        self.add(Severity.OK, *a, **k)

    def skip(self, title: str, reason: str):
        self.add(Severity.SKIP, title, detail=reason)

    def ev(self, hits: list[tuple[Path, int, str]], n: int = 6) -> str:
        """Compress the grep list into a proof string."""
        lines = [f"{self.ctx.rel(p)}:{ln}: {txt}" for p, ln, txt in hits[:n]]
        if len(hits) > n:
            lines.append(f"… and {len(hits) - n} more")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  MODULE 1 — Security and AppSec                                           #
# --------------------------------------------------------------------------- #


def audit_security(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "SEC")

    # 1. Weak/default SECRET_KEY in the code
    weak = ctx.grep(
        r"""(secret[_-]?key|jwt[_-]?secret)\s*[:=]\s*['"](change[-_ ]?me|dev|test|secret|changeme|please[-_ ]?change|insecure)""",
        subdirs=("src", "infra", "deploy"),
    )
    if weak:
        p.crit(
            "Hard-coded/default secret key",
            detail="A weak secret/jwt key literal was found — matches the historical audit (insecure default secret key).",
            evidence=p.ev(weak),
            fix="Move to the environment secrets; in production — a long random key; fail-fast on the default.",
        )
    else:
        p.ok("No default secret keys found in the code")

    # 2. Basic secret search (if gitleaks is available — a full scan of the history)
    gl = ctx.run(
        ["gitleaks", "detect", "--no-banner", "--redact", "-r", "/dev/stdout"],
        timeout=180,
    )
    if gl.skipped:
        # a fallback primitive regex over the working tree
        prim = ctx.grep(
            r"(AKIA[0-9A-Z]{16}|sk-ant-[A-Za-z0-9_\-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[0-9A-Za-z\-]{10,})",
            subdirs=("src", "infra", "deploy", "docs"),
        )
        if prim:
            p.high(
                "Looks like a secret in the working tree (primitive scan)",
                evidence=p.ev(prim),
                fix="Confirm with gitleaks; rotate the key; purge it from the git history.",
            )
        p.skip(
            "Secret scanner gitleaks",
            gl.skipped + " — only the primitive regex over the working tree was run",
        )
    else:
        if gl.out.strip():
            p.crit(
                "gitleaks found secrets",
                evidence=gl.out.strip()[:1500],
                fix="Rotate; purge the history (git filter-repo / BFG).",
            )
        else:
            p.ok("gitleaks: no secrets detected")

    # 3. Security headers in the Caddyfile (SITE-07/08)
    caddy = None
    for cand in ("Caddyfile", "deploy/Caddyfile", "infra/Caddyfile"):
        caddy = ctx.read(cand) or caddy
    if caddy is None:
        p.skip("Caddy security headers", "Caddyfile not found")
    else:
        need = {
            "Content-Security-Policy": Severity.HIGH,
            "Strict-Transport-Security": Severity.HIGH,
            "X-Content-Type-Options": Severity.MEDIUM,
            "Referrer-Policy": Severity.MEDIUM,
            "Permissions-Policy": Severity.LOW,
        }
        missing = [h for h in need if h.lower() not in caddy.lower()]
        if missing:
            sev = max(need[h] for h in missing)
            p.add(
                sev,
                "Not all security headers are set in the Caddyfile",
                detail="Matches SITE-07/08 (TASK 5).",
                evidence="Missing: " + ", ".join(missing),
                fix="Add CSP/HSTS/X-Content-Type-Options/Referrer-Policy/Permissions-Policy to the Caddy header block.",
            )
        else:
            p.ok("Basic security headers are present in the Caddyfile")

    # 4. Session cookie flags (HttpOnly/Secure/SameSite)
    auth_hits = ctx.grep(
        r"set_cookie|response\.cookie|Set-Cookie", subdirs=("src",), exts={".py"}
    )
    if not auth_hits:
        p.skip(
            "Cookie flags",
            "no set_cookie calls found (possibly a different session mechanism)",
        )
    else:
        cookie_files = {p_ for p_, _, _ in auth_hits}
        joined = "\n".join((ctx.read(ctx.rel(f)) or "") for f in cookie_files).lower()
        flags_missing = [
            f for f in ("httponly", "secure", "samesite") if f not in joined
        ]
        if flags_missing:
            p.high(
                "The session cookie does not have all protective flags set",
                evidence="Not found: " + ", ".join(flags_missing),
                fix="httponly=True, secure=True, samesite='lax' (as recorded in the SITE audit).",
            )
        else:
            p.ok("Cookie: HttpOnly + Secure + SameSite are present")

    # 5. Turnstile fail-closed
    if ctx.grep(r"turnstile", subdirs=("src",)):
        failopen = ctx.grep(
            r"turnstile.*(except|try).*(pass|return\s+True)",
            subdirs=("src",),
            flags=re.I | re.S,
        )
        p.info(
            "Turnstile is used",
            detail="The SITE audit noted fail-closed server-side. Check manually that on a verification error the access is CLOSED.",
            evidence=p.ev(failopen) if failopen else "",
        )
    else:
        p.skip("Turnstile", "no mentions found")

    # 6. RLS / superuser posture (AUD-002 / FIX-02)
    rls_force = ctx.grep(
        r"FORCE\s+ROW\s+LEVEL\s+SECURITY|ENABLE\s+ROW\s+LEVEL\s+SECURITY",
        subdirs=("alembic", "src", "migrations"),
        exts={".py", ".sql"},
    )
    if rls_force:
        p.ok("The migrations enable RLS", evidence=p.ev(rls_force, 3))
    else:
        p.high(
            "RLS is not FORCEd — tenant isolation only by app-level WHERE",
            detail="Known finding AUD-002 / FIX-02 (postponed by the owner until after the release). The application connects as a superuser → RLS is bypassed.",
            fix="In stages: a non-superuser role (BYPASSRLS) → then FORCE ROW LEVEL SECURITY + GUC plumbing.",
        )

    # 7. SQL injection: f-string/%-concatenation inside text()/execute()
    sqli = ctx.grep(
        r"(text|execute)\s*\(\s*f['\"]|(text|execute)\s*\([^)]*%\s*[\(\w]",
        subdirs=("src",),
        exts={".py"},
    )
    if sqli:
        p.high(
            "Possible SQL concatenation from f-string/%-formatting",
            evidence=p.ev(sqli),
            fix="Parameterized queries only (bind parameters), not interpolation of values.",
        )
    else:
        p.ok("No signs of value interpolation into SQL found")

    # 8. Are secrets/.env committed
    if ctx.has_git:
        ls = ctx.run(["git", "ls-files"], timeout=30)
        if not ls.skipped:
            tracked = ls.out.splitlines()
            leaked = [
                f
                for f in tracked
                if re.search(r"(^|/)(\.env(\.|$)|secrets?\.env$|infra/secrets)", f)
                and not f.endswith(
                    (".example", ".sample", ".template", ".dist", ".tpl")
                )
            ]
            if leaked:
                p.crit(
                    "Secret files are committed to git",
                    evidence="\n".join(leaked[:10]),
                    fix="git rm --cached + rotation; add to .gitignore.",
                )
            else:
                p.ok("Secret files (.env/secrets) are not in the git index")
    else:
        p.skip(
            "Check for secrets in the git index", "git is unavailable or not a git repository"
        )

    # 9. SAST via bandit (if installed)
    bd = ctx.run(["bandit", "-r", "src", "-f", "json", "-q"], timeout=300)
    if bd.skipped:
        p.skip(
            "SAST (bandit)", bd.skipped + " — install bandit (it is in run_full.sh)."
        )
    else:
        try:
            data = json.loads(bd.out or "{}")
            res = data.get("results", [])
            hi = [r for r in res if r.get("issue_severity") == "HIGH"]
            md = [r for r in res if r.get("issue_severity") == "MEDIUM"]
            if hi:
                p.high(
                    f"bandit: HIGH issues {len(hi)} (total {len(res)})",
                    evidence="\n".join(
                        f"{r.get('filename')}:{r.get('line_number')} {r.get('test_id')} {r.get('issue_text','')[:80]}"
                        for r in hi[:6]
                    ),
                    fix="Go through each bandit HIGH finding.",
                )
            elif md:
                p.med(
                    f"bandit: MEDIUM issues {len(md)} (no HIGH)",
                    evidence="\n".join(
                        f"{r.get('filename')}:{r.get('line_number')} {r.get('test_id')}"
                        for r in md[:6]
                    ),
                )
            else:
                p.ok(f"bandit: no HIGH/MEDIUM found (total records {len(res)})")
        except json.JSONDecodeError:
            p.info(
                "bandit ran, but the JSON was not parsed",
                evidence=(bd.out or bd.err)[:400],
            )

    # 10. Dangerous constructs
    danger = ctx.grep(
        r"\beval\(|\bexec\(|pickle\.loads|yaml\.load\(|subprocess[^\n]*shell\s*=\s*True|\bos\.system\(",
        subdirs=("src",),
        exts={".py"},
    )
    if danger:
        p.high(
            "Potentially dangerous constructs (eval/exec/pickle/yaml.load/shell=True/os.system)",
            evidence=p.ev(danger, 8),
            fix="eval/exec — remove; pickle only for trusted data; yaml.safe_load; shell=False + an argument list.",
        )
    else:
        p.ok("No dangerous eval/exec/pickle/shell=True found")

    # 11. DEBUG=True in production
    dbg = ctx.grep(
        r"\bDEBUG\s*=\s*True\b|debug\s*=\s*True",
        subdirs=("src", "infra", "deploy"),
        exts={".py", ".yaml", ".yml", ".env"},
    )
    dbg = [(f, ln, line) for f, ln, line in dbg if "test" not in ctx.rel(f).lower()]
    if dbg:
        p.med(
            "DEBUG=True enabled outside the tests",
            detail="In production DEBUG exposes tracebacks/details — an infrastructure leak.",
            evidence=p.ev(dbg, 5),
            fix="DEBUG from the environment, False by default.",
        )
    else:
        p.ok("DEBUG=True outside the tests not found")

    # 12. CORS wildcard
    cors = ctx.grep(
        r"allow_origins\s*=\s*\[\s*['\"]\*['\"]|CORSMiddleware[^\n]*\*",
        subdirs=("src",),
        exts={".py"},
    )
    if cors:
        p.high(
            "CORS is open to '*' (allow_origins=['*'])",
            evidence=p.ev(cors, 4),
            fix="List specific origins (app.nab.rs); not '*' with credentials.",
        )
    else:
        p.ok("CORS wildcard ('*') not found")

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 2 — Dependencies and supply chain                                      #
# --------------------------------------------------------------------------- #


def audit_deps(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "DEPS")
    pyproject = ctx.read("pyproject.toml")

    # 1. Pins of the CI linters
    blob = (pyproject or "") + "\n" + (ctx.read("requirements.txt") or "")
    for tool, ver in REQUIRED_PINS.items():
        if re.search(rf"{tool}\s*==\s*{re.escape(ver)}", blob):
            p.ok(f"{tool} pinned to {ver}")
        elif re.search(rf"\b{tool}\b", blob):
            p.med(
                f"{tool} is present, but NOT at the required version {ver}",
                fix=f"Pin {tool}=={ver} (match the CI Lint).",
            )
        else:
            p.low(
                f"{tool} not found in the dependencies",
                detail="Needed for local green status (Actions are frozen).",
            )

    # 2. Unpinned dependencies
    if pyproject:
        loose = re.findall(
            r'["\']([A-Za-z0-9_.\-]+)\s*(>=|\^|~=|~|>|\*)[^"\']*["\']', pyproject
        )
        loose_names = sorted({n for n, _ in loose})
        if loose_names:
            p.low(
                "Unpinned version ranges in pyproject",
                detail="For build reproducibility exact pins or a lock file are better.",
                evidence=", ".join(loose_names[:25]),
            )
        else:
            p.ok("pyproject has no “floating” version ranges")
    else:
        p.skip("Analysis of the pyproject pins", "pyproject.toml not found")

    # 3. Lock file
    locks = [
        f
        for f in ("poetry.lock", "uv.lock", "requirements.lock", "Pipfile.lock")
        if ctx.exists(f)
    ]
    if locks:
        p.ok("Lock file is present", evidence=", ".join(locks))
    else:
        p.low(
            "Lock file not found",
            fix="Fix the dependency tree (uv.lock / poetry.lock).",
        )

    # 4. CVE audit — AUTHORITATIVE for the actually deployed dependencies.
    #    Source of truth: `pip freeze` from the live container (full) → or requirements.txt.
    #    We do NOT make a venv audit: it would reflect the audit tools, not the project’s dependencies.
    def _parse_pip_audit(out: str) -> tuple[int, list[str]]:
        data = json.loads(out or "{}")
        deps = data.get("dependencies", data) if isinstance(data, dict) else data
        cnt, sample = 0, []
        for dep in (deps if isinstance(deps, list) else []):
            for v in dep.get("vulns") or []:
                cnt += 1
                if len(sample) < 8:
                    sample.append(
                        f"{dep.get('name')} {dep.get('version')}: {v.get('id')}"
                    )
        return cnt, sample

    audited_via = None
    req_file = None
    if ctx.is_full:
        for cont in DEPLOY_PY_CONTAINERS:
            fr = ctx.run(
                ["docker", "exec", cont, "python", "-m", "pip", "freeze"], timeout=60
            )
            if (not fr.skipped) and fr.code == 0 and fr.out.strip():
                snap = Path(tempfile.gettempdir()) / "tendersrb_audit_freeze.txt"
                try:
                    snap.write_text(fr.out, encoding="utf-8")
                    req_file = str(snap)
                    audited_via = f"pip freeze {cont} (actually deployed)"
                except OSError:
                    pass
                break
    if audited_via is None and ctx.exists("requirements.txt"):
        req_file = str(ctx.path("requirements.txt"))
        audited_via = "requirements.txt"

    if audited_via is None:
        p.skip(
            "CVE audit (pip-audit)",
            "no source of ACTUAL dependencies: the container is unavailable (--mode full on the VPS is needed) "
            "and there is no requirements.txt. A venv audit is deliberately not done (it does not reflect production).",
        )
    else:
        pa = ctx.run(
            ["pip-audit", "--progress-spinner", "off", "-f", "json", "-r", req_file],
            timeout=240,
        )
        if pa.skipped:
            p.skip(
                "CVE audit (pip-audit)",
                pa.skipped + " — install pip-audit (it is in run_full.sh).",
            )
        else:
            try:
                cnt, sample = _parse_pip_audit(pa.out)
                if cnt:
                    p.high(
                        f"pip-audit ({audited_via}): vulnerabilities — {cnt}",
                        evidence="\n".join(sample),
                        fix="Update the affected packages to safe versions; rebuild the image.",
                    )
                else:
                    p.ok(f"pip-audit ({audited_via}): no known vulnerabilities")
            except json.JSONDecodeError:
                p.info(
                    "pip-audit ran, but the JSON was not parsed",
                    evidence=(pa.out or pa.err)[:500],
                )

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 3 — Code quality and style                                           #
# --------------------------------------------------------------------------- #


def audit_code(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "CODE")

    # 1. ruff
    rf = ctx.run(["ruff", "check", "--output-format", "concise", "."])
    if rf.skipped:
        p.skip("ruff", rf.skipped)
    elif rf.code == 0:
        p.ok("ruff: no remarks")
    else:
        n = len([ln for ln in rf.out.splitlines() if ln.strip()])
        p.med(
            f"ruff: remarks ~{n}",
            evidence="\n".join(rf.out.splitlines()[:12]),
            fix="Run `ruff check --fix` locally (the green status of the PR is the worker’s job).",
        )

    # 2. black --check
    bk = ctx.run(["black", "--check", "--quiet", "."])
    if bk.skipped:
        p.skip("black", bk.skipped)
    elif bk.code == 0:
        p.ok("black: formatting is consistent")
    else:
        p.med(
            "black: there are files to reformat",
            evidence=(bk.err or bk.out).splitlines()[:12]
            and "\n".join((bk.err or bk.out).splitlines()[:12]),
            fix="Run `black .` locally.",
        )

    # 3. mypy (if there is a config)
    has_mypy_cfg = (
        ctx.exists("mypy.ini")
        or "tool.mypy" in (ctx.read("pyproject.toml") or "")
        or ctx.exists("setup.cfg")
    )
    if has_mypy_cfg:
        mp = ctx.run(["mypy", "src"], timeout=300)
        if mp.skipped:
            p.skip("mypy", mp.skipped)
        elif mp.code == 0:
            p.ok("mypy: typing is clean")
        else:
            errs = [ln for ln in mp.out.splitlines() if ": error:" in ln]
            p.low(f"mypy: type errors ~{len(errs)}", evidence="\n".join(errs[:10]))
    else:
        p.skip("mypy", "mypy config not found")

    # 4. U+FFFD / broken encoding — critical for large Cyrillic files (drift risk)
    corrupt: list[str] = []
    bad_enc: list[str] = []
    for f in ctx.files_under(
        ("src", "docs"), exts={".py", ".md", ".html", ".j2", ".jinja2", ".sql"}
    ):
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        try:
            txt = raw.decode("utf-8")
        except UnicodeDecodeError:
            bad_enc.append(ctx.rel(f))
            continue
        if "\ufffd" in txt:
            corrupt.append(ctx.rel(f))
    if bad_enc:
        p.high(
            "Files with non-UTF-8 bytes (drift/corruption likely)",
            evidence="\n".join(bad_enc[:10]),
            fix="Re-upload the whole file from a trusted source; check via get_commit full_patch.",
        )
    if corrupt:
        p.high(
            "The U+FFFD replacement character inside text files (Cyrillic drift)",
            detail="A known pitfall: reconstructing large Cyrillic files via MCP produces U+FFFD/fullwidth/CJK substitutions.",
            evidence="\n".join(corrupt[:10]),
            fix="Targeted by-anchor edits instead of reconstruction; a surgical diff in the web UI.",
        )
    if not bad_enc and not corrupt:
        p.ok("No U+FFFD/broken encoding found in .py/.md/.html")

    # 5. Inventory of TODO/FIXME/HACK/XXX
    todos = ctx.grep(
        r"#\s*(TODO|FIXME|HACK|XXX)\b|<!--\s*(TODO|FIXME)", subdirs=("src",), limit=200
    )
    if todos:
        sev = Severity.LOW if len(todos) < 40 else Severity.MEDIUM
        p.add(
            sev,
            f"Technical debt in code markers: {len(todos)}",
            evidence=p.ev(todos, 10),
        )
    else:
        p.ok("No TODO/FIXME/HACK/XXX markers in src/")

    # 6. Large files (drift risk on MCP edits)
    big: list[str] = []
    for f in ctx.files_under(("src",), exts={".py"}):
        try:
            n = sum(1 for _ in f.open("r", encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if n > LARGE_FILE_LINES:
            big.append(f"{ctx.rel(f)} ({n} lines)")
    if big:
        p.low(
            f"Large files (> {LARGE_FILE_LINES} lines) — edit them point by point",
            detail="For such files a whole-file reconstruction is risky (see STATE: src/bots/telegram.py).",
            evidence="\n".join(big[:12]),
        )
    else:
        p.ok(f"No files larger than {LARGE_FILE_LINES} lines")

    # 7. Cyclomatic complexity (radon, if available)
    rd = ctx.run(["radon", "cc", "-s", "-n", "C", "src"])
    if rd.skipped:
        p.skip("Complexity (radon cc)", rd.skipped)
    else:
        hot = [ln for ln in rd.out.splitlines() if re.search(r"\b[CDEF]\b", ln)]
        if hot:
            p.low(
                f"Sections of elevated complexity (radon, rank ≥ C): {len(hot)}",
                evidence="\n".join(hot[:10]),
            )
        else:
            p.ok("radon: no sections of complexity ≥ C found")

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 4 — QA and tests                                                      #
# --------------------------------------------------------------------------- #


def _pytest_available(ctx: Ctx) -> bool:
    r = ctx.run(["python3", "-c", "import pytest"], timeout=30)
    return (not r.skipped) and r.code == 0


def audit_tests(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "TEST")
    test_files = list(ctx.files_under(("tests", "src"), exts={".py"}))
    test_files = [
        f for f in test_files if re.search(r"(^|/)test_|_test\.py$", ctx.rel(f))
    ]

    has_pytest = _pytest_available(ctx)

    if not test_files:
        # absence of tests — a real finding, does NOT depend on the presence of pytest
        p.high(
            "No tests found (no test_*.py / _test.py)",
            fix="Cover the critical path: the access predicate, matching, plan/freeze gates, parsers.",
        )
        if not has_pytest:
            p.info(
                "pytest is not installed in the environment — a run is impossible here anyway."
            )
        return p.F

    p.info(
        f"Test files in the repo: {len(test_files)}",
        evidence="\n".join(ctx.rel(f) for f in test_files[:10]),
    )

    # Without pytest we do NOT issue a false HIGH — an honest SKIP
    if not has_pytest:
        p.skip(
            "Running/collecting tests",
            "pytest is not installed in the audit environment (this does NOT mean the tests fail). "
            "The real run — in the dev/container with the project’s dependencies.",
        )
        return p.F

    if not ctx.run_tests:
        cl = ctx.run(["python3", "-m", "pytest", "-q", "--collect-only"], timeout=120)
        if cl.skipped:
            p.skip("Collecting tests (pytest --collect-only)", cl.skipped)
        elif cl.code == 0:
            m = re.search(r"(\d+)\s+tests?\s+collected", cl.out) or re.search(
                r"collected\s+(\d+)", cl.out
            )
            cnt = m.group(1) if m else "?"
            p.ok(f"pytest collects the tests without errors (~{cnt})")
        else:
            p.high(
                "pytest already fails at the collection stage",
                evidence="\n".join((cl.out + cl.err).splitlines()[-12:]),
                fix="Fix imports/conftests before the run.",
            )
        p.info(
            "The full run with coverage was not started",
            detail="Run with the --run-tests flag (it may take long / require services).",
        )
        return p.F

    # the full run under the flag
    cov = ctx.run(
        ["python3", "-m", "pytest", "-q", "--cov=src", "--cov-report=term-missing"],
        timeout=900,
    )
    if cov.skipped:
        rr = ctx.run(["python3", "-m", "pytest", "-q"], timeout=900)
        if rr.skipped:
            p.skip("Running tests", rr.skipped)
        elif rr.code == 0:
            p.ok("pytest: all tests passed (without coverage — pytest-cov is not installed)")
        else:
            p.high(
                "pytest: there are failing tests",
                evidence="\n".join((rr.out + rr.err).splitlines()[-15:]),
            )
        return p.F
    tail = (cov.out + cov.err).splitlines()
    m = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", cov.out)
    pct = int(m.group(1)) if m else None
    if cov.code != 0:
        p.high("pytest: there are failing tests", evidence="\n".join(tail[-15:]))
    elif pct is not None and pct < 50:
        p.med(f"Low coverage: {pct}%", fix="Raise the coverage of the critical path ≥ 60–70%.")
    elif pct is not None:
        p.ok(f"pytest is green, coverage {pct}%")
    else:
        p.ok("pytest is green (the coverage percentage was not recognized)")
    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 5 — Database and migrations                                          #
# --------------------------------------------------------------------------- #


def _alembic_dir(ctx: Ctx) -> Optional[Path]:
    for cand in ("alembic/versions", "migrations/versions", "src/alembic/versions"):
        d = ctx.path(cand)
        if d.is_dir():
            return d
    return None


def audit_db(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "DB")
    vdir = _alembic_dir(ctx)
    if not vdir:
        p.skip("Alembic migrations", "the versions directory not found")
        return p.F

    revs: dict[str, Optional[str]] = {}
    files = sorted(vdir.glob("*.py"))
    for f in files:
        txt = f.read_text(encoding="utf-8", errors="replace")
        rid = re.search(r"^revision\s*(?::[^=\n]*)?=\s*['\"]([^'\"]+)", txt, re.M)
        down = re.search(
            r"^down_revision\s*(?::[^=\n]*)?=\s*(?:['\"]([^'\"]+)['\"]|None)", txt, re.M
        )
        if rid:
            revs[rid.group(1)] = down.group(1) if down and down.group(1) else None

    # 1. Several heads
    downs = {d for d in revs.values() if d}
    heads = [r for r in revs if r not in downs]
    if len(heads) > 1:
        p.high(
            "Several alembic heads (branching of migrations)",
            evidence="heads: " + ", ".join(heads),
            fix="Merge the branches via `alembic merge`; keep one line.",
        )
    elif len(heads) == 1:
        p.ok(f"The single migration head: {heads[0]} (total revisions {len(revs)})")
    else:
        p.info(f"Revisions found: {len(revs)}, the head could not be determined")

    # 2. Destructive operations
    dangerous = ctx.grep(
        r"\bDROP\s+TABLE\b|\bDROP\s+COLUMN\b|ALTER\s+COLUMN[^\n]*\bDROP\b|\bTRUNCATE\b|op\.drop_(table|column)",
        subdirs=(ctx.rel(vdir),),
        exts={".py"},
        limit=100,
    )
    if dangerous:
        p.med(
            f"Destructive operations in migrations: {len(dangerous)}",
            detail="RED path: rollout by hand by the owner + a fresh backup before the merge (the autodeploy timer ≤5 min exists).",
            evidence=p.ev(dangerous, 8),
        )
    else:
        p.ok("No destructive DROP/TRUNCATE found in migrations")

    # 3. RLS policies
    if ctx.grep(
        r"ROW\s+LEVEL\s+SECURITY|CREATE\s+POLICY",
        subdirs=(ctx.rel(vdir),),
        exts={".py"},
    ):
        p.ok("RLS policies are present in the migrations")
    else:
        p.info(
            "There are no RLS policies in the migrations",
            detail="Consistent with FIX-02 (RLS postponed; isolation by app-level WHERE).",
        )

    # 4. pgvector
    if ctx.grep(
        r"CREATE\s+EXTENSION[^\n]*vector|pgvector",
        subdirs=(ctx.rel(vdir),),
        exts={".py"},
    ):
        p.ok("The pgvector extension is created in the migrations")
    else:
        p.low(
            "No creation of the vector extension in the migrations found",
            detail="The stack declares pgvector — make sure the extension is enabled by a migration, not by hand.",
        )

    # 5. Live head (full only)
    live = ctx.docker_exec_psql("SELECT version_num FROM alembic_version;")
    if live.skipped:
        p.skip("The live DB head on the VPS", live.skipped)
    elif live.ok:
        head = live.out.strip()
        if head and head in revs:
            p.ok(f"The live DB head = {head} — present in the migration tree")
        else:
            p.high(
                f"The live DB head = '{head}' does NOT match the revisions of the repo",
                fix="Check the rollout (`build migrate` + up.sh); the risk of a production schema/code desync.",
            )
    else:
        p.info("The live head could not be read", evidence=(live.err or "")[:300])

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 6 — DevOps and infrastructure                                         #
# --------------------------------------------------------------------------- #


def _compose_path(ctx: Ctx) -> Optional[str]:
    for cand in (
        "deploy/docker-compose.yml",
        "deploy/docker-compose.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
    ):
        if ctx.exists(cand):
            return cand
    return None


def audit_infra(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "INFRA")
    compose_rel = _compose_path(ctx)
    compose = ctx.read(compose_rel) if compose_rel else None

    # 1. Compose validation (full)
    if compose_rel and ctx.is_full:
        cfg = ctx.run(
            ["docker", "compose", "-f", compose_rel, "config", "-q"], timeout=60
        )
        if cfg.skipped:
            p.skip("docker compose config", cfg.skipped)
        elif cfg.ok:
            p.ok("docker compose config: valid")
        else:
            p.high("docker compose config: errors", evidence=(cfg.err or cfg.out)[:600])
    elif compose_rel:
        p.skip("docker compose config", "static mode — compose is not validated")
    else:
        p.skip("docker-compose", "file not found")

    if compose:
        low = compose.lower()
        # 2. Healthchecks
        if "healthcheck" in low:
            p.ok("Healthchecks are defined in compose")
        else:
            p.med(
                "There are no healthchecks in compose",
                fix="Add healthchecks for the bot/AI/ingest/postgres (there is /healthz in main.py).",
            )
        # 3. restart policy
        if "restart:" in low:
            p.ok("restart policies are defined")
        else:
            p.low(
                "restart policies are not defined in compose",
                fix="restart: unless-stopped for the services.",
            )
        # 4. Publishing the DB/Redis ports outward
        exposed_db = re.findall(r"ports:\s*\n\s*-\s*['\"]?\d+:(?:5432|6379)", compose)
        if exposed_db:
            p.high(
                "The PostgreSQL/Redis port is published to the host",
                detail="The DB/cache must not stick out — only the internal docker network + Caddy.",
                fix="Remove ports: for postgres/redis; talk by the service name.",
            )
        else:
            p.ok("The PostgreSQL/Redis ports are not published outward")
        # 5. ANTHROPIC_API_KEY on the AI services (a pitfall)
        if "anthropic_api_key" not in low and ctx.grep(
            r"AiClient|anthropic", subdirs=("src",)
        ):
            p.high(
                "AiClient is used, but ANTHROPIC_API_KEY is not visible in compose",
                detail="Pitfall: a service calling AiClient (ai/bot) without the key fails with a TypeError when resolving auth.",
                fix="Pass ANTHROPIC_API_KEY into the environment of all AI-calling services.",
            )

    # 6. Dockerfile ↔ .dockerignore consistency (the pitfall of runtime files outside src/)
    dockerfile = None
    for cand in ("Dockerfile", "deploy/Dockerfile"):
        dockerfile = ctx.read(cand) or dockerfile
    if dockerfile:
        copies = re.findall(r"^\s*COPY\s+([^\s]+)\s", dockerfile, re.M)
        p.info(
            "Dockerfile COPY sources (check against .dockerignore by hand)",
            detail="Pitfall: a new runtime file outside src/ requires a COPY + removing the exclusion in .dockerignore.",
            evidence="COPY: " + ", ".join(sorted(set(copies))[:12]),
        )
        hadolint = ctx.run(["hadolint", cand if ctx.exists(cand) else "Dockerfile"])
        if hadolint.skipped:
            p.skip("hadolint (lint Dockerfile)", hadolint.skipped)
        elif hadolint.code == 0:
            p.ok("hadolint: no remarks on the Dockerfile")
        else:
            p.low(
                "hadolint: there are remarks on the Dockerfile",
                evidence="\n".join(hadolint.out.splitlines()[:10]),
            )
    else:
        p.skip("Dockerfile", "not found")

    # 7. Caddy validation (full)
    caddy_rel = next(
        (c for c in ("Caddyfile", "deploy/Caddyfile") if ctx.exists(c)), None
    )
    if caddy_rel and ctx.is_full:
        cv = ctx.run(["caddy", "validate", "--config", caddy_rel], timeout=30)
        if cv.skipped:
            p.skip("caddy validate", cv.skipped)
        elif cv.ok:
            p.ok("caddy validate: config is valid")
        else:
            p.med("caddy validate: errors", evidence=(cv.err or cv.out)[:400])
    elif caddy_rel:
        p.skip("caddy validate", "static mode")

    # 8. Containers on the VPS (full)
    if ctx.is_full:
        ps = ctx.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"], timeout=30
        )
        if ps.skipped:
            p.skip("State of the containers", ps.skipped)
        else:
            deploy_lines = [
                ln for ln in ps.out.splitlines() if ln.startswith("deploy-")
            ]
            unhealthy = [
                ln
                for ln in deploy_lines
                if "unhealthy" in ln.lower()
                or "restarting" in ln.lower()
                or "exited" in ln.lower()
            ]
            if unhealthy:
                p.high(
                    "There are unhealthy/restarting containers",
                    evidence="\n".join(unhealthy),
                )
            else:
                p.ok(
                    f"deploy containers up: {len(deploy_lines)}",
                    evidence="\n".join(deploy_lines[:14]),
                )

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 7 — Architecture (multi-tenant / multi-country)                      #
# --------------------------------------------------------------------------- #


def audit_arch(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "ARCH")

    # 1. Layering of src/
    expected = [
        "src/api",
        "src/bots",
        "src/workers",
        "src/parsers",
        "src/ai",
        "src/domain",
    ]
    present = [d for d in expected if ctx.exists(d)]
    missing = [d for d in expected if not ctx.exists(d)]
    p.info(
        f"Layers of src/: {len(present)}/{len(expected)}",
        evidence="present: "
        + ", ".join(present)
        + (("; missing: " + ", ".join(missing)) if missing else ""),
    )

    # 2. SSOT access predicate
    if ctx.exists("src/domain/access.py"):
        users = ctx.grep(
            r"from\s+.*domain\.access|import\s+access|access_active|is_(tenant_)?frozen",
            subdirs=("src",),
            exts={".py"},
        )
        p.ok(
            "The SSOT freeze predicate is present (src/domain/access.py)",
            evidence=f"uses in the code: {len(users)}",
        )
    else:
        p.med(
            "src/domain/access.py not found (the single access/freeze predicate)",
            detail="STATE records SSOT access_active as the single source of truth.",
        )

    # 3. country_code propagation (ADR-033)
    cc = ctx.grep(r"country_code", subdirs=("src",), exts={".py"}, limit=300)
    if len(cc) >= 5:
        p.ok(f"country_code is spread through the code (ADR-033): {len(cc)} mentions")
    elif cc:
        p.low(
            "country_code occurs rarely",
            detail="The multi-country foundations require a cross-cutting country_code from phase 0.",
            evidence=p.ev(cc, 5),
        )
    else:
        p.med(
            "country_code not found in the code",
            detail="ADR-033 requires a universal architecture (countries registry, country_code).",
        )

    # 4. Raw text() queries over tenant tables without a tenant filter (heuristic)
    raw = ctx.grep(r"text\(\s*['\"]", subdirs=("src",), exts={".py"}, limit=200)
    suspicious = []
    for f, ln, line in raw:
        if re.search(
            r"tenant|profiles|delivery|tenders_normalized", line, re.I
        ) and not re.search(r"tenant_id|access_until", line, re.I):
            suspicious.append((f, ln, line))
    if suspicious:
        p.info(
            "Raw SQL over tenant tables — check for the presence of a tenant filter",
            detail="A low-precision heuristic; RLS is postponed (FIX-02), isolation by app-level WHERE.",
            evidence=p.ev(suspicious, 6),
        )
    else:
        p.ok("No suspicious raw SQL without a tenant filter found (heuristic)")

    # 5. Circular imports (best effort, pyflakes/pylint if available)
    pl = ctx.run(
        ["pylint", "--disable=all", "--enable=cyclic-import", "src"], timeout=180
    )
    if pl.skipped:
        p.skip("Circular imports (pylint)", pl.skipped)
    else:
        cyc = [ln for ln in pl.out.splitlines() if "cyclic-import" in ln]
        if cyc:
            p.low(
                f"Possible circular imports: {len(cyc)}",
                evidence="\n".join(cyc[:8]),
            )
        else:
            p.ok("pylint: no circular imports found")

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 8 — Observability and reliability                                      #
# --------------------------------------------------------------------------- #


def audit_obs(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "OBS")

    # 1. /healthz
    if ctx.grep(
        r"/healthz|def\s+healthz|@app\.(get|router).*health",
        subdirs=("src",),
        exts={".py"},
    ):
        p.ok("The healthcheck endpoint /healthz is present")
    else:
        p.med(
            "The /healthz endpoint not found",
            fix="Add /healthz for the compose healthcheck.",
        )

    # 2. WorkerHealth wiring (REV-05)
    if ctx.exists("src/workers/health.py"):
        wired = ctx.grep(r"WorkerHealth", subdirs=("src/workers",), exts={".py"})
        if any("ingest" in ctx.rel(f) for f, _, _ in wired):
            p.ok("WorkerHealth exists and is bound in ingest (REV-05)")
        else:
            p.low(
                "WorkerHealth exists, but the binding in ingest is not visible",
                evidence=p.ev(wired, 4),
            )
        p.info(
            "REV-05: alerts are passive without the env wiring (compose ingest + Healthchecks.io) — a known RED tail."
        )
    else:
        p.low("src/workers/health.py not found (ingest observability)")

    # 3. DB backups (FIX-01)
    backup = None
    for _bdir in ("infra", "deploy", "scripts"):
        _bp = ctx.path(_bdir)
        if _bp.is_dir():
            _hits = sorted(_bp.rglob("*backup*.sh")) + sorted(_bp.rglob("*pg_dump*.sh"))
            if _hits:
                backup = ctx.rel(_hits[0])
                break
    if backup is None and ctx.exists("db_backup.sh"):
        backup = "db_backup.sh"
    if backup:
        p.ok("The DB backup script is present", evidence=backup)
        p.info(
            "FIX-01 LIVE: cron 02:30 UTC, pg_dump -Fc, retention 14 days. The cron fact is verified on the VPS."
        )
    else:
        p.high(
            "The DB auto-backup script was not found in the repo",
            detail="AUD-001 was CRITICAL (no backups).",
            fix="Commit db_backup.sh and document the cron.",
        )

    # 4. Logging
    if ctx.grep(
        r"logging\.(getLogger|basicConfig)|structlog|loguru",
        subdirs=("src",),
        exts={".py"},
    ):
        p.ok("Logging is initialized in the code")
    else:
        p.low("No explicit logging initialization found")

    # 5. Subscription reminders (LIVE)
    if ctx.exists("src/workers/subscription_reminders.py"):
        p.ok("The subscription_reminders worker is present")
    else:
        p.info("subscription_reminders.py not found (per STATE — LIVE)")

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 9 — AI pipeline                                                     #
# --------------------------------------------------------------------------- #


def audit_ai(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "AI")

    # 1. Tier slugs in infra/config.yaml
    cfg = ctx.read("infra/config.yaml")
    if cfg is None:
        p.skip("AI tiers (infra/config.yaml)", "file not found")
    else:
        miss = [t for t in EXPECTED_AI_TIERS if t not in cfg]
        if miss:
            p.med(
                "Not all AI tiers are in config.yaml",
                evidence="missing: " + ", ".join(miss),
                fix="Describe cheap/balanced/premium with the model slugs.",
            )
        else:
            p.ok("AI tiers (cheap/balanced/premium) are present in config.yaml")

    # 2. Token/usage accounting
    if ctx.exists("src/ai/usage.py") or ctx.grep(
        r"usage|input_tokens|output_tokens", subdirs=("src/ai",), exts={".py"}
    ):
        p.ok("Usage/token accounting is present in src/ai")
    else:
        p.low(
            "Token/usage accounting is not visible in src/ai",
            fix="Log the input/output tokens to control costs.",
        )

    # 3. Character caps (protection against expensive calls)
    caps = ctx.grep(
        r"_DOCUMENTS_MAX_CHARS|MAX_CHARS|max_tokens|TOKEN_BUDGET|char.{0,6}(limit|cap)",
        subdirs=("src",),
        exts={".py"},
    )
    if caps:
        p.ok("There are character/token limits on AI input", evidence=p.ev(caps, 5))
    else:
        p.med(
            "No character/token limits on AI input found",
            fix="Introduce a cap (there is _DOCUMENTS_MAX_CHARS=50000 for the specifications).",
        )

    # 4. L3 gate (Pro+ and the freeze BEFORE the gate)
    l3 = ctx.grep(
        r"ai:deep:|on_deep_analysis|deep_analysis", subdirs=("src",), exts={".py"}
    )
    if l3:
        gate = ctx.grep(
            r"is_frozen|frozen|tier.*pro|Pro\+|gate",
            subdirs=("src/bots",),
            exts={".py"},
        )
        p.ok("L3 “Deep analysis” is present", evidence=p.ev(l3, 4))
        p.info(
            "Check manually: the freeze is checked BEFORE the Pro+ gate, opus is NOT called for frozen accounts (FIX-SG-04).",
            evidence=p.ev(gate, 3) if gate else "",
        )
    else:
        p.info("L3 deep analysis not found in the code (per STATE — LIVE)")

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 10 — Data collection and parsing                                          #
# --------------------------------------------------------------------------- #


def audit_data(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "DATA")

    if not ctx.exists("src/parsers"):
        p.skip("Parsers (src/parsers)", "directory not found")
        return p.F

    # 1. Never-crash around fetch (ingest must not fall over)
    adapters = list(ctx.files_under(("src/parsers",), exts={".py"}))
    fetchers = [
        f for f in adapters if re.search(r"fetch|adapter|ingest", ctx.rel(f), re.I)
    ]
    guarded = 0
    for f in fetchers:
        txt = ctx.read(ctx.rel(f)) or ""
        if "try:" in txt and "except" in txt:
            guarded += 1
    if fetchers:
        if guarded == len(fetchers):
            p.ok(
                f"All fetch/adapter files have exception handling ({guarded}/{len(fetchers)})"
            )
        else:
            p.low(
                f"Not all parsers are wrapped in try/except ({guarded}/{len(fetchers)})",
                detail="Pitfall/principle: ingest must NEVER fall over because of one document/source.",
            )
    else:
        p.info("Fetch/adapter files not recognized by name")

    # 2. Cap on the document volume
    if ctx.grep(
        r"_DOCUMENTS_MAX_CHARS|50[_ ]?000", subdirs=("src/parsers",), exts={".py"}
    ):
        p.ok("The cap on the document volume of the specifications is set (REV-01)")
    else:
        p.low("The cap on the volume of extractable documents was not found")

    # 3. Whitelist of document types
    if ctx.grep(
        r"whitelist|allowed.{0,3}(types|ext|mime)|\.pdf|\.docx",
        subdirs=("src/parsers",),
        exts={".py"},
    ):
        p.ok("A filter/whitelist of document types is present")
    else:
        p.low("The whitelist of document types is not visible")

    # 4. The single unofficial-interface source (REV-02)
    p.info(
        "The eNabavke/jnportal source — an unofficial interface (REV-02): a single point of failure.",
        detail="A live pull is possible only from the VPS (egress from CI is closed). Monitor the availability of the source.",
    )

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 11 — Context ledger and management                                   #
# --------------------------------------------------------------------------- #


def audit_docs(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "DOCS")

    state = None  # the state log is not part of the public snapshot
    if state is None:
        p.info(
            "the project state log is not in the public snapshot",
            detail="The single source of truth on the project state.",
        )
    else:
        # freshness
        m = re.search(r"UPDATED:\s*(\d{4}-\d{2}-\d{2})", state)
        if m:
            try:
                d = datetime.strptime(m.group(1), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                age = (datetime.now(timezone.utc) - d).days
                if age > STATE_STALE_DAYS:
                    p.med(
                        f"The project state log is stale: updated {age} days ago ({m.group(1)})",
                        fix="Update the project state log in every working PR (worker).",
                    )
                else:
                    p.ok(f"The project state log is fresh (updated {age} days ago)")
            except ValueError:
                p.info("The project state log: the UPDATED date was not parsed")
        else:
            p.low("There is no UPDATED field in the project state log")
        # required sections
        req = ["LAST", "NOW", "NEXT", "BLOCKED"]
        miss = [s for s in req if not re.search(rf"\b{s}\b", state)]
        if miss:
            p.low("The project state log has no sections: " + ", ".join(miss))
        else:
            p.ok("The project state log contains LAST/NOW/NEXT/BLOCKED")

    # ADR
    dec = ctx.read("docs/DECISIONS.md")
    if dec:
        adrs = sorted(set(re.findall(r"ADR-(\d+)", dec)), key=int)
        p.info(
            f"ADRs in DECISIONS.md: {len(adrs)}",
            evidence="max ADR-" + (adrs[-1] if adrs else "?"),
        )
    else:
        p.low("docs/DECISIONS.md not found")

    # key docs
    for doc, sev in [
        ("docs/GLOSSARY.md", Severity.LOW),
        ("docs/audit/QUESTIONS_REGISTRY.md", Severity.LOW),
        ("docs/ARCHITECTURE.md", Severity.INFO),
        ("docs/archive/HANDOFF.md", Severity.INFO),
    ]:
        if ctx.exists(doc):
            p.ok(f"{doc} is present")
        else:
            p.add(sev, f"{doc} not found")

    # registries of audits
    audit_dir = ctx.path("docs/audit")
    if audit_dir.is_dir():
        files = [f.name for f in audit_dir.glob("*.md")]
        p.ok(
            f"The docs/audit/ directory is present ({len(files)} reports)",
            evidence=", ".join(sorted(files)[:10]),
        )
    else:
        p.info("docs/audit/ not found")

    # RED inventory
    red_present = []
    for f in ctx.text_files():
        rp = ctx.rel(f)
        if any(pat in rp for pat in RED_PATH_PATTERNS):
            red_present.append(rp)
    p.info(
        f"RED path: files for the owner’s manual rollout — {len(red_present)}",
        detail="Changes to these paths are merged/rolled out only by the owner (ADR-015, the production safety valve).",
        evidence="\n".join(sorted(set(red_present))[:12]),
    )

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 12 — Repository hygiene (git)                                      #
# --------------------------------------------------------------------------- #


def audit_git(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "GIT")
    if not ctx.has_git:
        p.skip("Git hygiene", "git is unavailable or not a git repository")
        return p.F

    ls = ctx.run(["git", "ls-files"], timeout=30)
    if ls.skipped:
        p.skip("git ls-files", ls.skipped)
        return p.F
    tracked = ls.out.splitlines()

    # 1. Junk in the index
    junk_rx = re.compile(
        r"(__pycache__|\.pyc$|\.pyo$|\.DS_Store$|\.log$|/backups?/|\.sqlite3?$|\.coverage$)"
    )
    junk = [f for f in tracked if junk_rx.search(f)]
    if junk:
        p.med(
            f"Junk artifacts in the git index: {len(junk)}",
            detail="Consistent with T-13/FIX-08 (git hygiene).",
            evidence="\n".join(junk[:12]),
            fix="git rm -r --cached <...> + extend .gitignore.",
        )
    else:
        p.ok("There are no junk artifacts in the git index")

    # 2. Large binaries in the index
    big = []
    for f in tracked:
        fp = ctx.root / f
        try:
            sz = fp.stat().st_size
        except OSError:
            continue
        if sz > LARGE_TRACKED_BYTES and fp.suffix not in {".json"}:
            big.append(f"{f} ({sz // 1024} KB)")
    if big:
        p.low(
            f"Large files in git (> {LARGE_TRACKED_BYTES // 1024} KB): {len(big)}",
            evidence="\n".join(big[:10]),
            fix="Consider git-lfs or moving them out of the repo (NB: cpv_codes.json ~672 KB — known, SITE-13).",
        )
    else:
        p.ok("There are no large binaries in the git index")

    # 3. .gitignore coverage
    gi = ctx.read(".gitignore") or ""
    need = ["__pycache__", ".env", "*.pyc", ".venv"]
    miss = [n for n in need if n not in gi]
    if miss:
        p.low(".gitignore does not cover: " + ", ".join(miss))
    else:
        p.ok(".gitignore covers the basic patterns")

    # 4. Secrets in the history (if gitleaks is available — it is already in SEC; here we do no quick grep over the recent commits,
    #    so as not to duplicate; we limit ourselves to the index)
    env_tracked = [f for f in tracked if re.search(r"(^|/)\.env$", f)]
    if env_tracked:
        p.crit(".env is committed to the repository", evidence="\n".join(env_tracked))
    else:
        p.ok(".env is absent from the index")

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 13 — Performance and scaling                                           #
# --------------------------------------------------------------------------- #


def audit_perf(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "PERF")
    vdir = _alembic_dir(ctx)

    # 1. Indexes in the migrations
    if vdir:
        idx = ctx.grep(
            r"op\.create_index|CREATE\s+INDEX|index=True|Index\(",
            subdirs=(ctx.rel(vdir),),
            exts={".py"},
            limit=300,
        )
        if idx:
            p.ok(f"Indexes are present in the migrations: {len(idx)} declarations")
        else:
            p.med(
                "No index creation found in the migrations",
                detail="FK/lookup columns without indexes → seq scan under load.",
                fix="Indexes on foreign keys and filter/sort fields.",
            )
    else:
        p.skip("DB indexes", "migrations directory not found")

    # 2. ANN index for pgvector
    if ctx.grep(
        r"\bvector\b|pgvector|embedding",
        subdirs=("src", "alembic", "migrations"),
        exts={".py", ".sql"},
    ):
        ann = ctx.grep(
            r"ivfflat|hnsw|vector_cosine_ops|vector_l2_ops|USING\s+ivfflat|USING\s+hnsw",
            subdirs=("src", "alembic", "migrations"),
            exts={".py", ".sql"},
        )
        if ann:
            p.ok("An ANN index for vector search is present (ivfflat/hnsw)")
        else:
            p.med(
                "pgvector is used, but no ANN index (ivfflat/hnsw) found",
                detail="Without an ANN index vector search does a full scan — degradation as the data grows.",
                fix="CREATE INDEX ... USING hnsw/ivfflat (vector_cosine_ops).",
            )
    else:
        p.skip("ANN index of pgvector", "no signs of vector search found")

    # 3. Connection pool
    pool = ctx.grep(
        r"pool_size|max_overflow|create_async_engine|asyncpg[^\n]*[Pp]ool|NullPool|pool_pre_ping",
        subdirs=("src",),
        exts={".py"},
    )
    if pool:
        p.ok("The connection pool configuration is present", evidence=p.ev(pool, 4))
    else:
        p.low(
            "No explicit connection pool setup found",
            fix="Set pool_size/max_overflow/pool_pre_ping on the async engine.",
        )

    # 4. Blocking calls in async modules (a file-level heuristic)
    blocking: list[tuple[Path, int, str]] = []
    for f in ctx.files_under(("src",), exts={".py"}):
        txt = ctx.read(ctx.rel(f)) or ""
        if "async def" not in txt:
            continue
        for i, line in enumerate(txt.splitlines(), 1):
            if re.search(
                r"\btime\.sleep\(|\brequests\.(get|post|put|delete)\(|urllib\.request\.urlopen\(",
                line,
            ):
                blocking.append((f, i, line.strip()[:150]))
    if blocking:
        p.med(
            f"Blocking calls in async modules: {len(blocking)}",
            detail="time.sleep/requests/urllib block the event loop — concurrency drops.",
            evidence=p.ev(blocking, 6),
            fix="asyncio.sleep; httpx.AsyncClient/aiohttp instead of requests.",
        )
    else:
        p.ok("No gross blocking calls found in the async code")

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 14 — HTTP layer (FastAPI)                                          #
# --------------------------------------------------------------------------- #


_API_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


def _api_docs_open_paths(ctx: Ctx) -> Optional[list[str]]:
    """API documentation paths mounted without API_DOCS_ENABLED.

    Importing the application in a separate process with the flag cleared; we look at the route table
    of the live FastAPI object. None — the application does not import.
    """
    probe = (
        "import json, os\n"
        "os.environ.pop('API_DOCS_ENABLED', None)\n"
        "from src.api.main import app\n"
        "paths = {getattr(r, 'path', '') for r in app.routes}\n"
        f"print(json.dumps(sorted(paths & set({list(_API_DOCS_PATHS)!r}))))\n"
    )
    # PYTHONPATH = the root of the tree under check: otherwise the inherited PYTHONPATH
    # (the supervisor gate) substitutes ITS OWN ``src`` package and the probe sees the wrong application.
    env = {**os.environ, "PYTHONPATH": str(ctx.root)}
    res = ctx.run([sys.executable, "-c", probe], timeout=60, env=env)
    if not res.ok:
        return None
    return json.loads(res.out.strip().splitlines()[-1])


def audit_api(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "API")
    if not ctx.exists("src/api"):
        p.skip("HTTP layer (src/api)", "directory not found")
        return p.F

    routes = ctx.grep(
        r"@(app|router)\.(get|post|put|delete|patch)\(",
        subdirs=("src/api",),
        exts={".py"},
        limit=500,
    )
    deps = ctx.grep(r"Depends\(", subdirs=("src/api",), exts={".py"}, limit=500)
    p.info(f"HTTP routes: {len(routes)}; uses of Depends(): {len(deps)}")
    if routes and not deps:
        p.med(
            "Routes exist, but Depends() is not used",
            detail="There may be no centralised authentication/validation on the endpoints.",
            fix="Protect the private routes with an authentication dependency.",
        )

    # Pydantic input validation
    if ctx.grep(r"BaseModel|pydantic", subdirs=("src/api",), exts={".py"}):
        p.ok("Pydantic models are present (input validation)")
    else:
        p.low(
            "No pydantic models are visible in the API",
            fix="Validate the input via pydantic schemas.",
        )

    # OpenAPI/docs in production: we check the BEHAVIOUR (which paths are actually mounted
    # without API_DOCS_ENABLED), not the literal. From #607 docs_url is set conditionally,
    # there is no literal docs_url=None in the code — the text grep was lying that they were “open”.
    open_docs = _api_docs_open_paths(ctx)
    if open_docs is None:
        p.skip(
            "OpenAPI/docs in production",
            "the application does not import in this environment (no dependencies)",
        )
    elif open_docs:
        p.info(
            "FastAPI serves the API documentation without API_DOCS_ENABLED: "
            + ", ".join(open_docs),
            detail="The schema discloses the endpoints of the admin, webhooks and payments.",
            fix="Keep docs_url/openapi_url conditional (API_DOCS_ENABLED) or close them with authorization.",
        )
    else:
        p.ok("Публичные /docs, /redoc, /openapi.json закрыты без API_DOCS_ENABLED")

    # Body size limit
    if ctx.grep(
        r"max.{0,3}(content|upload|body).{0,3}(length|size)|MAX_UPLOAD|content_length",
        subdirs=("src",),
        exts={".py"},
    ):
        p.ok("There is a limit on the request/upload size")
    else:
        p.low(
            "No request body size limit found",
            detail="Protection against large payloads (DoS).",
        )

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 15 — Telegram bot (aiogram)                                         #
# --------------------------------------------------------------------------- #


def audit_bot(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "BOT")
    if not ctx.exists("src/bots"):
        p.skip("Telegram bot (src/bots)", "directory not found")
        return p.F

    # Global aiogram error handler
    if ctx.grep(
        r"@dp\.errors|@router\.errors|errors\.register|ErrorEvent|register_errors_handler|error_handler",
        subdirs=("src",),
        exts={".py"},
    ):
        p.ok("The global bot error handler is registered")
    else:
        p.med(
            "No global aiogram error-handler found",
            detail="An unhandled exception in a handler drops the update without diagnostics.",
            fix="Register an errors handler with a log + an alert to ADMIN_CHAT_ID.",
        )

    # Token from the environment (not hard-coded)
    hard = ctx.grep(
        r"(bot_token|BOT_TOKEN|api_token)\s*[:=]\s*['\"]\d{6,}:[A-Za-z0-9_\-]{20,}",
        subdirs=("src", "infra", "deploy"),
        exts={".py", ".yaml", ".yml"},
    )
    if hard:
        p.crit(
            "The bot token appears to be hard-coded",
            evidence=p.ev(hard, 3),
            fix="Only from ENV; rotate the token.",
        )
    elif ctx.grep(
        r"BOT_TOKEN|TELEGRAM_TOKEN|getenv\(['\"][^'\"]*TOKEN",
        subdirs=("src",),
        exts={".py"},
    ):
        p.ok("The bot token is taken from the environment")
    else:
        p.info("The source of the bot token was not recognized")

    # Webhook / polling and secret_token
    if ctx.grep(
        r"set_webhook|start_polling|run_polling", subdirs=("src",), exts={".py"}
    ):
        if ctx.grep(
            r"secret_token|webhook[^\n]*secret", subdirs=("src",), exts={".py"}
        ):
            p.ok("The webhook has a secret_token set")
        else:
            p.info(
                "Webhook/polling mode — check the secret_token of the webhook",
                detail="For set_webhook set a secret_token, otherwise there is a risk of forged updates.",
            )

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 16 — Configuration and environment                                       #
# --------------------------------------------------------------------------- #


def audit_config(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "CONFIG")

    env_refs: set[str] = set()
    rx = re.compile(
        r"""os\.environ\[\s*['"]([A-Z0-9_]+)['"]|os\.environ\.get\(\s*['"]([A-Z0-9_]+)['"]"""
        r"""|os\.getenv\(\s*['"]([A-Z0-9_]+)['"]|[^.\w]getenv\(\s*['"]([A-Z0-9_]+)['"]"""
    )
    for f in ctx.files_under(("src",), exts={".py"}):
        txt = ctx.read(ctx.rel(f)) or ""
        for m in rx.finditer(txt):
            name = next((g for g in m.groups() if g), None)
            if name:
                env_refs.add(name)

    if not env_refs:
        p.info(
            "No direct accesses to os.environ/getenv found (possibly pydantic Settings)."
        )
    else:
        docs_blob = ""
        for cand in (
            ".env.example",
            ".env.sample",
            "env.example",
            "deploy/.env.example",
            "infra/secrets.env.example",
            "deploy/docker-compose.yml",
            "deploy/docker-compose.yaml",
            "README.md",
        ):
            docs_blob += "\n" + (ctx.read(cand) or "")
        undocumented = sorted(v for v in env_refs if v not in docs_blob)
        p.info(f"ENV variables used in the code: {len(env_refs)}")
        if undocumented:
            sev = Severity.MEDIUM if len(undocumented) > 5 else Severity.LOW
            p.add(
                sev,
                f"ENV variables without documentation/example: {len(undocumented)}",
                detail="Absent from .env.example/compose/README — the risk of missing a variable during deploy.",
                evidence=", ".join(undocumented[:25]),
                fix="Create a .env.example with all the variables and keep it current.",
            )
        else:
            p.ok("All ENV variables of the code appear in the examples/compose/README")

    if any(
        ctx.exists(c)
        for c in (
            ".env.example",
            ".env.sample",
            "deploy/.env.example",
            "infra/secrets.env.example",
        )
    ):
        p.ok(".env.example is present")
    else:
        p.low(
            "No .env.example file found",
            fix="Add an environment example for a reproducible deploy.",
        )

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 17 — Localization (sr/ru/en)                                         #
# --------------------------------------------------------------------------- #


def audit_i18n(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "I18N")
    i18n_file = next(
        (
            c
            for c in ("src/api/public/i18n.py", "src/api/i18n.py", "src/i18n.py")
            if ctx.exists(c)
        ),
        None,
    )
    if not i18n_file and not ctx.grep(
        r"i18n|gettext|babel|resolve_lang", subdirs=("src",), exts={".py"}
    ):
        p.skip("Localization", "no i18n mechanism detected")
        return p.F

    if i18n_file:
        p.ok(f"The localization module is present: {i18n_file}")
        blob = ctx.read(i18n_file) or ""
        langs = [ln for ln in ("sr", "ru", "en") if re.search(rf"['\"]{ln}['\"]", blob)]
        if set(langs) >= {"sr", "ru", "en"}:
            p.ok("sr/ru/en are supported")
        else:
            p.low(
                f"Not all languages were found in i18n: {', '.join(langs) or '—'}",
                detail="sr/ru/en were expected.",
            )
    else:
        p.info(
            "The i18n mechanism exists, but a separate module is not localised along the expected paths."
        )

    todo = ctx.grep(r"i18n_todo|i18n-todo|TODO[^\n]*i18n", subdirs=("src",), limit=100)
    if todo:
        p.med(
            f"Service i18n_todo markers remain: {len(todo)}",
            detail="SITE-26 removed such markers from the production labels — check whether they stick out in the UI.",
            evidence=p.ev(todo, 6),
        )
    else:
        p.ok("No i18n_todo markers found")

    return p.F


# --------------------------------------------------------------------------- #
#  MODULE 18 — Privacy and compliance (GDPR / Serbian ZZPL)                  #
# --------------------------------------------------------------------------- #


def audit_compliance(ctx: Ctx) -> list[Finding]:
    p = Probe(ctx, "COMPLIANCE")

    # Legal pages (privacy/terms/impressum) — SITE-05
    legal_sources = (
        "src/api/public/legal_texts.py",
        "src/api/public/legal.html",
        "src/api/templates/legal.html",
    )
    legal_blob = "\n".join((ctx.read(c) or "") for c in legal_sources)
    present = {}
    for k in ("privacy", "terms", "impressum"):
        in_legal = bool(re.search(k, legal_blob, re.I))
        in_code = bool(ctx.grep(k, subdirs=("src",), exts={".py", ".html"}))
        present[k] = in_legal or in_code
    missing = [k for k, v in present.items() if not v]
    if missing:
        p.high(
            "Legal pages not found: " + ", ".join(missing),
            detail="A SaaS in the EU/Serbia must publish a privacy policy and terms (GDPR/ZZPL). SITE-05.",
            fix="Publish privacy/terms/impressum (legal_texts.py + the /legal/* routes).",
        )
    else:
        p.ok("The legal pages privacy/terms/impressum are present")

    # PII/secrets in the logs
    pii = ctx.grep(
        r"(logger|log|logging)\.(debug|info|warning|error|exception)\([^)]*(email|password|passwd|secret|token|chat_id|phone)",
        subdirs=("src",),
        exts={".py"},
    )
    if pii:
        p.med(
            f"Possible logging of PII/secrets: {len(pii)}",
            detail="Email/phone/token/chat_id in the logs — a risk of leaking personal data (GDPR).",
            evidence=p.ev(pii, 6),
            fix="Mask/do not log PII; log only depersonalised identifiers.",
        )
    else:
        p.ok("No crude logging of PII/secrets found")

    # Data deletion / retention
    if ctx.grep(
        r"delete[^\n]*user|anonymi[sz]e|anonymize|retention|right[^\n]*forgott|gdpr|\bttl\b|expire",
        subdirs=("src",),
        exts={".py"},
    ):
        p.ok("There are traces of user data deletion/retention")
    else:
        p.low(
            "No path for deleting user data found (the right to be forgotten)",
            detail="GDPR/ZZPL: it must be possible to delete personal data on request.",
            fix="Implement account/PII deletion.",
        )

    return p.F


# --------------------------------------------------------------------------- #
#  Registry of modules                                                            #
# --------------------------------------------------------------------------- #

MODULES: list[tuple[str, str, Callable[[Ctx], list[Finding]]]] = [
    ("SEC", "Security and AppSec", audit_security),
    ("DEPS", "Dependencies and supply chain", audit_deps),
    ("CODE", "Code quality and style", audit_code),
    ("TEST", "QA and tests", audit_tests),
    ("DB", "Database and migrations", audit_db),
    ("PERF", "Performance and scaling", audit_perf),
    ("INFRA", "DevOps and infrastructure", audit_infra),
    ("ARCH", "Architecture (multi-tenant / multi-country)", audit_arch),
    ("API", "HTTP layer (FastAPI)", audit_api),
    ("BOT", "Telegram bot (aiogram)", audit_bot),
    ("OBS", "Observability and reliability", audit_obs),
    ("AI", "AI pipeline", audit_ai),
    ("DATA", "Data collection and parsing", audit_data),
    ("CONFIG", "Configuration and environment", audit_config),
    ("I18N", "Localization (sr/ru/en)", audit_i18n),
    ("COMPLIANCE", "Privacy and compliance (GDPR/ZZPL)", audit_compliance),
    ("DOCS", "Context ledger and management", audit_docs),
    ("GIT", "Repository hygiene", audit_git),
]


# --------------------------------------------------------------------------- #
#  Reporting                                                                 #
# --------------------------------------------------------------------------- #

SEV_ORDER = [
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
    Severity.OK,
    Severity.SKIP,
]


def tally(results: list[ModuleResult]) -> dict[Severity, int]:
    t = {s: 0 for s in SEV_ORDER}
    for r in results:
        for f in r.findings:
            t[f.severity] += 1
    return t


def print_console(results: list[ModuleResult], mode: str, root: Path):
    print("=" * 78)
    print(
        f" TenderSRB — PROJECT-WIDE AUDIT   |   mode: {mode}   |   root: {root}"
    )
    print(f" {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 78)
    for r in results:
        head = f"\n■ [{r.mid}] {r.name}"
        print(head)
        if r.crashed:
            print(f"   [CRIT] module crashed: {r.crashed}")
            continue
        ordered = sorted(r.findings, key=lambda f: SEV_ORDER.index(f.severity))
        for f in ordered:
            print(f"   {f.severity.mark} {f.title}")
            if f.evidence and f.severity >= Severity.MEDIUM:
                for line in f.evidence.splitlines()[:4]:
                    print(f"          · {line}")
    t = tally(results)
    print("\n" + "-" * 78)
    print(
        " TOTAL: "
        + "   ".join(
            f"{s.label}={t[s]}" for s in SEV_ORDER if t[s] or s <= Severity.HIGH
        )
    )
    print("-" * 78)


def build_markdown(results: list[ModuleResult], mode: str, root: Path) -> str:
    t = tally(results)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    out: list[str] = []
    a = out.append
    a("# Project-wide audit of TenderSRB — report\n")
    a(f"> Generated by `full_audit.py` · {now} · mode **{mode}** · root `{root}`\n")
    a(
        "> Status **SKIP** = the check was NOT performed (no tool/resource); this is NOT “passed”.\n"
    )

    # executive summary
    a("\n## Summary by severity\n")
    a("| CRITICAL | HIGH | MEDIUM | LOW | INFO | OK | SKIP |")
    a("|---:|---:|---:|---:|---:|---:|---:|")
    a(
        f"| {t[Severity.CRITICAL]} | {t[Severity.HIGH]} | {t[Severity.MEDIUM]} | "
        f"{t[Severity.LOW]} | {t[Severity.INFO]} | {t[Severity.OK]} | {t[Severity.SKIP]} |"
    )

    # top issues
    top = []
    for r in results:
        for f in r.findings:
            if f.severity in (Severity.CRITICAL, Severity.HIGH):
                top.append(f)
    if top:
        a("\n## 🔴 Priority findings (CRITICAL/HIGH)\n")
        for f in sorted(top, key=lambda x: SEV_ORDER.index(x.severity)):
            a(
                f"- **[{f.severity.label}] [{f.module}] {f.title}**"
                + (f" — {f.detail}" if f.detail else "")
            )
            if f.fix:
                a(f"  - _Fix:_ {f.fix}")
    else:
        a("\n## 🟢 No CRITICAL/HIGH findings\n")

    # per-module
    a("\n## Details by direction\n")
    for r in results:
        a(f"\n### [{r.mid}] {r.name}\n")
        if r.crashed:
            a(f"> ⚠️ Module crashed: `{r.crashed}`\n")
            continue
        a("| Sev. | Finding | Details | Evidence | Fix |")
        a("|---|---|---|---|---|")
        for f in sorted(r.findings, key=lambda x: SEV_ORDER.index(x.severity)):
            ev = (f.evidence or "").replace("\n", "<br>").replace("|", "\\|")[:400]
            dt = (f.detail or "").replace("\n", " ").replace("|", "\\|")[:300]
            fx = (f.fix or "").replace("\n", " ").replace("|", "\\|")[:300]
            a(
                f"| {f.severity.label} | {f.title.replace('|','\\|')} | {dt} | {ev} | {fx} |"
            )

    a("\n---\n")
    a(
        "_The audit is automatic and heuristic: INFO/heuristics require manual verification. "
        "RED paths and decisions at the owner level — outside automatic edits._\n"
    )
    return "\n".join(out)


def build_json(results: list[ModuleResult], mode: str) -> str:
    payload = {
        "tool": "full_audit.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "summary": {s.label: tally(results)[s] for s in SEV_ORDER},
        "modules": [
            {
                "id": r.mid,
                "name": r.name,
                "crashed": r.crashed,
                "findings": [
                    {
                        "severity": f.severity.label,
                        "title": f.title,
                        "detail": f.detail,
                        "evidence": f.evidence,
                        "fix": f.fix,
                    }
                    for f in r.findings
                ],
            }
            for r in results
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
#  main                                                                       #
# --------------------------------------------------------------------------- #


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="TenderSRB — project-wide audit (18 expert directions)."
    )
    ap.add_argument("--root", default=".", help="repository root (default .)")
    ap.add_argument(
        "--mode",
        choices=["static", "full"],
        default="static",
        help="static (safe in CI) | full (on the VPS: docker/psql/containers)",
    )
    ap.add_argument(
        "--only", default="", help="comma-separated list of modules, e.g. SEC,DB,INFRA"
    )
    ap.add_argument(
        "--run-tests",
        action="store_true",
        help="full pytest run with coverage (may take long)",
    )
    ap.add_argument(
        "--out",
        default="",
        help="path to the markdown report (default audit_report_<date>.md)",
    )
    ap.add_argument(
        "--json", default="", help="additionally write the machine-readable JSON to this path"
    )
    ap.add_argument(
        "--fail-on",
        choices=["critical", "high", "medium", "low", "never"],
        default="high",
        help="non-zero exit-code threshold",
    )
    ap.add_argument("--list", action="store_true", help="show the modules and exit")
    args = ap.parse_args(argv)

    if args.list:
        print("Audit modules (expert directions):")
        for mid, name, _ in MODULES:
            print(f"  {mid:5s} — {name}")
        return 0

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"ERROR: root does not exist: {root}", file=sys.stderr)
        return 3

    only = {m.strip().upper() for m in args.only.split(",") if m.strip()}
    ctx = Ctx(root=root, mode=args.mode, run_tests=args.run_tests)

    results: list[ModuleResult] = []
    for mid, name, fn in MODULES:
        if only and mid not in only:
            continue
        res = ModuleResult(mid=mid, name=name)
        try:
            res.findings = fn(ctx)
        except Exception as e:  # noqa: BLE001 — a module must not fail the audit
            res.crashed = f"{type(e).__name__}: {e}"
        results.append(res)

    print_console(results, args.mode, root)

    out_path = (
        Path(args.out)
        if args.out
        else Path(f"audit_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md")
    )
    out_path.write_text(build_markdown(results, args.mode, root), encoding="utf-8")
    print(f"\nMarkdown report: {out_path}")
    if args.json:
        Path(args.json).write_text(build_json(results, args.mode), encoding="utf-8")
        print(f"JSON report:     {args.json}")

    # exit code
    t = tally(results)
    threshold = {
        "critical": [Severity.CRITICAL],
        "high": [Severity.CRITICAL, Severity.HIGH],
        "medium": [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM],
        "low": [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW],
        "never": [],
    }[args.fail_on]
    hits = sum(t[s] for s in threshold)
    if any(r.crashed for r in results):
        hits += 1
    if hits == 0:
        return 0
    return (
        2
        if (t[Severity.CRITICAL] or t[Severity.HIGH]) and Severity.HIGH in threshold
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
