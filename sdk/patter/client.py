"""Patter SDK — Connect AI agents to phone numbers in 4 lines of code.

Three modes:

  Cloud (Patter-managed):
    phone = Patter(api_key="pt_xxx")
    await phone.connect(on_message=handler)
    await phone.call(to="+39...", first_message="Ciao!")

  Self-hosted (bring your own keys, Patter backend):
    phone = Patter(api_key="pt_xxx", backend_url="ws://localhost:8000")
    await phone.connect(
        provider="twilio", provider_key="AC...",
        number="+1...",
        stt=phone.deepgram(api_key="dg_..."),
        tts=phone.elevenlabs(api_key="el_..."),
        on_message=handler,
    )

  Local (fully embedded, no Patter backend):
    phone = Patter(twilio_sid="AC...", twilio_token="...", openai_key="sk-...",
                   phone_number="+1...", webhook_url="abc.ngrok.io")
    agent = phone.agent(system_prompt="You are a helpful assistant.")
    await phone.serve(agent, port=8000)
"""

from __future__ import annotations

import logging
from typing import Callable, Awaitable

import httpx

logger = logging.getLogger("patter")

from patter.connection import PatterConnection
from patter.exceptions import PatterConnectionError, ProvisionError
from patter.models import Agent, Guardrail, IncomingMessage, STTConfig, TTSConfig
from patter.local_config import LocalConfig
from patter.providers import (
    deepgram as _deepgram,
    whisper as _whisper,
    elevenlabs as _elevenlabs,
    openai_tts as _openai_tts,
)

DEFAULT_BACKEND_URL = "wss://api.getpatter.com"
DEFAULT_REST_URL = "https://api.getpatter.com"


