# No Competitor References Or External License Headers

Patter is an open-source SDK released under its own MIT license (see [LICENSE](../../LICENSE)). The codebase must stand on its own: no copied license headers from other projects, no provenance comments crediting other projects, no name-checking other voice/telephony SDKs inside source files.

## Hard no — never commit any of these

### 1. External license headers

Do NOT add or keep file-level license blocks that came from another project. Specifically forbidden:

- `# Copyright (c) <year> <Other Project>` (any third-party attribution block)
- Apache 2.0 boilerplate (`Licensed under the Apache License, Version 2.0 ...`)
- `SPDX-License-Identifier: Apache-2.0` lines carried over from another codebase
- BSD / GPL / LGPL block headers from any external project

Patter source files do **not** carry per-file copyright headers. The single root `LICENSE` covers the whole repo. Adding per-file headers — even Patter-branded ones — is unnecessary noise.

### 2. Provenance / "ported-from" comments

Do NOT add comments that credit or trace lineage to another project:

- `# Adapted from <other-project>`
- `// Based on <other-repo>/<file>`
- `# Inspired by <other SDK>`
- URLs in comments pointing to other projects' repos or vendor blogs

If an algorithm was originally learned from another project, re-implement it in Patter idioms and don't call it out in the source. Citing an **integrated provider's own** public spec or repo — e.g. a Twilio or Telnyx API doc when implementing that integration — is fine; that's documenting an integration, not crediting a competitor.

### 3. Competitor name-checking in code

Do NOT mention competing-product or company names in:

- Source code identifiers
- Docstrings and code comments
- Error messages exposed to the user
- Test names and test descriptions
- Example file names and example content

This applies to the names of competing voice/telephony agent SDKs and platforms. Use a neutral, Patter-native term instead.

### 4. Personal paths, usernames, emails

Same rule as `security.md` and `no-internal-docs.md`: never commit `/Users/<name>/` home paths, maintainer usernames, or personal emails into the public repo.

## Allowed exceptions

These are **integrations** we ship and document — naming them is required, not forbidden:

- **Voice providers we integrate with**: OpenAI, ElevenLabs, Deepgram, Cartesia, Whisper, Cerebras, Anthropic, Google (Gemini Live), Inworld. The provider name appears in module names (`providers/elevenlabs_tts.py`), in docstrings ("ElevenLabs TTS adapter"), and in user-facing docs. Fine.
- **Telephony carriers we integrate with**: Twilio, Telnyx, Plivo. Same — `carriers/twilio.py` is fine. The raw carrier API is a runtime dependency, not a competing SDK.
- **`docs/` and `CHANGELOG.md` / `CONTRIBUTING.md`**: documentation can mention any technology by name when describing integrations or migration guides for users. The rule above is about **source files** (`.py`, `.ts`, `.js`, in `libraries/`, `examples/`, `scripts/`, `tests/`).

## Migration / cleanup

Any source file that still carries an external license header, a provenance comment, or a competing-product name in an identifier/string must be cleaned of it. After cleanup the file should read as if it was written for Patter from scratch.

## Enforcement

- **Pre-commit**: a hook should fail the commit if any pattern from sections 1–3 is detected in the staged diff (planned: `scripts/pre-commit-no-competitor.sh`).
- **CI**: the test workflow scans `main` periodically to catch leakage.
- **Code review**: the `code-reviewer` agent flags any new file containing patterns from this rule.
- **AI agent**: before staging any file under `libraries/**`, `examples/**`, `tests/**`, `scripts/**`, grep for the patterns above and clean before staging.

## Why

This provenance/attribution discipline avoids needless attribution disputes and keeps the implementation unambiguously Patter's own. Re-implement ideas in Patter idioms and own the result.
