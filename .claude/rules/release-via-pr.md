# Release via PR — never push to `main` directly

## The rule

**Every code change that ships in a release MUST land on `main` via a merged pull request.** No exceptions, no fast paths, no "small fixes". Tagging a release version is forbidden until the corresponding code is on `main` via a merged PR.

## Why this exists

We had a near-miss on 2026-04-27 where an AI agent direct-pushed two release commits straight to `main`, then tagged `v0.5.4`, then triggered the publish workflow — bypassing every CI gate, every review, every chance to catch a regression. The package shipped fine in that case, but the path was unsound: the next time a "small fix" goes that route, a regression bypassed CI, hits PyPI/npm, and the only recourse is a hotfix release.

The user is a repo admin so they have the GitHub-level power to direct-push. The rule prevents the *intent* to direct-push, not the *capability* — discipline, not enforcement.

## Required workflow (every time, no shortcuts)

1. **Branch off `main`** — `git checkout -b release/X.Y.Z` or `fix/<short>` or `feat/<short>`. Never edit `main` in place.
2. **Commit changes** in conventional-commit style (`feat:`, `fix:`, `perf:`, `docs:`, `chore:`, `refactor:`, `test:`).
3. **Version bumps land in the same commit** as the release-triggering change. ALL THREE files MUST move together:
   - `sdk-py/getpatter/__init__.py` → `__version__ = "X.Y.Z"`
   - `sdk-py/pyproject.toml` → `version = "X.Y.Z"`
   - `sdk-ts/package.json` → `"version": "X.Y.Z"`
   Forgetting `pyproject.toml` is the most common slip — the PyPI publish job 404s silently when the version literal doesn't match the tag.
4. **Push the branch** (`git push -u origin <branch>`) and **open a PR** (`gh pr create`).
5. **Wait for full CI green** before merging:
   - Python SDK Tests (3.11, 3.12, 3.13)
   - Python All-Extras Tests
   - TypeScript SDK Tests (Node 20, Node 22)
   - E2E Tests
   - Security Tests
   - Pre-commit (lint + hygiene)
   - Secret scan (trufflehog)
   - Bandit + pip-audit + npm audit
6. **The user merges the PR.** AI agents do not auto-merge.
7. **AFTER the PR is merged on GitHub**, locally:
   ```bash
   git checkout main && git pull --ff-only origin main
   git tag -a vX.Y.Z -m "Release X.Y.Z — <one-line summary>"
   git push origin vX.Y.Z
   ```
   The tag-push triggers `.github/workflows/release.yml` which auto-publishes to PyPI (OIDC) and npm (token).
8. **Create the GitHub Release annotation** via `gh release create vX.Y.Z` with notes that link the merged PR.

## Hard nos

- ❌ `git push origin main` with new code — even for a typo fix.
- ❌ `git tag vX.Y.Z` before the corresponding code is merged on `main` via PR.
- ❌ `git push origin vX.Y.Z` for a tag that points at unmerged code.
- ❌ Local `twine upload` / `npm publish` / `npm run publish`. Publishes go through GitHub Actions only.
- ❌ Skipping any of the three version files in step 3.
- ❌ "Quick fix, no PR needed" reasoning. The PR workflow exists precisely for those — CI catches surprises that local tests miss.
- ❌ `gh pr merge --admin` to bypass a failing required check, ever. See [never-admin-merge.md](./never-admin-merge.md) — the agent never auto-promotes to admin merge; the user must explicitly authorise the bypass in the current turn AND the agent must surface which check is being bypassed and why.

## When the user says "fast" or "quick" or "small fix"

They almost always mean "do it through a PR, just quickly" — not "skip the PR". When in doubt, clarify before touching `main`. A fast PR (branch + commit + push + open + merge + tag + auto-publish) is ~5-10 minutes total and offers an audit trail. A direct push saves ~3 minutes and risks breaking production for every user who pins a version.

## What the AI agent does when it catches itself about to direct-push

1. STOP.
2. Switch to a fresh branch off `main`.
3. Cherry-pick or recommit the change there.
4. Open the PR.
5. Tell the user explicitly: "I almost direct-pushed; switching to a PR per the release-via-pr rule."

## Recovery if a direct push has already happened

If the AI agent (or anyone) realises a direct push already landed on `main`:

1. Do NOT tag and publish from the direct-push commit.
2. Revert the direct-push commit (`git revert <sha>`), push the revert via PR.
3. Re-do the work in a new branch, open PR, merge, tag, publish.
4. If the publish has already shipped (PyPI / npm), the version is permanently locked. Flag the workflow violation in chat; ship the next-version fix via the proper PR path.
