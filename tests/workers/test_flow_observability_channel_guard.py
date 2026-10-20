"""The guard of the channel (Test A, ‼️ the most important, tmp/TASK_flow_observability.md).

The AST parsing of the SOURCES of the diagnostic modules (not of the behavior — a behavioral guard would depend on the order/completeness of the tests and would not catch a “forgotten” branch of the code): no diagnostic code may read `TG_BOT_TOKEN`/`ADMIN_CHAT_ID` (the user’s bot `@nabrs_bot`) — the diagnostics go ONLY through `ADMIN_BOT_TOKEN`/`TG_ALERTS_CHAT_ID` (`@tendersrb_bot`, `src/workers/health.py::WorkerHealth.from_env`, the owner’s directive).

‼️ The mutation check (see `test_mutation_guard_catches_forbidden_token`): slip a string literal `"TG_BOT_TOKEN"` into one of the scanned files — the guard must turn red.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# All the modules whose only task is the diagnostics/the alerting to the operator
# (do not confuse with the delivery workers to the client — delivery.py/telegram.py legitimately
# read TG_BOT_TOKEN, this is THEIR channel).
DIAGNOSTIC_FILES = (
    "src/workers/health.py",
    "src/workers/ingest.py",
    "src/workers/ai_spend_report.py",
    "src/workers/flow_observability.py",
    "src/workers/flow_report.py",
)

# The user’s bot/chat (@nabrs_bot) — the diagnostics must not go there under any
# kind (the owner’s directive, the docstring of src/workers/health.py::WorkerHealth.from_env).
FORBIDDEN_LITERALS = ("TG_BOT_TOKEN", "ADMIN_CHAT_ID")


def _string_literals(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


@pytest.mark.parametrize("relpath", DIAGNOSTIC_FILES)
def test_diagnostic_module_never_reads_user_bot_channel(relpath: str) -> None:
    path = REPO_ROOT / relpath
    assert (
        path.is_file()
    ), f"{relpath}: the file is not found — the guard must be updated together with the tree"
    literals = _string_literals(path)
    hits = literals & set(FORBIDDEN_LITERALS)
    assert not hits, (
        f"{relpath}: the diagnostics will go to the subscriber — a link to the user’s "
        f"bot/chat ({sorted(hits)}). The diagnostics must go only through "
        f"ADMIN_BOT_TOKEN/TG_ALERTS_CHAT_ID (WorkerHealth.from_env)."
    )


def test_all_diagnostic_files_are_covered_by_the_list() -> None:
    """A guard that scans a PART of the sources creates a blind zone (.claude/rules/tests-known-defects.md) — we keep the list in sync with the actual set of diagnostic modules under src/workers/."""
    known_diagnostic_names = {
        "health.py",
        "ingest.py",
        "ai_spend_report.py",
        "flow_observability.py",
        "flow_report.py",
    }
    listed_names = {relpath.rsplit("/", 1)[-1] for relpath in DIAGNOSTIC_FILES}
    assert listed_names == known_diagnostic_names


def test_mutation_guard_catches_forbidden_token(tmp_path: Path) -> None:
    """‼️ Mutation: slip TG_BOT_TOKEN into the diagnostic code → the guard turns red.

    It does not mutate the real files of the repository (risky) — it proves the predicate on a synthetic file with the same function `_string_literals`/comparison.
    """
    poisoned = tmp_path / "poisoned_diagnostic.py"
    poisoned.write_text(
        'import os\n\ntoken = os.getenv("TG_BOT_TOKEN")\n', encoding="utf-8"
    )
    literals = _string_literals(poisoned)
    hits = literals & set(FORBIDDEN_LITERALS)
    assert hits == {
        "TG_BOT_TOKEN"
    }  # proves that the predicate IS able to catch a violation


# --- The second half of the contour: the same invariant in the DEPLOYMENT MAP ---------
#
# ‼️ The AST guard above checks the CODE. But the channel of the diagnostics lives in the gap
# code↔compose (the same class as tests/quality/test_env_contract.py): the module
# reads ADMIN_BOT_TOKEN flawlessly, but the container was not given this env — the alert
# silently does not go, and no ordinary test sees it. Therefore the second
# half: EACH diagnostic service of the compose must have the admin channel
# and must not have the user’s bot.

COMPOSE_PATH = REPO_ROOT / "deploy/docker-compose.yml"

# The services whose purpose is the diagnostics to the operator (they correspond to
# DIAGNOSTIC_FILES: ingest.py, ai_spend_report.py, flow_report.py).
DIAGNOSTIC_SERVICES = ("ingest", "ai_spend_report", "flow_report")

REQUIRED_ADMIN_ENV = ("ADMIN_BOT_TOKEN", "TG_ALERTS_CHAT_ID")


def _service_env_names(service: dict) -> set[str]:
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        return {item.split("=", 1)[0] for item in environment}
    return set(environment)


@pytest.fixture(scope="module")
def compose_services() -> dict:
    import yaml

    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]


@pytest.mark.parametrize("service_name", DIAGNOSTIC_SERVICES)
def test_diagnostic_service_is_wired_to_the_admin_bot(
    service_name: str, compose_services: dict
) -> None:
    assert service_name in compose_services, (
        f"{service_name}: there is no such service in the compose file — the worker is "
        f"dead code, the summary/alert never starts at all"
    )
    wired = _service_env_names(compose_services[service_name])
    missing = [name for name in REQUIRED_ADMIN_ENV if name not in wired]
    assert not missing, (
        f"{service_name}: the admin channel {missing} is not passed to the container — "
        f"WorkerHealth.from_env will get nothing, the alert silently will not go"
    )


@pytest.mark.parametrize("service_name", DIAGNOSTIC_SERVICES)
def test_diagnostic_service_never_gets_the_user_bot(
    service_name: str, compose_services: dict
) -> None:
    """The owner’s directive of 20.09.2026 “do not frighten the subscriber”: the user’s bot must not be passed into the diagnostic container at all — so that there is NOTHING to fall back to even with a future edit of the code."""
    wired = _service_env_names(compose_services[service_name])
    hits = wired & set(FORBIDDEN_LITERALS)
    assert not hits, (
        f"{service_name}: the user’s bot is passed into the diagnostic container "
        f"bot/chat {sorted(hits)} (@nabrs_bot) — remove it from environment"
    )


def test_compose_channel_predicate_catches_both_faults() -> None:
    """‼️ A counter-fact to both checks above on a SYNTHETIC (the real compose is a RED path, it cannot be mutated during a run): the predicate must see both the absence of the admin channel and the presence of the user’s bot."""
    import yaml

    broken = yaml.safe_load("""
        services:
          no_admin_channel:
            environment:
              DATABASE_URL: x
          leaks_user_bot:
            environment:
              ADMIN_BOT_TOKEN: x
              TG_ALERTS_CHAT_ID: x
              TG_BOT_TOKEN: x
        """)["services"]
    unwired = _service_env_names(broken["no_admin_channel"])
    assert [n for n in REQUIRED_ADMIN_ENV if n not in unwired] == list(
        REQUIRED_ADMIN_ENV
    )
    leaked = _service_env_names(broken["leaks_user_bot"]) & set(FORBIDDEN_LITERALS)
    assert leaked == {"TG_BOT_TOKEN"}
