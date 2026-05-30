# No Internal Docs in the Public Repo

The `PatterAI/Patter` GitHub repo is public. Material meant for maintainers only must not land here.

## Hard no — never commit to the public repo

These classes of document are internal-only and must NEVER be added, even under `docs/`:

1. **Competitor / market analysis** — comparisons against other voice/telephony platforms, feature-gap matrices. Files like `COMPETITIVE_ANALYSIS.md`, `market-research.md`.
2. **Internal execution plans / PRDs / RFCs** — `PLAN-*.md`, `PRD-*.md`, `RFC-*.md`, dated roadmap docs, sprint plans, OKRs.
3. **Test run reports** — `test-reports/*`, `*-test-report.md`, CI run summaries, coverage snapshots, flaky-test triage notes. Running output that exposes local paths (`/Users/<name>/...`) is doubly forbidden.
4. **Benchmarks vs other platforms** — latency/accuracy numbers comparing Patter to a named third-party product.
5. **Stakeholder / investor / hiring material** — pitch decks, investor updates, interview rubrics, offer templates.
6. **Sales / GTM collateral** — pricing strategy, customer lists, churn analysis, pipeline notes.
7. **Agent prompts and internal playbooks authored for our own tooling** — dated `prompts/*` files, private `devlog.md` entries, AI-agent session transcripts.

## Allowed in the public repo

- Product documentation (Mintlify-driven `docs/`): quickstart, reference, examples.
- Technical content about integrated providers (Twilio, Telnyx, OpenAI, ElevenLabs, Deepgram, Whisper, …) — **integrations are not "competitor analyses."** Documenting how Patter uses Twilio is fine; benchmarking Patter against Twilio's own voice SDK is not.
- Branding assets referenced from public surfaces: logos under `docs/logo/`, `docs/github-banner.png` (used by README).
- Changelogs, contributing guide, license, security policy, code of conduct.

## Where internal docs live instead

- A separate **private repository** — plans, RFCs, PRDs.
- Local only: `docs/internal/` is already in `.gitignore`. Use it for scratch analyses that must never leave the machine.
- An external docs tool (Notion / Linear / Google Docs) for anything stakeholder-facing.

## Enforcement

Before any `git add` / `git commit` that touches `docs/`, `README*`, or the repo root:

1. Check the file against the "Hard no" list above. Filename heuristics that should trigger a stop:
   - contains `COMPETITIVE`, `COMPETITOR`, `COMPARISON`, `VS-`, `-VS-`, `BENCHMARK`
   - contains `PLAN-`, `-PLAN-`, `RFC-`, `PRD-`, `ROADMAP`
   - is inside `test-reports/`, `prompts/`, `internal/`, `private/`, `notes/`
   - has an ISO date prefix (`YYYY-MM-DD-`) and lives under `docs/`
2. Grep the file body for internal-path signatures: `/Users/<name>/` home paths, maintainer usernames, the private assets repo path, and references to un-shipped features by internal codename.
3. If ANY check trips, stop and ask before staging. Do not "fix the filename" and proceed — the decision belongs to a maintainer.

## If an internal doc already leaked to the public repo

1. STOP. Do not push more commits on top — they bake the leak deeper into history.
2. Flag it immediately. Name the file and the commit.
3. Wait for direction. Options are (a) `git rm` + new commit (file is gone from `HEAD` but stays in history), (b) rewrite history (`git filter-repo` / `bfg`) + force-push, coordinated because it breaks every open clone.
4. Treat any secrets in the leaked file as compromised — rotate.

## Why

Internal-only documents were once committed to the public repo by mistake, and the cleanup `git rm` left the content visible in history. This rule keeps it from happening again.
