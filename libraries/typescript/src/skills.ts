/**
 * Runtime SKILLS primitive — progressive-disclosure activation of a named
 * playbook (instructions) plus optional scoped tools, INLINE in the agent loop.
 *
 * A {@link Skill} given to `phone.agent({ skills: [...] })` is surfaced to the
 * primary agent in two layers (the Anthropic Agent Skills pattern), keeping
 * latency minimal:
 *
 * - **Discovery** (call start): only each skill's `name` + `description` are
 *   advertised, via the built-in `use_skill` tool whose `skill_name` is an ENUM
 *   of the skill names (the same enum trick `buildHandoffTool` uses). The full
 *   `instructions` are NOT loaded into the prompt yet.
 * - **Activation** (model calls `use_skill`): the chosen skill's `instructions`
 *   are layered into the system prompt as a `# Skill: <name>` block and its
 *   `tools` are unlocked — ADDITIVELY (the base prompt + existing tools stay;
 *   the skill stacks on top). This is the reversible counterpart to the one-way
 *   `handoff_to` REPLACE. Applied via the same `updateAgent` (Pipeline) /
 *   `session.update` (Realtime) plumbing the handoff uses, so it is
 *   provider-agnostic.
 *
 * These helpers are pure (no I/O, no provider coupling) so they can be unit
 * tested directly and reused by both the Realtime and Pipeline paths. Parity
 * with Python `getpatter.tools.skills`.
 */

import type { AgentOptions, Skill, ToolDefinition } from './types';

/**
 * Name of the built-in skill-activation tool injected when `agent.skills` is
 * non-empty. The model calls `use_skill({ skill_name })` to activate.
 */
export const USE_SKILL_TOOL_NAME = 'use_skill';

/**
 * Built-in tool names a skill (or skill tool) may never shadow. A stable
 * public contract, kept here so the agent factory and stream handler share it.
 */
export const RESERVED_TOOL_NAMES: ReadonlySet<string> = new Set([
  'transfer_call',
  'end_call',
  'handoff_to',
  USE_SKILL_TOOL_NAME,
]);

/**
 * Build the `use_skill` tool schema for the given `skills`.
 *
 * The skill names are surfaced both as a JSON-schema `enum` (so the model can
 * only pick a configured skill) and, with their descriptions, in the tool
 * description — the *discovery* layer of progressive disclosure: the model
 * learns WHICH skills exist and WHEN to use them WITHOUT the full instructions
 * being loaded. Sorted by name for a deterministic schema, like
 * `buildHandoffTool`. Parity with Python `build_use_skill_tool`.
 */
export function buildUseSkillTool(skills: ReadonlyArray<Skill>): {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
} {
  const ordered = [...skills].sort((a, b) => a.name.localeCompare(b.name));
  const names = ordered.map((s) => s.name);
  const catalog = ordered.map((s) => `- ${s.name}: ${s.description}`).join('\n');
  return {
    name: USE_SKILL_TOOL_NAME,
    description:
      'Activate a specialized skill to load its detailed playbook and dedicated ' +
      "tools for the current task. Call this the moment the caller's request " +
      'matches one of the skills below, BEFORE attempting the task — it layers ' +
      "the skill's step-by-step instructions into your context and unlocks its " +
      'tools for the rest of the call. Available skills:\n' + catalog,
    parameters: {
      type: 'object',
      properties: {
        skill_name: {
          type: 'string',
          enum: names,
          description: 'Name of the skill to activate.',
        },
      },
      required: ['skill_name'],
    },
  };
}

/** Render the system-prompt block layered in when `skill` activates. */
export function skillActivationBlock(skill: Skill): string {
  return `# Skill: ${skill.name}\n\n${skill.instructions}`;
}

/** Render the system-style transcript line recording a skill activation. */
export function skillActivationHistoryText(name: string): string {
  return `[skill] Activated skill '${name}'`;
}

/**
 * Return a copy of `agent` with `skill` layered in ADDITIVELY.
 *
 * Unlike `applyHandoffTarget` (a one-way REPLACE), this MERGES: the skill's
 * `instructions` are appended to the current `systemPrompt` as a
 * `# Skill: <name>` block, and the skill's `tools` are appended to the current
 * `tools` (deduped by name). The shared `agent` is never mutated — a new object
 * is returned, retaining the base prompt, existing tools, `skills` list, and
 * all audio/engine config. Parity with Python `apply_skill_activation`.
 */
export function applySkillActivation(agent: AgentOptions, skill: Skill): AgentOptions {
  const block = skillActivationBlock(skill);
  const basePrompt = agent.systemPrompt ?? '';
  const newPrompt = basePrompt ? `${basePrompt}\n\n${block}` : block;
  const existing = (agent.tools as ReadonlyArray<ToolDefinition> | undefined) ?? [];
  const existingNames = new Set(existing.map((t) => t.name));
  const additions = ((skill.tools as ReadonlyArray<ToolDefinition> | undefined) ?? []).filter(
    (t) => t.name && !existingNames.has(t.name),
  );
  return {
    ...agent,
    systemPrompt: newPrompt,
    tools: [...existing, ...additions] as AgentOptions['tools'],
  };
}

/**
 * Throw on any skill name / skill-tool name collision.
 *
 * Generalises `MCPManager.assertNoConflicts` (user↔MCP tools) to the skill
 * surface, guarding three classes of collision so an activated skill never
 * shadows a built-in or duplicates an existing tool:
 *
 * 1. Two skills sharing a `name`.
 * 2. A skill tool named like a RESERVED built-in (`transfer_call`, `end_call`,
 *    `handoff_to`, `use_skill`).
 * 3. A skill tool colliding with a top-level agent tool, or with a tool
 *    declared by another skill.
 *
 * `agentTools` should be the ORIGINAL top-level tools (before any skill
 * activation merges tools in). No-op when `skills` is empty. Parity with Python
 * `assert_no_skill_conflicts`.
 */
export function assertNoSkillConflicts(
  skills: ReadonlyArray<Skill> | undefined,
  agentTools?: ReadonlyArray<{ name?: string }> | null,
): void {
  if (!skills || skills.length === 0) return;
  const agentToolNames = new Set(
    (agentTools ?? []).map((t) => t?.name).filter((n): n is string => Boolean(n)),
  );
  const seenSkillNames = new Set<string>();
  const seenToolNames = new Set<string>();
  for (const skill of skills) {
    const name = skill.name;
    if (!name) throw new Error('Each skill must have a non-empty name.');
    if (seenSkillNames.has(name)) throw new Error(`Duplicate skill name '${name}'.`);
    seenSkillNames.add(name);
    for (const tool of skill.tools ?? []) {
      const tname = (tool as { name?: string }).name;
      if (!tname) continue;
      if (RESERVED_TOOL_NAMES.has(tname)) {
        throw new Error(
          `Skill '${name}' declares a tool named '${tname}', which is a reserved ` +
            'built-in tool name (transfer_call, end_call, handoff_to, use_skill). ' +
            'Rename the skill tool.',
        );
      }
      if (agentToolNames.has(tname)) {
        throw new Error(
          `Skill '${name}' tool '${tname}' collides with a top-level agent tool of ` +
            'the same name. Rename one of them.',
        );
      }
      if (seenToolNames.has(tname)) {
        throw new Error(
          `Skill tool '${tname}' is declared by more than one skill. Skill tool ` +
            'names must be unique across skills.',
        );
      }
      seenToolNames.add(tname);
    }
  }
}
