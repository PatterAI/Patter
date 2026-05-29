## Summary

Brief description of what this PR does and why.

## Changes

-

## Pre-merge checklist

- [ ] **Local validation is green**: `bash scripts/pr-validate.sh` (mirrors the
      PR-blocking CI — Python + TypeScript tests, lint, pre-commit, notebooks).
- [ ] **Both SDKs** updated when the change is user-visible — every public
      feature ships in **Python AND TypeScript** in the same PR, same API shape
      and defaults (`snake_case` ↔ `camelCase`). New/Python-only or TS-only is
      not accepted.
- [ ] **`CHANGELOG.md` updated** — added an entry under `## Unreleased`
      (`### Added` / `### Changed` / `### Fixed` / …) for any user-visible
      change. Refactor / test-only / docs-only diffs are exempt — say so below.
- [ ] **Tests** added for new behaviour; only the paid/external boundary
      (carrier / provider WebSocket) is mocked.
- [ ] **No secrets, credentials, or real phone numbers / PII** in the diff.
- [ ] **No external license headers or "ported from <repo>" provenance
      comments** in source files (integrating a provider/carrier and naming it
      is fine; copying a competitor's lineage is not).
- [ ] If notebooks changed: `python3 scripts/check_notebook_parity.py` passes
      and `nbstripout` has been run (both covered by `pr-validate.sh`).

## Breaking change?

No — or describe the migration path.
