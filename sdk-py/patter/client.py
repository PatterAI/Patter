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
        stt=DeepgramSTT(api_key="dg_..."),
        tts=ElevenLabsTTS(api_key="el_..."),
        on_message=handler,
    )

  Local (fully embedded, no Patter backend):
    phone = Patter(
        carrier=Twilio(account_sid="AC...", auth_token="..."),
        phone_number="+1...",
        tunnel=Static(hostname="abc.ngrok.io"),
    )
    agent = phone.agent(engine=OpenAIRealtime(), system_prompt="hi")
    await phone.serve(agent, port=8000)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Awaitable

import httpx

logger = logging.getLogger("patter")

from patter.connection import PatterConnection
from patter.exceptions import PatterConnectionError, ProvisionError
from patter.models import Agent, Guardrail, IncomingMessage, STTConfig, TTSConfig
from patter.local_config import LocalConfig
from patter.providers.base import STTProvider, TTSProvider

if TYPE_CHECKING:  # pragma: no cover — typing only
    from patter.carriers.twilio import Carrier as TwilioCarrier
    from patter.carriers.telnyx import Carrier as TelnyxCarrier
    from patter.engines.openai import Realtime as OpenAIRealtimeEngine
    from patter.engines.elevenlabs import ConvAI as ElevenLabsConvAIEngine
    from patter.tunnels import CloudflareTunnel, Ngrok, Static
    from patter._public_api import Tool

DEFAULT_BACKEND_URL = "wss://api.getpatter.com"
DEFAULT_REST_URL = "https://api.getpatter.com"


