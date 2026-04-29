#!/usr/bin/env bash
# scripts/pr-validate.sh — Run every PR-blocking CI check locally before
# opening a PR. Mirrors .github/workflows/{test,notebooks}.yml so a green
# local run lines up with a green CI run.
#
# Usage:
#   bash scripts/pr-validate.sh                # core checks (default, ~3-5 min)
#   bash scripts/pr-validate.sh --quick        # pre-commit + parity only (~30s)
#   bash scripts/pr-validate.sh --full         # core + e2e + python-all-extras (~10 min)
#   bash scripts/pr-validate.sh --skip-py      # skip Python jobs
#   bash scripts/pr-validate.sh --skip-ts      # skip TypeScript jobs
#   bash scripts/pr-validate.sh --skip-notebooks
#   bash scripts/pr-validate.sh --no-stop      # don't stop on first failure
#
# Exits 0 when all selected checks pass, non-zero on any failure. Prints a
# summary table at the end.

set -uo pipefail

# ── Locate repo root regardless of where we're invoked from ─────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ── Flags ───────────────────────────────────────────────────────────────
MODE="core"
SKIP_PY=0
SKIP_TS=0
SKIP_NOTEBOOKS=0
NO_STOP=0
for arg in "$@"; do
    case "$arg" in
        --quick) MODE="quick" ;;
        --full) MODE="full" ;;
        --skip-py) SKIP_PY=1 ;;
        --skip-ts) SKIP_TS=1 ;;
        --skip-notebooks) SKIP_NOTEBOOKS=1 ;;
        --no-stop) NO_STOP=1 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *)
            echo "unknown flag: $arg" >&2
            exit 2 ;;
    esac
done

# ── ANSI ────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    GREEN='\033[0;32m'; RED='\033[0;31m'; YEL='\033[0;33m'; DIM='\033[2m'; OFF='\033[0m'
else
    GREEN=''; RED=''; YEL=''; DIM=''; OFF=''
fi

# ── Result tracker ──────────────────────────────────────────────────────
declare -a RESULTS_NAME RESULTS_STATUS RESULTS_TIME RESULTS_DETAIL
TOTAL_FAILED=0

run_check() {
    local name="$1"; shift
    local needs_cmd="${1:-}"; shift || true
    if [ -n "$needs_cmd" ] && ! command -v "$needs_cmd" >/dev/null 2>&1; then
        printf "${YEL}⚠${OFF}  %-45s ${DIM}skipped (missing: %s)${OFF}\n" "$name" "$needs_cmd"
        RESULTS_NAME+=("$name"); RESULTS_STATUS+=("SKIP"); RESULTS_TIME+=("0s"); RESULTS_DETAIL+=("missing $needs_cmd")
        return 0
    fi

    printf "${DIM}▶${OFF}  %-45s ${DIM}running...${OFF}\r" "$name"
    local log; log="$(mktemp)"
    local start end elapsed
    start="$(date +%s)"
    if "$@" >"$log" 2>&1; then
        end="$(date +%s)"; elapsed=$((end - start))
        printf "${GREEN}✓${OFF}  %-45s ${DIM}%ds${OFF}\n" "$name" "$elapsed"
        RESULTS_NAME+=("$name"); RESULTS_STATUS+=("PASS"); RESULTS_TIME+=("${elapsed}s"); RESULTS_DETAIL+=("")
        rm -f "$log"
        return 0
    fi
    end="$(date +%s)"; elapsed=$((end - start))
    printf "${RED}✗${OFF}  %-45s ${DIM}%ds${OFF}\n" "$name" "$elapsed"
    echo "${DIM}--- last 30 lines of output ---${OFF}"
    tail -n 30 "$log" | sed 's/^/    /'
    echo "${DIM}--- (full log: $log) ---${OFF}"
    RESULTS_NAME+=("$name"); RESULTS_STATUS+=("FAIL"); RESULTS_TIME+=("${elapsed}s"); RESULTS_DETAIL+=("$log")
    TOTAL_FAILED=$((TOTAL_FAILED + 1))
    if [ "$NO_STOP" = "0" ]; then
        echo
        echo "${RED}Stopping on first failure (use --no-stop to continue).${OFF}"
        summary
        exit 1
    fi
}