class Patter:
    """Main Patter SDK client.

    Cloud mode::

        phone = Patter(api_key="pt_xxx")

    Local (embedded) mode::

        phone = Patter(twilio_sid="AC...", twilio_token="...",
                       openai_key="sk-...", phone_number="+1...",
                       webhook_url="abc.ngrok.io")

    Args:
        api_key: Your Patter API key (starts with ``pt_``). Required for cloud mode.
        backend_url: WebSocket URL for the Patter backend (cloud/self-hosted).
        rest_url: REST API URL for the Patter backend (cloud/self-hosted).
        mode: ``"cloud"`` (default) or ``"local"``.  Auto-detected when
            ``twilio_sid`` is supplied without ``api_key``.
        twilio_sid: Twilio Account SID (local mode).
        twilio_token: Twilio Auth Token (local mode).
        telnyx_key: Telnyx API key (local mode, Telnyx).
        telnyx_connection_id: Telnyx Call Control App ID (local mode, Telnyx).
        openai_key: OpenAI API key for the Realtime API (local mode).
        elevenlabs_key: ElevenLabs API key (local mode, optional pipeline).
        deepgram_key: Deepgram API key (local mode, optional pipeline).
        phone_number: Your phone number in E.164 format (local mode).
        webhook_url: Public hostname (no scheme) of this server, e.g.
            ``"abc.ngrok.io"`` (local mode).
    """

    def __init__(
        self,
        # Cloud mode
        api_key: str = "",
        backend_url: str = DEFAULT_BACKEND_URL,
        rest_url: str = DEFAULT_REST_URL,
        # Local mode
        mode: str = "cloud",
        twilio_sid: str = "",
        twilio_token: str = "",
        telnyx_key: str = "",
        telnyx_connection_id: str = "",
        openai_key: str = "",
        elevenlabs_key: str = "",
        deepgram_key: str = "",
        phone_number: str = "",
        webhook_url: str = "",
        # Cost tracking
        pricing: dict | None = None,
    ) -> None:
        self._mode = mode
        self._pricing = pricing

        if mode == "local" or (twilio_sid and not api_key) or (telnyx_key and not api_key):
            self._mode = "local"
            # --- Local mode validation (only when telephony keys are provided) ---
            # Auto-detected local mode (twilio_sid/telnyx_key supplied without api_key)
            # requires a complete configuration; explicit mode="local" with no keys is allowed
            # for testing or future providers.
            _has_telephony = bool(twilio_sid or telnyx_key)
            _auto_detected = (twilio_sid and not api_key) or (telnyx_key and not api_key)
            if _auto_detected or _has_telephony:
                if not phone_number:
                    raise ValueError(
                        "Local mode requires phone_number (e.g., phone_number='+15550001234')."
                    )
                if twilio_sid and not twilio_token:
                    raise ValueError(
                        "twilio_token is required when using twilio_sid."
                    )
            self._local_config = LocalConfig(
                telephony_provider="twilio" if twilio_sid else "telnyx",
                twilio_sid=twilio_sid,
                twilio_token=twilio_token,
                telnyx_key=telnyx_key,
                telnyx_connection_id=telnyx_connection_id,
                openai_key=openai_key,
                elevenlabs_key=elevenlabs_key,
                deepgram_key=deepgram_key,
                phone_number=phone_number,
                webhook_url=webhook_url,
            )
            # TODO: Remove beta warning when Telnyx is validated in production
            if telnyx_key:
                logger.warning(
                    "Telnyx support is in beta — tested locally but not yet "
                    "validated in production. If you encounter issues, please "
                    "report them at https://github.com/PatterAI/Patter/issues"
                )
            self._server = None
            self._tunnel_handle = None
            self._connection = None
            self._http = None
        else:
            # Cloud / self-hosted mode (existing behaviour)
            self._api_key = api_key
            self._backend_url = backend_url
            self._rest_url = rest_url
            if self._mode != "local" and backend_url.startswith("ws://"):
                from urllib.parse import urlparse
                host = urlparse(backend_url).hostname
                if host not in ("localhost", "127.0.0.1"):
                    logger.warning(
                        "Using unencrypted ws:// for non-localhost host '%s'. "
                        "Use wss:// to protect your API key in transit.", host
                    )
            self._connection = PatterConnection(api_key=api_key, backend_url=backend_url)
            self._http = httpx.AsyncClient(
                base_url=rest_url,
                headers={"X-API-Key": api_key},
                timeout=30.0,
            )

    @property
    def api_key(self) -> str:
        """Public read-only access to the API key (backward compatibility)."""
        return getattr(self, "_api_key", "")

    async def connect(
        self,
        on_message: Callable[[IncomingMessage], Awaitable[str]],
        on_call_start: Callable[[dict], Awaitable[None]] | None = None,
        on_call_end: Callable[[dict], Awaitable[None]] | None = None,
        *,
        # Self-hosted mode (optional — omit for managed mode)
        provider: str | None = None,
        provider_key: str | None = None,
        provider_secret: str | None = None,
        number: str | None = None,
        country: str = "US",
        stt: STTConfig | None = None,
        tts: TTSConfig | None = None,
    ) -> None:
        """Connect to Patter and start listening for calls.

        Managed mode (recommended):
            await phone.connect(on_message=handler)

        Self-hosted mode:
            await phone.connect(
                provider="twilio", provider_key="AC...",
                number="+1...", on_message=handler,
            )
        """
        if self._mode == "local":
            raise PatterConnectionError(
                "connect() is not available in local mode. Use serve() instead."
            )

        # Self-hosted: register number with provider credentials
        if provider and provider_key and number:
            await self._register_number(
                provider=provider,
                provider_key=provider_key,
                provider_secret=provider_secret,
                number=number,
                country=country,
                stt=stt,
                tts=tts,
            )

        await self._connection.connect(
            on_message=on_message,
            on_call_start=on_call_start,
            on_call_end=on_call_end,
        )

    async def call(
        self,
        to: str,
        on_message: Callable[[IncomingMessage], Awaitable[str]] | None = None,
        first_message: str = "",
        from_number: str = "",
        agent_id: str | None = None,
        agent: Agent | None = None,
        machine_detection: bool = False,
        on_machine: Callable[[dict], Awaitable[None]] | None = None,
        voicemail_message: str = "",
    ) -> None:
        """Make an outbound call.

        Cloud mode:
            await phone.call(to="+39123", on_message=handler, first_message="Ciao!")

        Local mode:
            await phone.call(to="+39123", agent=my_agent)

        Args:
            to: Phone number to call (E.164 format).
            on_message: Handler for conversation (cloud mode). If not connected,
                auto-connects.
            first_message: What the AI says when the callee answers (cloud mode).
            from_number: Number to call from. If empty, uses configured number.
            agent_id: Agent ID to use (cloud mode, optional).
            agent: ``Agent`` instance to use (local mode, required).
            voicemail_message: If set and AMD detects a machine, speak this message
                and hang up (local mode, requires machine_detection=True).
        """
        if self._mode == "local":
            if not agent:
                raise PatterConnectionError(
                    "Local mode call() requires the agent parameter."
                )
            if not isinstance(to, str) or not to.startswith("+"):
                raise ValueError(
                    f"'to' must be a phone number in E.164 format (e.g., '+1234567890'), got '{to}'."
                )
            # Store voicemail message on embedded server so AMD webhook can use it
            if voicemail_message and self._server is not None:
                self._server.voicemail_message = voicemail_message
            config = self._local_config
            if config.telephony_provider == "twilio":
                from patter.providers.twilio_adapter import TwilioAdapter  # type: ignore[import]

                adapter = TwilioAdapter(
                    account_sid=config.twilio_sid,
                    auth_token=config.twilio_token,
                )
                stream_url = (
                    f"wss://{config.webhook_url}/ws/stream/outbound"
                )
                extra_params: dict = {}
                if machine_detection:
                    extra_params["MachineDetection"] = "DetectMessageEnd"
                    extra_params["AsyncAmd"] = "true"
                    extra_params["AsyncAmdStatusCallback"] = (
                        f"https://{config.webhook_url}/webhooks/twilio/amd"
                    )
                call_id = await adapter.initiate_call(
                    config.phone_number or from_number,
                    to,
                    stream_url,
                    extra_params=extra_params,
                )
                logger.info("Outbound call initiated: %s", call_id)
            elif config.telephony_provider == "telnyx":
                from patter.providers.telnyx_adapter import TelnyxAdapter  # type: ignore[import]

                adapter = TelnyxAdapter(
                    api_key=config.telnyx_key,
                    connection_id=config.telnyx_connection_id,
                )
                stream_url = (
                    f"wss://{config.webhook_url}/ws/telnyx/stream/outbound"
                )
                call_id = await adapter.initiate_call(
                    config.phone_number or from_number,
                    to,
                    stream_url,
                )
                logger.info("Outbound call initiated: %s", call_id)
            return

        # Cloud mode
        if not self._connection.is_connected:
            if on_message:
                await self._connection.connect(on_message=on_message)
            else:
                raise PatterConnectionError(
                    "Not connected. Call connect() first or pass on_message."
                )

        await self._connection.request_call(
            from_number=from_number,
            to_number=to,
            first_message=first_message,
        )

    # === Local mode helpers ===

    def agent(
        self,
        system_prompt: str,
        voice: str = "alloy",
        model: str = "gpt-4o-mini-realtime-preview",
        language: str = "en",
        first_message: str = "",
        tools: list[dict] | None = None,
        provider: str = "openai_realtime",
        stt: STTConfig | None = None,
        tts: TTSConfig | None = None,
        variables: dict | None = None,
        guardrails: list | None = None,
    ) -> Agent:
        """Create an ``Agent`` configuration for local mode.

        Args:
            system_prompt: Instructions for the AI agent.
            voice: TTS voice name (e.g. ``"alloy"``, ``"echo"``).
            model: OpenAI Realtime model ID.
            language: BCP-47 language code, e.g. ``"en"``.
            first_message: If set, the agent speaks this immediately on connect.
            tools: List of tool dicts with ``name``, ``description``,
                ``parameters``, and optional ``webhook_url``.
            provider: AI provider mode — ``"openai_realtime"``,
                ``"elevenlabs_convai"``, or ``"pipeline"``.
            stt: STT provider config (pipeline mode). Use ``Patter.deepgram()``
                or ``Patter.whisper()``. Falls back to ``deepgram_key`` when
                omitted.
            tts: TTS provider config (pipeline mode). Use
                ``Patter.elevenlabs()`` or ``Patter.openai_tts()``. Falls back
                to ``elevenlabs_key`` when omitted.
            variables: Dict of ``{placeholder: value}`` pairs substituted into
                ``system_prompt`` at call start.
            guardrails: List of guardrail dicts created with
                ``Patter.guardrail()``.  Responses matching a guardrail are
                replaced before TTS.
        """
        _VALID_PROVIDERS = ("openai_realtime", "elevenlabs_convai", "pipeline")
        if provider not in _VALID_PROVIDERS:
            raise ValueError(
                f"provider must be one of {_VALID_PROVIDERS}, got '{provider}'."
            )

        if provider == "openai_realtime" and self._mode == "local":
            if not self._local_config.openai_key:
                raise ValueError(
                    "OpenAI Realtime mode requires openai_key in the Patter() constructor."
                )

        if provider == "pipeline" and self._mode == "local":
            if not stt and not self._local_config.deepgram_key:
                raise ValueError(
                    "Pipeline mode requires stt config (e.g., Patter.deepgram(api_key='...')) "
                    "or deepgram_key in the Patter() constructor."
                )
            if not tts and not self._local_config.elevenlabs_key:
                raise ValueError(
                    "Pipeline mode requires tts config (e.g., Patter.elevenlabs(api_key='...')) "
                    "or elevenlabs_key in the Patter() constructor."
                )

        if tools is not None:
            if not isinstance(tools, list):
                raise TypeError(
                    f"tools must be a list, got {type(tools).__name__}."
                )
            for i, tool in enumerate(tools):
                if not isinstance(tool, dict):
                    raise TypeError(
                        f"tools[{i}] must be a dict, got {type(tool).__name__}."
                    )
                if "name" not in tool:
                    raise ValueError(f"tools[{i}] missing required 'name' field.")
                if "webhook_url" not in tool and "handler" not in tool:
                    raise ValueError(
                        f"tools[{i}] requires either 'webhook_url' or 'handler'."
                    )

        if variables is not None and not isinstance(variables, dict):
            raise TypeError(
                f"variables must be a dict, got {type(variables).__name__}."
            )

        if guardrails is not None and not isinstance(guardrails, list):
            raise TypeError(
                f"guardrails must be a list, got {type(guardrails).__name__}."
            )

        return Agent(
            system_prompt=system_prompt,
            voice=voice,
            model=model,
            language=language,
            first_message=first_message,
            tools=tools,
            provider=provider,
            stt=stt,
            tts=tts,
            variables=variables,
            guardrails=guardrails,
        )

    async def serve(
        self,
        agent: Agent,
        port: int = 8000,
        recording: bool = False,
        on_call_start: Callable[[dict], Awaitable[None]] | None = None,
        on_call_end: Callable[[dict], Awaitable[None]] | None = None,
        on_transcript: Callable[[dict], Awaitable[None]] | None = None,
        on_message: Callable[[dict], Awaitable[str]] | str | None = None,
        voicemail_message: str = "",
        on_metrics: Callable[[dict], Awaitable[None]] | None = None,
        dashboard: bool = True,
        dashboard_token: str = "",
        tunnel: bool = False,
    ) -> None:
        """Start the embedded server for inbound calls (local mode only).

        This call blocks until the server is stopped.

        Args:
            agent: The ``Agent`` to use for all calls.
            port: TCP port to bind to (default 8000).
            on_call_start: Optional async callable(dict) — fires on call start.
            on_call_end: Optional async callable(dict) — fires on call end.
            on_transcript: Optional async callable(dict) — fires per utterance.
            on_message: Optional async callable(dict) -> str — called with the
                user's transcribed text in pipeline mode; the return value is
                synthesised to speech and played back to the caller.
            recording: When ``True``, record each call via the Twilio Recordings API.
            voicemail_message: If set, spoken as a voicemail message when AMD
                detects a machine (requires machine_detection=True on call()).
            dashboard: When ``True`` (default), serves a local metrics dashboard
                at ``http://localhost:{port}/dashboard``.
            dashboard_token: Optional bearer token for dashboard authentication.
                When set, all dashboard routes require this token.
            tunnel: When ``True``, start a cloudflared tunnel automatically.
                Requires ``cloudflared`` binary on PATH. Mutually exclusive
                with ``webhook_url``.
        """
        if self._mode != "local":
            raise PatterConnectionError(
                "serve() is only available in local mode. "
                "Initialise Patter with twilio_sid/telnyx_key instead of api_key."
            )

        if not isinstance(agent, Agent):
            raise TypeError(
                f"agent must be an Agent instance, got {type(agent).__name__}. "
                "Use phone.agent() to create one."
            )
        if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
            raise ValueError(
                f"port must be an integer between 1 and 65535, got {port!r}."
            )
        if not isinstance(recording, bool):
            raise TypeError(
                f"recording must be a bool, got {type(recording).__name__}."
            )

        # Resolve webhook_url: tunnel or explicit
        config = self._local_config

        if tunnel and config.webhook_url:
            raise ValueError(
                "Cannot use both tunnel=True and webhook_url. Pick one."
            )

        if tunnel:
            from patter.tunnel import start_tunnel

            handle = await start_tunnel(port)
            self._tunnel_handle = handle
            # Replace config with the tunnel hostname (frozen dataclass)
            from dataclasses import replace

            config = replace(config, webhook_url=handle.hostname)
            self._local_config = config

        if not config.webhook_url:
            raise ValueError(
                "No webhook_url configured. Either:\n"
                "  - Pass webhook_url in the Patter constructor\n"
                "  - Use tunnel=True in serve() to auto-create a tunnel"
            )

        from patter.server import EmbeddedServer

        self._server = EmbeddedServer(
            config=config,
            agent=agent,
            recording=recording,
            voicemail_message=voicemail_message,
            pricing=self._pricing,
            dashboard=dashboard,
            dashboard_token=dashboard_token,
        )
        self._server.on_call_start = on_call_start
        self._server.on_call_end = on_call_end
        self._server.on_transcript = on_transcript
        self._server.on_message = on_message
        self._server.on_metrics = on_metrics
        await self._server.start(port=port)

    async def test(
        self,
        agent: Agent,
        on_message: Callable[[dict], Awaitable[str]] | None = None,
        on_call_start: Callable[[dict], Awaitable[None]] | None = None,
        on_call_end: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        """Start an interactive terminal test session (local mode only).

        Simulates a phone call without telephony, STT, or TTS — pure
        text input/output.  When no ``on_message`` handler is provided and
        an ``openai_key`` is configured, the built-in LLM loop is used.

        Args:
            agent: The ``Agent`` to test.
            on_message: Optional message handler (same as ``serve()``).
            on_call_start: Optional call start callback.
            on_call_end: Optional call end callback.
        """
        if self._mode != "local":
            raise PatterConnectionError(
                "test() is only available in local mode."
            )
        if not isinstance(agent, Agent):
            raise TypeError(
                f"agent must be an Agent instance, got {type(agent).__name__}."
            )

        from patter.test_mode import TestSession

        session = TestSession()
        await session.run(
            agent=agent,
            openai_key=self._local_config.openai_key,
            on_message=on_message,
            on_call_start=on_call_start,
            on_call_end=on_call_end,
        )

    # === Agent Management ===

    async def create_agent(
        self,
        name: str,
        system_prompt: str,
        model: str = "gpt-4o-mini-realtime-preview",
        voice: str = "alloy",
        voice_provider: str = "openai",
        language: str = "en",
        first_message: str | None = None,
        tools: list[dict] | None = None,
    ) -> dict:
        """Create a voice AI agent."""
        if self._mode == "local":
            raise PatterConnectionError("This method is only available in cloud mode.")
        response = await self._http.post("/api/agents", json={
            "name": name, "system_prompt": system_prompt, "model": model,
            "voice": voice, "voice_provider": voice_provider, "language": language,
            "first_message": first_message, "tools": tools,
        })
        if response.status_code != 201:
            raise ProvisionError(f"Failed to create agent: {response.text}")
        return response.json()

    async def list_agents(self) -> list[dict]:
        """List all agents."""
        if self._mode == "local":
            raise PatterConnectionError("This method is only available in cloud mode.")
        response = await self._http.get("/api/agents")
        return response.json()

    async def update_agent(self, agent_id: str, **kwargs) -> dict:
        """Update an agent."""
        if self._mode == "local":
            raise PatterConnectionError("This method is only available in cloud mode.")
        response = await self._http.patch(f"/api/agents/{agent_id}", json=kwargs)
        if response.status_code != 200:
            raise ProvisionError(f"Failed to update agent: {response.text}")
        return response.json()

    async def delete_agent(self, agent_id: str) -> None:
        """Delete an agent."""
        if self._mode == "local":
            raise PatterConnectionError("This method is only available in cloud mode.")
        response = await self._http.delete(f"/api/agents/{agent_id}")
        if response.status_code != 204:
            raise ProvisionError(f"Failed to delete agent: {response.text}")

    # === Number Management ===

    async def buy_number(self, country: str = "US", provider: str = "twilio") -> dict:
        """Buy a phone number."""
        if self._mode == "local":
            raise PatterConnectionError("This method is only available in cloud mode.")
        response = await self._http.post("/api/numbers/buy", json={"country": country, "provider": provider})
        if response.status_code != 201:
            raise ProvisionError(f"Failed to buy number: {response.text}")
        return response.json()

    async def list_numbers(self) -> list[dict]:
        """List all phone numbers."""
        if self._mode == "local":
            raise PatterConnectionError("This method is only available in cloud mode.")
        response = await self._http.get("/api/numbers")
        return response.json()

    async def assign_agent(self, number_id: str, agent_id: str) -> dict:
        """Assign an agent to a phone number."""
        if self._mode == "local":
            raise PatterConnectionError("This method is only available in cloud mode.")
        response = await self._http.post(f"/api/phone-numbers/{number_id}/assign-agent", json={"agent_id": agent_id})
        if response.status_code != 200:
            raise ProvisionError(f"Failed to assign agent: {response.text}")
        return response.json()

    # === Call Management ===

    async def list_calls(self, limit: int = 50) -> list[dict]:
        """List recent calls."""
        if self._mode == "local":
            raise PatterConnectionError("This method is only available in cloud mode.")
        response = await self._http.get(f"/api/calls?limit={limit}")
        return response.json()

    async def get_call(self, call_id: str) -> dict:
        """Get call details with transcript."""
        if self._mode == "local":
            raise PatterConnectionError("This method is only available in cloud mode.")
        response = await self._http.get(f"/api/calls/{call_id}")
        if response.status_code != 200:
            raise ProvisionError(f"Call not found: {response.text}")
        return response.json()

    async def disconnect(self) -> None:
        """Disconnect from Patter."""
        if self._mode == "local":
            if self._server:
                await self._server.stop()
            if self._tunnel_handle:
                self._tunnel_handle.stop()
                self._tunnel_handle = None
            return
        await self._connection.disconnect()
        await self._http.aclose()

    # === Provider helpers (for self-hosted setup) ===

    @staticmethod
    def deepgram(api_key: str, language: str = "en") -> STTConfig:
        return _deepgram(api_key=api_key, language=language)

    @staticmethod
    def whisper(api_key: str, language: str = "en") -> STTConfig:
        return _whisper(api_key=api_key, language=language)

    @staticmethod
    def elevenlabs(api_key: str, voice: str = "rachel") -> TTSConfig:
        return _elevenlabs(api_key=api_key, voice=voice)

    @staticmethod
    def openai_tts(api_key: str, voice: str = "alloy") -> TTSConfig:
        return _openai_tts(api_key=api_key, voice=voice)

    @staticmethod
    def guardrail(
        name: str,
        blocked_terms: list[str] | None = None,
        check: Callable[[str], bool] | None = None,
        replacement: str = "I'm sorry, I can't respond to that.",
    ) -> dict:
        """Create an output guardrail dict for use with ``agent(guardrails=[...])``.

        Output guardrails intercept AI responses before they are sent to TTS.
        When a response is blocked, ``replacement`` is spoken instead.

        Args:
            name: Identifier used in log warnings when the guardrail fires.
            blocked_terms: List of words/phrases — any case-insensitive match
                blocks the response.
            check: Custom callable ``(text: str) -> bool`` that returns
                ``True`` when the response should be blocked.  Evaluated after
                ``blocked_terms``.
            replacement: What the agent says instead when a response is blocked.
                Defaults to ``"I'm sorry, I can't respond to that."``.

        Example::

            phone.agent(
                system_prompt="You are a customer service agent.",
                guardrails=[
                    Patter.guardrail(
                        name="No medical advice",
                        blocked_terms=["diagnosis", "prescription"],
                        replacement="Please consult a doctor.",
                    ),
                    Patter.guardrail(
                        name="Custom check",
                        check=lambda text: "competitor" in text.lower(),
                    ),
                ],
            )
        """
        return {
            "name": name,
            "blocked_terms": blocked_terms,
            "check": check,
            "replacement": replacement,
        }

    @staticmethod
    def tool(
        name: str,
        description: str = "",
        parameters: dict | None = None,
        handler: object = None,
        webhook_url: str = "",
    ) -> dict:
        """Create a tool dict for use with ``agent(tools=[...])``.

        Either *handler* (a Python callable) or *webhook_url* must be provided.

        Args:
            name: Tool name (visible to the LLM).
            description: What the tool does (visible to the LLM).
            parameters: JSON Schema for tool arguments.
            handler: Async or sync callable ``(arguments: dict, context: dict) -> str | dict``.
                Called directly in-process when the LLM invokes the tool.
            webhook_url: URL to POST to when the LLM invokes the tool.
                Mutually exclusive with *handler*.

        Example::

            phone.agent(
                system_prompt="You are a pizza bot.",
                provider="openai_realtime",
                tools=[
                    Patter.tool(
                        name="check_menu",
                        description="Check available menu items",
                        parameters={"type": "object", "properties": {}},
                        handler=check_menu_fn,
                    ),
                ],
            )
        """
        if not handler and not webhook_url:
            raise ValueError("tool() requires either handler or webhook_url.")
        t: dict = {"name": name, "description": description}
        if parameters:
            t["parameters"] = parameters
        else:
            t["parameters"] = {"type": "object", "properties": {}}
        if handler:
            t["handler"] = handler
        if webhook_url:
            t["webhook_url"] = webhook_url
        return t

    # === Internal ===

    async def _register_number(
        self,
        provider: str,
        provider_key: str,
        provider_secret: str | None,
        number: str,
        country: str,
        stt: STTConfig | None,
        tts: TTSConfig | None,
    ) -> None:
        if self._mode == "local":
            raise PatterConnectionError("This method is only available in cloud mode.")
        credentials: dict = {"api_key": provider_key}
        if provider_secret:
            credentials["api_secret"] = provider_secret
        response = await self._http.post(
            "/api/phone-numbers",
            json={
                "number": number,
                "provider": provider,
                "provider_credentials": credentials,
                "country": country,
                "stt_config": stt.to_dict() if stt else None,
                "tts_config": tts.to_dict() if tts else None,
            },
        )
        if response.status_code == 409:
            return
        if response.status_code != 201:
            raise ProvisionError(f"Failed to register number: {response.text}")
