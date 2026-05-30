# Security Rule

Patter handles phone calls, webhooks, API keys, and (optionally) audio recordings. Security bugs are customer-visible incidents.

## Hard no

1. **No API keys in source or tests.** Use env vars: `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, `TWILIO_AUTH_TOKEN`, `TELNYX_API_KEY`. Tests use mocks.
2. **No secrets in logs.** See `.claude/rules/logging.md`.
3. **No unsafe webhook signature paths.** `if signature: verify else pass` is a vulnerability.
4. **No `eval`, `exec`, `Function(...)`** on user-supplied strings. Ever.
5. **No shell = True / child_process.exec** with user input. Use arg arrays.
6. **No hardcoded PII in docs, examples, or tests.** Real phone numbers, emails, customer SIDs, and internal IP addresses do not belong in the repo — not even in README snippets.

### What counts as PII/sensitive in this repo

| Data | Safe form | Never commit |
|---|---|---|
| Phone number | `+1555xxxxxxx` (NANP 555 fiction range), `+14155551234`, `+39111222333` | Any real customer or company number |
| Email | `user@example.com`, `hello@patter.example` | Real addresses tied to a person |
| Twilio Account SID | `ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`, `ACtest...000` | Live `AC[0-9a-f]{32}` |
| Twilio Call SID | `CA0000000000000000000000000000a001` (per-test hex placeholder) | Live call SIDs from production logs |
| IP address | `127.0.0.1`, `192.168.x.x`, or documented blocklist targets (`169.254.169.254` for SSRF tests) | Production infra IPs |
| API key | env var reference only (`${OPENAI_API_KEY}`) | Any token with 20+ entropy chars |

### Enforcement

Two layers run automatically:

1. **`.claude/hooks/scan-sensitive-on-write.sh`** — `PostToolUse` hook, fires after Write/Edit/MultiEdit. Blocks (exit 2) on high-confidence secrets (private keys, `sk-proj-*`, `sk-ant-*`, `AKIA*`, `ghp_*`, `xox*`, `AIza*`). Warns (stderr, exit 0) on possible hardcoded phone / email / Twilio SID / public IPv4. Whitelists the placeholder patterns in the table above plus `.env.example`, `.claude/hooks/`, `.claude/rules/`, and existing security test files.
2. **`.githooks/pre-push`** — last-line defence before any git push. Greps the whole repo for the same high-confidence patterns and aborts the push.

If the hook blocks, **rotate** the leaked credential (regenerate at the provider), replace the hardcode with an env var reference, and re-run.

## Webhook verification (MUST)

### Twilio
- Verify `X-Twilio-Signature` with the auth token.
- Reconstruct the exact URL Twilio signed: scheme + host + path + sorted POST params.
- See `sdk/patter/handlers/common.py` → `validate_twilio_signature` (canonical impl).

### Telnyx
- Verify Ed25519 signature (`X-Telnyx-Signature-Ed25519`).
- Check `X-Telnyx-Timestamp` is within ±5 minutes (anti-replay).
- See `handlers/common.py` → `validate_telnyx_signature`.

Both validators must return `False` (not raise) on missing header — caller decides whether to 401.

## Input validation

- Phone numbers: E.164 validated BEFORE passing to telephony API (`+[country][number]`).
- Tool arguments: validate against the tool's declared schema before executing.
- Template variables in `system_prompt`: restrict to declared variable names; no `{{__import__('os')}}` escapes.

## Audio recording

- Opt-in only (`Patter.agent(record=True)`).
- Recordings stored with customer credentials (Twilio/Telnyx), not re-uploaded elsewhere.
- Never log recording URLs with signed tokens at INFO level.

## Dashboard

- Basic auth ONLY in front of the dashboard. Disable by default when exposed beyond `127.0.0.1`.
- Dashboard serves transcripts — treat as PII, don't cache in service workers.

## Dependency hygiene

- `pip-audit` (Python) / `npm audit` (TS) run in CI; fail on HIGH or CRITICAL.
- Never add a dependency without reading its source / checking download counts / checking last release date.
- Minimise deps — we already have very few and want to keep it that way.

## Reporting

Security issue found by Claude during review:
1. **STOP** the current task.
2. Summarise the issue privately in the chat — do NOT commit details to the repo.
3. Wait for user direction before fixing.
4. If already committed: flag to rotate any exposed secret and open a security advisory.
