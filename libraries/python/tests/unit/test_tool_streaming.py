"""Unit tests for async-generator handler support (#5 streaming).

Parity with libraries/typescript/tests/tool-streaming.test.ts.
"""

from __future__ import annotations

import json

import pytest

from getpatter.tools.tool_executor import ToolExecutor


class TestStreamingHandlers:
    @pytest.mark.asyncio
    async def test_forwards_progress_yields_and_returns_final_result(self) -> None:
        progress_updates: list[str] = []

        async def on_progress(text: str) -> None:
            progress_updates.append(text)

        async def streaming_search(_args: dict, _ctx: dict):
            yield {"progress": "Searching the database..."}
            yield {"progress": "Found 12 matches."}
            yield {"result": json.dumps({"count": 12, "items": ["a", "b", "c"]})}

        executor = ToolExecutor()
        result = await executor.execute(
            tool_name="streaming_search",
            arguments={},
            call_context={},
            handler=streaming_search,
            on_progress=on_progress,
        )

        assert progress_updates == [
            "Searching the database...",
            "Found 12 matches.",
        ]
        assert json.loads(result) == {"count": 12, "items": ["a", "b", "c"]}

    @pytest.mark.asyncio
    async def test_works_without_on_progress(self) -> None:
        async def streaming(_args: dict, _ctx: dict):
            yield {"progress": "this disappears"}
            yield {"result": "ok"}

        executor = ToolExecutor()
        result = await executor.execute(
            tool_name="no_sink",
            arguments={},
            call_context={},
            handler=streaming,
        )
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_plain_async_function_still_works(self) -> None:
        async def classic(_args: dict, _ctx: dict) -> str:
            return '"plain result"'

        executor = ToolExecutor()
        result = await executor.execute(
            tool_name="classic",
            arguments={},
            call_context={},
            handler=classic,
        )
        assert result == '"plain result"'

    @pytest.mark.asyncio
    async def test_generator_errors_are_retried(self) -> None:
        attempts = {"count": 0}

        async def flaky_generator(_args: dict, _ctx: dict):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("transient")
            yield {"progress": "ok now"}
            yield {"result": '"recovered"'}

        executor = ToolExecutor()
        # Override RETRY_DELAY to keep the test fast.
        executor.RETRY_DELAY = 0.001  # type: ignore[misc]
        result = await executor.execute(
            tool_name="flaky_generator",
            arguments={},
            call_context={},
            handler=flaky_generator,
        )
        assert result == '"recovered"'
        assert attempts["count"] == 3
