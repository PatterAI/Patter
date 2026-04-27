#!/usr/bin/env bash
# Run all notebooks headlessly, strip outputs, report pass/fail.
# Usage:
#   bash scripts/run_all_notebooks.sh python      # Python notebooks only
#   bash scripts/run_all_notebooks.sh typescript  # TypeScript notebooks only
#   bash scripts/run_all_notebooks.sh             # both languages

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NOTEBOOKS_DIR="$ROOT/examples/notebooks"

LANG="${1:-all}"
PASS=0
FAIL=0
FAILED_NOTEBOOKS=()

run_notebook() {
  local nb="$1"
  local label
  label="$(basename "$(dirname "$nb")")/$(basename "$nb")"
  echo "--- running $label"
  if jupyter nbconvert \
      --to notebook \
      --execute \
      --ExecutePreprocessor.timeout=300 \
      --output "$nb" \
      "$nb" 2>&1; then
    # Strip outputs so the repo stays output-free
    if command -v nbstripout &>/dev/null; then
      nbstripout "$nb"
    else
      jupyter nbconvert --to notebook --ClearOutputPreprocessor.enabled=True --output "$nb" "$nb" 2>/dev/null
    fi
    echo "    PASS $label"
    PASS=$((PASS + 1))
  else
    echo "    FAIL $label"
    FAIL=$((FAIL + 1))
    FAILED_NOTEBOOKS+=("$label")
  fi
}

run_language() {
  local lang="$1"
  local dir="$NOTEBOOKS_DIR/$lang"
  if [[ ! -d "$dir" ]]; then
    echo "directory not found: $dir"
    return 1
  fi
  echo "=== $lang ==="
  for nb in "$dir"/*.ipynb; do
    [[ -f "$nb" ]] || continue
    run_notebook "$nb"
  done
}

case "$LANG" in
  python|typescript)
    run_language "$LANG"
    ;;
  all)
    run_language python
    run_language typescript
    ;;
  *)
    echo "usage: $0 [python|typescript|all]" >&2
    exit 1
    ;;
esac

echo ""
echo "=== Summary: $PASS passed, $FAIL failed ==="
if [[ ${#FAILED_NOTEBOOKS[@]} -gt 0 ]]; then
  echo "Failed notebooks:"
  for nb in "${FAILED_NOTEBOOKS[@]}"; do
    echo "  - $nb"
  done
  exit 1
fi
