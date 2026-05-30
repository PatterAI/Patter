# SDK Parity Rule

Patter ships two SDKs. A user on Python must be able to build the same thing, with the same API shape, as a user on TypeScript.

## Hard invariants

1. **Every public feature exists in BOTH SDKs.** No exceptions, no "TS-only" or "Python-only" config fields.
2. **Defaults match byte-for-byte** across SDKs. If `temperature` defaults to `0.7` in Python, it defaults to `0.7` in TS.
3. **Error taxonomy matches.** `PatterError` subclasses in `exceptions.py` have a matching class in `errors.ts` with the same name.
4. **One PR lands in both SDKs.** Do not merge a Python feature and "TS coming soon".

## Language-appropriate differences (allowed)

- Names: `snake_case` (Python) ↔ `camelCase` (TS) — same concept, same position.
- Models: `@dataclass(frozen=True)` (Python) ↔ `readonly interface` (TS).
- Async: `async def` (Python, asyncio) ↔ `async function` (TS, Promises).
- Config objects: `kwargs` (Python) ↔ single options object (TS). Keys match 1:1.

## Internal implementation may diverge

- Python separates handler files (`handlers/twilio_handler.py`), TS inlines them in `server.ts`. Fine.
- Python uses `websockets` lib, TS uses `ws` lib. Fine.
- Internal helpers (`_build_frame`, `parseChunk`) don't need parity — they're not public.

## Checking parity

Before opening a PR that touches any of these files, run `/parity-check`:

- `client.py` ↔ `client.ts`
- `models.py` ↔ `types.ts`
- `exceptions.py` ↔ `errors.ts`
- `server.py` routes ↔ `server.ts` routes
- `pricing.py` defaults ↔ `pricing.ts` defaults
- `services/metrics.py` fields ↔ `metrics.ts` fields
- `providers/*` constructor signatures

## When in doubt, Python is the reference

The Python SDK was developed first and has more complete feature set. If a new feature lands in Python first, port to TypeScript in the SAME PR, following TS idioms.
