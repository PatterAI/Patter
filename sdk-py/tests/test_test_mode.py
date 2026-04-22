"""Tests for the interactive terminal test mode."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.models import Agent
from getpatter.test_mode import TestSession


@pytest.mark.asyncio
async def test_session_with_on_message(monkeypatch):
    """Test session invokes on_message handler and prints response."""
    inputs = iter(["Hello", "/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    on_message = AsyncMock(return_value="Hi there!")
    agent = Agent(system_prompt="You are a test.", provider="pipeline")

    session = TestSession()
    await session.run(agent=agent, on_message=on_message)

    on_message.assert_called_once()
    call_data = on_message.call_args[0][0]
    assert call_data["text"] == "Hello"
    assert "call_id" in call_data


@pytest.mark.asyncio
async def test_session_with_call_control(monkeypatch):
    """Test session passes CallControl to 2-param on_message."""
    inputs = iter(["test", "/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    async def handler(data, call):
        assert hasattr(call, "transfer")
        assert hasattr(call, "hangup")
        return "Got it!"

    agent = Agent(system_prompt="You are a test.", provider="pipeline")
    session = TestSession()
    await session.run(agent=agent, on_message=handler)


@pytest.mark.asyncio
async def test_session_fires_lifecycle_callbacks(monkeypatch):
    """Test session calls on_call_start and on_call_end."""
    inputs = iter(["/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    on_start = AsyncMock(return_value=None)
    on_end = AsyncMock()
    agent = Agent(system_prompt="Test.", provider="pipeline")

    session = TestSession()
    await session.run(
        agent=agent,
        on_call_start=on_start,
        on_call_end=on_end,
    )

    on_start.assert_called_once()
    start_data = on_start.call_args[0][0]
    assert start_data["direction"] == "test"

    on_end.assert_called_once()
    end_data = on_end.call_args[0][0]
    assert "transcript" in end_data


@pytest.mark.asyncio
async def test_session_first_message(monkeypatch, capsys):
    """Test session prints first_message at start."""
    inputs = iter(["/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    agent = Agent(
        system_prompt="Test.",
        provider="pipeline",
        first_message="Welcome!",
    )

    session = TestSession()
    await session.run(agent=agent)

    captured = capsys.readouterr()
    assert "Welcome!" in captured.out


@pytest.mark.asyncio
async def test_session_history_command(monkeypatch, capsys):
    """Test /history command prints conversation history."""
    inputs = iter(["Hello", "/history", "/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    on_message = AsyncMock(return_value="Hi!")
    agent = Agent(system_prompt="Test.", provider="pipeline")

    session = TestSession()
    await session.run(agent=agent, on_message=on_message)

    captured = capsys.readouterr()
    assert "User: Hello" in captured.out
    assert "Assistant: Hi!" in captured.out


@pytest.mark.asyncio
async def test_session_eof_exits(monkeypatch, capsys):
    """Test session handles EOF gracefully."""
    def raise_eof(prompt):
        raise EOFError()

    monkeypatch.setattr("builtins.input", raise_eof)

    agent = Agent(system_prompt="Test.", provider="pipeline")
    session = TestSession()
    await session.run(agent=agent)

    captured = capsys.readouterr()
    assert "Session ended" in captured.out
