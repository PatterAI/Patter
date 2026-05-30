# Documentation & Plan-Tracking Best Practices

Rule that guards: plans never get lost, every public feature reaches the docs, SDKs stay in sync with what was actually shipped, every shipped change is reflected in the changelog so the wiki / release notes can be regenerated cheaply.

## Hard invariants

### 0. Every code change updates `CHANGELOG.md` in the same unit of work

The repo-root `CHANGELOG.md` carries an `## Unreleased` section that must reflect everything pending for the next version bump. Whenever Claude (or a human) lands a change to either SDK that is user-visible — feature added, behaviour changed, bug fixed, default changed, dependency bumped — an entry MUST be appended to `## Unreleased` **in the same commit / PR** as the code change.

**What counts as "user-visible"** (entry required):
- Public API additions / removals / renames
- Default value changes (e.g. tunnel grace 2.5 → 5 s, model defaults, timeout defaults)
- Bug fixes that change observable behaviour (silent drops, dropped events, rate limits)
- Performance characteristics that callers can measure (latency, memory, startup)
- Dependency upgrades that change the runtime contract (Node minimum, Python minimum, TLS, …)

**What does NOT need an entry**:
- Pure refactors with zero behaviour change (move file, rename internal symbol, dead-code removal). Note in the commit message, not the changelog.
- Test-only additions / fixes.
- Comment-only or docstring-only changes.
- Internal helpers (prefix `_` Py / un-exported TS) that no caller can reach.

**Entry format** — group under one of: `### Added` / `### Changed` / `### Fixed` / `### Deprecated` / `### Removed` / `### Security`. One bullet per logical change. Lead with the user-visible effect, then explain *why*. Reference SDK file paths so future-Claude can grep back. Example:

```markdown
### Fixed

- **Cloudflared quick-tunnel WSS upgrade race**. Bumped grace
  window 2.5 → 5 s in `libraries/typescript/src/client.ts` /
  `libraries/python/getpatter/client.py`. The prior 2.5 s covered
  HTTP only — Twilio's media-stream WSS upgrade goes through a
  different cloudflared edge route ~1-3 s slower to propagate, and
  ~5 % of first calls dropped silently at pickup. 5 s drops the
  failure rate to <1 %.
```

**Version bump rotation**: when the user bumps version (per release-via-pr.md), the entire `## Unreleased` block is renamed to `## X.Y.Z (YYYY-MM-DD)` and a fresh empty `## Unreleased` is added on top. The version-bump commit can do this rename in the same diff.

**Why this exists**: the CHANGELOG is the single source of truth that gets pulled into the wiki, the GitHub Release notes (`gh release create vX.Y.Z`), the docs Mintlify "What's new" page, and the inventory xlsx `ships_in_version` column. If entries are written contemporaneously they are accurate; if they are reconstructed at release time from `git log` they are lossy and the *why* is gone.

**Verification**:
- Per-PR (blocking): the PR body's "Test plan" implicitly covers this — if the diff touches `libraries/{python,typescript}/` and `CHANGELOG.md` is unchanged, the reviewer (or `code-reviewer` agent) flags it. Refactors / tests / docs-only diffs are acknowledged in the PR description.
- Pre-release: `## Unreleased` MUST be non-empty before tagging a release. An empty Unreleased means either (a) no user-visible changes — don't release, or (b) entries were forgotten — recover from `git log --oneline <last-tag>..HEAD` and write them in.

### 1. All plans are mirrored to the shared assets folder

Every plan file Claude (or a human) produces — regardless of where it was originally authored — must end up in the private assets repository:

```
$PATTER_ASSETS/claude-code-plans/
```

(`$PATTER_ASSETS` is a local environment variable pointing at the private assets repository — it is not part of this public repo and is not required to build or use the SDK.)

This applies to:
- Plans generated via `ExitPlanMode` (`~/.claude/plans/*.md`)
- Strategy documents like `docs/TELCO_STRATEGY.md`
- Research bundles (`docs/research/*.md`)
- **Plans produced inside git worktrees** (`.claude/worktrees/<name>/docs/...`) — still mirrored, even though the worktree is throwaway

The repo-side copy is the working artefact. A copy archived outside this repo is the durable record that survives worktree deletion, branch pruning, and `git reset --hard`.

**How to apply**:
```bash
cp -r <plan-or-docs> "$PATTER_ASSETS/claude-code-plans/"
```
If the plan is a multi-file bundle (strategy + research), preserve the directory structure (e.g. `telco-research/r1-r4.md`).

### 2. Every shipped feature is logged in the SDK feature inventory

The canonical feature inventory lives at:

```
$PATTER_ASSETS/patter_sdk_features.xlsx
```

When Claude implements, modifies, or removes a public feature in either SDK (Python or TypeScript), the inventory row must be added/updated in the **same unit of work** that ships the code. Columns to fill:

- `feature_name` — short identifier (e.g. `silero_vad`, `anthropic_llm`)
- `status` — `shipped` / `beta` / `deprecated` / `removed`
- `sdk` — `python`, `typescript`, or `both` (see `sdk-parity.md`)
- `ships_in_version` — SDK version that introduced it (or "unreleased")
- `docs_page` — path in `docs/` where the feature is documented (empty if not yet documented)
- `test_coverage` — `unit` / `integration` / `e2e` / `none`
- `owner` — `claude` or human
- `date_updated` — ISO date

### 3. Docs follow code, automatically

Every feature that hits the inventory must appear in the Mintlify documentation under `docs/`. Whenever Claude ships a feature, it must immediately dispatch the `docs-sync` subagent (see `.claude/agents/docs-sync.md`) to:
1. Append/modify the feature row in the feature-inventory spreadsheet
2. Generate or update the corresponding page under `docs/python-sdk/` and `docs/typescript-sdk/`
3. Cross-link the feature from `docs/docs.json` navigation

If the docs-sync agent reports a diff (xlsx row exists without matching docs page or vice-versa), Claude must resolve the drift **before** reporting the task complete.

### 4. Daily consistency check

A scheduled GitHub Action (`.github/workflows/docs-feature-drift.yml`) runs at 03:00 UTC and:
- Reads the feature-inventory spreadsheet
- Cross-references every row against `docs/`
- Opens an issue labelled `docs-drift` listing any feature present in one but missing in the other
- Is a no-op on clean days

This catches drift that escaped the in-PR workflow. The daily issue is not optional — if it fires, resolution is priority-0 for the next working session.

## Allowed exceptions

- **Internal-only helpers** (prefix `_`, not exported from the package root) are exempt from the inventory and docs.
- **WIP features behind a feature flag** that aren't publicly reachable are logged with `status=beta` but `docs_page` can be empty until GA.
- **Bug-fix-only PRs** with no new public surface don't need an inventory row change, but if they touch behaviour documented in `docs/` the doc must be updated.

## Language-appropriate differences

- Single inventory row covers both SDKs; use `sdk=both` when parity is respected, `python` or `typescript` only during the transition window (which must close in the same release cycle per `sdk-parity.md`).
- Docs pages are per-SDK when the invocation differs (usually they do — keyword args vs options object); feature description and behaviour are shared prose.

## Verification / enforcement

- **Per-PR** (blocking): the docs-sync subagent is dispatched by Claude automatically at task completion; output must be clean or resolved.
- **Daily cron**: `.github/workflows/docs-feature-drift.yml` opens a `docs-drift` issue if mismatches exist.
- **Pre-release**: before tagging a release, the release PR body must embed the inventory diff (use `scripts/feature-diff.py` if present).

Keep the xlsx file small: one feature per row, no formulas, no hidden sheets — this keeps the cron diff cheap and readable.
