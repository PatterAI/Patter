"""Pre-first-token cancel abort for agent-runtime LLM providers.

Hermes / OpenClaw run tools/memory/skills for tens of seconds BEFORE the first
SSE byte. The per-chunk ``cancel_event.is_set()`` check inside ``async for chunk
in response`` never runs during that window (the consumer is parked awaiting the
first byte), so a barge-in could not free the connection and the next user turn
blocked behind it. The provider now races ``create()`` + first-byte against the
cancel event and spawns a watchdog that ``close()``s the response the instant
the event fires, returning promptly without yielding.

Only the external boundary is mocked: a fake AsyncOpenAI client whose streaming
response parks on an event until ``close()`` is called.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.llm.openai_compatible import OpenAICompatibleLLMProvider


class _ParkingResponse:
    """Async-iterable streaming response that parks on the first ``__anext__``
    until ``close()`` is called — modelling Hermes holding the connection open
    while it runs tools before the first token."""

    def __init__(self) -> None:
        self._closed = asyncio.Event()
        self.close_calls = 0
        self.yielded_any = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._closed.wait()
        # A read after the httpx stream is closed raises — model that so the
        # provider's except-branch (swallow-when-cancelling) is exercised.
        raise RuntimeError("stream closed mid-read")

    async def close(self) -> None:
        self.close_calls += 1
        self._closed.set()


def _provider_with_fake_client(response: _ParkingResponse) -> OpenAICompatibleLLMProvider:
    provider = OpenAICompatibleLLMProvider(base_url="http://127.0.0.1:9/v1", model="m")
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=response)
    provider._client = fake_client  # type: ignore[assignment]
    return provider


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_cancel_event_closes_response_before_first_token() -> None:
    provider = _provider_with_fake_client(resp := _ParkingResponse())
    cancel = asyncio.Event()
    chunks: list = []

    async def _consume() -> None:
        async for chunk in provider.stream(
            [{"role": "user", "content": "hi"}], cancel_event=cancel
        ):
            chunks.append(chunk)

    task = asyncio.create_task(_consume())
    # Let it reach the parked first __anext__.
    await asyncio.sleep(0.05)
    assert chunks == []  # parked pre-first-token

    # Barge-in: the watchdog must close the response and stream() must return
    # promptly WITHOUT yielding or raising.
    cancel.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert resp.close_calls >= 1  # watchdog tore the request down
    assert chunks == []  # nothing spoken


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_cancel_during_create_aborts_in_flight_post() -> None:
    """If the cancel fires while ``create()`` itself is still awaiting (the
    server hasn't even sent headers), the in-flight POST is cancelled and
    stream() returns nothing — no response object, no yield."""
    provider = OpenAICompatibleLLMProvider(base_url="http://127.0.0.1:9/v1", model="m")
    cancel = asyncio.Event()
    create_started = asyncio.Event()

    async def _never_returns(**_kwargs):
        create_started.set()
        await asyncio.Event().wait()  # parks forever (server never responds)

    fake_client = MagicMock()
    fake_client.chat.completions.create = _never_returns
    provider._client = fake_client  # type: ignore[assignment]

    chunks: list = []

    async def _consume() -> None:
        async for chunk in provider.stream(
            [{"role": "user", "content": "hi"}], cancel_event=cancel
        ):
            chunks.append(chunk)

    task = asyncio.create_task(_consume())
    await asyncio.wait_for(create_started.wait(), timeout=1.0)
    cancel.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert chunks == []


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_task_cancel_aborts_in_flight_create_no_orphan() -> None:
    """When the containing dispatch task is hard-cancelled (cleanup / hangup)
    while parked pre-first-token, the in-flight create() POST must be aborted —
    not orphaned (which would later raise 'Task exception was never retrieved'
    and leak the Hermes/OpenClaw connection)."""
    provider = OpenAICompatibleLLMProvider(base_url="http://127.0.0.1:9/v1", model="m")
    cancel = asyncio.Event()
    create_started = asyncio.Event()
    create_cancelled = {"value": False}

    async def _never_returns(**_kwargs):
        create_started.set()
        try:
            await asyncio.Event().wait()  # parks (server running tools)
        except asyncio.CancelledError:
            create_cancelled["value"] = True
            raise

    fake_client = MagicMock()
    fake_client.chat.completions.create = _never_returns
    provider._client = fake_client  # type: ignore[assignment]

    async def _consume() -> None:
        async for _chunk in provider.stream(
            [{"role": "user", "content": "hi"}], cancel_event=cancel
        ):
            pass

    task = asyncio.create_task(_consume())
    await asyncio.wait_for(create_started.wait(), timeout=1.0)
    # Simulate cleanup() hard-cancelling _dispatch_task while parked pre-create.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)  # let the abort propagate
    assert create_cancelled["value"] is True


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_no_cancel_event_streams_normally() -> None:
    """Regression guard: with no cancel_event the watchdog is never spawned and
    a normal streamed response yields its text unchanged."""

    class _Chunk:
        def __init__(self, content):
            self.usage = None
            self.choices = [
                MagicMock(delta=MagicMock(content=content, tool_calls=None))
            ]

    class _OneShot:
        def __init__(self):
            self._items = [_Chunk("Hello "), _Chunk("there.")]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    provider = OpenAICompatibleLLMProvider(base_url="http://127.0.0.1:9/v1", model="m")
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=_OneShot())
    provider._client = fake_client  # type: ignore[assignment]

    texts = [
        c["content"]
        async for c in provider.stream([{"role": "user", "content": "hi"}])
        if c.get("type") == "text"
    ]
    assert texts == ["Hello ", "there."]
