#!/usr/bin/env bash
# PRE-PR GATE — a mirror of .github/workflows/ci.yml (lint + test + mypy).
# Run: bash scripts/gate.sh
# The source of truth for the commands: ci.yml + claude-review.yml. Change in sync.
set -euo pipefail

FAIL=0
ORIGIN_BASE="origin/main"

step_header() { echo; echo "━━━ $1 ━━━"; }

# ─── a. PREFLIGHT ─────────────────────────────────────────────────────────────
step_header "a. PREFLIGHT — tools"
missing=""
for tool in ruff black pytest mypy; do
    if ! command -v "$tool" &>/dev/null; then
        missing="$missing $tool"
    fi
done
if [ -n "$missing" ]; then
    echo "❌ Not found in PATH:$missing"
    echo "   Install: pip install -e \".[dev]\" && pip install ruff==0.15.16 black==26.5.1"
    exit 1
fi
echo "✅ ruff black pytest mypy — found"

# ─── b. BRANCH FRESHNESS ────────────────────────────────────────────────────────
step_header "b. FETCH + FRESHNESS relative to $ORIGIN_BASE"
git fetch origin main --quiet 2>&1 || true
behind=$(git rev-list --count "HEAD..${ORIGIN_BASE}" 2>/dev/null || echo 0)
if [ "$behind" -gt 0 ]; then
    echo "⚠️  The branch is behind ${ORIGIN_BASE} by ${behind} commit(s). A rebase is desirable."
else
    echo "✅ The branch is up to date (not behind ${ORIGIN_BASE})"
fi

# ─── c. JUNK / SLOP ──────────────────────────────────────────────────────────
step_header "c. JUNK/SLOP — check of the diff vs ${ORIGIN_BASE}"
changed_files=$(git diff --name-only "${ORIGIN_BASE}...HEAD" 2>/dev/null || true)
slop=""
if [ -n "$changed_files" ]; then
    while IFS= read -r f; do
        case "$f" in
            tmp/TASK_*.md|tmp/*.log|*.orig|*.rej|*__pycache__/*)
                slop="$slop\n  $f"
                ;;
        esac
    done <<< "$changed_files"
    # __pycache__ dirs (without a trailing slash)
    while IFS= read -r f; do
        if echo "$f" | grep -q '__pycache__'; then
            slop="$slop\n  $f"
        fi
    done <<< "$changed_files"
fi
if [ -n "$slop" ]; then
    echo -e "❌ Junk files found:${slop}"
    FAIL=1
else
    echo "✅ No junk files found"
fi

# ─── BLOCKING PAUSE AFTER SLOP ────────────────────────────────────────────
if [ "$FAIL" -eq 1 ]; then
    echo
    echo "❌ GATE FAIL — fix it, do NOT push"
    exit 1
fi

# ─── d. RUFF ──────────────────────────────────────────────────────────────────
step_header "d. RUFF — ruff check src tests"
if ruff check src tests; then
    echo "✅ ruff OK"
else
    echo "❌ ruff found errors"
    FAIL=1
fi

# ─── e. BLACK ─────────────────────────────────────────────────────────────────
step_header "e. BLACK — black --check --diff src tests"
if black --check --diff src tests; then
    echo "✅ black OK"
else
    echo "❌ black found indentation/formatting issues"
    FAIL=1
fi

if [ "$FAIL" -eq 1 ]; then
    echo
    echo "❌ GATE FAIL — fix it, do NOT push"
    exit 1
fi

# ─── f0. ONE-OFF PG FOR tests/db ──────────────────────────────────────────
# Without TEST_RLS_DATABASE_URL, tests/db/* (RLS, SQL predicates, migrations) are silently
# skipped — a green gate was silent about them (01.10.2026, #574). If docker is available,
# we bring up a one-off PG, roll alembic to head and run them as tendersrb_app.
# GATE_DB=0 — an explicit refusal (then the step loudly marks the skip).
step_header "f0. tests/db — one-off PG (GATE_DB=${GATE_DB:-1})"
DB_TESTS=skipped
if [ -n "${TEST_RLS_DATABASE_URL:-}" ]; then
    DB_TESTS=external
    echo "✅ TEST_RLS_DATABASE_URL is set from outside — tests/db run on it"
elif [ "${GATE_DB:-1}" = 1 ] && command -v docker &>/dev/null && docker info &>/dev/null; then
    pg_name="gate-pg-$$"
    pg_port=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])")
    docker run -d --rm --name "$pg_name" -e POSTGRES_PASSWORD=t -e POSTGRES_DB=tendersrb \
        -p "127.0.0.1:${pg_port}:5432" pgvector/pgvector:pg16 >/dev/null
    trap 'docker stop "$pg_name" >/dev/null 2>&1 || true' EXIT
    for _ in $(seq 1 60); do
        docker exec "$pg_name" pg_isready -U postgres -d tendersrb &>/dev/null && break
        sleep 1
    done
    # shellcheck source=scripts/test_db_setup.sh
    if . scripts/test_db_setup.sh 127.0.0.1 "$pg_port"; then
        DB_TESTS=disposable
        echo "✅ PG on :${pg_port}, alembic upgrade head OK — tests/db will run as tendersrb_app"
    else
        FAIL=1
    fi
else
    echo "⚠️  tests/db SKIPPED (no docker or GATE_DB=0) — RLS/SQL are NOT checked by this gate"
fi

# ─── f. PYTEST + COVERAGE ─────────────────────────────────────────────────────
step_header "f. PYTEST + COVERAGE — pytest --cov=src --cov-report=term-missing --cov-fail-under=70"
pytest_log=$(mktemp)
set +e
pytest -rs --cov=src --cov-report=term-missing --cov-fail-under=70 2>&1 | tee "$pytest_log"
pytest_rc=${PIPESTATUS[0]}
set -e
if [ "$pytest_rc" -eq 0 ]; then
    echo "✅ pytest OK"
else
    echo "❌ pytest failed or coverage < 70%"
    FAIL=1
fi
# Applied check: with the PG up, no tests/db may go to skip over the URL.
if [ "$DB_TESTS" != skipped ] && grep -q "TEST_RLS_DATABASE_URL is not set\|TEST_SUPERUSER_DATABASE_URL" "$pytest_log" \
    && grep "SKIPPED" "$pytest_log" | grep -q "tests/db/"; then
    echo "❌ tests/db skipped although the PG is up ($DB_TESTS) — check the variables"
    FAIL=1
fi
echo "ℹ️  tests/db: $DB_TESTS, tests/db skips: $(grep "SKIPPED" "$pytest_log" | grep -c "tests/db/" || true)"
rm -f "$pytest_log"

# ─── g. MYPY (NON-BLOCKING) ───────────────────────────────────────────────────
step_header "g. MYPY (non-blocking, like continue-on-error in CI)"
mypy src || true

# ─── FINAL ────────────────────────────────────────────────────────────────────
echo
if [ "$FAIL" -eq 0 ]; then
    echo "✅ GATE PASS — you can push + open the PR"
    exit 0
else
    echo "❌ GATE FAIL — fix it, do NOT push"
    exit 1
fi
