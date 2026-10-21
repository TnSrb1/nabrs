# Agentic delivery

How this product is built: two kinds of AI agent and one human owner, with every hand-off checked by a command
that can be repeated. This describes roles and rules, not a particular vendor, plan or machine.
Numbers are the values in the repository on **20 October 2026**.

<img src="assets/agentic-delivery-animated.svg" alt="Animated agentic delivery: value check, brief, isolated worktree, mutation check, draft pull request, independent gate, second-agent review, merge, automatic rollout, production check and knowledge layers" width="100%">

<sub>Animated: each phase is highlighted in turn. The text below follows the same order.</sub>

## Roles

| Role | Does | Does not |
|---|---|---|
| **Coordinator agent** | checks whether a task is worth doing, writes the brief, reviews the result, checks production | write the feature itself while a worker runs |
| **Worker agent** | implements one task in an isolated git worktree, writes the tests and a test report | touch the main checkout, merge, or decide what is risky |
| **Human owner** | sets priorities, accepts risk, merges the high-risk paths | review every line: the gate and the second agent come first |
| **Automation** | runs the gate, rolls out `main` | decide anything |

## 1. Decide: is the task real?

A task starts with a question, not a brief: is the problem present in the data, does the client see it, is it our layer?
Each answer is checked against the live database or the running site. On 25 July 2026 six candidate tasks in a row were closed this way:
for example a fallback for tenders without a title (0 such tenders out of 47,788) and for awards without a winner (0 out of 2,333).
A note in a document is a hypothesis until a command confirms it, and a tool's finding stays a hypothesis until someone acts on it (clicks the button, sends the request).
This rule is written down in the project's internal instructions, which are not published.

## 2. Build: isolated, small, pushed often

- The brief states scope, paths the worker must not touch, how to verify, and the format of the report.
- The worker runs in its own git worktree on its own branch. The rollout timer resets the main checkout, so nothing is ever done there.
- The worker pushes after every logical step and opens a **draft** pull request at once, because a run can be cut off at any moment; work that is not pushed is lost.
- Every new test is **mutation-checked**: remove the fix, the test must go red, restore the fix. A test that stays green proves nothing.
- The worker writes a test report: the red command first (the symptom before the fix), then each check as a command that can be repeated verbatim, then what was *not* checked, then a verdict.

## 3. Verify: two independent checks

- **The gate** (`scripts/gate.sh`, mirrored by `.github/workflows/ci.yml` in the private repository and by [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) here): ruff, black, pytest with a coverage floor of 70% (`--cov-fail-under=70`) and a mypy ratchet. Database tests run against a throwaway PostgreSQL, and the gate fails if they are silently skipped while the database is up.
- **The second agent** does not trust the report. It runs the gate itself, repeats the commands from the test report, reads the diff in two passes and compares the numbers with production. A passing report is not evidence; a repeated command is.

Two lessons from this process are now rules: a pipeline such as `pytest | tail && git commit` takes the exit code of `tail` and can commit over red tests, so test runs are never piped; and branches that are green one by one do not make a green `main`, because a semantic conflict is invisible inside a branch.

## 4. Ship: the human owns the risk

Paths where a mistake is expensive are merged only by the human owner: database migrations, deployment and server configuration, authentication and security, billing and payments, architecture decision records, secrets configuration. Everything else is merged by the coordinator after its own gate run. `main` is then rolled out by a timer on the server, and the coordinator checks the live site: the rendered page and the data, not the test report.

A workflow that spends money per run (an automated model review of every pull request) is not enabled without an explicit owner decision; the one that existed was switched off on 1 October 2026.

## 5. Learn: knowledge moves up by proof

Observations are kept in layers (`docs/MEMORY_LAYERS.md` in the private repository):

| Layer | What | Becomes the next layer when |
|---|---|---|
| L0 raw | run transcripts, command output, screenshots | a fact is **verified** and the way to verify it is written down |
| L1 fact | one verified fact with a date | it repeated twice, or one failure would be costly (money, delivery, auth, data loss) |
| L2 procedure | steps with a check at each step (a runbook section or a skill) | its violation already caused a production defect, or it breaks isolation of tenants or money |
| L3 invariant | a rule that is never broken: **ban, reason, how to check** | — |

The difference from "remember what comes up often" is deliberate: frequency produces phantom tasks, proof does not.
Row-level security with `FORCE` is an example of an L3 invariant that came from a real defect: `ENABLE` alone does not apply to the table owner, so the check reads the catalog (`relforcerowsecurity`) instead of trusting behaviour.

## Where to look in this repository

| Practice | Evidence |
|---|---|
| Gate | `scripts/gate.sh`, `scripts/test_db_setup.sh`, `.github/workflows/ci.yml` |
| Throwaway database for tests | `scripts/test_db_setup.sh` |
| Row-level security tests | `tests/db/test_rls_isolation.py` |
| mypy ratchet | `tests/quality/test_mypy_debt.py` |
| Refusals visible to the customer (a rule found by this process) | `tests/quality/test_limit_refusal_is_visible.py` |
| Generated code map | `scripts/code_map.py` → [`docs/CODE_MAP.md`](CODE_MAP.md) |
