"""Runtime SKILLS primitive — progressive-disclosure activation of a named
playbook (instructions) plus optional scoped tools, INLINE in the agent loop.

A :class:`getpatter.models.Skill` given to ``Patter.agent(skills=[...])`` is
surfaced to the primary agent in two layers (the Anthropic Agent Skills
pattern), keeping latency minimal:

* **Discovery** (call start): only each skill's ``name`` + ``description`` are
  advertised, via the built-in ``use_skill`` tool whose ``skill_name`` is an
  ENUM of the skill names (the same enum trick :func:`build_handoff_tool`
  uses). The full ``instructions`` are NOT loaded into the prompt yet.
* **Activation** (model calls ``use_skill``): the chosen skill's
  ``instructions`` are layered into the system prompt as a ``# Skill: <name>``
  block and its ``tools`` are unlocked — ADDITIVELY (the base prompt + existing
  tools stay; the skill stacks on top). This is the reversible counterpart to
  the one-way ``handoff_to`` REPLACE. Applied via the same ``update_agent``
  (Pipeline) / ``session.update`` (Realtime) plumbing the handoff uses, so it
  is provider-agnostic.

These helpers are pure (no I/O, no provider coupling) so they can be unit
tested directly and reused by both the Realtime and Pipeline stream handlers.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from getpatter.models import Agent, Skill

#: Name of the built-in skill-activation tool injected when ``Agent.skills``
#: is non-empty. The model calls ``use_skill(skill_name=...)`` to activate.
USE_SKILL_TOOL_NAME = "use_skill"

#: Built-in tool names a skill (or skill tool) may never shadow. Kept as plain
#: literals — importing them from ``stream_handler`` here would create an
#: import cycle, and these names are a stable public contract.
RESERVED_TOOL_NAMES = frozenset(
    {"transfer_call", "end_call", "handoff_to", USE_SKILL_TOOL_NAME}
)


def build_use_skill_tool(skills: "tuple[Skill, ...]") -> dict:
    """Build the ``use_skill`` tool schema for the given *skills*.

    The skill names are surfaced both as a JSON-schema ``enum`` (so the model
    can only pick a configured skill) and, with their descriptions, in the tool
    description — this is the *discovery* layer of progressive disclosure: the
    model learns WHICH skills exist and WHEN to use them WITHOUT the full
    instructions being loaded. Sorted by name for a deterministic schema, like
    :func:`build_handoff_tool`. Parity with TS ``buildUseSkillTool``.
    """
    ordered = sorted(skills, key=lambda s: s.name)
    names = [s.name for s in ordered]
    catalog = "\n".join(f"- {s.name}: {s.description}" for s in ordered)
    return {
        "name": USE_SKILL_TOOL_NAME,
        "description": (
            "Activate a specialized skill to load its detailed playbook and "
            "dedicated tools for the current task. Call this the moment the "
            "caller's request matches one of the skills below, BEFORE "
            "attempting the task — it layers the skill's step-by-step "
            "instructions into your context and unlocks its tools for the rest "
            "of the call. Available skills:\n" + catalog
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "enum": names,
                    "description": "Name of the skill to activate.",
                },
            },
            "required": ["skill_name"],
        },
    }


def skill_activation_block(skill: "Skill") -> str:
    """Render the system-prompt block layered in when *skill* activates."""
    return f"# Skill: {skill.name}\n\n{skill.instructions}"


def skill_activation_history_text(name: str) -> str:
    """Render the system-style transcript line recording a skill activation."""
    return f"[skill] Activated skill '{name}'"


def apply_skill_activation(agent: "Agent", skill: "Skill") -> "Agent":
    """Return a copy of *agent* with *skill* layered in ADDITIVELY.

    Unlike :func:`getpatter.stream_handler._apply_handoff_target` (a one-way
    REPLACE), this MERGES: the skill's ``instructions`` are appended to the
    current ``system_prompt`` as a ``# Skill: <name>`` block, and the skill's
    ``tools`` are appended to the current ``tools`` (deduped by name, so a tool
    already present is not added twice). ``Agent`` is a frozen dataclass, so a
    new instance is returned via :func:`dataclasses.replace` — the base prompt,
    existing tools, ``skills`` list, and all audio/engine config are retained.
    """
    block = skill_activation_block(skill)
    base_prompt = agent.system_prompt or ""
    new_prompt = f"{base_prompt}\n\n{block}" if base_prompt else block

    existing = list(agent.tools or [])
    existing_names = {t.get("name") for t in existing if isinstance(t, dict)}
    additions = [
        t
        for t in (getattr(skill, "tools", None) or ())
        if isinstance(t, dict) and t.get("name") and t.get("name") not in existing_names
    ]
    return dataclasses.replace(
        agent,
        system_prompt=new_prompt,
        tools=tuple(existing) + tuple(additions),
    )


def assert_no_skill_conflicts(
    skills: "tuple[Skill, ...] | None",
    agent_tools: "list[dict] | tuple[dict, ...] | None" = None,
) -> None:
    """Raise ``ValueError`` on any skill name / skill-tool name collision.

    Generalises ``MCPManager.assert_no_conflicts`` (user↔MCP tools) to the
    skill surface, guarding three classes of collision so an activated skill
    never shadows a built-in or duplicates an existing tool:

    1. Two skills sharing a ``name``.
    2. A skill tool named like a RESERVED built-in (``transfer_call``,
       ``end_call``, ``handoff_to``, ``use_skill``).
    3. A skill tool colliding with a top-level agent tool, or with a tool
       declared by another skill.

    *agent_tools* should be the ORIGINAL top-level tool dicts (before any skill
    activation merges tools in) so a skill's own tools are not mistaken for a
    pre-existing collision. No-op when *skills* is empty.
    """
    if not skills:
        return
    agent_tool_names = {
        t.get("name") for t in (agent_tools or []) if isinstance(t, dict)
    }
    seen_skill_names: set[str] = set()
    seen_tool_names: set[str] = set()
    for skill in skills:
        name = getattr(skill, "name", "")
        if not name:
            raise ValueError("Each skill must have a non-empty name.")
        if name in seen_skill_names:
            raise ValueError(f"Duplicate skill name {name!r}.")
        seen_skill_names.add(name)
        for tool in getattr(skill, "tools", None) or ():
            tname = _tool_name(tool)
            if not tname:
                continue
            if tname in RESERVED_TOOL_NAMES:
                raise ValueError(
                    f"Skill {name!r} declares a tool named {tname!r}, which is a "
                    "reserved built-in tool name (transfer_call, end_call, "
                    "handoff_to, use_skill). Rename the skill tool."
                )
            if tname in agent_tool_names:
                raise ValueError(
                    f"Skill {name!r} tool {tname!r} collides with a top-level "
                    "agent tool of the same name. Rename one of them."
                )
            if tname in seen_tool_names:
                raise ValueError(
                    f"Skill tool {tname!r} is declared by more than one skill. "
                    "Skill tool names must be unique across skills."
                )
            seen_tool_names.add(tname)


def _tool_name(tool: Any) -> str | None:
    """Best-effort tool name from a dict tool or a ``Tool`` instance."""
    if isinstance(tool, dict):
        return tool.get("name")
    return getattr(tool, "name", None)
