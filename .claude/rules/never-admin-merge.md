# Never `--admin` merge a PR with red checks

## The rule

**Never use `gh pr merge --admin` (or any GitHub admin override) to bypass a failing required status check.** If a check is red, fix it before merging — even when the check looks like a flake, even when the PR is just docs, even when it's "just" the author's own PR.

This rule binds the AI agent. The user is the repo admin and can do whatever they want via the GitHub UI; the agent must not invoke the admin path on the user's behalf without explicit, in-the-moment confirmation that the failing check has been understood and the bypass is intentional.

## Why this exists

PR #103 (an external contributor's docs-only change) was merged via `gh pr merge 103 --squash --admin` on 2026-05-25 after the agent saw `gh pr merge 103` rejected with "the base branch policy prohibits the merge". The blocking check was `TypeScript SDK Tests (22) — Failing`. The agent flagged the prior `gh pr merge` failure to the user but auto-promoted to `--admin` without surfacing what was failing and why — and as a result a real Node 22 test flake landed on `main`. The right call was to surface the failing check name to the user, ship a separate fix PR for the flake, and only then merge #103 once green.

The asymmetry is asymmetric in cost: a 2-minute pause to investigate beats a green-main commit that masks a regression.

## How to apply

**When `gh pr merge` returns "base branch policy prohibits the merge":**

1. **Stop.** Do not retry with `--admin` automatically.
2. Read the failing check via `gh pr checks <PR#>`.
3. For each failing check, fetch the failure: `gh run view --job <job-id> --log-failed | tail -50`.
4. Classify the failure:
   - **Real bug in the PR** → comment on the PR, request changes, do not merge.
   - **Test flake / infra hiccup unrelated to the PR** → open a separate fix PR for the flake, get it green on `main`, then re-trigger CI on the original PR; merge the original PR only after the re-trigger goes green.
   - **Blocking check is wrong / no longer applicable** → fix the workflow definition in its own PR.
5. **Only after the failing check is green** may the merge proceed via the normal `gh pr merge --squash` path.

**`--admin` is permitted only when:**

- The user has explicitly typed "use admin merge" (or equivalent) in the current turn, AND
- The agent has surfaced *which* check is being bypassed and *why* it's safe to bypass it.

A user saying "merge it" or "go ahead" does NOT count as admin-merge authorisation — that's authorisation for the normal path, which a red check should block.

## What to do when the user asks for an admin merge

Restate the failing check name and the failure mode in the same message that performs the merge, so the bypass is visible in the conversation transcript. Example:

> Merging PR #103 with `--admin` per your explicit confirmation. Bypassing `TypeScript SDK Tests (22)` which is failing on `tests/server.test.ts:384` — an unrelated Node 22 timing flake (covered separately in PR #105).

If the user asked for `--admin` but the failing check is **not** a flake (real bug), push back: ask the user to confirm they want to land a known-broken commit, and link the failing-test logs. The user's "yes" still goes; the agent's job is to make sure they're saying yes with full information.

## Related

- `release-via-pr.md` — every release lands on `main` via a merged PR; this rule adds the "every merged PR has green required checks" half of the same discipline.
- `git-workflow` (in `~/.claude/rules/common/`) — confirm risky/destructive actions before performing them.
