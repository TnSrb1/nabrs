"""infra/vps/scripts/tendersrb-autodeploy.sh — DOCS-SKIP: the skip of the rollout for a doc-only push.

A temporary bare origin + a clone (“the production”), up.sh is substituted by a marker stub through AUTODEPLOY_UP_SCRIPT. The production, systemd and the real up.sh are not touched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra/vps/scripts/tendersrb-autodeploy.sh"

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}

SEED_FILES = [
    "deploy/up.sh",
    "src/a.py",
    "src/parsers/README.md",
    "docs/x.md",
    "README.md",
]


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True
    )
    return res.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def env_repo(tmp_path):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    prod = tmp_path / "prod"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    for rel in SEED_FILES:
        _write(seed, rel, f"seed {rel}\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    _git(tmp_path, "clone", "-q", str(origin), str(prod))

    marker = tmp_path / "up_ran.marker"
    stub = tmp_path / "stub_up.sh"
    # The stub of up.sh: as the real up.sh shifts the clone (reset --hard origin/main), then the marker.
    stub.write_text(
        '#!/bin/bash\ngit reset -q --hard origin/main\ntouch "$AUTODEPLOY_TEST_MARKER"\n',
        encoding="utf-8",
    )
    return {
        "origin": origin,
        "seed": seed,
        "prod": prod,
        "marker": marker,
        "stub": stub,
    }


def _push_change(env, ops):
    seed = env["seed"]
    for op in ops:
        if op[0] == "write":
            _write(seed, op[1], "changed\n")
            _git(seed, "add", op[1])
        elif op[0] == "mv":
            _git(seed, "mv", op[1], op[2])
    _git(seed, "commit", "-q", "-m", "change")
    _git(seed, "push", "-q", "origin", "main")


def _run_autodeploy(env):
    run_env = {
        **GIT_ENV,
        "AUTODEPLOY_REPO_DIR": str(env["prod"]),
        "AUTODEPLOY_UP_SCRIPT": str(env["stub"]),
        "AUTODEPLOY_TEST_MARKER": str(env["marker"]),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=run_env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.parametrize(
    "ops, expect_full_deploy",
    [
        # (a) only docs → skip, the clone is shifted to origin/main
        ([("write", "docs/x.md")], False),
        # (b) docs + code → the full rollout
        ([("write", "docs/x.md"), ("write", "src/a.py")], True),
        # (c) *.md in the repo root → skip
        ([("write", "README.md")], False),
        # (d) *.md inside src/ → the full rollout (a nested .md is not in the whitelist)
        ([("write", "src/parsers/README.md")], True),
        # (e) deploy/ → the full rollout
        ([("write", "deploy/up.sh")], True),
        # (f) an extra: the rename of src/ → docs/ must not look like a doc-only push
        ([("mv", "src/a.py", "docs/a.py")], True),
    ],
    ids=[
        "docs-only",
        "docs-plus-src",
        "root-md",
        "nested-md",
        "deploy",
        "rename-src-to-docs",
    ],
)
def test_docs_skip_decision(env_repo, ops, expect_full_deploy):
    _push_change(env_repo, ops)
    origin_head = _git(env_repo["origin"], "rev-parse", "main")
    prod_head_before = _git(env_repo["prod"], "rev-parse", "HEAD")
    assert prod_head_before != origin_head

    res = _run_autodeploy(env_repo)

    assert res.returncode == 0, res.stdout + res.stderr
    assert env_repo["marker"].exists() is expect_full_deploy, res.stdout + res.stderr
    # In any case the clone must become equal to origin/main (otherwise the next cycle will shift it again).
    assert _git(env_repo["prod"], "rev-parse", "HEAD") == origin_head
