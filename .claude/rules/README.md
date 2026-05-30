# Patter `.claude/rules/` — Rule Index

Rules the AI (and humans) follow when working in this repo. They encode hard invariants we learned the hard way; breaking one is a bug, not a stylistic choice.

| Rule | What it guards |
|------|----------------|
| [release-via-pr](./release-via-pr.md) | **Every release lands on `main` via a merged PR — never direct-push, never pre-tag** |
| [never-admin-merge](./never-admin-merge.md) | **Never `gh pr merge --admin` to bypass a failing required check — fix the check first, even on docs PRs** |
| [sdk-parity](./sdk-parity.md) | Every feature in BOTH Python and TypeScript SDKs |
| [authentic-tests](./authentic-tests.md) | Tests verify real behaviour; mocks only at external boundary and explicitly tagged |
| [testing](./testing.md) | 80% coverage, TDD, pytest + vitest conventions |
| [immutability](./immutability.md) | Frozen dataclasses (Python) / readonly (TS) on public config |
| [opt-in-config](./opt-in-config.md) | New fields optional with safe defaults (backward compat) |
| [logging](./logging.md) | No `print()`, no PII in logs, Python `logging`, TS `console.*` |
| [async-everywhere](./async-everywhere.md) | All I/O is async; no blocking in event loop |
| [security](./security.md) | No secrets in source, webhook signatures verified, input validated |
| [no-internal-docs](./no-internal-docs.md) | Competitor analyses, internal plans, test reports never land in the public repo |
| [no-competitor-references](./no-competitor-references.md) | No external license headers, "ported from" comments, or competitor names in source files |
| [documentation-best-practices](./documentation-best-practices.md) | CHANGELOG `## Unreleased` updated in the same commit as the code; plans archived to a private repo; every shipped feature logged in the feature inventory; docs-sync after every ship |

## Precedence

When a rule conflicts with the default system prompt or a general skill, **rules win**. When two rules seem to conflict, ask — it's probably a bug in the rule I should fix.

## Adding a rule

Rules earn their place. A rule is justified when:
- You've debugged the same class of issue twice.
- The rule would have prevented the second occurrence.
- It's too specific to be a general coding-standard.

File format:
- Clear single-sentence title (what it guards).
- Hard invariants up top.
- Allowed exceptions (if any) below.
- Verification / enforcement mechanism (test, hook, review).

Keep each rule under ~80 lines — if you need more, you're writing a design doc, not a rule.
