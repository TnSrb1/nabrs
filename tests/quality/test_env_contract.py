"""The guard: the service code requires env — but is it passed to ITS container?

The class of the defect. The code is correct, the tests are green, the review is clean — but the env is absent in the container where the code runs. The error lives neither in the code nor in the compose separately, but in the GAP between them, therefore no ordinary test sees it. The worker will never find it: ``deploy/`` is a RED path for it, it sees only the code.
In one night THREE holes were found this way, all lived in production for months (a historical trace; all three by 17.07 are either closed or moved beyond the guard's boundaries):

1. ``api`` without ``ANTHROPIC_API_KEY`` → all the AI features of the cabinet (#326–#335) and the translation of the card (#367) fell. ``anthropic.AsyncAnthropic()`` reads the key from the env ITSELF — there is not a single ``os.getenv(“ANTHROPIC_API_KEY”)`` in the code, therefore the hole is not found by grep. Closed by the RED-PR #368 (in production on 17.07).
2. ``delivery`` without ``SMTP_HOST``/``POSTMARK_SERVER_TOKEN`` → the email channel silently went to the ``MockEmailSender``: “success” in the log, no letter. Closed by the RED-PR #346.
3. ``ingest`` with empty ``INGEST_HEALTHCHECK_URL``/``ADMIN_BOT_TOKEN`` → the monitoring is off. It is NOT caught here and this is NOT a bug: in ``deploy/docker-compose.yml`` the keys are passed (``${ADMIN_BOT_TOKEN:-}``), they are empty in ``deploy/.env`` on the VPS. A separate class, a separate task of the owner — see “Boundaries” below.

What proves the guard itself (the lesson of 17.07). At first the workability of the heuristic was checked by the assertion “the holes #346/#368 are visible on the current main” — that is, the greenness of the test depended on the production being BROKEN. The holes were patched → the guard turned red, although everything worked correctly. The same class as the stale doc: a record of a debt outlived the debt itself. Now the heuristic is proved on a SYNTHETIC (a hand-written compose fragment below), and the real ``deploy/docker-compose.yml`` is checked for exactly one thing — “there are no holes”. The state of the production no longer affects whether the test proves itself.

How the guard works. Two maps, a reconciliation between them:

* the deployment map — from ``deploy/docker-compose.yml`` (read as DATA, the file is not changed: it is RED): service → the entrypoint module → the passed env;
* the code map — from the AST: which env the modules read that the service loads.

The heuristic (deliberately simple — a clear guard that covers 80% of the cases is better than an exact one that nobody reads). Two different rules, and this is not carelessness:

* the EDGES of the import graph — only module-level (``import`` at the top level of the file, including the wrappers ``if``/``try``). The semantics: “what the interpreter loads at the start of the container”. A lazy ``import`` inside a function is NOT counted as an edge;
* the READS of the env — anywhere inside the loaded module (``ast.walk``). The semantics: “the module is LOADED → we assume any of its env may be needed”.

The asymmetry of the rules is deliberate and it is load-bearing. To narrow the reads to “the functions that are really called” means building a call graph and losing the main finding: the key in #368 is read inside the method ``AiClient._get_sdk``, not at the module level. To widen the edges to the full closure means drowning in the false ones: ``delivery`` inside a function does ``from src.bots.telegram import build_bot``, telegram pulls ``deep_analysis_service`` → ``src.ai.client``, and it turns out that delivery “requires” ANTHROPIC_API_KEY — although AI there is reachable only through the callback handler of the bot, which delivery does not dispatch. Measured on the real main: the full closure gives 10 findings (8 false), the module-level — exactly 2, both real (#368, #346).

Boundaries (what the guard does NOT catch — it is important that a green run is not read wider):

* ‼️ an env DECLARED in the compose but EMPTY in ``deploy/.env`` on the VPS. The guard compares the NAMES, the repository does not see the values at all — a whole CLASS, which is caught only inside the live container. An example from the history — hole #3 (``ingest`` with empty ``ADMIN_BOT_TOKEN``/``TG_ALERTS_CHAT_ID``); it is CLOSED: the keys are in ``secrets.env``, and in ``HOST_KEYS`` (``deploy/up.sh``), and in ``environment:`` — checked in the container on 03.08.2026. The class of holes remains, the concrete example is already history;
* the env of a feature that the service calls ONLY through a lazy import: there is no edge → we will not ask. Concretely: ``delivery`` sends to Telegram via a lazy ``build_bot``, and if you take its ``TG_BOT_TOKEN`` — the guard will stay silent. Widening the edges for the sake of this case was tried: it is caught at the price of three false ones (reminders/summary/award_watch_notifier “require” SMTP merely because the import of the package ``src.workers.adapters`` loads the ``__init__`` with ``make_email_sender``). The false ones would have to be entered into KNOWN_GAPS — and it is about the REAL holes and must clean itself; the junk in it would kill both its meaning and the trust in the guard;
* an env whose name is assembled at runtime (``os.getenv(f“PREFIX_{x}”)``).
"""

