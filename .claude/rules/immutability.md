# Immutability Rule

All public config objects are immutable. Mutation of shared config across calls is a bug class we don't want.

## Python

Use `@dataclass(frozen=True)` for every public model:

```python
@dataclass(frozen=True)
class Agent:
    system_prompt: str
    voice: str = "alloy"
    tools: tuple[Tool, ...] = ()     # tuples, not lists
    guardrails: tuple[Guardrail, ...] = ()
```

- Collections: `tuple`, not `list`. `frozenset`, not `set`. `types.MappingProxyType`, not `dict` (or accept immutability by convention).
- Mutations return new instances: `agent_v2 = dataclasses.replace(agent, voice="echo")`.

## TypeScript

Use `readonly` on every field of every exported interface:

```ts
export interface Agent {
  readonly systemPrompt: string;
  readonly voice?: string;
  readonly tools?: readonly Tool[];
  readonly guardrails?: readonly Guardrail[];
}
```

- Arrays: `readonly T[]` or `ReadonlyArray<T>`.
- Records: `Readonly<{ [k: string]: V }>` or `ReadonlyMap<K, V>`.
- Mutations return new objects: `const v2 = { ...agent, voice: "echo" }`.

## What counts as "public"

- Anything re-exported from `sdk/patter/__init__.py` or `sdk-ts/src/index.ts`.
- Anything a user can pass to `Patter(...)` or receive from a callback.

## Internal state may mutate

- `MetricsStore` ring buffer, `StreamHandler` session object, provider connection state — internal, may mutate.
- Mutation must be local to one call/session; never shared across calls.

## Enforcement

- Python: `frozen=True` raises `FrozenInstanceError` on write — caught in tests.
- TypeScript: `readonly` enforced at compile time via `npm run lint` (`tsc --noEmit`).
- Review: `code-reviewer` agent flags any new `@dataclass` without `frozen=True` or any interface without `readonly`.
