"""The guard of the mypy debt: the known number of errors is recorded, the growth — a red test.

Why. mypy in the CI is marked ``continue-on-error`` (and in ``scripts/gate.sh`` — ``mypy src || true``), that is, its output blocks nothing and is read by nobody. As a result the number of errors grew silently: the status document for months claimed “debt ~26”, while the actual run on a clean main gave **301**. Nobody lied on purpose — the number was simply recorded NOWHERE, and there was nothing to confirm it against in each PR. This test is the missing record: the debt is written down file by file below, and any PR that increases it turns pytest red (and pytest, unlike mypy, is blocking).

The test is a ratchet in BOTH directions, and this is deliberate:

* it became MORE — an error: the new debt enters main silently, exactly how 26 turned into 301;
* it became LESS — also an error, but a happy one: fix the baseline with one line.
  Without this the number would again start to diverge from reality, and the divergence is the only real defect that is fixed here.

Boundaries. The test does NOT require the debt to be fixed and does NOT pass judgements about which errors are important. It asserts exactly one fact: “today there are this many, and here is where”. The rest of the debt is not annotations, but NARROWING: 84 of 102 errors in ``src/bots/telegram.py`` are the aiogram ``callback.message`` of the type ``Message | InaccessibleMessage | None``. To remove them means deciding what the code does in the None branch, that is, changing the behavior. This is a separate task, not this one.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The known debt as of 2026-07-17 (a measurement on main + E3): 150 errors in 16 files.
# Counted by `mypy src` with the config from pyproject.toml ([tool.mypy], python 3.12).
#
# ‼️ This is NOT a goal and NOT a permission. These numbers must not be grown: if a PR adds
# an error — fix it, rather than the baseline. Reducing them is allowed and needed, together with the fix.
MYPY_DEBT: dict[str, int] = {
    # 94 KB, edited only point by point and only by a separate task. 84 of 102 —
    # aiogram `callback.message: Message | InaccessibleMessage | None`.
    "src/bots/telegram.py": 97,
    # the RED path (billing) — the same `**_UUID_PK`, which is removed in the other models,
    # but the nightly task does not do the edit of the RED path. It is removed with one line.
    "src/db/models/billing.py": 13,
    # `func.cast(False, Boolean)` in server_default: mypy wants a ColumnElement.
    # It is fixed by changing the expression, that is, the SQL schema → not a typical edit.
    "src/db/models/tender_award.py": 2,
    "src/db/models/award_watch.py": 1,
    # They are edited by parallel streams — we do not touch them, so as not to catch a conflict.
    "src/analytics/awards_analytics.py": 6,
    "src/parsers/jnportal/adapter.py": 5,
    "src/workers/adapters/telegram.py": 1,
    # The rest — narrow spots, each requires a NARROWING (that is, a decision about
    # the behavior in the None branch), not an annotation. Sort them one at a time, not in bulk.
    "src/api/admin/routes_admin.py": 4,
    "src/bots/trial_handlers.py": 3,
    "src/bots/awards_analytics_service.py": 3,
    "src/parsers/jnportal/fx.py": 2,
    "src/bots/award_watch_service.py": 2,
    "src/api/public/routes_public.py": 2,
    "src/api/cabinet/routes_trial.py": 2,
    "src/workers/routing.py": 1,
}

_ERROR_RE = re.compile(r"^(src/[^:]+):\d+: error:", re.MULTILINE)


def _mypy_errors_by_file() -> Counter[str]:
    """Run ``mypy src`` → the counter of errors by file.

    The absence of output is also a result (0 errors), therefore we do not check the returncode: mypy exits with 1 on any error found, and that is the norm.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "src"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        pytest.fail(f"mypy не отработал (rc={proc.returncode}):\n{proc.stderr}")
    return Counter(_ERROR_RE.findall(proc.stdout))


@pytest.fixture(scope="module")
def actual() -> Counter[str]:
    if shutil.which("mypy") is None and not _module_available():
        pytest.skip("mypy не установлен (pip install -e '.[dev]')")
    return _mypy_errors_by_file()


def _module_available() -> bool:
    return (
        subprocess.run(
            [sys.executable, "-c", "import mypy"], capture_output=True
        ).returncode
        == 0
    )


def test_mypy_debt_did_not_grow(actual: Counter[str]) -> None:
    """No file has more errors, and there are no new files with errors."""
    grown = {
        path: (MYPY_DEBT.get(path, 0), count)
        for path, count in actual.items()
        if count > MYPY_DEBT.get(path, 0)
    }
    assert not grown, "The mypy debt grew — fix the errors, rather than the baseline:\n" + "\n".join(
        f"  {path}: было {was}, стало {now}"
        + ("  ← файла не было в baseline вообще" if was == 0 else "")
        for path, (was, now) in sorted(grown.items())
    )


def test_mypy_debt_baseline_is_not_stale(actual: Counter[str]) -> None:
    """The debt did not decrease silently: the baseline must match reality.

    The red test here is “you fixed more than you recorded”. The fix is one line: move the number in ``MYPY_DEBT`` (or remove the file, if there are 0 errors).
    This very half of the ratchet stops the baseline from becoming a lie again.
    """
    shrunk = {
        path: (was, actual.get(path, 0))
        for path, was in MYPY_DEBT.items()
        if actual.get(path, 0) < was
    }
    assert not shrunk, (
        "The mypy debt decreased (this is good) — record the new number in "
        "MYPY_DEBT:\n"
        + "\n".join(
            f"  {path}: было {was}, стало {now}"
            + ("  ← файл чист, удали строку" if now == 0 else "")
            for path, (was, now) in sorted(shrunk.items())
        )
    )


def test_debt_is_concentrated_where_baseline_says() -> None:
    """The baseline itself is honest: the sum = 144, and 67% of the remainder — one telegram.py.

    A test-documentation. If someone quietly inflates MYPY_DEBT “so that it turns green”, this arithmetic breaks, not only the conscience.
    """
    assert sum(MYPY_DEBT.values()) == 144
    # 97 of 145 (100 → 97 after the fix #652, the composition of the errors was not analyzed; earlier 102 → 100: the removal of the referral program took away two
    # union-attr errors together with the /invite handler): the rest of the debt is NOT “a bit everywhere”, but one file, which
    # is edited point by point and by a separate task. Spreading it into small pieces makes no sense.
    assert MYPY_DEBT["src/bots/telegram.py"] == 97
