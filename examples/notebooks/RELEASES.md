# Notebook Series — Release Run Log

Each row is a manual **Run All** pass across all 24 notebooks against the named SDK release, with full provider keys and `ENABLE_LIVE_CALLS=1`.  Record the first and last time you ran a given release — intermediate spot-checks are optional.

## How to fill in a row

1. Set `PATTER_VERSION=<release>` in `.env`.
2. Run `bash scripts/run_all_notebooks.sh python` and `bash scripts/run_all_notebooks.sh typescript`.
3. Note any cell failures in the **Notes** column (format: `notebook_name: cell_tag — short reason`).
4. Record the result as ✅ (all T1–T3 cells passed, T4 placed calls successfully) or ⚠️ (partial pass — note which cells failed).

## Run log

| Date | SDK version | Language | Operator | Result | Notes |
|------|-------------|----------|----------|--------|-------|
|      |             |          |          |        |       |

## Known issues by release

Use this section for issues that recur across multiple runs or need investigation.

<!-- Example entry:
### v0.5.4
- `02_realtime / ultravox_realtime` — Ultravox WS occasionally returns empty audio on first connect; retry fixes it.
-->
