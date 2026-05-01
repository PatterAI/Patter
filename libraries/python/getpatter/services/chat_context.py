"""Typed conversation history management with truncation support.

Replaces raw ``list[dict]`` history with a structured ChatContext class
that provides immutable messages, automatic ID generation, truncation
preserving system prompts, and format conversion for OpenAI / Anthropic.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ChatRole = Literal["system", "user", "assistant", "tool"]


def _generate_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class ChatMessage:
    """An immutable chat message with auto-generated ID and timestamp."""

    id: str
    role: ChatRole
    content: str
    timestamp: float
    name: str | None = None
    tool_call_id: str | None = None


# ---------------------------------------------------------------------------
# ChatContext
# ---------------------------------------------------------------------------


class ChatContext:
    """Manages a conversation history with truncation and format conversion."""

    def __init__(self, system_prompt: str | None = None) -> None:
        self._items: list[ChatMessage] = []
        if system_prompt is not None:
            self._items.append(
                ChatMessage(
                    id=_generate_id(),
                    role="system",
                    content=system_prompt,
                    timestamp=time.time(),
                )
            )

    # ------------------------------------------------------------------
    # Add messages
    # ------------------------------------------------------------------

    def add_user(self, content: str) -> ChatMessage:
        msg = ChatMessage(
            id=_generate_id(),
            role="user",
            content=content,
            timestamp=time.time(),
        )
        self._items = [*self._items, msg]
        return msg

    def add_assistant(self, content: str) -> ChatMessage:
        msg = ChatMessage(
            id=_generate_id(),
            role="assistant",
            content=content,
            timestamp=time.time(),
        )
        self._items = [*self._items, msg]
        return msg

    def add_system(self, content: str) -> ChatMessage:
        msg = ChatMessage(
            id=_generate_id(),
            role="system",
            content=content,
            timestamp=time.time(),
        )
        self._items = [*self._items, msg]
        return msg

    def add_tool_result(self, content: str, tool_call_id: str) -> ChatMessage:
        msg = ChatMessage(
            id=_generate_id(),
            role="tool",
            content=content,
            timestamp=time.time(),
            tool_call_id=tool_call_id,
        )
        self._items = [*self._items, msg]
        return msg

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    def get_messages(self) -> tuple[ChatMessage, ...]:
        """Return all messages as an immutable tuple."""
        return tuple(self._items)

    def get_last_n(self, n: int) -> tuple[ChatMessage, ...]:
        """Return the last *n* messages."""
        if n <= 0:
            return ()
        return tuple(self._items[-n:])

    @property
    def length(self) -> int:
        return len(self._items)

    # ------------------------------------------------------------------
    # Truncation
    # ------------------------------------------------------------------

    def truncate(self, max_messages: int) -> None:
        """Keep the first system message (if any) plus the last *max_messages*.

        When no system message exists at index 0, simply keeps the last
        *max_messages* messages.
        """
        if max_messages < 0:
            return

        has_leading_system = len(self._items) > 0 and self._items[0].role == "system"

        if has_leading_system:
            system_msg = self._items[0]
            rest = self._items[1:]
            kept = rest[-max_messages:] if max_messages > 0 else []
            self._items = [system_msg, *kept]
        else:
            self._items = list(self._items[-max_messages:]) if max_messages > 0 else []

    # ------------------------------------------------------------------
    # Provider format conversion
    # ------------------------------------------------------------------

    def to_openai(self) -> list[dict[str, Any]]:
        """Convert to OpenAI chat completion message format."""
        results: list[dict[str, Any]] = []
        for msg in self._items:
            entry: dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.name is not None:
                entry["name"] = msg.name
            if msg.tool_call_id is not None:
                entry["tool_call_id"] = msg.tool_call_id
            results.append(entry)
        return results

    def to_anthropic(self) -> dict[str, Any]:
        """Convert to Anthropic format.

        The first system message is extracted into a separate ``system`` key;
        only user/assistant messages appear in the ``messages`` list.
        """
        system: str | None = None
        messages: list[dict[str, str]] = []

        for msg in self._items:
            if msg.role == "system":
                if system is None:
                    system = msg.content
                continue
            messages.append({"role": msg.role, "content": msg.content})

        return {"system": system, "messages": messages}

    # ------------------------------------------------------------------
    # Copy
    # ------------------------------------------------------------------

    def copy(self) -> ChatContext:
        """Create a deep copy of this context."""
        ctx = ChatContext()
        ctx._items = copy.deepcopy(self._items)
        return ctx

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "messages": [
                {
                    "id": msg.id,
                    "role": msg.role,
                    "content": msg.content,
                    "timestamp": msg.timestamp,
                    **({"name": msg.name} if msg.name is not None else {}),
                    **({"tool_call_id": msg.tool_call_id} if msg.tool_call_id is not None else {}),
                }
                for msg in self._items
            ]
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ChatContext:
        """Deserialize from a JSON-compatible dict."""
        ctx = cls()
        ctx._items = [
            ChatMessage(
                id=m["id"],
                role=m["role"],
                content=m["content"],
                timestamp=m["timestamp"],
                name=m.get("name"),
                tool_call_id=m.get("tool_call_id"),
            )
            for m in data.get("messages", [])
        ]
        return ctx
