"""Diff paired Python/TypeScript notebooks.

Usage:
    python3 scripts/check_notebook_parity.py         # check all pairs, exit 1 on drift
    python3 scripts/check_notebook_parity.py --quiet # suppress per-file output
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY_DIR = REPO / "examples/notebooks/python"
TS_DIR = REPO / "examples/notebooks/typescript"


def _section_titles(path: Path) -> list[str]:
    """Return each markdown cell's first line, when it's a heading (starts with #).

    Only the heading is compared across language pairs — the descriptive prose
    that follows can legitimately differ (snake_case vs camelCase, etc.).
    """
    nb = json.loads(path.read_text())
    titles: list[str] = []
    for c in nb["cells"]:
        if c["cell_type"] != "markdown":
            continue
        if not c["source"]:
            continue
        # source[0] may itself contain multiple lines (Python implicit
        # string concatenation in the cell helpers); split on the first \n.
        first_line = c["source"][0].split("\n", 1)[0].strip()
        if first_line.startswith("#"):
            titles.append(first_line)
    return titles


def diff_pair(py_path: Path, ts_path: Path) -> list[str]:
    py_titles = _section_titles(py_path)
    ts_titles = _section_titles(ts_path)
    diffs: list[str] = []
    for i, (a, b) in enumerate(zip(py_titles, ts_titles)):
        if a != b:
            diffs.append(f"section [{i}] differs: py={a!r} ts={b!r}")
    if len(py_titles) != len(ts_titles):
        diffs.append(
            f"section count mismatch: py={len(py_titles)} ts={len(ts_titles)} "
            f"(py extras: {py_titles[len(ts_titles):]} / "
            f"ts extras: {ts_titles[len(py_titles):]})"
        )
    return diffs


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    py_files = sorted(PY_DIR.glob("[0-9][0-9]_*.ipynb"))
    drift_count = 0
    for py in py_files:
        ts = TS_DIR / py.name
        if not ts.exists():
            print(f"❌ {py.name}: no TypeScript twin")
            drift_count += 1
            continue
        diffs = diff_pair(py, ts)
        if diffs:
            drift_count += 1
            print(f"❌ {py.name}:")
            for d in diffs:
                print(f"    {d}")
        elif not args.quiet:
            print(f"✅ {py.name}")
    if drift_count:
        print(f"\n{drift_count} notebook pair(s) drifted")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