from __future__ import annotations

import ast
import re
from collections import deque
from pathlib import Path
from typing import Iterable

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "deploy/docker-compose.yml"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"

# The image of the app services. Everything not on it (caddy/postgres/redis) is not our code.
APP_IMAGE = "tendersrb-app:local"

# The critical env: their absence BREAKS the feature, but silently — without a failure at the start.
# Each is justified; a group = alternatives, any one of them is enough.
#
# The non-critical env (CABINET_*, TURNSTILE_*, DELIVERY_DAILY_CAP, ...) are deliberately
# NOT included here: they have safe defaults, their absence does not break the feature.
CRITICAL_ENV: tuple[tuple[str, ...], ...] = (
    # The SDK reads the key from the env itself; without the key AsyncAnthropic().api_key = None →
    # every AI call falls at runtime. There is no default and there cannot be one.
    ("ANTHROPIC_API_KEY",),
    # Both mail transports are optional separately, but make_email_sender()
    # without BOTH returns MockEmailSender → “success” in the log, no letter (#346).
    # Therefore this is a group: either of the two suits, the absence of both is a hole.
    ("SMTP_HOST", "POSTMARK_SERVER_TOKEN"),
    # Both have a localhost default in the code — inside a container this is silently
    # a wrong address: the service will start and knock on nowhere.
    ("DATABASE_URL",),
    ("REDIS_URL",),
    # An empty token → the adapter will not send a single message.
    ("TG_BOT_TOKEN",),
)

# A synthetic deployment map: on it it is proved that the heuristic WORKS.
# It is hand-written deliberately — the guard must prove itself, rather than depend on whether
# the production is broken now. The code map here is real (the modules are real): we check
# the compose↔code gap itself, not the YAML parser.
#
# The form of the hole is exactly #346: the worker calls make_email_sender(), there is no mail env.
SYNTHETIC_COMPOSE = """
services:
  mailer_unwired:
    image: tendersrb-app:local
    command: ["python", "-m", "src.workers.delivery"]
    environment:
      DATABASE_URL: ${DATABASE_URL}
  mailer_wired:
    image: tendersrb-app:local
    command: ["python", "-m", "src.workers.delivery"]
    environment:
      DATABASE_URL: ${DATABASE_URL}
      SMTP_HOST: ${SMTP_HOST:-}
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: не-наш-образ-сторожа-не-касается
"""

_CMD_RE = re.compile(r"CMD\s+\[(.*?)\]", re.DOTALL)


def _dockerfile_entrypoint_module() -> str:
    """The module from the ``CMD`` of the Dockerfile — the entrypoint of the services without their own ``command``.

    Now it is ``uvicorn src.api.main:app`` (the service api). We parse rather than hard-code: if the CMD changes, the guard will follow it, rather than check a corpse.
    """
    match = _CMD_RE.search(DOCKERFILE_PATH.read_text())
    assert (
        match
    ), "no CMD [...] was found in the Dockerfile — the guard does not know the entrypoint of api"
    argv = [part.strip().strip('"') for part in match.group(1).split(",")]
    for arg in argv:
        # uvicorn: "src.api.main:app" → src.api.main
        if arg.startswith("src.") and ":" in arg:
            return arg.split(":", 1)[0]
        if arg.startswith("src."):
            return arg
    raise AssertionError(f"the CMD of the Dockerfile has no src.* module: {argv}")


def _module_path(module: str) -> Path | None:
    """The file of the module ``src.foo.bar`` — or None, if it is not our module."""
    as_file = REPO_ROOT / (module.replace(".", "/") + ".py")
    if as_file.is_file():
        return as_file
    as_package = REPO_ROOT / module.replace(".", "/") / "__init__.py"
    if as_package.is_file():
        return as_package
    return None