summary() {
    echo
    echo "${DIM}─── Summary ──────────────────────────────────────────${OFF}"
    local i
    for i in "${!RESULTS_NAME[@]}"; do
        local color; case "${RESULTS_STATUS[$i]}" in
            PASS) color="$GREEN" ;;
            FAIL) color="$RED" ;;
            *) color="$YEL" ;;
        esac
        printf "  ${color}%-4s${OFF}  %-45s  ${DIM}%s${OFF}\n" \
            "${RESULTS_STATUS[$i]}" "${RESULTS_NAME[$i]}" "${RESULTS_TIME[$i]}"
    done
    echo "${DIM}──────────────────────────────────────────────────────${OFF}"
    if [ "$TOTAL_FAILED" -gt 0 ]; then
        echo "${RED}$TOTAL_FAILED check(s) failed.${OFF}"
    else
        echo "${GREEN}All selected checks passed.${OFF}"
    fi
}

# ── Pre-commit (always, fast) ───────────────────────────────────────────
# Mirrors test.yml `pre-commit` job. Catches whitespace, EOF, nbstripout,
# secret patterns. ~5-10s warm.
#
# Local-environment escape hatches (CI runs the full pre-commit unaltered):
#   PR_VALIDATE_SKIP_GITLEAKS=1   bypass the bundled gitleaks Go build
#                                 (OOMs on memory-constrained machines —
#                                 the script falls back to a system gitleaks)
#   PR_VALIDATE_SKIP_NBSTRIPOUT=1 bypass pre-commit's nbstripout venv
#                                 (rpds.so mmap fails on hardened macOS —
#                                 the script falls back to a system nbstripout)
PRECOMMIT_SKIP=""
[ -n "${PR_VALIDATE_SKIP_GITLEAKS:-}" ] && PRECOMMIT_SKIP="${PRECOMMIT_SKIP:+$PRECOMMIT_SKIP,}gitleaks"
[ -n "${PR_VALIDATE_SKIP_NBSTRIPOUT:-}" ] && PRECOMMIT_SKIP="${PRECOMMIT_SKIP:+$PRECOMMIT_SKIP,}nbstripout"
[ -n "${PRE_COMMIT_SKIP:-}" ] && PRECOMMIT_SKIP="${PRECOMMIT_SKIP:+$PRECOMMIT_SKIP,}$PRE_COMMIT_SKIP"

if command -v pre-commit >/dev/null 2>&1; then
    if [ -n "$PRECOMMIT_SKIP" ]; then
        run_check "pre-commit (lint + hygiene)" pre-commit env SKIP="$PRECOMMIT_SKIP" pre-commit run --all-files
    else
        run_check "pre-commit (lint + hygiene)" pre-commit pre-commit run --all-files
    fi
else
    printf "${YEL}⚠${OFF}  %-45s ${DIM}skipped (pip install pre-commit==3.8.0)${OFF}\n" "pre-commit (lint + hygiene)"
    RESULTS_NAME+=("pre-commit (lint + hygiene)"); RESULTS_STATUS+=("SKIP"); RESULTS_TIME+=("0s"); RESULTS_DETAIL+=("install pre-commit")
fi

# Fallbacks when pre-commit's bundled hooks were skipped above.
# We intentionally don't fall back to a system gitleaks — it can OOM-kill on
# memory-constrained machines too, and CI runs trufflehog/gitleaks anyway as
# its own job. This local fallback only verifies notebook hygiene.
# When pre-commit's nbstripout was skipped (rpds.so mmap fails on hardened
# macOS), the next check ("notebooks: outputs stripped") already greps for
# outputs in committed notebooks, so we're covered there — no separate
# fallback step needed.