class Patter:
    """Main Patter SDK client.

    Cloud mode::

        phone = Patter(api_key="pt_xxx")

    Local (embedded) mode::

        phone = Patter(
            carrier=Twilio(account_sid="AC...", auth_token="..."),
            phone_number="+1...",
            tunnel=Static(hostname="abc.ngrok.io"),
        )

    Args:
        api_key: Your Patter API key (starts with ``pt_``). Required for cloud mode.
        backend_url: WebSocket URL for the Patter backend (cloud/self-hosted).
        rest_url: REST API URL for the Patter backend (cloud/self-hosted).
        mode: ``"cloud"`` (default) or ``"local"``.  Auto-detected when
            ``carrier`` is supplied without ``api_key``.
        carrier: ``Twilio(...)`` or ``Telnyx(...)`` instance (local mode).
        phone_number: Your phone number in E.164 format (local mode).
        webhook_url: Public hostname (no scheme) of this server, e.g.
            ``"abc.ngrok.io"`` (local mode). Mutually exclusive with ``tunnel``.
        tunnel: ``CloudflareTunnel()``, ``Ngrok(hostname=...)``, ``Static(hostname=...)``,
            or ``True`` (alias for ``CloudflareTunnel()``). Used to expose the
            embedded server publicly (local mode).
        pricing: Optional pricing overrides for cost tracking (local mode).
    """

    def __init__(
        self,
        # Cloud mode
        api_key: str = "",
        backend_url: str = DEFAULT_BACKEND_URL,
        rest_url: str = DEFAULT_REST_URL,
        # Mode override
        mode: str = "cloud",
        # Local mode — instance-based API
        carrier: Any = None,
        phone_number: str = "",
        webhook_url: str = "",
        tunnel: Any = None,
        # Cost tracking
        pricing: dict | None = None,
    ) -> None:
        self._mode = mode
        self._pricing = pricing

        # --- Carrier normalisation ---
        carrier_kind, carrier_creds = self._unpack_carrier(carrier)

        # --- Tunnel directive → webhook_url override ---
        tunnel_directive, tunnel_webhook = self._unpack_tunnel(tunnel)
        if tunnel_directive is not None and tunnel_webhook:
            if webhook_url and webhook_url != tunnel_webhook:
                raise ValueError(
                    "Patter() received a tunnel=Static(...)/Ngrok(hostname=...) "
                    "and a conflicting webhook_url. Provide only one."
                )
            webhook_url = tunnel_webhook
        self._tunnel_directive = tunnel_directive

        # --- Mode detection ---
        # Local mode is selected when either mode="local" is explicit OR a
        # carrier instance is provided without an api_key.
        if mode == "local" or (carrier_kind is not None and not api_key):
            self._mode = "local"

            twilio_sid = ""
            twilio_token = ""
            telnyx_key = ""
            telnyx_connection_id = ""
            telnyx_public_key = ""

            if carrier_kind == "twilio":
                twilio_sid = carrier_creds["account_sid"]
                twilio_token = carrier_creds["auth_token"]
            elif carrier_kind == "telnyx":
                telnyx_key = carrier_creds["api_key"]
                telnyx_connection_id = carrier_creds["connection_id"]
                telnyx_public_key = carrier_creds.get("public_key", "")

            # --- Local mode validation (only when a carrier is provided) ---
            if carrier_kind is not None:
                if not phone_number:
                    raise ValueError(
                        "Local mode requires phone_number (e.g., phone_number='+15550001234')."
                    )

            self._local_config = LocalConfig(
                telephony_provider=carrier_kind or "twilio",
                twilio_sid=twilio_sid,
                twilio_token=twilio_token,
                telnyx_key=telnyx_key,
                telnyx_connection_id=telnyx_connection_id,
                telnyx_public_key=telnyx_public_key,
                phone_number=phone_number,
                webhook_url=webhook_url,
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

    @staticmethod
    def _unpack_carrier(carrier: Any) -> tuple[str | None, dict]:
        """Convert a ``Twilio(...)``/``Telnyx(...)`` instance to kind + creds.

        Returns ``(None, {})`` when *carrier* is ``None``. Raises
        :class:`TypeError` if the argument does not expose a ``.kind`` attribute
        matching one of the supported carriers.
        """
        if carrier is None:
            return None, {}
        # Import lazily to keep the module import graph flat.
        from patter.carriers.telnyx import Carrier as _Telnyx
        from patter.carriers.twilio import Carrier as _Twilio

        if isinstance(carrier, _Twilio):
            return "twilio", {
                "account_sid": carrier.account_sid,
                "auth_token": carrier.auth_token,
            }
        if isinstance(carrier, _Telnyx):
            return "telnyx", {
                "api_key": carrier.api_key,
                "connection_id": carrier.connection_id,
                "public_key": carrier.public_key,
            }
        raise TypeError(
            "carrier= must be a Twilio(...) or Telnyx(...) instance, got "
            f"{type(carrier).__name__}"
        )

    @staticmethod
    def _unpack_tunnel(tunnel: Any) -> tuple[Any, str]:
        """Resolve the tunnel directive.

        Returns ``(directive, webhook_url)`` where *directive* is the raw object
        to keep around (used later by :meth:`serve`) and *webhook_url* is the
        host to feed into :class:`LocalConfig` right now — empty when the
        tunnel must be auto-started at ``serve()`` time.
        """
        if tunnel is None:
            return None, ""
        # Legacy shorthand: ``tunnel=True`` == ``tunnel=CloudflareTunnel()``.
        if isinstance(tunnel, bool):
            if not tunnel:
                return None, ""
            from patter.tunnels import CloudflareTunnel

            return CloudflareTunnel(), ""

        from patter.tunnels import CloudflareTunnel, Ngrok, Static

        if isinstance(tunnel, CloudflareTunnel):
            return tunnel, ""
        if isinstance(tunnel, Static):
            return tunnel, tunnel.hostname
        if isinstance(tunnel, Ngrok):
            if not tunnel.hostname:
                raise NotImplementedError(
                    "Ngrok() with no hostname is not yet supported — programmatic "
                    "ngrok launch is planned for a future release. For now, run "
                    "ngrok yourself and pass tunnel=Static(hostname='abc.ngrok.io') "
                    "or tunnel=Ngrok(hostname='abc.ngrok.io')."
                )
            return tunnel, tunnel.hostname
        raise TypeError(
            "tunnel= must be a CloudflareTunnel(), Ngrok(...), Static(...) "
            f"instance, or bool, got {type(tunnel).__name__}"
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
        ring_timeout: int | None = None,
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
                if ring_timeout is not None:
                    extra_params["Timeout"] = int(ring_timeout)
                # Status callback so the dashboard sees ringing/failed/
                # no-answer transitions before any media webhook fires.
                extra_params.setdefault(
                    "StatusCallback",
                    f"https://{config.webhook_url}/webhooks/twilio/status",
                )
                extra_params.setdefault("StatusCallbackMethod", "POST")
                extra_params.setdefault(
                    "StatusCallbackEvent",
                    "initiated ringing answered completed",
                )
                call_id = await adapter.initiate_call(
                    config.phone_number or from_number,
                    to,
                    stream_url,
                    extra_params=extra_params,
                )
                logger.info("Outbound call initiated: %s", call_id)
                # Pre-register the call so the dashboard surfaces attempts
                # that never reach media (no-answer, busy, carrier-reject).
                if self._server is not None and getattr(self._server, "_metrics_store", None) is not None:
                    try:
                        self._server._metrics_store.record_call_initiated({
                            "call_id": call_id,
                            "caller": config.phone_number or from_number,
                            "callee": to,
                            "direction": "outbound",
                        })
                    except Exception as exc:
                        logger.debug("record_call_initiated: %s", exc)
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
                    ring_timeout=ring_timeout,
                )
                logger.info("Outbound call initiated: %s", call_id)
                if self._server is not None and getattr(self._server, "_metrics_store", None) is not None:
                    try:
                        self._server._metrics_store.record_call_initiated({
                            "call_id": call_id,
                            "caller": config.phone_number or from_number,
                            "callee": to,
                            "direction": "outbound",
                        })
                    except Exception as exc:
                        logger.debug("record_call_initiated: %s", exc)
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

    @staticmethod
    def _resolve_stt(stt: Any) -> STTProvider | None:
        """Validate that *stt* is an ``STTProvider`` instance or ``None``."""
        if stt is None:
            return None
        if isinstance(stt, STTProvider):
            return stt
        raise TypeError(
            "stt must be an STTProvider instance (e.g. DeepgramSTT(api_key=...)) "
            f"or None; got {type(stt).__name__}"
        )

    @staticmethod
    def _resolve_tts(tts: Any) -> TTSProvider | None:
        """Validate that *tts* is a ``TTSProvider`` instance or ``None``."""
        if tts is None:
            return None
        if isinstance(tts, TTSProvider):
            return tts
        raise TypeError(
            "tts must be a TTSProvider instance (e.g. ElevenLabsTTS(api_key=...)) "
            f"or None; got {type(tts).__name__}"
        )

    def agent(
        self,
        system_prompt: str,
        voice: str = "alloy",
        model: str = "gpt-4o-mini-realtime-preview",
        language: str = "en",
        first_message: str = "",
        tools: "list[Tool] | None" = None,
        stt: STTProvider | None = None,
        tts: TTSProvider | None = None,
        variables: dict | None = None,
        guardrails: "list[Guardrail] | None" = None,
        hooks: "PipelineHooks | None" = None,
        text_transforms: "list[Callable] | None" = None,
        vad: "VADProvider | None" = None,
        audio_filter: "AudioFilter | None" = None,
        background_audio: "BackgroundAudioPlayer | None" = None,
        barge_in_threshold_ms: int = 300,
        engine: Any = None,
    ) -> Agent:
        """Create an ``Agent`` configuration for local mode.

        The AI provider mode is derived from the arguments:

        * ``engine=OpenAIRealtime(...)`` → OpenAI Realtime API.
        * ``engine=ElevenLabsConvAI(...)`` → ElevenLabs Conversational AI.
        * No ``engine`` + ``stt``/``tts`` set → pipeline mode (STT + LLM + TTS).
        * No ``engine`` and no ``stt``/``tts`` → defaults to OpenAI Realtime (the
          server will look up the OpenAI credentials from the engine or env).

        Args:
            system_prompt: Instructions for the AI agent.
            voice: TTS voice name (e.g. ``"alloy"``, ``"echo"``).
            model: OpenAI Realtime model ID.
            language: BCP-47 language code, e.g. ``"en"``.
            first_message: If set, the agent speaks this immediately on connect.
            tools: List of ``Tool`` instances (build with the ``tool()`` factory).
            stt: ``STTProvider`` instance for pipeline mode (e.g.
                ``DeepgramSTT(api_key=...)``).
            tts: ``TTSProvider`` instance for pipeline mode (e.g.
                ``ElevenLabsTTS(api_key=...)``).
            variables: Dict of ``{placeholder: value}`` pairs substituted into
                ``system_prompt`` at call start.
            guardrails: List of ``Guardrail`` instances (build with the
                ``guardrail()`` factory). Responses matching a guardrail are
                replaced before TTS.
            engine: ``OpenAIRealtime(...)`` or ``ElevenLabsConvAI(...)``.
        """
        # --- Engine dispatch ---
        openai_engine_key: str = ""
        elevenlabs_engine_key: str = ""
        if engine is not None:
            engine_kind, engine_fields = self._unpack_engine(engine)
            provider = engine_kind
            # Engine-supplied voice/model win over the method defaults, but we
            # let any *explicit* voice=/model= kwarg pass through unchanged —
            # users sometimes pass the engine AND a specific voice.
            if voice == "alloy" and engine_fields.get("voice"):
                voice = engine_fields["voice"]
            if model == "gpt-4o-mini-realtime-preview" and engine_fields.get("model"):
                model = engine_fields["model"]
            if engine_kind == "openai_realtime":
                openai_engine_key = engine_fields.get("api_key", "")
            elif engine_kind == "elevenlabs_convai":
                elevenlabs_engine_key = engine_fields.get("api_key", "")
        elif stt is not None or tts is not None:
            provider = "pipeline"
        else:
            provider = "openai_realtime"

        # Validate instance types for stt/tts and drop legacy forms.
        stt_resolved = self._resolve_stt(stt)
        tts_resolved = self._resolve_tts(tts)

        # In local mode we backfill any credentials the engine carries into
        # LocalConfig so downstream validation / dispatch sees them even when
        # the user didn't also set them on the Patter() constructor.
        if self._mode == "local":
            from dataclasses import replace

            if openai_engine_key and not self._local_config.openai_key:
                self._local_config = replace(
                    self._local_config, openai_key=openai_engine_key
                )
            if elevenlabs_engine_key and not self._local_config.elevenlabs_key:
                self._local_config = replace(
                    self._local_config, elevenlabs_key=elevenlabs_engine_key
                )

            if provider == "openai_realtime" and not self._local_config.openai_key:
                raise ValueError(
                    "OpenAI Realtime mode requires an OpenAI API key. Pass "
                    "engine=OpenAIRealtime(api_key='sk-...') or set OPENAI_API_KEY "
                    "in the environment."
                )

            if provider == "pipeline":
                if stt_resolved is None:
                    raise ValueError(
                        "Pipeline mode requires an STT provider instance. "
                        "Pass stt=DeepgramSTT(api_key='...') (or another supported "
                        "STTProvider) to agent()."
                    )
                # TTS may be omitted when the user supplies an on_message handler
                # that returns pre-synthesised audio, but most users will need it.
                # We no longer hard-require a TTS key on the Patter() constructor
                # because the TTS instance carries its own credentials.

        # --- Normalise tools ---
        tools_out: list[dict] | None = None
        if tools is not None:
            if not isinstance(tools, list):
                raise TypeError(
                    f"tools must be a list, got {type(tools).__name__}."
                )
            tools_out = [self._tool_to_dict(t, index=i) for i, t in enumerate(tools)]

        if variables is not None and not isinstance(variables, dict):
            raise TypeError(
                f"variables must be a dict, got {type(variables).__name__}."
            )

        # --- Normalise guardrails ---
        guardrails_out: list[dict] | None = None
        if guardrails is not None:
            if not isinstance(guardrails, list):
                raise TypeError(
                    f"guardrails must be a list, got {type(guardrails).__name__}."
                )
            guardrails_out = [
                self._guardrail_to_dict(g, index=i) for i, g in enumerate(guardrails)
            ]

        return Agent(
            system_prompt=system_prompt,
            voice=voice,
            model=model,
            language=language,
            first_message=first_message,
            tools=tools_out,
            provider=provider,
            stt=stt_resolved,
            tts=tts_resolved,
            variables=variables,
            guardrails=guardrails_out,
            hooks=hooks,
            text_transforms=text_transforms,
            vad=vad,
            audio_filter=audio_filter,
            background_audio=background_audio,
            barge_in_threshold_ms=barge_in_threshold_ms,
        )

    @staticmethod
    def _unpack_engine(engine: Any) -> tuple[str, dict]:
        """Convert an engine instance to ``(kind, {voice, model, api_key, agent_id})``."""
        from patter.engines.elevenlabs import ConvAI as _ConvAI
        from patter.engines.openai import Realtime as _Realtime

        if isinstance(engine, _Realtime):
            return "openai_realtime", {
                "api_key": engine.api_key,
                "voice": engine.voice,
                "model": engine.model,
            }
        if isinstance(engine, _ConvAI):
            return "elevenlabs_convai", {
                "api_key": engine.api_key,
                "agent_id": engine.agent_id,
                "voice": engine.voice,
            }
        raise TypeError(
            "engine= must be an OpenAIRealtime(...) or ElevenLabsConvAI(...) "
            f"instance, got {type(engine).__name__}"
        )

    @staticmethod
    def _tool_to_dict(tool: Any, *, index: int) -> dict:
        """Normalise a ``Tool`` instance into the internal dict shape.

        Raises ``TypeError`` if *tool* is not a ``Tool`` instance — the legacy
        raw-dict form was removed in v0.5.0.
        """
        from patter._public_api import Tool as _Tool

        if not isinstance(tool, _Tool):
            raise TypeError(
                f"tools[{index}] must be a Tool instance (build with "
                f"patter.tool(...)), got {type(tool).__name__}."
            )
        out: dict = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters
            if tool.parameters is not None
            else {"type": "object", "properties": {}},
        }
        if tool.handler is not None:
            out["handler"] = tool.handler
        if tool.webhook_url:
            out["webhook_url"] = tool.webhook_url
        return out

    @staticmethod
    def _guardrail_to_dict(guardrail: Any, *, index: int) -> dict:
        """Normalise a ``Guardrail`` instance into the internal dict shape.

        Raises ``TypeError`` if *guardrail* is not a ``Guardrail`` instance —
        the legacy raw-dict form was removed in v0.5.0.
        """
        if not isinstance(guardrail, Guardrail):
            raise TypeError(
                f"guardrails[{index}] must be a Guardrail instance (build with "
                f"patter.guardrail(...)), got {type(guardrail).__name__}."
            )
        return {
            "name": guardrail.name,
            "blocked_terms": guardrail.blocked_terms,
            "check": guardrail.check,
            "replacement": guardrail.replacement,
        }

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
                "Initialise Patter with carrier=Twilio(...) / carrier=Telnyx(...) instead of api_key."
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

        # If Patter(tunnel=CloudflareTunnel()) was passed, route through the
        # same cloudflared auto-start path as ``serve(tunnel=True)``.
        from patter.tunnels import CloudflareTunnel as _CFT

        if isinstance(self._tunnel_directive, _CFT) and not tunnel:
            tunnel = True

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