def _string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _toplevel_imports(tree: ast.Module) -> set[str]:
    """The top-level ``src.*`` imports = what is loaded when the module is imported.

    We do NOT descend into functions/classes (a lazy import is not counted as an edge), but we enter the ``if``/``try`` at the top level: there live the conditional imports, which the interpreter executes anyway on loading.
    """
    found: set[str] = set()

    def collect(body: Iterable[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                found.update(a.name for a in node.names if a.name.startswith("src."))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if not node.level and module.startswith("src"):
                    found.add(module)
                    # `from src.pkg import mod` — the name may be a module of the package.
                    found.update(f"{module}.{a.name}" for a in node.names)
            elif isinstance(node, (ast.If, ast.Try)):
                collect(node.body)
                collect(node.orelse)
                collect(getattr(node, "finalbody", []))
                for handler in getattr(node, "handlers", []):
                    collect(handler.body)

    collect(tree.body)
    return found


def _env_reads(tree: ast.Module) -> set[str]:
    """The names of the env that the module reads anywhere inside itself.

    Three ways in which the env gets into the code in this repository:
    ``os.getenv("X")`` / ``os.environ["X"]``, the pydantic ``Field(alias="X")``
    and IMPLICITLY — ``anthropic.AsyncAnthropic()``, which reaches for
    ``ANTHROPIC_API_KEY`` by itself. The third way is the reason for the hole #368: the code has no name of the key at all, grep is silent.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in ("getenv", "get")
                and node.args
                and ast.unparse(func.value) in ("os", "os.environ", "environ")
            ):
                if name := _string(node.args[0]):
                    found.add(name)
            if isinstance(func, ast.Name) and func.id == "Field":
                for keyword in node.keywords:
                    if keyword.arg == "alias" and (name := _string(keyword.value)):
                        found.add(name)
            if ast.unparse(func).endswith("AsyncAnthropic"):
                found.add("ANTHROPIC_API_KEY")
        elif isinstance(node, ast.Subscript):
            if ast.unparse(node.value) == "os.environ" and (
                name := _string(node.slice)
            ):
                found.add(name)
    return found


def _loaded_modules(entrypoint: str) -> set[str]:
    """The closure of the module-level imports from the entrypoint — what is loaded at the start."""
    seen: set[str] = set()
    queue = deque([entrypoint])
    while queue:
        module = queue.popleft()
        if module in seen:
            continue
        path = _module_path(module)
        if path is None:
            continue
        seen.add(module)
        tree = ast.parse(path.read_text())
        queue.extend(_toplevel_imports(tree))
    return seen


def _required_env(entrypoint: str) -> set[str]:
    """All the env that the code loaded by this entrypoint may require."""
    required: set[str] = set()
    for module in _loaded_modules(entrypoint):
        path = _module_path(module)
        assert path is not None  # _loaded_modules puts in only the resolved ones
        required |= _env_reads(ast.parse(path.read_text()))
    return required


def _service_entrypoints(compose: dict) -> dict[str, str]:
    """The service → its python module. Only the app image; migrate (alembic) drops out."""
    entrypoints: dict[str, str] = {}
    for name, service in compose["services"].items():
        if service.get("image") != APP_IMAGE:
            continue
        command = service.get("command")
        if command is None:
            entrypoints[name] = _dockerfile_entrypoint_module()
        elif len(command) >= 3 and command[:2] == ["python", "-m"]:
            entrypoints[name] = command[2]
    return entrypoints


def _wired_env(service: dict) -> set[str]:
    """The names of the env passed to the service in the compose (the dict and the list form)."""
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        return {item.split("=", 1)[0] for item in environment}
    return set(environment)


def _gaps(compose: dict) -> dict[tuple[str, str], tuple[str, ...]]:
    """The holes: {(service, the first env of the group): group} — the code asks, the compose is silent."""
    found: dict[tuple[str, str], tuple[str, ...]] = {}
    for service_name, entrypoint in _service_entrypoints(compose).items():
        required = _required_env(entrypoint)
        wired = _wired_env(compose["services"][service_name])
        for group in CRITICAL_ENV:
            if required.isdisjoint(group):
                continue  # this code does not ask for such an env — not our business
            if wired.isdisjoint(group):
                found[(service_name, group[0])] = group
    return found


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


@pytest.fixture
def synthetic() -> dict:
    """A fresh copy of the synthetic map: the tests below mutate it."""
    return yaml.safe_load(SYNTHETIC_COMPOSE)


def test_compose_map_is_not_vacuous(compose):
    """The deployment map was collected — otherwise there is nothing to compare with and the test is empty.

    A guard that silently checks nothing is worse than an absent one: it gives green as proof. We nail both ends of the map.
    """
    entrypoints = _service_entrypoints(compose)
    assert len(entrypoints) >= 10, (
        f"only {len(entrypoints)} app services were found: {sorted(entrypoints)} — "
        "it looks like the compose parser broke and the guard checks nothing"
    )
    assert entrypoints["api"] == "src.api.main"
    assert entrypoints["ai"] == "src.workers.ai"
    assert "migrate" not in entrypoints, "migrate runs alembic, not python -m"


def test_code_map_is_not_vacuous():
    """The code map was collected: the closure did not collapse and the env are found.

    We check both breadth (api has a hundred modules) and depth (the implicit ANTHROPIC from AsyncAnthropic — exactly the way the hole #368 hid from grep).
    """
    api_modules = _loaded_modules("src.api.main")
    assert (
        len(api_modules) > 50
    ), f"the closure of api collapsed: {len(api_modules)} modules"

    ai_env = _required_env("src.workers.ai")
    assert "ANTHROPIC_API_KEY" in ai_env, (
        "the ai worker does not “ask” for ANTHROPIC_API_KEY — the parsing of the implicit "
        "reading of the env via anthropic.AsyncAnthropic() broke"
    )
    assert "DATABASE_URL" in _required_env("src.workers.routing")
    assert "TG_BOT_TOKEN" in _required_env("src.bots.telegram")
    # The precondition of the synthetic: take away from delivery the reading of SMTP_HOST — and the tests on
    # SYNTHETIC_COMPOSE will turn green EMPTY (_gaps will silently skip a group that the
    # code does not ask for). We nail it here, so that the hole in the proof is visible.
    assert "SMTP_HOST" in _required_env("src.workers.delivery")


def test_no_env_gaps(compose):
    """The MAIN invariant: the code asks for a critical env → its container gets it.

    There are no exceptions and none should be introduced: #346/#368 were closed on 17.07, the list of known holes died with them. The test turns red on ANY new hole — for example, a new worker calls make_email_sender(), and SMTP_HOST was not passed to it: exactly the scenario of #346, but caught at the PR, not through a month in production. That the heuristic is alive is proved by the synthetic below, not by the state of this file.
    """
    gaps = _gaps(compose)
    assert not gaps, "\n".join(
        [
            "Сервис исполняет код, который читает критичную env, но в "
            "docker-compose.yml она ему НЕ прокинута:",
            *(
                f"  · {service}: нужна {' или '.join(group)} "
                f"(entrypoint {_service_entrypoints(compose)[service]})"
                for (service, _), group in sorted(gaps.items())
            ),
            "",
            "Фича молча не работает в проде: env читается в рантайме, старт "
            "контейнера не падает. Прокинь env сервису в deploy/docker-compose.yml "
            "(RED-путь → PR ждёт owner) — либо, если код env не требует, проверь, "
            "почему модуль вообще попал в загрузку.",
        ]
    )


def test_guard_sees_a_hand_written_gap(synthetic):
    """The heuristic works: on the hand-written map the guard finds exactly one hole.

    Here EVERYTHING is proved at once, and each service of the synthetic answers for its own class: ``mailer_unwired`` — the hole exists and must be seen; ``mailer_wired`` — the same code with the env passed, there must be no false alarm; ``postgres`` — a foreign image, not subject to the guard. We check the whole set entirely (==, not in): “found what is needed” and “did not invent anything superfluous” — one assertion.
    """
    assert _gaps(synthetic) == {
        ("mailer_unwired", "SMTP_HOST"): ("SMTP_HOST", "POSTMARK_SERVER_TOKEN")
    }


def test_guard_goes_quiet_when_the_hand_written_gap_is_closed(synthetic):
    """Mutation №1: we close the hole in the synthetic → the guard must go silent.

    Without this half the first test would prove only that the guard can always shout. We check both alternatives of the group: there are two mail transports, any one is enough — otherwise a passed POSTMARK would be read as a hole.
    """
    for transport in ("SMTP_HOST", "POSTMARK_SERVER_TOKEN"):
        patched = yaml.safe_load(SYNTHETIC_COMPOSE)
        patched["services"]["mailer_unwired"]["environment"][transport] = "${X:-}"
        assert not _gaps(patched), (
            f"the hole is closed ({transport} is passed), but the guard still shouts — "
            "it catches not the compose↔code gap, but something of its own"
        )


def test_guard_sees_a_freshly_opened_gap(synthetic):
    """Mutation №2: we introduce a NEW hole into the synthetic → the guard must see it.

    The other side: the guard must react to a change of the map, rather than remember one learned answer. We take DATABASE_URL away from a healthy service — an env of another group, not the one it was “trained” on.
    """
    del synthetic["services"]["mailer_wired"]["environment"]["DATABASE_URL"]

    assert ("mailer_wired", "DATABASE_URL") in _gaps(synthetic), (
        "the guard did NOT notice the fresh hole (mailer_wired without DATABASE_URL) — "
        "a green run on the real compose is worth nothing"
    )
