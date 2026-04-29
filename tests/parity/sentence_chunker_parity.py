#!/usr/bin/env python3
"""Standalone parity test for SentenceChunker (Python ↔ TypeScript).

Loads ``scenarios/sentence_chunker.json``, runs each case through the Python
``SentenceChunker``, then spawns ``node sentence_chunker_shim.js`` to run the
same cases through the TypeScript ``SentenceChunker``, and compares the
sentence emissions case by case.

A case may carry a ``current_behavior`` field documenting the actual output of
the current implementation when it differs from ``expected_sentences``. The
runner accepts ``current_behavior`` as an xfail (warning, no failure) and
clears it once the regression is fixed. Cases marked ``regression: true`` are
real bugs to fix; cases marked ``quirk: true`` are accepted by-design.

Self-contained — does NOT depend on the main parity runner. The TS shim
requires ``sdk-ts/dist/index.js`` (run ``npm --prefix sdk-ts run build`` first).

Usage:
    python3 tests/parity/sentence_chunker_parity.py
    python3 tests/parity/sentence_chunker_parity.py --verbose
    python3 tests/parity/sentence_chunker_parity.py --case italian_decimal_comma_pi
    python3 tests/parity/sentence_chunker_parity.py --strict   # treat xfail as fail
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PARITY_DIR = Path(__file__).parent
SCENARIO_FILE = PARITY_DIR / "scenarios" / "sentence_chunker.json"
TS_SHIM = PARITY_DIR / "sentence_chunker_shim.js"
PROJECT_ROOT = PARITY_DIR.parent.parent

sys.path.insert(0, str(PROJECT_ROOT / "sdk-py"))


def run_python_case(tokens: list[str]) -> list[str]:
    from getpatter.services.sentence_chunker import SentenceChunker

    chunker = SentenceChunker()
    emitted: list[str] = []
    for token in tokens:
        emitted.extend(chunker.push(token))
    emitted.extend(chunker.flush())
    return emitted


def run_ts_case(tokens: list[str]) -> list[str] | dict[str, str]:
    payload = json.dumps({"tokens": tokens})
    try:
        result = subprocess.run(
            ["node", str(TS_SHIM)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        return {"error": "TS shim timed out"}
    except FileNotFoundError:
        return {"error": "node not found — install Node.js"}

    if result.returncode != 0:
        return {"error": f"TS shim exited {result.returncode}: {result.stderr.strip()[:300]}"}

    try:
        parsed = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        return {"error": f"TS shim output not valid JSON: {exc}; raw: {result.stdout[:300]!r}"}

    if isinstance(parsed, dict) and "error" in parsed:
        return parsed
    if not isinstance(parsed, list):
        return {"error": f"TS shim returned non-list: {parsed!r}"}
    return parsed


def classify(
    expected: list[str],
    current_behavior: list[str] | None,
    actual: list[str],
) -> str:
    """Return PASS / XFAIL / FAIL.

    PASS    — actual matches expected_sentences (the ideal target)
    XFAIL   — actual matches current_behavior (known-broken, documented)
    FAIL    — actual matches neither (true regression)
    """
    if actual == expected:
        return "PASS"
    if current_behavior is not None and actual == current_behavior:
        return "XFAIL"
    return "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="Run only the named case")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat XFAIL (known regressions/quirks) as hard failures.",
    )
    parser.add_argument(
        "--side",
        choices=("both", "python", "typescript"),
        default="both",
        help="Run only one side (skips parity comparison).",
    )
    args = parser.parse_args()

    scenario = json.loads(SCENARIO_FILE.read_text())
    cases = scenario["cases"]
    if args.case:
        cases = [c for c in cases if c["name"] == args.case]
        if not cases:
            print(f"ERROR: no case named {args.case!r}")
            return 2

    print(f"SentenceChunker parity — {len(cases)} cases  (side={args.side})")
    print("=" * 68)

    counts = {"PASS": 0, "XFAIL": 0, "FAIL": 0, "PARITY_FAIL": 0, "TS_ERROR": 0}

    for case in cases:
        name = case["name"]
        tokens = case["tokens"]
        expected = case["expected_sentences"]
        current = case.get("current_behavior")
        marker = "regression" if case.get("regression") else "quirk" if case.get("quirk") else None

        py_actual = run_python_case(tokens) if args.side in ("both", "python") else None
        ts_raw = run_ts_case(tokens) if args.side in ("both", "typescript") else None

        ts_actual: list[str] | None = None
        ts_error: str | None = None
        if ts_raw is not None:
            if isinstance(ts_raw, dict):
                ts_error = ts_raw["error"]
            else:
                ts_actual = ts_raw

        py_class = classify(expected, current, py_actual) if py_actual is not None else "SKIP"
        ts_class = classify(expected, current, ts_actual) if ts_actual is not None else "SKIP"

        parity_ok: bool | None
        if py_actual is None or ts_actual is None:
            parity_ok = None
        else:
            parity_ok = py_actual == ts_actual

        # Tally — only count classes for sides that actually ran. SKIP is
        # treated as a no-op, so single-side runs don't penalise the totals.
        active = [c for c in (py_class, ts_class) if c != "SKIP"]
        if ts_error is not None:
            counts["TS_ERROR"] += 1
        elif "FAIL" in active:
            counts["FAIL"] += 1
        elif parity_ok is False:
            counts["PARITY_FAIL"] += 1
        elif active and all(c == "PASS" for c in active):
            counts["PASS"] += 1
        else:
            counts["XFAIL"] += 1

        # Output
        symbol = {
            "PASS": "  ✓",
            "XFAIL": " x?",
            "FAIL": "  ✗",
            "SKIP": "  -",
        }
        py_sym = symbol.get(py_class, "  ?")
        ts_sym = symbol.get(ts_class, "  ?")
        parity_sym = "  =" if parity_ok else (" ≠ " if parity_ok is False else "  -")

        suffix = ""
        if marker:
            suffix = f"  [{marker}]"
        if ts_error:
            suffix += f"  [TS_ERROR: {ts_error[:80]}]"

        if args.verbose or py_class == "FAIL" or ts_class == "FAIL" or parity_ok is False or ts_error:
            print(f"  py={py_sym}  ts={ts_sym}  parity={parity_sym}  {name}{suffix}")
            if py_class == "FAIL":
                print(f"      python expected   : {expected!r}")
                print(f"      python actual     : {py_actual!r}")
                if current:
                    print(f"      python current_beh: {current!r}")
            if ts_class == "FAIL" and ts_actual is not None:
                print(f"      typescript expected   : {expected!r}")
                print(f"      typescript actual     : {ts_actual!r}")
                if current:
                    print(f"      typescript current_beh: {current!r}")
            if parity_ok is False and py_actual is not None and ts_actual is not None:
                print(f"      python    : {py_actual!r}")
                print(f"      typescript: {ts_actual!r}")

    print("=" * 68)
    total = sum(counts.values())
    print(
        f"  PASS {counts['PASS']}   "
        f"XFAIL {counts['XFAIL']}   "
        f"FAIL {counts['FAIL']}   "
        f"PARITY_FAIL {counts['PARITY_FAIL']}   "
        f"TS_ERROR {counts['TS_ERROR']}   "
        f"(total {total})"
    )

    hard_fail = counts["FAIL"] + counts["PARITY_FAIL"] + counts["TS_ERROR"]
    if args.strict:
        hard_fail += counts["XFAIL"]
    return 0 if hard_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