# ── Notebook gates ──────────────────────────────────────────────────────
if [ "$SKIP_NOTEBOOKS" = "0" ]; then
    run_check "notebooks: parity" python3 python3 scripts/check_notebook_parity.py
    run_check "notebooks: outputs stripped" python3 bash -c '
        set -e
        for f in examples/notebooks/python/*.ipynb examples/notebooks/typescript/*.ipynb; do
            if grep -q "\"outputs\": \[\(\s*{\)" "$f"; then
                echo "FAIL: $f contains outputs (run nbstripout)"
                exit 1
            fi
        done
        python3 scripts/scan_notebook_secrets.py examples/notebooks/python/*.ipynb examples/notebooks/typescript/*.ipynb
    '
    if [ "$MODE" != "quick" ]; then
        run_check "notebooks: scaffold tests" pytest \
            pytest scripts/test_scaffold_notebook.py scripts/test_check_notebook_parity.py scripts/test_generate_notebook_fixtures.py -q
        run_check "notebooks: setup tests (Python)" pytest \
            pytest examples/notebooks/python/tests -q
        if [ -d examples/notebooks/typescript ] && [ -f examples/notebooks/typescript/package.json ]; then
            run_check "notebooks: setup tests (TS)" npm \
                bash -c "cd examples/notebooks/typescript && npm install --silent && npm test --silent"
        fi
    fi
fi

# ── Python SDK ──────────────────────────────────────────────────────────
if [ "$SKIP_PY" = "0" ] && [ "$MODE" != "quick" ]; then
    run_check "sdk-py: install (.[dev])" pip \
        bash -c "cd sdk-py && pip install -e '.[dev]' --quiet"
    run_check "sdk-py: tests" pytest \
        bash -c "cd sdk-py && pytest tests/ -q --tb=line"
    run_check "sdk-py: security tests" pytest \
        bash -c "cd sdk-py && pytest tests/security/ -q -m security"
    if [ "$MODE" = "full" ]; then
        run_check "sdk-py: all-extras tests (slow)" pytest \
            bash -c "cd sdk-py && pip install -e '.[dev,silero,deepfilternet,ivr,anthropic,groq,cerebras,google,cartesia,soniox,assemblyai,rime,lmnt,ultravox,gemini-live,evals,tracing,scheduling,background-audio,telnyx-ai]' --quiet && pytest tests/ -q --tb=line"
    fi
fi

# ── TypeScript SDK ──────────────────────────────────────────────────────
if [ "$SKIP_TS" = "0" ] && [ "$MODE" != "quick" ]; then
    if [ -d sdk-ts ] && [ -f sdk-ts/package.json ]; then
        run_check "sdk-ts: install" npm \
            bash -c "cd sdk-ts && npm ci --silent"
        run_check "sdk-ts: lint (tsc --noEmit)" npm \
            bash -c "cd sdk-ts && npm run lint"
        run_check "sdk-ts: tests" npm \
            bash -c "cd sdk-ts && npm test --silent"
        run_check "sdk-ts: build" npm \
            bash -c "cd sdk-ts && npm run build --silent"
        if [ "$MODE" = "full" ]; then
            run_check "sdk-ts: e2e (Playwright, slow)" npx \
                bash -c "cd sdk-ts && npx playwright install --with-deps && npx playwright test"
        fi
    fi
fi

# ── Secret scan (optional — prefers trufflehog if installed) ────────────
if command -v trufflehog >/dev/null 2>&1; then
    run_check "trufflehog: secret scan" trufflehog \
        trufflehog filesystem --no-update --results=verified,unknown --quiet .
fi

# ── Done ────────────────────────────────────────────────────────────────
summary
[ "$TOTAL_FAILED" -eq 0 ] || exit 1
