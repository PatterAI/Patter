# Opt-In Config Rule

Every new config field is **optional with a sensible default**. Never break a working user.

## The rule

When adding a field to:
- `Patter.__init__` / `new Patter(...)` — optional with default
- `Patter.agent(...)` options — optional with default
- Any provider factory (`Patter.deepgram`, `Patter.elevenlabs_tts`, etc.) — optional with default
- `local_config.py` / `LocalOptions` — optional with default

## Defaults must be safe

- **Never** require a new API key (breaks existing `Patter(api_key=...)` calls).
- **Never** change behavior of existing arguments (adding a new option is OK; changing an old default is not).
- **Never** default to a paid feature (recording, transcription upload) — user must opt in.

## Examples

### GOOD: opt-in with safe default

```python
@dataclass(frozen=True)
class Agent:
    system_prompt: str
    voice: str = "alloy"
    # New field — optional, safe default
    max_turn_duration_ms: int | None = None   # None = no limit
```

```ts
interface Agent {
  readonly systemPrompt: string;
  readonly voice?: string;
  readonly maxTurnDurationMs?: number;   // undefined = no limit
}
```

### BAD: required param or changed default

```python
# Breaks every existing caller that doesn't pass max_turn_duration_ms
max_turn_duration_ms: int
```

```python
# Changes behavior silently — existing users suddenly see different voice
voice: str = "echo"   # was "alloy"
```

## Deprecation path

If you MUST remove/rename a field:
1. Keep the old name. Emit a `DeprecationWarning` (Python) / `console.warn` (TS) on first use per process.
2. Document deprecation in `CHANGELOG.md` with the target removal version.
3. Remove no sooner than one minor version later (semver discipline).

## Verification

- Changing or adding a public field? Add a test that instantiates with the OLD shape (no new field) and confirms it still works.
- Parity: the new field must ship in both SDKs with identical name-mapping and default.
