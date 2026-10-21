# Contributing

Private project — contributions come from the core team and its agents.

## Flow

1. Branch from fresh `origin/main`: `claude/<task>`.
2. Make the change with tests. Code invariants are documented in the code and enforced by the tests.
3. Run the gate **after the last commit**:
   ```bash
   python3 -m black src tests
   bash scripts/gate.sh        # must end with GATE PASS
   ```
4. Open a **draft** pull request; describe what was checked and how.
5. Review + own gate run → squash merge.

## RED paths — merged only by the owner

`alembic/`, `deploy/`, `docker-compose*`, `Caddyfile`, `.github/workflows/`, auth/security, billing, `infra/config.yaml`, `infra/secrets`, `docs/DECISIONS.md`.

## Rules that save the most time

- Never pipe tests into `tail` before a commit — the exit code is lost.
- New files must be in the index (`git status`), otherwise production will not start.
- New test → mutation check: remove the fix, the test must go red.
- New dependency → upper version bound in `pyproject.toml`.
- Migration number → check against `origin/main` before push; one Alembic head.
