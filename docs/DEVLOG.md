# Development Log

## Log

### [2026-05-07] — `manageWebhook` opt-out for `serve()` (TS)

**Type:** feat
**Branch:** feat/manage-webhook-opt-out
**PR:** _pending_

**What it does:**
Adds a new `manageWebhook?: boolean` option to `ServeOptions` (TypeScript SDK), defaulting to `true` so existing callers see no behaviour change. When set to `false`, `serve()` skips the call to `autoConfigureCarrier`, leaving the carrier's `voice_url` untouched. This is required for users running the SDK behind a router/gateway whose Twilio webhook is managed externally (Terraform, infra-as-code) — otherwise every container boot silently overwrites the externally-managed value, breaking the gating layer.

`tunnel: true` overrides `manageWebhook: false` (the tunnel hostname is dynamic and only known at runtime, so the carrier MUST be reconfigured for inbound calls to land).

**Why:**
Discovered when a downstream consumer (Patter demo phone line, fronted by a Cloud Functions voice-router that does rate-limiting + killswitch enforcement) noticed Twilio's `voice_url` was being silently rewritten to point at the Cloud Run agent on every boot — bypassing the gating function. There was no SDK opt-out. Locally this same code path is also responsible for "I ran the SDK on my laptop and Twilio is now pointing at my dev tunnel" footguns: the SDK proactively patches Twilio without asking.

**Implementation details:**
- `ServeOptions.manageWebhook` is the only new field.
- The gate (`opts.manageWebhook !== false || wantsCloudflared`) treats undefined as the default `true`, an explicit `true` as opt-in, an explicit `false` as opt-out (unless tunnel mode forces the auto-configure).
- Python SDK has no auto-configure path, so it already behaves as-if `manageWebhook=false`. This is closing a parity gap, not creating one.

**Files changed:**

| File | Change |
|------|--------|
| `sdk-ts/src/types.ts` | Added `manageWebhook?: boolean` to `ServeOptions` with full doc comment. |
| `sdk-ts/src/client.ts` | Gated the `autoConfigureCarrier` call on `wantsCarrierManagement`. |
| `sdk-ts/tests/unit/client.test.ts` | Added 3 tests: default, explicit `true`, explicit `false`. |
| `docs/typescript-sdk/local-mode.mdx` | Added `manageWebhook` row to the ServeOptions table. |
| `CHANGELOG.md` | Added Unreleased entry. |

**Tests added:**
- `sdk-ts/tests/unit/client.test.ts` — 3 cases under `serve() > manageWebhook opt-out`: asserts the Twilio API URL is hit by default, hit when `true`, and **not** hit when `false`. Uses real `globalThis.fetch` swap to capture call URLs (no mock-on-mock).

**Breaking changes:** None. Default `true` preserves existing behaviour byte-for-byte.

**Docs to update:**
- [x] `docs/typescript-sdk/local-mode.mdx` — added row.
- [ ] `docs/typescript-sdk/configuration.mdx` — add a callout under "Manual webhook management" if a section is added later.
- [ ] `patter_sdk_features.xlsx` — log feature row (`manage_webhook`, status=shipped, sdk=typescript, version=unreleased).

**Parity:**
- Python: no equivalent change needed — Python SDK has never auto-configured carriers. This TS change brings `manageWebhook: false` mode into line with default Python behaviour.
