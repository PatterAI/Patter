"""Interactive terminal test mode for voice agents.

Simulates a phone call without telephony, STT, or TTS — pure text
input/output in the terminal.  Useful for rapid agent development.

Usage::

    phone = Patter(mode="local", phone_number="+15550001234")
    agent = phone.agent(
        system_prompt="You are helpful.",
        stt=DeepgramSTT(api_key="..."),
        tts=ElevenLabsTTS(api_key="..."),
    )
    await phone.test(agent)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid

logger = logging.getLogger("getpatter")


class TestSession:
    """Interactive terminal test session for an agent."""

    async def run(
        self,
        agent,
        openai_key: str = "",
        on_message=None,
        on_call_start=None,
        on_call_end=None,
    ) -> None:
        """Start an interactive REPL simulating a phone call.

        Args:
            agent: An ``Agent`` instance to test.
            openai_key: OpenAI API key (needed when no ``on_message`` is
                provided and the built-in LLM loop should be used).
            on_message: Optional message handler (same as ``serve()``).
            on_call_start: Optional call start handler.
            on_call_end: Optional call end handler.
        """
        call_id = f"test_{uuid.uuid4().hex[:12]}"
        caller = "+15550000001"
        callee = "+15550000002"
        conversation_history: list[dict] = []

        print()
        print("=" * 60)
        print("  PATTER TEST MODE")
        print("=" * 60)
        print(f"  Agent: {agent.model} / {agent.voice}")
        print(f"  Provider: {agent.provider}")
        print(f"  Call ID: {call_id}")
        print(f"  Caller: {caller}  →  Callee: {callee}")
        print("-" * 60)
        print("  Commands: /quit  /transfer <number>  /hangup  /history")
        print("=" * 60)
        print()

        # Fire on_call_start
        if on_call_start:
            result = await on_call_start(
                {
                    "call_id": call_id,
                    "caller": caller,
                    "callee": callee,
                    "direction": "test",
                }
            )
            if isinstance(result, dict):
                logger.info("on_call_start returned overrides: %s", list(result.keys()))

        # Play first message
        if agent.first_message:
            print(f"  Agent: {agent.first_message}")
            print()
            conversation_history.append(
                {
                    "role": "assistant",
                    "text": agent.first_message,
                    "timestamp": time.time(),
                }
            )

        # Set up LLM loop if no on_message and openai_key is available
        llm_loop = None
        if on_message is None and openai_key:
            from getpatter.services.llm_loop import LLMLoop
            from getpatter.tools.tool_executor import ToolExecutor

            tool_executor = ToolExecutor() if agent.tools else None
            llm_model = agent.model
            if "realtime" in llm_model:
                llm_model = "gpt-4o-mini"

            # Resolve variables in system prompt
            resolved_prompt = agent.system_prompt
            if agent.variables:
                for k, v in agent.variables.items():
                    resolved_prompt = resolved_prompt.replace(f"{{{k}}}", str(v))

            llm_loop = LLMLoop(
                openai_key=openai_key,
                model=llm_model,
                system_prompt=resolved_prompt,
                tools=agent.tools,
                tool_executor=tool_executor,
                disable_phone_preamble=getattr(agent, "disable_phone_preamble", False),
            )

        # Set up CallControl
        from getpatter.models import CallControl

        _transferred = False
        _hung_up = False

        async def _test_transfer(number):
            nonlocal _transferred
            _transferred = True
            print(f"  [Transfer → {number}]")

        async def _test_hangup():
            nonlocal _hung_up
            _hung_up = True
            print("  [Call ended by agent]")

        call_control = CallControl(
            call_id=call_id,
            caller=caller,
            callee=callee,
            telephony_provider="test",
            _transfer_fn=_test_transfer,
            _hangup_fn=_test_hangup,
        )

        # Check if on_message accepts CallControl
        msg_accepts_call = False
        if on_message is not None and callable(on_message):
            try:
                sig = inspect.signature(on_message)
                msg_accepts_call = len(sig.parameters) >= 2
            except (ValueError, TypeError):
                pass

        # REPL loop
        loop = asyncio.get_running_loop()
        while True:
            try:
                user_input = await loop.run_in_executor(None, input, "  You: ")
            except (EOFError, KeyboardInterrupt):
                print("\n  [Session ended]")
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            # Handle commands
            if user_input == "/quit":
                print("  [Session ended]")
                break
            elif user_input == "/hangup":
                print("  [You hung up]")
                break
            elif user_input.startswith("/transfer "):
                number = user_input[10:].strip()
                print(f"  [Transfer → {number}]")
                break
            elif user_input == "/history":
                for entry in conversation_history:
                    role = entry["role"].capitalize()
                    print(f"    {role}: {entry['text']}")
                continue

            conversation_history.append(
                {
                    "role": "user",
                    "text": user_input,
                    "timestamp": time.time(),
                }
            )

            # Get response
            if on_message is not None and callable(on_message):
                msg_data = {
                    "text": user_input,
                    "call_id": call_id,
                    "caller": caller,
                    "history": list(conversation_history),
                }
                if msg_accepts_call:
                    result = on_message(msg_data, call_control)
                else:
                    result = on_message(msg_data)

                if asyncio.iscoroutine(result):
                    response_text = await result
                elif inspect.isasyncgen(result):
                    parts = []
                    async for token in result:
                        parts.append(token)
                        print(token, end="", flush=True)
                    print()
                    response_text = "".join(parts)
                else:
                    response_text = result

            elif llm_loop is not None:
                call_ctx = {
                    "call_id": call_id,
                    "caller": caller,
                    "callee": callee,
                }
                parts = []
                print("  Agent: ", end="", flush=True)
                async for token in llm_loop.run(
                    user_input, conversation_history, call_ctx
                ):
                    parts.append(token)
                    print(token, end="", flush=True)
                print()
                response_text = "".join(parts)
            else:
                print("  [No on_message handler or LLM loop configured]")
                continue

            if call_control.ended:
                break

            if response_text:
                if on_message is not None and callable(on_message):
                    print(f"  Agent: {response_text}")
                conversation_history.append(
                    {
                        "role": "assistant",
                        "text": response_text,
                        "timestamp": time.time(),
                    }
                )
                print()

        # Fire on_call_end
        if on_call_end:
            await on_call_end(
                {
                    "call_id": call_id,
                    "caller": caller,
                    "callee": callee,
                    "direction": "test",
                    "transcript": conversation_history,
                }
            )
