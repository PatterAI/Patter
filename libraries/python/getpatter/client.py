"""Patter SDK — Connect AI agents to phone numbers in 4 lines of code.

Local mode (the only mode in this release):

    phone = Patter(
        carrier=Twilio(account_sid="AC...", auth_token="..."),
        phone_number="+15550001234",
        tunnel=Static(hostname="abc.ngrok.io"),
    )
    agent = phone.agent(engine=OpenAIRealtime(), system_prompt="hi")
    await phone.serve(agent, port=8000)

Patter Cloud (the hosted backend) is not yet available in this SDK release;
passing ``api_key=`` raises :class:`NotImplementedError`. Cloud mode will
return in a future release.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

logger = logging.getLogger("getpatter")

from getpatter.exceptions import PatterConnectionError
from getpatter.local_config import LocalConfig
from getpatter.models import (
    Agent,
    CallResult,
    ConsultConfig,
    Guardrail,
    MachineDetectionResult,
    PipelineHooks,
    RealtimeTurnDetection,
)
from getpatter.providers.base import (
    AudioFilter,
    BackgroundAudioPlayer,
    STTProvider,
    TTSProvider,
    VADProvider,
)
from getpatter.services.llm_loop import LLMProvider
from getpatter._public_api import Tool
from getpatter._speech_events import SpeechEventCallback


# Maximum concurrent entries in the prewarm-first-message cache. Bounds
# memory consumption when an outbound flood (or attacker-controlled
# ``Patter.call`` invocations) would otherwise pile up tens of MB of
# orphan TTS bytes that never evict because the carrier never fires
# ``start``. When the cap is reached, new prewarm spawns are refused
# (logged at WARN, call still proceeds with live TTS). See FIX #96 in
# the parity audit. Mirrors ``PREWARM_CACHE_MAX`` in TS client.
_PREWARM_CACHE_MAX = 200
# Extra grace window beyond ``ring_timeout`` after which a prewarmed
# entry that was never consumed is forcibly evicted. The TTS bill was
# paid; without TTL eviction a carrier that never fires ``start`` (e.g.
# on a never-completed dial that bypassed the status callback) would
# leak the bytes for the lifetime of the Patter instance.
_PREWARM_TTL_GRACE_S = 5.0

# Safety TTL after which a parked provider WebSocket whose carrier never
# fired ``start`` is force-closed. 30 s is a comfortable superset of
# typical ring + AMD windows (Twilio ~25 s, Telnyx ~25 s).
_PARKED_CONN_TTL_S = 30.0

# Wire format the OpenAI Realtime parked-connection should emit per carrier.
# Single source of truth — keeps the prewarm-parking path from sprinkling
# carrier-name checks. Carriers absent from this map fall back to ``pcm16``.
_CARRIER_REALTIME_AUDIO_FORMAT: dict[str, str] = {
    "twilio": "g711_ulaw",
    "plivo": "g711_ulaw",
    "telnyx": "pcm16",
}


_CLOUD_NOT_IMPLEMENTED_MSG = (
    "Patter Cloud is not yet available in this SDK release. Use local mode "
    "with a `carrier=` and `phone_number=`. Cloud mode will return in a "
    "future release."
)


def _resolve_persist_root(persist: bool | str | None) -> str | None:
    """Resolve the user-supplied ``persist`` option into a concrete
    filesystem path or ``None``. Layered precedence:

    - ``persist is False`` → ``None`` (force off, even if env var is set)
    - ``persist is True`` → platform default (``resolve_log_root("auto")``)
    - ``persist`` is a string → exactly that path (after ``~`` expansion)
    - ``persist is None`` → ``PATTER_LOG_DIR`` env var if set, else platform
      default (``resolve_log_root("auto")``). Changed from the prior
      opt-in behaviour on 2026-05-21: the dashboard's hydrate path
      requires on-disk records to survive process restarts, so persistence
      now defaults to ON. Set ``persist=False`` to keep the old
      ephemeral-RAM-only behaviour.
    """
    from getpatter.services.call_log import resolve_log_root

    if persist is False:
        return None
    if persist is True:
        result = resolve_log_root("auto")
        return str(result) if result is not None else None
    if isinstance(persist, str):
        result = resolve_log_root(persist)
        return str(result) if result is not None else None
    result = resolve_log_root()
    if result is not None:
        return str(result)
    # No explicit persist + no env var → fall back to platform default so
    # the dashboard hydrate path always has something to read.
    result = resolve_log_root("auto")
    return str(result) if result is not None else None


def _build_telemetry_client(flag: bool | None) -> Any:
    """Construct the anonymous telemetry client (always safe; no-op when disabled)."""
    from getpatter import __version__
    from getpatter.telemetry import TelemetryClient

    return TelemetryClient(sdk_version=__version__, flag=flag)


def _telemetry_tunnel_kind(directive: Any) -> str:
    """Coarse tunnel kind for telemetry: static | configured | none.

    Mirrors the TypeScript SDK exactly (Static -> static, any other tunnel ->
    configured, no tunnel -> none).
    """
    if directive is None:
        return "none"
    from getpatter.tunnels import Static

    return "static" if isinstance(directive, Static) else "configured"


def _telemetry_environment_dims() -> dict[str, Any]:
    """Anonymous deploy-shape + install-age dims for ``sdk_initialized``.

    Presence-only env/file probes (never the value of an env var) + the upgrade
    funnel. Wrapped so it can never break construction.
    """
    try:
        from getpatter import __version__
        from getpatter.telemetry import environment as _env
        from getpatter.telemetry.install_id import (
            days_since_install_bucket,
            previous_version,
        )

        dims: dict[str, Any] = {
            "invoked_by_agent": _env.invoked_by_agent(),
            "container": _env.in_container(),
            "serverless": _env.serverless(),
            "cloud": _env.cloud(),
            "package_manager": _env.package_manager(),
            "days_since_install_bucket": days_since_install_bucket(),
        }
        prev = previous_version(__version__)
        if prev:
            dims["previous_sdk_version"] = prev
        return dims
    except Exception:
        return {}


def _telemetry_engine_family(
    engine: Any, provider: str | None, stt: Any, tts: Any
) -> str:
    """Coarse engine family for telemetry: realtime | convai | pipeline.

    Mirrors the TypeScript ``telemetryEngineFamily`` exactly so the same logical
    agent yields the same value in both SDKs.
    """
    if engine is not None:
        name = type(engine).__name__.lower()
        return "convai" if "convai" in name else "realtime"
    if provider == "elevenlabs_convai":
        return "convai"
    if provider == "pipeline":
        return "pipeline"
    if provider == "openai_realtime":
        return "realtime"
    if stt is not None or tts is not None:
        return "pipeline"
    return "realtime"


def _telemetry_provider_family(family: str) -> str:
    """Coarse vendor family derived from the engine family: realtime -> openai,
    convai -> elevenlabs, pipeline -> other.

    Mirrors the TS ``telemetryProviderFamily``. The per-vendor pipeline provider
    is intentionally not introspected (would rely on minifiable class names).
    """
    if family == "realtime":
        return "openai"
    if family == "convai":
        return "elevenlabs"
    return "other"


def _telemetry_bucket_custom_tools(n: int) -> str:
    """Coarse bucket for the custom-tool count (never the names)."""
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    if n <= 3:
        return "2_3"
    if n <= 6:
        return "4_6"
    if n <= 12:
        return "7_12"
    return "13_plus"


def _telemetry_bucket_mcp(n: int) -> str:
    """Coarse bucket for the configured MCP-server count."""
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    if n <= 3:
        return "2_3"
    return "4_plus"


def _telemetry_integration(consult: Any, mcp_servers: Any) -> tuple[str, str, str]:
    """Return ``(integration, integration_kind, mcp_server_count_bucket)``.

    OpenClaw is detected from a consult target on a **discarded local copy** —
    only the boolean result leaves; the raw model / base_url strings (which can
    embed a customer's brand or endpoint) are never stored or emitted.
    """
    n_mcp = len(mcp_servers) if mcp_servers else 0
    if n_mcp > 0:
        return "mcp", "mcp", _telemetry_bucket_mcp(n_mcp)
    if consult is not None:
        is_openclaw = False
        oc = getattr(consult, "openai_compatible", None)
        if oc is not None:
            model = getattr(oc, "model", "") or ""
            base_url = getattr(oc, "base_url", "") or ""
            is_openclaw = model.startswith("openclaw/") or ":18789" in base_url
        return ("openclaw" if is_openclaw else "other"), "consult", "0"
    return "none", "none", "0"


def _close_parked_slot(slot: dict[str, Any]) -> None:
    """Close every parked socket inside a parked-connections slot.

    Each slot may hold provider-specific handles:

    - ``stt`` → ``(aiohttp.ClientSession, aiohttp.ClientWebSocketResponse)``
      (Cartesia STT pattern).
    - ``tts`` → :class:`ElevenLabsParkedWS` (or any object exposing ``.ws``).
    - ``openai_realtime`` → :class:`websockets.WebSocketClientProtocol`.

    Closes are scheduled fire-and-forget on the running loop because
    this helper may be invoked synchronously from waste-record or
    disconnect paths that do not own an awaitable scope.
    """
    for handle in slot.values():
        try:
            asyncio.create_task(_safe_close_handle(handle))
        except RuntimeError:
            # No running loop — best-effort sync close where supported.
            pass


async def _safe_close_handle(handle: Any) -> None:
    """Best-effort async close of a parked handle.

    Handles the three flavours used by the SDK:
      - tuple ``(session, ws)`` from Cartesia STT.
      - :class:`ElevenLabsParkedWS` (or any object with ``.ws``).
      - bare WebSocket / WebSocketClientProtocol.
    """
    try:
        if isinstance(handle, tuple) and len(handle) == 2:
            session, ws = handle
            try:
                await ws.close()
            except Exception:
                pass
            try:
                await session.close()
            except Exception:
                pass
            return
        ws = getattr(handle, "ws", None)
        if ws is not None:
            await ws.close()
            return
        # Bare websocket — may have a parked keepalive task attached by
        # the GA Realtime parker. Cancel it before closing so the loop
        # doesn't race the close handshake with another send().
        ka = getattr(handle, "_parked_keepalive_task", None)
        if ka is not None:
            try:
                ka.cancel()
            except Exception:
                pass
        await handle.close()
    except Exception:
        pass


class Patter:
    """Main Patter SDK client (local mode only).

    Construct with a carrier and phone number::

        phone = Patter(
            carrier=Twilio(account_sid="AC...", auth_token="..."),
            phone_number="+1...",
            tunnel=Static(hostname="abc.ngrok.io"),
        )

    Args:
        carrier: ``Twilio(...)`` or ``Telnyx(...)`` instance.
        phone_number: Your phone number in E.164 format.
        webhook_url: Public hostname (no scheme) of this server, e.g.
            ``"abc.ngrok.io"``. Mutually exclusive with ``tunnel``.
        tunnel: ``CloudflareTunnel()``, ``Ngrok(hostname=...)``,
            ``Static(hostname=...)``, or ``True`` (alias for
            ``CloudflareTunnel()``). Used to expose the embedded server
            publicly.
        pricing: Optional pricing overrides for cost tracking.
    """

    def __init__(
        self,
        carrier: Any = None,
        phone_number: str = "",
        webhook_url: str = "",
        tunnel: Any = None,
        pricing: dict | None = None,
        persist: bool | str | None = None,
        telemetry: bool | None = None,
        **kwargs: Any,
    ) -> None:
        # --- Reject cloud-mode kwargs explicitly ---
        if "api_key" in kwargs or "backend_url" in kwargs or "rest_url" in kwargs:
            raise NotImplementedError(_CLOUD_NOT_IMPLEMENTED_MSG)
        # ``mode="local"`` is the historical opt-in flag. Accept it silently
        # for backward compatibility; reject anything else.
        mode = kwargs.pop("mode", "local")
        if mode != "local":
            raise NotImplementedError(_CLOUD_NOT_IMPLEMENTED_MSG)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(
                f"Patter() got unexpected keyword argument(s): {unexpected}"
            )

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

        # Normalise webhook_url to a bare hostname: every consumer builds
        # ``https://{webhook_url}`` / ``wss://{webhook_url}`` URLs, so a
        # schemed value produced broken ``wss://https://...`` stream URLs and
        # permanent carrier 403s. Mirrors the TS client normalisation.
        if webhook_url:
            webhook_url = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", webhook_url)
            webhook_url = webhook_url.rstrip("/")

        twilio_sid = ""
        twilio_token = ""
        telnyx_key = ""
        telnyx_connection_id = ""
        telnyx_public_key = ""
        plivo_auth_id = ""
        plivo_auth_token = ""

        if carrier_kind == "twilio":
            twilio_sid = carrier_creds["account_sid"]
            twilio_token = carrier_creds["auth_token"]
        elif carrier_kind == "telnyx":
            telnyx_key = carrier_creds["api_key"]
            telnyx_connection_id = carrier_creds["connection_id"]
            telnyx_public_key = carrier_creds.get("public_key", "")
        elif carrier_kind == "plivo":
            plivo_auth_id = carrier_creds["auth_id"]
            plivo_auth_token = carrier_creds["auth_token"]

        # --- Anonymous usage telemetry (opt-out, default ON) ---
        # Constructed BEFORE the activation-blocker validation below so the
        # ``config_incomplete`` signal can be emitted on the failing path. Separate
        # from getpatter.observability (user-facing OTel). Fire-and-forget and
        # fail-safe: it can never block or break a call. Disable with
        # PATTER_TELEMETRY_DISABLED=1, DO_NOT_TRACK=1, or telemetry=False.
        self._telemetry = _build_telemetry_client(telemetry)
        self._telemetry_seen_engines: set[str] = set()
        self._telemetry_seen_agent_shapes: set[tuple] = set()
        # Activation-blocker signal is emitted at most once per instance.
        self._telemetry_config_incomplete_emitted = False

        # --- Local mode validation (only when a carrier is provided) ---
        if carrier_kind is not None:
            if not phone_number:
                self._record_config_incomplete("carrier_credentials")
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
            plivo_auth_id=plivo_auth_id,
            plivo_auth_token=plivo_auth_token,
            phone_number=phone_number,
            webhook_url=webhook_url,
            persist_root=_resolve_persist_root(persist),
        )

        # The telemetry client + dedup state were constructed above (before the
        # activation-blocker validation) so ``config_incomplete`` could be emitted
        # on the failing path. Here we emit the normal startup events.
        # Environment dims only when telemetry is ENABLED: the helper's
        # ``previous_version`` probe writes ``~/.getpatter/version`` (and
        # ``days_since_install_bucket`` mkdirs the state root), violating the
        # documented invariant that opting out never touches the filesystem.
        _init_dims = {
            "carrier": carrier_kind or "none",
            "tunnel": _telemetry_tunnel_kind(self._tunnel_directive),
            **(
                _telemetry_environment_dims()
                if getattr(self._telemetry, "enabled", False)
                else {}
            ),
        }
        # Activation marker: emit ``first_run`` once per install (the run that
        # creates the install-id state). Gated on the enabled path so opting out
        # never touches the filesystem; the deploy-shape dims mirror sdk_initialized.
        if getattr(self._telemetry, "enabled", False):
            try:
                from getpatter.telemetry.install_id import is_first_run

                if is_first_run():
                    self._telemetry.record("first_run", **_init_dims)
            except Exception:
                pass
        self._telemetry.record("sdk_initialized", **_init_dims)

        self._server = None
        self._tunnel_handle = None
        # Observability — set by _attach_span_exporter, default safe.
        self._patter_side: str = "uut"
        # tunnel_ready future — resolved once ``serve()`` knows the public
        # webhook hostname (either statically configured or freshly minted by
        # the tunnel). Initialised lazily below to avoid pulling asyncio
        # imports into module-init.
        self._tunnel_ready: asyncio.Future[str] | None = None
        # Pre-resolve when webhook_url is static — no tunnel cold-start to
        # wait on. We can't create the Future here (no running loop yet) so
        # stash the value and create+resolve on first ``tunnel_ready`` access.
        self._tunnel_ready_pre_resolved: str | None = (
            webhook_url if webhook_url else None
        )
        # ``ready`` is the safe signal for outbound calls — resolves only
        # after ``serve()`` brings the embedded server up to ``listen``
        # state. Never pre-resolved at construction even when webhook_url
        # is static, because the WS routes only register inside ``serve()``.
        self._ready: asyncio.Future[str] | None = None
        # True iff ``_local_config.webhook_url`` was populated by ``serve()``
        # from a freshly-started cloudflared tunnel (rather than by the
        # constructor from an explicit ``webhook_url`` value). ``disconnect()``
        # uses this flag to clear ONLY the auto-assigned hostname so a
        # subsequent ``serve()`` call (e.g. from an integration that disposes
        # + restarts on agent-identity changes) does not throw
        # ``Cannot use both tunnel=True and webhook_url``.
        self._tunnel_owns_webhook_url: bool = False

        # Speech-edge events for turn-taking instrumentation. Public surface:
        # the seven ``on_*`` proxy attributes below plus the
        # ``conversation_state`` snapshot. Defaults are no-ops — existing
        # users who never set a callback see exactly the previous behaviour.
        # See ``getpatter._speech_events`` for the full event taxonomy and
        # the OpenAI Realtime alignment table.
        # Imported inline to keep client.py's top-level import graph minimal.
        from getpatter._speech_events import SpeechEvents as _SpeechEvents

        self.speech_events = _SpeechEvents()

        # Pre-rendered first-message TTS audio per outbound call_id.
        # Populated by :meth:`call` when ``agent.prewarm_first_message`` is
        # True; consumed by the StreamHandler firstMessage emit path so
        # the greeting streams instantly on ``start`` instead of paying the
        # 200-700 ms TTS first-byte latency. See ``Agent.prewarm_first_message``.
        # Stores raw bytes in the TTS provider's native sample rate; the
        # carrier-side AudioSender resamples on emit.
        self._prewarm_audio: dict[str, bytes] = {}
        # Call IDs whose prewarm cache slot has already been consumed —
        # either by ``pop_prewarm_audio`` (cache hit OR miss on the
        # firstMessage emit path) or by ``_record_prewarm_waste`` (call
        # ended before pickup). The prewarm task checks this set BEFORE
        # writing bytes so a slow synth that finishes after the consumer
        # already polled doesn't orphan bytes in ``_prewarm_audio``. See
        # FIX #92 in the parity audit.
        self._prewarm_consumed: set[str] = set()
        # Background tasks tracked so :meth:`disconnect` can cancel any
        # still-running prewarm-first-message synth before tearing down.
        self._prewarm_tasks: set[asyncio.Task] = set()
        # TTL eviction tasks tracked so :meth:`disconnect` can cancel any
        # pending eviction timer before tearing down. Keyed by call_id so
        # a follow-up consume / waste-record path can also cancel the
        # timer when the slot drains naturally.
        self._prewarm_ttl_tasks: dict[str, asyncio.Task] = {}
        # Pre-opened, fully-handshaked provider WebSockets keyed by
        # carrier-issued call_id. Populated by
        # :meth:`_park_provider_connections` during the carrier
        # ringing window; consumed by the per-call StreamHandler at
        # ``start`` via ``adopt_websocket(...)`` so STT / TTS /
        # Realtime audio can flow on the first turn without paying
        # the 150-900 ms TLS + WS-upgrade + protocol-handshake
        # round-trip again.
        #
        # Each value is a ``dict`` with optional keys ``stt``, ``tts``,
        # ``openai_realtime`` — provider-specific handles that the
        # StreamHandler hands to the matching adapter's
        # ``adopt_websocket`` method.
        #
        # Distinct from ``_prewarm_audio`` (pre-rendered TTS bytes for
        # the first message); the two features are complementary and
        # orthogonal — both can be active for the same call.
        self._prewarmed_connections: dict[str, dict[str, Any]] = {}
        # TTL eviction tasks for parked connections, keyed by call_id.
        self._prewarmed_conn_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Speech-edge event callback proxies
    # ------------------------------------------------------------------
    # The seven ``on_*`` attributes below follow the canonical voice-agent
    # metric set (user/agent state transitions, turn boundaries, TTFT, audio
    # first-byte) and align with OpenAI Realtime where applicable. They
    # proxy to ``self.speech_events`` so the dispatcher remains the single
    # source of truth (state + OTel).

    @property
    def on_user_speech_started(self) -> SpeechEventCallback | None:
        return self.speech_events.on_user_speech_started

    @on_user_speech_started.setter
    def on_user_speech_started(self, cb: SpeechEventCallback | None) -> None:
        self.speech_events.on_user_speech_started = cb

    @property
    def on_user_speech_ended(self) -> SpeechEventCallback | None:
        return self.speech_events.on_user_speech_ended

    @on_user_speech_ended.setter
    def on_user_speech_ended(self, cb: SpeechEventCallback | None) -> None:
        self.speech_events.on_user_speech_ended = cb

    @property
    def on_user_speech_eos(self) -> SpeechEventCallback | None:
        return self.speech_events.on_user_speech_eos

    @on_user_speech_eos.setter
    def on_user_speech_eos(self, cb: SpeechEventCallback | None) -> None:
        self.speech_events.on_user_speech_eos = cb

    @property
    def on_agent_speech_started(self) -> SpeechEventCallback | None:
        return self.speech_events.on_agent_speech_started

    @on_agent_speech_started.setter
    def on_agent_speech_started(self, cb: SpeechEventCallback | None) -> None:
        self.speech_events.on_agent_speech_started = cb

    @property
    def on_agent_speech_ended(self) -> SpeechEventCallback | None:
        return self.speech_events.on_agent_speech_ended

    @on_agent_speech_ended.setter
    def on_agent_speech_ended(self, cb: SpeechEventCallback | None) -> None:
        self.speech_events.on_agent_speech_ended = cb

    @property
    def on_llm_token(self) -> SpeechEventCallback | None:
        return self.speech_events.on_llm_token

    @on_llm_token.setter
    def on_llm_token(self, cb: SpeechEventCallback | None) -> None:
        self.speech_events.on_llm_token = cb

    @property
    def on_audio_out(self) -> SpeechEventCallback | None:
        return self.speech_events.on_audio_out

    @on_audio_out.setter
    def on_audio_out(self, cb: SpeechEventCallback | None) -> None:
        self.speech_events.on_audio_out = cb

    @property
    def conversation_state(self) -> dict[str, str]:
        """Snapshot of the current per-side state of the call.

        Returns ``{"user": <state>, "agent": <state>}`` — the user_state /
        agent_state snapshot. Read-only and safe to call at any time.
        """
        return self.speech_events.conversation_state

    @staticmethod
    def _unpack_carrier(carrier: Any) -> tuple[str | None, dict]:
        """Convert a ``Twilio(...)``/``Telnyx(...)``/``Plivo(...)`` instance to
        kind + creds.

        Returns ``(None, {})`` when *carrier* is ``None``. Raises
        :class:`TypeError` if the argument does not expose a ``.kind`` attribute
        matching one of the supported carriers.
        """
        if carrier is None:
            return None, {}
        # Import lazily to keep the module import graph flat.
        from getpatter.carriers.plivo import Carrier as _Plivo
        from getpatter.carriers.telnyx import Carrier as _Telnyx
        from getpatter.carriers.twilio import Carrier as _Twilio

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
        if isinstance(carrier, _Plivo):
            return "plivo", {
                "auth_id": carrier.auth_id,
                "auth_token": carrier.auth_token,
            }
        raise TypeError(
            "carrier= must be a Twilio(...), Telnyx(...) or Plivo(...) instance, "
            f"got {type(carrier).__name__}"
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
            from getpatter.tunnels import CloudflareTunnel

            return CloudflareTunnel(), ""

        from getpatter.tunnels import CloudflareTunnel, Ngrok, Static

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
    def metrics_store(self):
        """Live ``MetricsStore`` for the embedded server.

        Returns ``None`` before ``serve()`` is called. Exposed so integrations
        like ``PatterTool`` can subscribe to per-call lifecycle events
        (``call_initiated``, ``call_start``, ``call_end``).
        """
        server = getattr(self, "_server", None)
        if server is None:
            return None
        return getattr(server, "_metrics_store", None)

    @property
    def tunnel_ready(self) -> asyncio.Future[str]:
        """Future that resolves as soon as the public webhook hostname is known.

        **Prefer ``ready`` for outbound calls.** ``tunnel_ready`` resolves
        before the embedded server is in ``listen`` state, so a
        ``phone.call`` placed immediately afterwards can still race the
        Twilio Media Streams upgrade and produce an 11100 call drop.

        Kept as a separate signal because some integrations (e.g. webhook
        registration) only need the hostname, not the WS server.
        """
        if self._tunnel_ready is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
            self._tunnel_ready = loop.create_future()
            if self._tunnel_ready_pre_resolved is not None:
                self._tunnel_ready.set_result(self._tunnel_ready_pre_resolved)
        return self._tunnel_ready

    @property
    def ready(self) -> asyncio.Future[str]:
        """Future that resolves once the SDK is fully ready for callbacks.

        Resolves after tunnel + carrier auto-config + embedded server
        ``listen`` are all complete. This is the safe signal for outbound
        calls — the documented pattern is::

            task = asyncio.create_task(phone.serve(agent, tunnel=True))
            host = await phone.ready
            await phone.call(to=..., agent=agent)

        Rejects with the underlying exception if ``serve()`` fails before
        the server is listening.
        """
        if self._ready is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
            self._ready = loop.create_future()
        return self._ready

    def _resolve_tunnel_ready(self, hostname: str) -> None:
        """Resolve the tunnel-ready future. Safe to call multiple times."""
        # Force lazy creation, then set if not already done.
        fut = self.tunnel_ready
        if not fut.done():
            fut.set_result(hostname)

    def _reject_tunnel_ready(self, err: BaseException) -> None:
        """Reject the tunnel-ready future. Safe to call multiple times."""
        fut = self.tunnel_ready
        if not fut.done():
            fut.set_exception(err)

    def _resolve_ready(self, hostname: str) -> None:
        """Resolve the server-ready future. Safe to call multiple times."""
        fut = self.ready
        if not fut.done():
            fut.set_result(hostname)

    def _reject_ready(self, err: BaseException) -> None:
        """Reject the server-ready future. Safe to call multiple times."""
        fut = self.ready
        if not fut.done():
            fut.set_exception(err)

    async def call(
        self,
        to: str,
        agent: Agent | None = None,
        first_message: str = "",
        from_number: str = "",
        machine_detection: bool = True,
        on_machine_detection: (
            Callable[[MachineDetectionResult], Awaitable[None] | None] | None
        ) = None,
        voicemail_message: str = "",
        ring_timeout: int | None = 25,
        wait: bool = False,
    ) -> "CallResult | None":
        """Make an outbound call.

        Args:
            to: Phone number to call (E.164 format).
            agent: ``Agent`` instance to use (required).
            first_message: What the AI says when the callee answers.
            from_number: Number to call from. If empty, uses configured number.
            machine_detection: **Defaults to ``True``** — the SDK asks Twilio
                (``MachineDetection=DetectMessageEnd`` + Async AMD) or Telnyx
                (``answering_machine_detection=greeting_end``) to classify the
                callee. Async AMD on Twilio adds ~0 answer-latency on human
                pickups, so ON-by-default is safe. Pass ``False`` to skip
                per-call AMD billing when the destination is known.
            on_machine_detection: Called once when the carrier reports the AMD
                outcome. Fires for both ``human`` and ``machine`` results so
                acceptance tests can mark a run INVALID when classification
                is not ``human``.
            voicemail_message: If set and AMD detects a machine, speak this
                message and hang up. Implicitly enables ``machine_detection``.
            ring_timeout: Ring timeout in seconds before treating the call as
                no-answer. Defaults to 25 s — the production-recommended value
                that limits phantom calls. Pass ``ring_timeout=60`` for legacy
                carrier-default parity, or ``None`` to omit the parameter
                entirely (carrier picks its own default).
            wait: When ``True``, block until the call reaches a terminal state
                and return a :class:`CallResult` (``outcome`` ∈ answered /
                voicemail / no_answer / busy / failed, plus duration,
                transcript, cost). **Requires an active server** — call
                ``serve(...)`` first or use ``async with Patter(...)`` — because
                the terminal signals (carrier status callback, AMD, media-stream
                end) are delivered to the embedded server's webhooks. The
                default (``False``) is fire-and-forget and returns ``None``
                the instant the carrier accepts the dial (unchanged behaviour).

        Returns:
            ``None`` when ``wait=False`` (default). A :class:`CallResult` when
            ``wait=True``.
        """
        if not agent:
            raise PatterConnectionError("call() requires the agent parameter.")
        if first_message:
            # Per-call greeting override — the parameter was documented but
            # never referenced in the body. The Agent is a frozen dataclass,
            # so build a per-call copy: prewarm synthesis, the WS bridge and
            # the stream handler all read agent.first_message from here on.
            from dataclasses import replace as _dc_replace

            agent = _dc_replace(agent, first_message=first_message)
        from getpatter.telephony.common import _validate_e164

        if not isinstance(to, str) or not _validate_e164(to):
            raise ValueError(
                f"'to' must be a valid E.164 phone number (e.g., '+1234567890'), got '{to}'."
            )
        if from_number and not _validate_e164(from_number):
            raise ValueError(
                f"'from_number' must be a valid E.164 phone number, got '{from_number}'."
            )
        if wait and self._server is None:
            raise PatterConnectionError(
                "call(wait=True) requires an active server to receive the "
                "carrier completion webhooks. Call `await phone.serve(...)` "
                "first, or use `async with Patter(...) as phone:` which keeps "
                "the server up for the duration of the block."
            )
        # Store voicemail message on embedded server so AMD webhook can use it
        if voicemail_message and self._server is not None:
            self._server.voicemail_message = voicemail_message
        # Wire the per-call AMD callback into the embedded server BEFORE
        # dispatching the call so a fast Twilio Async AMD result (typically
        # 2-5 s after answer) cannot arrive before the callback is in place.
        # The single-slot ``on_machine_detection`` is the pre-dispatch
        # fallback (used only for the brief window before the carrier issues
        # a call id); once ``initiate_call`` returns we also register the
        # callback in ``on_machine_detection_by_call_sid`` keyed by that id so
        # concurrent outbound calls do not clobber each other's callbacks
        # (parity with the TypeScript ``onMachineDetectionByCallSid`` map).
        # AMD is **on by default**; pass ``machine_detection=False`` to
        # explicitly skip it. A non-empty ``voicemail_message`` also
        # implicitly requires AMD.
        wants_amd = bool(machine_detection) or bool(voicemail_message)
        if self._server is not None:
            # Clear/refresh the legacy single-slot fallback on every call so a
            # previous call's callback cannot leak through the fallback path.
            self._server.on_machine_detection = on_machine_detection  # type: ignore[attr-defined]

        # Pre-warm provider connections in parallel with the carrier-side
        # ``initiate_call`` so DNS / TLS / HTTP/2 handshakes complete during
        # the ringing window (3-15 s typically). Best-effort: warmup
        # failures are logged at DEBUG and never abort the call. Off when
        # the user explicitly sets ``Agent(prewarm=False)``.
        if getattr(agent, "prewarm", True):
            self._spawn_provider_warmup(agent)

        # Hoisted to method scope so the wait=True block below can correlate
        # the carrier-issued id with its completion future. Assigned in each
        # carrier branch from ``initiate_call``.
        call_id: str = ""
        config = self._local_config
        if config.telephony_provider == "twilio":
            from getpatter.providers.twilio_adapter import TwilioAdapter  # type: ignore[import]

            adapter = TwilioAdapter(
                account_sid=config.twilio_sid,
                auth_token=config.twilio_token,
            )
            stream_url = f"wss://{config.webhook_url}/ws/stream/outbound"
            extra_params: dict = {}
            if wants_amd:
                # DetectMessageEnd waits for the greeting to finish before
                # reporting ``machine_end_*`` so a follow-up voicemail-drop
                # lands after the beep (~100% accuracy in US, slightly lower
                # internationally). AsyncAmd avoids the 3-5 s answer-latency
                # penalty on human pickups — the call connects immediately
                # and the result arrives via the ``/webhooks/twilio/amd``
                # callback. Twilio best-practice default.
                #
                # NOTE: All keys here MUST be snake_case. The twilio-python
                # SDK's ``client.calls.create(**kwargs)`` accepts snake_case
                # arguments and internally translates them to the PascalCase
                # form Twilio's REST API requires on the wire. Passing
                # ``MachineDetection`` / ``StatusCallback`` etc. directly to
                # ``calls.create`` raises ``TypeError: unexpected keyword
                # argument`` and crashes every outbound call (the bug that
                # shipped through 0.5.x and was reported externally).
                extra_params["machine_detection"] = "DetectMessageEnd"
                extra_params["async_amd"] = "true"
                extra_params["async_amd_status_callback"] = (
                    f"https://{config.webhook_url}/webhooks/twilio/amd"
                )
            if ring_timeout is not None:
                extra_params["timeout"] = int(ring_timeout)
            # Status callback so the dashboard sees ringing/failed/
            # no-answer transitions before any media webhook fires.
            extra_params.setdefault(
                "status_callback",
                f"https://{config.webhook_url}/webhooks/twilio/status",
            )
            extra_params.setdefault("status_callback_method", "POST")
            # ``status_callback_event`` must be a list (twilio-python
            # serialises it as repeated query params), NOT a
            # space-separated single string. The space-separated form
            # triggered Twilio notification 21626 ("invalid
            # statusCallbackEvents") and on some ingestion paths also
            # broke the answer-handler webhook (root cause of intermittent
            # 11100 WS-upgrade failures).
            # See https://www.twilio.com/docs/voice/api/call-resource#statuscallbackevent
            extra_params.setdefault(
                "status_callback_event",
                ["initiated", "ringing", "answered", "completed"],
            )
            call_id = await adapter.initiate_call(
                from_number or config.phone_number,
                to,
                stream_url,
                extra_params=extra_params,
            )
            logger.info("Outbound call initiated: %s", call_id)
            # Pre-register the call so the dashboard surfaces attempts
            # that never reach media (no-answer, busy, carrier-reject).
            initiated_payload = {
                "call_id": call_id,
                "caller": from_number or config.phone_number,
                "callee": to,
                "direction": "outbound",
                "status": "initiated",
            }
            if (
                self._server is not None
                and getattr(self._server, "_metrics_store", None) is not None
            ):
                try:
                    self._server._metrics_store.record_call_initiated(initiated_payload)
                except Exception as exc:
                    logger.debug("record_call_initiated: %s", exc)
            # Relay to a standalone dashboard (``patter dashboard`` running
            # in a separate process) so it surfaces the dial attempt the
            # moment we hand off to the carrier, not only when media arrives
            # on pickup. Fire-and-forget — silent when no standalone
            # dashboard is listening.
            try:
                from getpatter.dashboard.persistence import notify_dashboard

                asyncio.create_task(notify_dashboard(initiated_payload))
            except Exception:
                pass
            self._spawn_prewarm_first_message(
                agent, call_id, ring_timeout=ring_timeout, carrier="twilio"
            )
            # Park provider WebSockets in parallel so the per-call
            # StreamHandler can adopt them at ``start`` instead of
            # paying the cold-handshake on first turn. Off when the
            # user explicitly sets ``agent.prewarm=False``.
            if getattr(agent, "prewarm", True) is not False:
                self._park_provider_connections(agent, call_id, carrier="twilio")
        elif config.telephony_provider == "telnyx":
            from getpatter.providers.telnyx_adapter import TelnyxAdapter  # type: ignore[import]

            adapter = TelnyxAdapter(
                api_key=config.telnyx_key,
                connection_id=config.telnyx_connection_id,
            )
            stream_url = f"wss://{config.webhook_url}/ws/telnyx/stream/outbound"
            try:
                call_id = await adapter.initiate_call(
                    from_number or config.phone_number,
                    to,
                    stream_url,
                    ring_timeout=ring_timeout,
                    machine_detection=wants_amd,
                )
            finally:
                # The adapter opens a pooled httpx.AsyncClient in __init__;
                # without this close every outbound dial leaked the pool.
                try:
                    await adapter.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
            logger.info("Outbound call initiated: %s", call_id)
            initiated_payload = {
                "call_id": call_id,
                "caller": from_number or config.phone_number,
                "callee": to,
                "direction": "outbound",
                "status": "initiated",
            }
            if (
                self._server is not None
                and getattr(self._server, "_metrics_store", None) is not None
            ):
                try:
                    self._server._metrics_store.record_call_initiated(initiated_payload)
                except Exception as exc:
                    logger.debug("record_call_initiated: %s", exc)
            try:
                from getpatter.dashboard.persistence import notify_dashboard

                asyncio.create_task(notify_dashboard(initiated_payload))
            except Exception:
                pass
            self._spawn_prewarm_first_message(
                agent, call_id, ring_timeout=ring_timeout, carrier="telnyx"
            )
            # Park provider WebSockets in parallel so the per-call
            # StreamHandler can adopt them at ``start`` instead of
            # paying the cold-handshake on first turn. Off when the
            # user explicitly sets ``agent.prewarm=False``.
            if getattr(agent, "prewarm", True) is not False:
                self._park_provider_connections(agent, call_id, carrier="telnyx")
        elif config.telephony_provider == "plivo":
            from getpatter.providers.plivo_adapter import PlivoAdapter  # type: ignore[import]

            adapter = PlivoAdapter(
                auth_id=config.plivo_auth_id,
                auth_token=config.plivo_auth_token,
            )
            # Plivo fetches ``answer_url`` on pickup and that handler returns
            # the ``<Stream>`` XML — so the WSS path is unused as a dial param
            # (retained only for TelephonyProvider parity). The same
            # ``/webhooks/plivo/voice`` route serves inbound and outbound.
            stream_url = f"wss://{config.webhook_url}/ws/plivo/stream/outbound"
            answer_url = f"https://{config.webhook_url}/webhooks/plivo/voice"
            status_url = f"https://{config.webhook_url}/webhooks/plivo/status"
            amd_url = f"https://{config.webhook_url}/webhooks/plivo/amd"
            try:
                call_id = await adapter.initiate_call(
                    from_number or config.phone_number,
                    to,
                    stream_url,
                    answer_url=answer_url,
                    # hangup_url is Plivo's StatusCallback analogue — without
                    # it, the /webhooks/plivo/status route never fires for
                    # outbound calls and the dashboard misses no-answer /
                    # busy / failed.
                    hangup_url=status_url,
                    ring_timeout=ring_timeout,
                    machine_detection=wants_amd,
                    machine_detection_url=amd_url if wants_amd else "",
                )
            finally:
                # The adapter opens a pooled httpx.AsyncClient in __init__;
                # without this close every outbound dial leaked the pool.
                try:
                    await adapter.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
            logger.info("Outbound call initiated: %s", call_id)
            initiated_payload = {
                "call_id": call_id,
                "caller": from_number or config.phone_number,
                "callee": to,
                "direction": "outbound",
                "status": "initiated",
            }
            if (
                self._server is not None
                and getattr(self._server, "_metrics_store", None) is not None
            ):
                try:
                    self._server._metrics_store.record_call_initiated(initiated_payload)
                except Exception as exc:
                    logger.debug("record_call_initiated: %s", exc)
            try:
                from getpatter.dashboard.persistence import notify_dashboard

                asyncio.create_task(notify_dashboard(initiated_payload))
            except Exception:
                pass
            self._spawn_prewarm_first_message(
                agent, call_id, ring_timeout=ring_timeout, carrier="plivo"
            )
            if getattr(agent, "prewarm", True) is not False:
                self._park_provider_connections(agent, call_id, carrier="plivo")

        # Register the AMD callback keyed by the carrier-issued call id so
        # concurrent outbound calls each get their own callback (the embedded
        # server's webhook handlers prefer this per-call entry over the
        # single-slot fallback and remove it after firing once).
        if self._server is not None and on_machine_detection is not None and call_id:
            self._server.on_machine_detection_by_call_sid[call_id] = (  # type: ignore[attr-defined]
                on_machine_detection
            )

        # --- wait=True: block until the call reaches a terminal state ---
        # Register the completion future now that the carrier has issued the
        # call_id. The future resolves from the first terminal signal handled
        # by the embedded server (status callback / AMD + media-stream end).
        # The race window between ``initiate_call`` returning and this
        # registration is harmless: the callee is still ringing, so no
        # terminal signal can fire before we register.
        if wait:
            server = self._server
            if server is None or not call_id:
                # Should be unreachable — the precondition above raised when
                # no server, and both carrier branches set call_id — but stay
                # defensive rather than await a future that can never resolve.
                raise PatterConnectionError(
                    "call(wait=True): no active server or carrier call id."
                )
            fut = server.register_completion(call_id)
            # Backstop only — the real resolution comes from a carrier signal.
            # Sized at the ring window plus a generous in-call ceiling so a
            # legitimately long conversation is never cut short.
            backstop = float((ring_timeout or 25) + 1800)
            try:
                return await asyncio.wait_for(fut, timeout=backstop)
            except asyncio.TimeoutError as exc:
                # Drop the dangling future so a late signal can't resolve a
                # result nobody is awaiting.
                server._completions.pop(call_id, None)
                raise TimeoutError(
                    f"call(wait=True): no terminal signal for call {call_id} "
                    f"within {backstop:.0f}s"
                ) from exc
        return None

    # === Pre-warm helpers ===

    def _spawn_provider_warmup(self, agent: Agent) -> None:
        """Spawn a fire-and-forget task that warms up STT / TTS / LLM in
        parallel with the carrier-side ``initiate_call``.

        Best-effort: each provider's ``warmup()`` is wrapped in
        ``asyncio.gather(..., return_exceptions=True)`` so a slow or
        failing endpoint cannot block the others. The default
        ``warmup()`` on the abstract base classes is a no-op, so providers
        that don't override it contribute nothing to call latency.
        """
        targets = []
        for provider in (
            getattr(agent, "stt", None),
            getattr(agent, "tts", None),
            getattr(agent, "llm", None),
        ):
            if provider is None:
                continue
            warmup = getattr(provider, "warmup", None)
            if warmup is None or not callable(warmup):
                continue
            targets.append(provider)

        if not targets:
            return

        async def _run_all() -> None:
            results = await asyncio.gather(
                *(p.warmup() for p in targets),
                return_exceptions=True,
            )
            for provider, result in zip(targets, results):
                if isinstance(result, BaseException):
                    logger.debug(
                        "Provider warmup failed (%s): %s",
                        type(provider).__name__,
                        result,
                    )

        task = asyncio.create_task(_run_all())
        # Track but don't await — warmup runs in parallel with the carrier
        # call and never blocks the user.
        self._prewarm_tasks.add(task)
        task.add_done_callback(self._prewarm_tasks.discard)

    def _alias_prewarm(self, old_id: str, new_id: str) -> None:
        """Re-key prewarm caches from a dial-time id to the live carrier id.

        Plivo issues ``request_uuid`` at dial time but the media stream and
        webhooks carry ``CallUUID`` — without re-keying, prewarmed first
        message audio and parked provider sockets never matched and always
        TTL-evicted as "wasted".
        """
        if not old_id or not new_id or old_id == new_id:
            return
        for attr in (
            "_prewarm_audio",
            "_prewarm_ttl_tasks",
            "_prewarmed_connections",
            "_prewarmed_conn_tasks",
        ):
            mapping = getattr(self, attr, None)
            if (
                isinstance(mapping, dict)
                and old_id in mapping
                and new_id not in mapping
            ):
                mapping[new_id] = mapping.pop(old_id)

    def pop_prewarmed_connections(self, call_id: str) -> dict[str, Any] | None:
        """Pop and return the parked provider WS handles for ``call_id``,
        or ``None`` when no parked connections exist.

        Wired into the per-call ``StreamHandler`` so it can adopt the
        parked sockets at the carrier ``start`` event instead of paying
        the cold handshake on first turn.
        """
        slot = self._prewarmed_connections.pop(call_id, None)
        ttl_task = self._prewarmed_conn_tasks.pop(call_id, None)
        if ttl_task is not None:
            ttl_task.cancel()
        return slot

    def close_prewarmed_connections(self, call_id: str) -> None:
        """Close any parked provider WSs for ``call_id`` cleanly.

        Wired into call-termination paths (no-answer, busy, failed,
        canceled, AMD voicemail) so the sockets drop instead of being
        left to the upstream timeout.
        """
        slot = self._prewarmed_connections.pop(call_id, None)
        ttl_task = self._prewarmed_conn_tasks.pop(call_id, None)
        if ttl_task is not None:
            ttl_task.cancel()
        if slot is not None:
            _close_parked_slot(slot)

    def _park_provider_connections(
        self,
        agent: Agent,
        call_id: str,
        *,
        carrier: str | None = None,
    ) -> None:
        """Open and park provider WebSockets in parallel with the
        carrier-side ``initiate_call``. Unlike :meth:`_spawn_provider_warmup`
        (which closes the WS after a brief idle), the sockets opened here
        stay OPEN and are handed off to the per-call ``StreamHandler`` on
        ``start``.

        Structural fix for first-turn cold-start: opening + closing a WS
        does NOT warm TLS for the next open — every fresh
        ``websockets.connect`` re-pays the full TCP + TLS + HTTP-101
        round-trip. Keeping the WS open and adopting it directly skips
        the handshake entirely (saves ~150-900 ms depending on provider).

        Best-effort: each provider's parking task is wrapped in
        ``asyncio.gather(..., return_exceptions=True)`` so a slow or
        failing endpoint cannot block the others. Providers without
        ``open_parked_connection`` contribute nothing.
        """
        stt = getattr(agent, "stt", None)
        tts = getattr(agent, "tts", None)
        stt_open = getattr(stt, "open_parked_connection", None) if stt else None
        tts_open = getattr(tts, "open_parked_connection", None) if tts else None
        provider = getattr(agent, "provider", None)
        wants_realtime_park = provider in ("openai_realtime", "openai_realtime_2")
        if stt_open is None and tts_open is None and not wants_realtime_park:
            return

        slot: dict[str, Any] = {}
        self._prewarmed_connections[call_id] = slot

        started_at = time.monotonic()

        async def _park_stt() -> None:
            if stt_open is None:
                return
            try:
                handle = await stt_open()
                # Slot may have been drained while we were opening.
                if self._prewarmed_connections.get(call_id) is not slot:
                    await _safe_close_handle(handle)
                    return
                slot["stt"] = handle
                logger.info(
                    "[PREWARM] callId=%s provider=stt ms=%d",
                    call_id,
                    int((time.monotonic() - started_at) * 1000),
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.debug("Park STT failed for %s: %s", call_id, exc)

        async def _park_tts() -> None:
            if tts_open is None:
                return
            try:
                handle = await tts_open()
                if self._prewarmed_connections.get(call_id) is not slot:
                    await _safe_close_handle(handle)
                    return
                slot["tts"] = handle
                logger.info(
                    "[PREWARM] callId=%s provider=tts ms=%d",
                    call_id,
                    int((time.monotonic() - started_at) * 1000),
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.debug("Park TTS failed for %s: %s", call_id, exc)

        async def _park_openai_realtime() -> None:
            if not wants_realtime_park:
                return
            # Build a throw-away adapter instance JUST to call
            # ``open_parked_connection`` and produce a primed WS. The
            # per-call StreamHandler builds its own adapter and adopts
            # the returned WS via ``adopt_websocket``. Constructed with
            # the same agent-derived kwargs the StreamHandler would use,
            # so the parked session.update matches what the live session
            # expects — no second session.update round-trip on adopt.
            from getpatter.providers.openai_realtime_2 import (  # type: ignore[import]
                OpenAIRealtime2Adapter,
            )

            # The OpenAI key lives on ``LocalConfig.openai_key`` (set by
            # the user when constructing ``Patter()``); fall back to
            # ``OPENAI_API_KEY`` env var when not explicitly configured.
            api_key = getattr(self._local_config, "openai_key", None) or os.environ.get(
                "OPENAI_API_KEY"
            )
            if not api_key:
                logger.info(
                    "[PREWARM] callId=%s provider=openai_realtime SKIPPED — "
                    "no OPENAI_API_KEY available",
                    call_id,
                )
                return
            try:
                adapter_kwargs: dict[str, Any] = {
                    "api_key": api_key,
                    "model": agent.model,
                    "voice": agent.voice,
                    "instructions": agent.system_prompt or "",
                    "language": agent.language,
                    "tools": [],
                    # Carrier-derived placeholder; the GA adapter's session
                    # always emits ``audio/pcm @ 24000`` regardless of this
                    # value (it transcodes mulaw↔pcm internally), so any
                    # non-None value keeps the parent class happy. Looked up
                    # via ``_CARRIER_REALTIME_AUDIO_FORMAT`` so the
                    # "Plivo is like Twilio audio-wise" decision lives in
                    # exactly one place.
                    "audio_format": _CARRIER_REALTIME_AUDIO_FORMAT.get(
                        carrier or "", "pcm16"
                    ),
                }
                reasoning_effort = getattr(
                    agent, "openai_realtime_reasoning_effort", None
                )
                if reasoning_effort is not None:
                    adapter_kwargs["reasoning_effort"] = reasoning_effort
                transcription_model = getattr(
                    agent,
                    "openai_realtime_input_audio_transcription_model",
                    None,
                )
                if transcription_model is not None:
                    adapter_kwargs["input_audio_transcription_model"] = (
                        transcription_model
                    )
                tmp_adapter = OpenAIRealtime2Adapter(**adapter_kwargs)
                ws = await tmp_adapter.open_parked_connection()
                if self._prewarmed_connections.get(call_id) is not slot:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                slot["openai_realtime"] = ws
                logger.info(
                    "[PREWARM] callId=%s provider=openai_realtime ms=%d",
                    call_id,
                    int((time.monotonic() - started_at) * 1000),
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                # Bumped to INFO so prewarm failures surface in normal
                # logs — they're best-effort but invisible failures make
                # the latency optimisation hard to debug. Callers can
                # silence with a logging filter if they really want.
                logger.info(
                    "[PREWARM] callId=%s provider=openai_realtime FAILED: %s",
                    call_id,
                    exc,
                )

        async def _run_all() -> None:
            await asyncio.gather(
                _park_stt(),
                _park_tts(),
                _park_openai_realtime(),
                return_exceptions=True,
            )

        task = asyncio.create_task(_run_all())
        self._prewarm_tasks.add(task)

        def _on_park_done(_t: asyncio.Task) -> None:
            self._prewarm_tasks.discard(_t)
            # Schedule TTL cleanup so a never-adopted slot is force-closed.
            if call_id not in self._prewarmed_connections:
                return
            try:
                ttl_task = asyncio.create_task(
                    self._evict_parked_after(call_id, _PARKED_CONN_TTL_S)
                )
            except RuntimeError:
                # No running loop — drop synchronously.
                orphan = self._prewarmed_connections.pop(call_id, None)
                if orphan is not None:
                    _close_parked_slot(orphan)
                return
            self._prewarmed_conn_tasks[call_id] = ttl_task
            ttl_task.add_done_callback(
                lambda _t, cid=call_id: self._prewarmed_conn_tasks.pop(cid, None)
            )

        task.add_done_callback(_on_park_done)

    async def _evict_parked_after(self, call_id: str, ttl_s: float) -> None:
        """Sleep ``ttl_s`` then force-close any parked sockets still
        present for ``call_id``. No-op if the slot was already
        consumed / closed.
        """
        try:
            await asyncio.sleep(ttl_s)
        except asyncio.CancelledError:
            return
        slot = self._prewarmed_connections.pop(call_id, None)
        if slot is not None:
            _close_parked_slot(slot)
            logger.warning(
                "[PREWARM] parked connections evicted by TTL for %s — "
                "call never reached start (~%.0fs).",
                call_id,
                ttl_s,
            )

    def _spawn_prewarm_first_message(
        self,
        agent: Agent,
        call_id: str,
        *,
        ring_timeout: int | None,
        carrier: str | None = None,
    ) -> None:
        """Pre-render ``agent.first_message`` to TTS bytes during the
        ringing window and stash them in ``_prewarm_audio[call_id]``.

        Skipped silently when ``agent.prewarm_first_message`` is False or
        when ``agent.tts`` / ``agent.first_message`` is missing. The synth
        is bounded by ``ring_timeout`` (default 25 s) so a never-answered
        call doesn't tie up the TTS connection. On timeout / error the
        cache is left empty and the StreamHandler falls back to live TTS.

        **Pipeline mode only.** Realtime / ConvAI provider modes never
        consume the prewarm cache (the StreamHandler for those modes runs
        its first-message emit through the provider's own audio path).
        Spawning the prewarm in those modes pays the TTS bill for nothing
        — refused with a WARN.

        **Capped at ``_PREWARM_CACHE_MAX`` concurrent entries.** Refused
        with a WARN when the cap is reached (the call still proceeds —
        StreamHandler falls back to live TTS).

        ``carrier`` — when provided (``"twilio"`` / ``"telnyx"``), the TTS
        adapter's ``set_telephony_carrier`` hook is called BEFORE synthesis
        so it can produce wire-native bytes (``ulaw_8000`` for Twilio,
        ``pcm_16000`` for Telnyx) and skip the client-side transcode.
        Parity with TS ``Patter.spawnPrewarmFirstMessage(carrier)``.
        """
        if not getattr(agent, "prewarm_first_message", False):
            return
        # FIX #94 — Realtime / ConvAI never consume the cache. Refuse early
        # so the user notices the silent TTS waste instead of paying for a
        # synth no caller will ever hear.
        provider_mode = getattr(agent, "provider", "openai_realtime")
        if provider_mode != "pipeline":
            logger.warning(
                "agent.prewarm_first_message=True is only supported in pipeline "
                "mode (provider=%s); skipping pre-synth to avoid wasted TTS spend.",
                provider_mode,
            )
            return
        first_message = getattr(agent, "first_message", "") or ""
        tts = getattr(agent, "tts", None)
        if not first_message or tts is None:
            return
        synthesize = getattr(tts, "synthesize", None)
        if synthesize is None or not callable(synthesize):
            return

        # Advise the TTS adapter of the telephony carrier BEFORE we trigger
        # the synth so it can produce wire-native bytes (``ulaw_8000`` for
        # Twilio, ``pcm_16000`` for Telnyx) — skipping the client-side
        # resample + mulaw encode that produced audible artifacts on the
        # prewarmed firstMessage during 0.6.2 acceptance. The hook is opt-in
        # per-adapter; adapters that don't expose it (or that the user
        # configured with an explicit output_format) keep their format.
        # Parity with TS ``Patter.spawnPrewarmFirstMessage``.
        if carrier:
            set_carrier = getattr(tts, "set_telephony_carrier", None)
            if callable(set_carrier):
                try:
                    set_carrier(carrier)
                except Exception as _exc:
                    logger.debug(
                        "Prewarm TTS set_telephony_carrier failed for %s: %s",
                        call_id,
                        _exc,
                    )

        # FIX #96 — refuse to spawn when the cache (live entries +
        # in-flight synth tasks) would exceed the cap. Counting both
        # active entries AND pending tasks keeps the bound honest under
        # outbound-flood conditions where carrier ``start`` events lag.
        in_flight = len(self._prewarm_audio) + len(self._prewarm_tasks)
        if in_flight >= _PREWARM_CACHE_MAX:
            logger.warning(
                "Prewarm cache full (%d/%d in-flight) — skipping pre-synth for "
                "call %s; falling back to live TTS at pickup.",
                in_flight,
                _PREWARM_CACHE_MAX,
                call_id,
            )
            return

        timeout_s = float(ring_timeout) if ring_timeout is not None else 25.0

        async def _run() -> None:
            try:
                buf = bytearray()

                async def _accumulate() -> None:
                    async for chunk in synthesize(first_message):
                        if isinstance(chunk, (bytes, bytearray)):
                            buf.extend(chunk)

                await asyncio.wait_for(_accumulate(), timeout=timeout_s)
                if buf:
                    # FIX #92 — race guard. If the consumer already polled
                    # (cache hit or miss) before the synth finished, the
                    # StreamHandler has already fallen back to live TTS;
                    # writing bytes here would orphan them in
                    # ``_prewarm_audio`` until ``end_call`` ever runs.
                    if call_id in self._prewarm_consumed:
                        logger.warning(
                            "Prewarm orphaned for call %s — synth completed "
                            "(~%d bytes) AFTER consumer polled; bytes dropped, "
                            "TTS bill already paid.",
                            call_id,
                            len(buf),
                        )
                        return
                    self._prewarm_audio[call_id] = bytes(buf)
                    logger.debug(
                        "Prewarm first-message ready for call %s (%d bytes)",
                        call_id,
                        len(buf),
                    )
            except asyncio.TimeoutError:
                logger.debug(
                    "Prewarm first-message timed out for call %s after %.1fs",
                    call_id,
                    timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.debug(
                    "Prewarm first-message failed for call %s: %s", call_id, exc
                )

        task = asyncio.create_task(_run())
        self._prewarm_tasks.add(task)

        def _on_synth_done(_t: asyncio.Task) -> None:
            self._prewarm_tasks.discard(_t)
            # FIX #96 — schedule TTL eviction once the synth task has
            # produced (or failed to produce) cache bytes. If the carrier
            # never fires ``start`` AND the status / hangup callback
            # never runs (e.g. cloud-side telephony quirk), the entry
            # would otherwise leak. The eviction task itself is short
            # (just an ``asyncio.sleep`` + pop) and is no-op when the
            # slot has already been drained by ``pop_prewarm_audio`` /
            # ``_record_prewarm_waste``.
            if call_id not in self._prewarm_audio:
                return
            ttl_s = timeout_s + _PREWARM_TTL_GRACE_S
            try:
                evict_task = asyncio.create_task(
                    self._evict_prewarm_after(call_id, ttl_s)
                )
            except RuntimeError:
                # No running loop (process shutting down) — drop the
                # entry synchronously to avoid leaking it.
                self._prewarm_audio.pop(call_id, None)
                return
            self._prewarm_ttl_tasks[call_id] = evict_task
            evict_task.add_done_callback(
                lambda _t, cid=call_id: self._prewarm_ttl_tasks.pop(cid, None)
            )

        task.add_done_callback(_on_synth_done)

    async def _evict_prewarm_after(self, call_id: str, ttl_s: float) -> None:
        """Sleep ``ttl_s`` then drop ``call_id`` from the cache if still present.

        The TTS bill was paid by the synth task; this WARN flags the
        unconsumed entry so users notice never-answered calls that
        slipped past the status / hangup callback. Cancelled by
        :meth:`disconnect` and a no-op when the entry was already
        consumed via ``pop_prewarm_audio`` / ``_record_prewarm_waste``.
        """
        try:
            await asyncio.sleep(ttl_s)
        except asyncio.CancelledError:
            return
        bytes_ = self._prewarm_audio.pop(call_id, None)
        if bytes_ is not None:
            self._prewarm_consumed.add(call_id)
            logger.warning(
                "Prewarm bytes evicted by TTL — call %s never consumed them "
                "(~%d bytes synthesised, %.1fs after ring_timeout).",
                call_id,
                len(bytes_),
                ttl_s,
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

    def _record_config_incomplete(self, missing: str) -> None:
        """Emit the activation-blocker ``config_incomplete`` signal, at most once.

        Fire-and-forget: this records the coarse ``missing`` category right before
        a validation ``raise`` and then lets that error propagate unchanged. It
        NEVER throws and NEVER alters control flow. Deduped per instance so a retry
        loop or a second ``agent()`` call on the same Patter cannot double-emit.
        Only the allowlisted enum leaves the process — never the key value, env var
        name, carrier credentials, or any PII. ``config_incomplete`` is in the
        never-sampled set, so the telemetry client always delivers it.
        """
        try:
            if self._telemetry_config_incomplete_emitted:
                return
            self._telemetry_config_incomplete_emitted = True
            self._telemetry.record("config_incomplete", missing=missing)
        except Exception:
            # Telemetry must never break construction or agent setup.
            pass

    def agent(
        self,
        system_prompt: str,
        voice: str | None = None,
        model: str | None = None,
        language: str = "en",
        first_message: str = "",
        llm_error_message: str | None = None,
        long_turn_message: str | None = None,
        long_turn_message_after_s: float = 4.0,
        tools: list[Tool] | None = None,
        stt: STTProvider | None = None,
        tts: TTSProvider | None = None,
        variables: dict | None = None,
        guardrails: list[Guardrail] | None = None,
        hooks: PipelineHooks | None = None,
        text_transforms: list[Callable] | None = None,
        vad: VADProvider | None = None,
        turn_detector: "TurnDetectorProvider | None" = None,
        max_semantic_hold_ms: int = 1200,
        audio_filter: AudioFilter | None = None,
        background_audio: BackgroundAudioPlayer | None = None,
        barge_in_threshold_ms: int = 300,
        barge_in_mode: str = "cancel",
        barge_in_confirm_ms: int = 1500,
        aggressive_first_flush: bool = False,
        disable_phone_preamble: bool = False,
        echo_cancellation: bool = False,
        engine: Any = None,
        provider: str | None = None,
        llm: LLMProvider | None = None,
        mcp_servers: list | None = None,
        consult: ConsultConfig | None = None,
        handoffs: dict[str, Agent] | None = None,
        prewarm_first_message: bool | None = None,
        openai_realtime_noise_reduction: Literal["near_field", "far_field"]
        | None = None,
        realtime_turn_detection: "RealtimeTurnDetection | None" = None,
        realtime_gate_response_on_transcript: bool | None = None,
        tool_call_preambles: bool | str = False,
        preemptive_generation: bool = False,
        preemptive_min_stable_ms: int = 300,
    ) -> Agent:
        """Create an ``Agent`` configuration.

        The AI provider mode is derived from the arguments:

        * ``engine=OpenAIRealtime(...)`` → OpenAI Realtime API.
        * ``engine=ElevenLabsConvAI(...)`` → ElevenLabs Conversational AI.
        * No ``engine`` + ``stt``/``tts`` set → pipeline mode (STT + LLM + TTS).
        * No ``engine`` and no ``stt``/``tts`` → defaults to OpenAI Realtime (the
          server will look up the OpenAI credentials from the engine or env).

        Args:
            system_prompt: Instructions for the AI agent.
            voice: TTS voice name (e.g. ``"alloy"``, ``"echo"``). When omitted,
                the ``engine=`` marker's voice applies (default ``"alloy"``);
                an explicit value always wins over the engine.
            model: OpenAI Realtime model ID. When omitted, the ``engine=``
                marker's model applies (default ``"gpt-realtime-mini"``); an
                explicit value always wins over the engine.
            language: BCP-47 language code, e.g. ``"en"``.
            first_message: If set, the agent speaks this immediately on connect.
            long_turn_message: Pipeline mode only. Opt-in short filler spoken
                when a turn is SLOW (e.g. an agent runtime running tools) and no
                audio has reached the carrier after
                ``long_turn_message_after_s`` seconds — distinct from
                ``llm_error_message`` (which fires on an error). ``None``
                (default) keeps today's silence-while-thinking behaviour. Speaks
                at most once per turn and never once real audio has started.
            long_turn_message_after_s: Seconds to wait before the
                ``long_turn_message`` filler fires. Default ``4.0``.
            preemptive_generation: Pipeline mode (built-in LLM loop) only.
                When ``True``, start the LLM + TTS early on a confident
                INTERIM transcript and hold the audio; release it the moment
                the matching final transcript commits (big latency win), or
                discard silently and re-dispatch when the final differs.
                Default ``False``.
            preemptive_min_stable_ms: How long (ms) a non-punctuated interim
                transcript must remain unchanged before it qualifies for
                preemptive generation. Default ``300``.
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
            handoffs: Multi-agent handoff targets — ``{name: other_agent}``
                where each value is another ``Agent`` built with
                ``phone.agent(...)``. When set, Patter injects a built-in
                ``handoff_to(name, reason?)`` tool (Realtime + Pipeline
                modes); calling it swaps the CURRENT call to the target
                agent's system prompt, tools, variables, guardrails, and
                onward handoffs mid-call. Audio infrastructure established
                at call start (STT/TTS/engine connection — and therefore
                voice on engines that cannot switch voice mid-session) is
                retained. Chained handoffs follow the target's own
                ``handoffs`` map.
            engine: ``OpenAIRealtime(...)`` or ``ElevenLabsConvAI(...)``.
            tool_call_preambles: Realtime modes only. ``False`` (default) ships
                ``system_prompt`` unchanged. ``True`` prepends a native
                "# Preambles" guidance block so the model speaks one short,
                action-describing sentence (e.g. "I'll check that order now.")
                immediately before a slow tool call, in its own voice — the
                recommended UX for 30-60 s tools. A ``str`` overrides the block
                verbatim. Most effective on ``gpt-realtime-2``, where preambles
                are first-class. Pipeline mode is unaffected (it already
                prepends its own phone preamble).
        """
        # --- Anonymous telemetry: engine family + the composed stack, deduped ---
        # Deduped by the *whole* stack signature (not just the engine family) so a
        # second agent with a different STT/TTS/LLM still records its composition.
        from getpatter.telemetry.stack import model_token, stack_dimensions

        _family = _telemetry_engine_family(engine, provider, stt, tts)
        _stack = stack_dimensions(stt, tts, llm)
        if _family == "realtime":
            # Realtime has no composed stack, so the model variant is the whole
            # story — resolve it the same way the engine unpacking below does
            # (an explicit model= kwarg wins; otherwise the engine's model).
            _rt_model = (
                model
                if model is not None
                else (getattr(engine, "model", None) or "gpt-realtime-mini")
            )
            _stack = {**_stack, "llm_model": model_token("openai", _rt_model)}
        _feature_key = (
            _family + "|" + ",".join(f"{k}={v}" for k, v in sorted(_stack.items()))
        )
        if _feature_key not in self._telemetry_seen_engines:
            self._telemetry_seen_engines.add(_feature_key)
            self._telemetry.record(
                "feature_used",
                engine=_family,
                provider=_telemetry_provider_family(_family),
                **_stack,
            )
        # agent_configured: tool counts + integration target (deduped per shape).
        # Built-in tools (transfer_call/end_call) and the consult tool are injected
        # at call time, NOT here, so at this point ``tools`` holds only user tools.
        _builtin = 1 if consult is not None else 0
        _custom_bucket = _telemetry_bucket_custom_tools(len(tools) if tools else 0)
        _integration, _integration_kind, _mcp_bucket = _telemetry_integration(
            consult, mcp_servers
        )
        # Feature-adoption: read tuning from the agent() kwargs OR the engine
        # object (the value isn't backfilled from the engine until later).
        _nr = openai_realtime_noise_reduction or getattr(
            engine, "noise_reduction", None
        )
        _noise_reduction = _nr if _nr in ("near_field", "far_field") else "none"
        _td = realtime_turn_detection
        if _td is None:
            _td = getattr(engine, "turn_detection", None)
        _turn_detection = "custom" if _td is not None else "none"
        _preambles = bool(tool_call_preambles)
        _per_tool_timeouts = isinstance(tools, (list, tuple)) and any(
            getattr(t, "timeout_s", None) is not None for t in tools
        )
        _llm_fallback = llm is not None and "fallback" in type(llm).__name__.lower()
        _shape = (
            _builtin,
            _custom_bucket,
            _integration,
            _integration_kind,
            _mcp_bucket,
            _noise_reduction,
            _turn_detection,
            _preambles,
            _per_tool_timeouts,
            _llm_fallback,
        )
        if _shape not in self._telemetry_seen_agent_shapes:
            self._telemetry_seen_agent_shapes.add(_shape)
            self._telemetry.record(
                "agent_configured",
                builtin_tool_count=_builtin,
                custom_tool_count_bucket=_custom_bucket,
                integration=_integration,
                integration_kind=_integration_kind,
                mcp_server_count_bucket=_mcp_bucket,
                noise_reduction=_noise_reduction,
                turn_detection=_turn_detection,
                preambles_used=_preambles,
                per_tool_timeouts_set=_per_tool_timeouts,
                llm_fallback_configured=_llm_fallback,
            )

        # --- Validate llm= (runtime-checkable Protocol) ---
        if llm is not None and not isinstance(llm, LLMProvider):
            raise TypeError(
                "llm must be an LLMProvider instance (e.g. AnthropicLLM(api_key=...)) "
                f"or None; got {type(llm).__name__}"
            )

        # --- Validate provider= (parity with the TypeScript SDK) ---
        # ``provider`` is an explicit alternative to ``engine`` for selecting the
        # AI mode. Optional and default ``None`` — existing callers are unaffected.
        if provider is not None:
            if engine is not None:
                raise ValueError(
                    "Cannot pass both engine= and provider=. Use one (engine is preferred)."
                )
            _valid_providers = ("openai_realtime", "elevenlabs_convai", "pipeline")
            if provider not in _valid_providers:
                raise ValueError(
                    "provider must be one of: "
                    + ", ".join(_valid_providers)
                    + f". Got: {provider!r}"
                )

        # --- Validate barge_in_mode (parity with TS ``bargeInMode`` union) ---
        _valid_barge_in_modes = ("cancel", "pause_resume")
        if barge_in_mode not in _valid_barge_in_modes:
            raise ValueError(
                "barge_in_mode must be one of: "
                + ", ".join(_valid_barge_in_modes)
                + f". Got: {barge_in_mode!r}"
            )

        # --- Engine dispatch ---
        openai_engine_key: str = ""
        elevenlabs_engine_key: str = ""
        # Engine-supplied OpenAI Realtime extras propagated to Agent so the
        # stream-handler can forward them to ``OpenAIRealtimeAdapter``.
        openai_realtime_reasoning_effort: str | None = None
        openai_realtime_input_audio_transcription_model: str | None = None
        if engine is not None:
            # Engine mode handles the LLM internally — `llm=` is ignored.
            # Emit a one-time warning so the user knows.
            if llm is not None:
                logger.warning(
                    "llm= ignored when engine= is set (the engine handles the LLM internally)."
                )
            engine_kind, engine_fields = self._unpack_engine(engine)
            provider = engine_kind
            # Engine-supplied voice/model fill the slots the caller left unset
            # (sentinel ``None``); an *explicit* voice=/model= kwarg always
            # wins — even when its value equals the documented default.
            # Mirrors TS ``opts.model ?? engineModel ?? default``.
            if voice is None and engine_fields.get("voice"):
                voice = engine_fields["voice"]
            if model is None and engine_fields.get("model"):
                model = engine_fields["model"]
            if engine_kind in ("openai_realtime", "openai_realtime_2"):
                openai_engine_key = engine_fields.get("api_key", "")
                openai_realtime_reasoning_effort = engine_fields.get("reasoning_effort")
                openai_realtime_input_audio_transcription_model = engine_fields.get(
                    "input_audio_transcription_model"
                )
                # Explicit agent() kwargs win over the engine marker value.
                if openai_realtime_noise_reduction is None:
                    openai_realtime_noise_reduction = engine_fields.get(
                        "noise_reduction"
                    )
                if realtime_turn_detection is None:
                    realtime_turn_detection = engine_fields.get("turn_detection")
                if realtime_gate_response_on_transcript is None:
                    realtime_gate_response_on_transcript = engine_fields.get(
                        "gate_response_on_transcript"
                    )
            elif engine_kind == "elevenlabs_convai":
                elevenlabs_engine_key = engine_fields.get("api_key", "")
        elif provider is not None:
            # User explicitly selected the mode via provider= (TS parity); the
            # value was validated above. Keep it as the resolved mode.
            pass
        elif stt is not None or tts is not None or llm is not None:
            provider = "pipeline"
        else:
            provider = "openai_realtime"

        # Validate instance types for stt/tts and drop legacy forms.
        stt_resolved = self._resolve_stt(stt)
        tts_resolved = self._resolve_tts(tts)

        # Backfill any credentials the engine carries into LocalConfig so
        # downstream validation / dispatch sees them even when the user
        # didn't also set them on the Patter() constructor.
        from dataclasses import replace

        if openai_engine_key and not self._local_config.openai_key:
            self._local_config = replace(
                self._local_config, openai_key=openai_engine_key
            )
        if elevenlabs_engine_key and not self._local_config.elevenlabs_key:
            self._local_config = replace(
                self._local_config, elevenlabs_key=elevenlabs_engine_key
            )

        if (
            provider in ("openai_realtime", "openai_realtime_2")
            and not self._local_config.openai_key
        ):
            # The provider= string path has no engine marker to carry the key —
            # honour OPENAI_API_KEY from the environment (the error message has
            # always promised it, TS accepts it, and the engine markers already
            # env-fallback). Backfill the local config so the CALL PATH uses the
            # key too — accepting here but dialing with an empty key later
            # would be a dead call instead of a clear error.
            _env_openai_key = os.environ.get("OPENAI_API_KEY", "")
            if _env_openai_key:
                self._local_config = replace(
                    self._local_config, openai_key=_env_openai_key
                )
            else:
                self._record_config_incomplete("llm_key")
                raise ValueError(
                    "OpenAI Realtime mode requires an OpenAI API key. Pass "
                    "engine=OpenAIRealtime(api_key='sk-...') or set OPENAI_API_KEY "
                    "in the environment."
                )

        if provider == "pipeline":
            if stt_resolved is None:
                self._record_config_incomplete("engine_config")
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
                raise TypeError(f"tools must be a list, got {type(tools).__name__}.")
            tools_out = [self._tool_to_dict(t, index=i) for i, t in enumerate(tools)]
            # Structural sanity + strict-mode validation for tool JSON schemas.
            # Surfaces typos / missing required fields at agent() build time so
            # they don't blow up mid-call. Built-in tools (transfer_call,
            # end_call) are injected later in the Realtime adapter. Parity
            # with TS ``validateAllToolSchemas``.
            from getpatter.tools.schema_validation import validate_all_tool_schemas

            validate_all_tool_schemas(tools_out)

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

        # ``prewarm_first_message`` is opt-in (default False) — reverted
        # from 2026-05-18's default-on attempt after the 0.6.2 acceptance
        # run surfaced a phantom-barge-in interaction: prewarm bursts
        # audio at pickup, the very first inbound carrier frame triggered
        # Silero VAD speech_start, the firstMessage was cancelled
        # mid-playback and the user heard a clipped (graffiante) fragment.
        # Until the root cause (anchoring the barge-in gate on
        # first-mark-echo rather than ``first_audio_sent_at = begin_speaking
        # time``) is fully addressed, default it off so most pipeline calls
        # take the live-streaming path that the user is happy with. Opt in
        # explicitly per agent when willing to pay the trade-off.
        if prewarm_first_message is None:
            prewarm_first_message = False

        # The consult tool is injected only in Realtime and Pipeline modes;
        # ElevenLabs ConvAI tools live on the ElevenLabs-hosted agent, so warn
        # that the setting will have no effect there.
        if consult is not None and provider == "elevenlabs_convai":
            logger.warning(
                "consult is set but provider is ElevenLabs ConvAI; the consult "
                "tool is only injected in Realtime and Pipeline modes and will "
                "be ignored for this agent."
            )

        # --- Validate handoffs (multi-agent handoff targets) ---
        if handoffs is not None:
            if not isinstance(handoffs, dict):
                raise TypeError(
                    f"handoffs must be a dict of {{name: Agent}}, got "
                    f"{type(handoffs).__name__}."
                )
            for h_name, h_agent in handoffs.items():
                if not isinstance(h_name, str) or not h_name:
                    raise ValueError(
                        "handoffs keys must be non-empty strings (the names the "
                        "LLM passes to handoff_to)."
                    )
                if not isinstance(h_agent, Agent):
                    raise TypeError(
                        f"handoffs[{h_name!r}] must be an Agent (build with "
                        f"phone.agent(...)), got {type(h_agent).__name__}."
                    )
            if provider == "elevenlabs_convai":
                logger.warning(
                    "handoffs is set but provider is ElevenLabs ConvAI; the "
                    "handoff_to tool is only injected in Realtime and Pipeline "
                    "modes and will be ignored for this agent."
                )

        # Apply the documented defaults to slots neither the caller nor an
        # engine marker filled (sentinel ``None`` distinguishes "unset" from an
        # explicit value equal to the default).
        if voice is None:
            voice = "alloy"
        if model is None:
            model = "gpt-realtime-mini"

        return Agent(
            system_prompt=system_prompt,
            voice=voice,
            model=model,
            language=language,
            first_message=first_message,
            llm_error_message=llm_error_message,
            long_turn_message=long_turn_message,
            long_turn_message_after_s=long_turn_message_after_s,
            tools=tuple(tools_out) if tools_out is not None else None,
            provider=provider,
            stt=stt_resolved,
            tts=tts_resolved,
            variables=variables,
            guardrails=tuple(guardrails_out) if guardrails_out is not None else None,
            hooks=hooks,
            text_transforms=(
                tuple(text_transforms) if text_transforms is not None else None
            ),
            vad=vad,
            turn_detector=turn_detector,
            max_semantic_hold_ms=max_semantic_hold_ms,
            audio_filter=audio_filter,
            background_audio=background_audio,
            barge_in_threshold_ms=barge_in_threshold_ms,
            barge_in_mode=barge_in_mode,
            barge_in_confirm_ms=barge_in_confirm_ms,
            aggressive_first_flush=aggressive_first_flush,
            disable_phone_preamble=disable_phone_preamble,
            echo_cancellation=echo_cancellation,
            llm=llm,
            mcp_servers=tuple(mcp_servers) if mcp_servers is not None else None,
            consult=consult,
            handoffs=dict(handoffs) if handoffs is not None else None,
            prewarm_first_message=prewarm_first_message,
            openai_realtime_reasoning_effort=openai_realtime_reasoning_effort,
            openai_realtime_input_audio_transcription_model=openai_realtime_input_audio_transcription_model,
            openai_realtime_noise_reduction=openai_realtime_noise_reduction,
            realtime_turn_detection=realtime_turn_detection,
            realtime_gate_response_on_transcript=realtime_gate_response_on_transcript,
            tool_call_preambles=tool_call_preambles,
            preemptive_generation=preemptive_generation,
            preemptive_min_stable_ms=preemptive_min_stable_ms,
        )

    @staticmethod
    def _unpack_engine(engine: Any) -> tuple[str, dict]:
        """Convert an engine instance to ``(kind, {voice, model, api_key, agent_id})``."""
        from getpatter.engines.elevenlabs import ConvAI as _ConvAI
        from getpatter.engines.openai import Realtime as _Realtime
        from getpatter.engines.openai_realtime_2 import Realtime2 as _Realtime2

        if isinstance(engine, _Realtime2):
            return "openai_realtime_2", {
                "api_key": engine.api_key,
                "voice": engine.voice,
                "model": engine.model,
                "reasoning_effort": engine.reasoning_effort,
                "input_audio_transcription_model": engine.input_audio_transcription_model,
                "noise_reduction": engine.noise_reduction,
                "turn_detection": engine.turn_detection,
                "gate_response_on_transcript": engine.gate_response_on_transcript,
            }
        if isinstance(engine, _Realtime):
            return "openai_realtime", {
                "api_key": engine.api_key,
                "voice": engine.voice,
                "model": engine.model,
                "reasoning_effort": engine.reasoning_effort,
                "input_audio_transcription_model": engine.input_audio_transcription_model,
                "noise_reduction": engine.noise_reduction,
                "turn_detection": engine.turn_detection,
                "gate_response_on_transcript": engine.gate_response_on_transcript,
            }
        if isinstance(engine, _ConvAI):
            return "elevenlabs_convai", {
                "api_key": engine.api_key,
                "agent_id": engine.agent_id,
                "voice": engine.voice,
            }
        raise TypeError(
            "engine= must be an OpenAIRealtime(...), OpenAIRealtime2(...), or "
            f"ElevenLabsConvAI(...) instance, got {type(engine).__name__}"
        )

    @staticmethod
    def _tool_to_dict(tool: Any, *, index: int) -> dict:
        """Normalise a ``Tool`` instance into the internal dict shape.

        Raises ``TypeError`` if *tool* is not a ``Tool`` instance — the legacy
        raw-dict form was removed in v0.5.0.
        """
        from getpatter._public_api import Tool as _Tool

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
        # Propagate strict mode opt-in so downstream Realtime/Pipeline
        # adapters can pass ``strict: true`` to OpenAI. Default False on
        # the Tool dataclass — present in the dict only when explicitly set.
        if getattr(tool, "strict", False):
            out["strict"] = True
        # Propagate reassurance config (string shorthand or dict) so the
        # Realtime stream handler can schedule a filler message during
        # slow tool calls.
        reassurance = getattr(tool, "reassurance", None)
        if reassurance:
            out["reassurance"] = reassurance
        # Propagate the per-tool execution timeout (seconds) so the executor
        # uses it for both the handler and webhook paths instead of the 10s
        # default. Present only when explicitly set (None → default path).
        timeout_s = getattr(tool, "timeout_s", None)
        if timeout_s is not None:
            out["timeout_s"] = timeout_s
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
        local_recording: bool | str = False,
        on_call_start: Callable[[dict], Awaitable[None]] | None = None,
        on_call_end: Callable[[dict], Awaitable[None]] | None = None,
        on_transcript: Callable[[dict], Awaitable[None]] | None = None,
        on_message: Callable[[dict], Awaitable[str]] | str | None = None,
        voicemail_message: str = "",
        on_metrics: Callable[[dict], Awaitable[None]] | None = None,
        dashboard: bool = True,
        dashboard_token: str = "",
        allow_insecure_dashboard: bool = False,
        tunnel: bool = False,
    ) -> None:
        """Start the embedded server for inbound calls.

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
            local_recording: Carrier-neutral local call recording. When
                ``True``, the SDK records each call at the transport as an
                interleaved stereo WAV — left channel = caller, right channel
                = agent — at 16 kHz PCM16, written incrementally to
                ``<call_log_dir>/recording.wav`` when call logging
                (``persist`` / ``PATTER_LOG_DIR``) is enabled, else to
                ``./recordings/<call_id>.wav``. Pass a directory string to
                choose where the WAVs go. Works on every carrier (Twilio,
                Telnyx, Plivo) and every engine mode; independent of the
                carrier-side ``recording`` flag (both can be on). The final
                path is surfaced as ``recording_path`` in the ``on_call_end``
                payload and in the call-log metadata. Default ``False``.
            voicemail_message: If set, spoken as a voicemail message when AMD
                detects a machine (requires machine_detection=True on call()).
            dashboard: When ``True`` (default), serves a local metrics dashboard
                at ``http://localhost:{port}/dashboard``.
            dashboard_token: Optional bearer token for dashboard authentication.
                When set, all dashboard routes require this token.
            allow_insecure_dashboard: Opt-out from the auto-token protection.
                The embedded metrics dashboard and call-data ``/api/*`` routes
                expose call transcripts and metadata (PII). With ``False``
                (default, safe), when the server is reachable beyond
                ``127.0.0.1`` (e.g. via a tunnel or a public ``webhook_url``)
                without a configured ``dashboard_token``, the SDK
                auto-generates a one-time token and mounts the dashboard behind
                it — the startup banner prints the ready-to-use URL with
                ``?token=...``. The dashboard is always available; it just
                requires the printed or configured token. Set ``True`` to serve
                the dashboard fully OPEN (unauthenticated) on a
                publicly-reachable bind (NOT recommended on a public network).
                Loopback-only local dev is unchanged: served open with no
                token. Carrier webhooks and ``/health`` always mount, so calls
                keep working regardless of this flag.
            tunnel: When ``True``, start a cloudflared tunnel automatically.
                Requires ``cloudflared`` binary on PATH. Mutually exclusive
                with ``webhook_url``.
        """
        if not isinstance(agent, Agent):
            raise TypeError(
                f"agent must be an Agent instance, got {type(agent).__name__}. "
                "Use phone.agent() to create one."
            )
        if agent.llm is not None and on_message is not None:
            raise ValueError(
                "Cannot pass both `llm=` on the agent and `on_message=` on serve(). "
                "Pick one — `llm=` for built-in LLMs, `on_message=` for custom logic."
            )
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or port < 1
            or port > 65535
        ):
            raise ValueError(
                f"port must be an integer between 1 and 65535, got {port!r}."
            )
        if not isinstance(recording, bool):
            raise TypeError(
                f"recording must be a bool, got {type(recording).__name__}."
            )
        if not isinstance(local_recording, (bool, str)):
            raise TypeError(
                "local_recording must be a bool or a directory string, got "
                f"{type(local_recording).__name__}."
            )

        # Pre-import AEC at serve startup so the first call doesn't pay
        # the dynamic-import cost on the hot path. ``echo_cancellation``
        # is opt-in and rarely set on PSTN, but when it is the lazy
        # ``from getpatter.audio.aec import NlmsEchoCanceller`` inside
        # the StreamHandler can serialise with first-message TTS startup
        # and eat first-turn latency. Eagerly importing here costs
        # nothing for users who never enable AEC.
        if getattr(agent, "echo_cancellation", False):
            try:
                import getpatter.audio.aec  # noqa: F401
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.debug("AEC pre-import failed at serve(): %s", exc)

        # Resolve webhook_url: tunnel or explicit
        config = self._local_config

        # If Patter(tunnel=CloudflareTunnel()) was passed, route through the
        # same cloudflared auto-start path as ``serve(tunnel=True)``.
        from getpatter.tunnels import CloudflareTunnel as _CFT

        if isinstance(self._tunnel_directive, _CFT) and not tunnel:
            tunnel = True

        if tunnel and config.webhook_url:
            raise ValueError("Cannot use both tunnel=True and webhook_url. Pick one.")

        from getpatter.banner import show_banner

        show_banner()

        if tunnel:
            from getpatter.tunnel import start_tunnel

            try:
                handle = await start_tunnel(port)
                self._tunnel_handle = handle
                # Replace config with the tunnel hostname (frozen dataclass).
                # Mark the assignment as tunnel-owned so ``disconnect()`` can
                # clear it back out without touching explicit ``webhook_url``
                # values that the caller passed at construction time.
                from dataclasses import replace

                config = replace(config, webhook_url=handle.hostname)
                self._local_config = config
                self._tunnel_owns_webhook_url = True
                # Resolve the tunnel-ready future for callers awaiting the
                # public hostname before placing outbound calls.
                self._resolve_tunnel_ready(handle.hostname)
            except Exception as exc:
                self._reject_tunnel_ready(exc)
                raise

        if not config.webhook_url:
            err = ValueError(
                "No webhook_url configured. Either:\n"
                "  - Pass webhook_url in the Patter constructor\n"
                "  - Use tunnel=True in serve() to auto-create a tunnel"
            )
            self._reject_tunnel_ready(err)
            raise err

        from getpatter.server import EmbeddedServer

        self._server = EmbeddedServer(
            config=config,
            agent=agent,
            recording=recording,
            local_recording=local_recording,
            voicemail_message=voicemail_message,
            pricing=self._pricing,
            dashboard=dashboard,
            dashboard_token=dashboard_token,
            allow_insecure_dashboard=allow_insecure_dashboard,
        )
        self._server.on_call_start = on_call_start
        self._server.on_call_end = on_call_end
        self._server.on_transcript = on_transcript
        self._server.on_message = on_message
        self._server.on_metrics = on_metrics
        # Give the server the telemetry client so the per-call ``call_completed``
        # event can be emitted from the call-end path.
        self._server._telemetry = self._telemetry
        # Forward the Patter-level SpeechEvents dispatcher so the per-call
        # StreamHandler can fire turn-taking edges into observers attached
        # via ``phone.on_user_speech_started`` etc. Without this the SDK's
        # ``_emit_*_speech_*`` paths short-circuit on ``self.speech_events
        # is None`` and zero events ever reach the runner's tap.
        self._server.speech_events = self.speech_events
        # Forward the prewarm-audio accessor so the per-call StreamHandler
        # can consume the pre-rendered first-message audio (if any) on
        # ``start``. The server stores a closure rather than a back-ref to
        # avoid a circular reference (Patter → server → Patter).
        self._server.pop_prewarm_audio = self.pop_prewarm_audio  # type: ignore[attr-defined]
        # Forward the parked-connections accessor so the per-call
        # StreamHandler can adopt pre-opened STT / TTS / Realtime WSs at
        # ``start`` instead of paying the cold handshake on first turn.
        self._server.pop_prewarmed_connections = self.pop_prewarmed_connections  # type: ignore[attr-defined]
        self._server.alias_prewarm = self._alias_prewarm  # type: ignore[attr-defined]
        # Forward the waste-recorder so the carrier status / hangup
        # webhook handlers can evict the cache when a call terminates
        # before the media stream starts (no-answer, busy, failed,
        # canceled, or AMD voicemail). Without this, ``_record_prewarm_waste``
        # is only invoked from ``end_call`` — and the server-side teardown
        # path leaks the bytes for the lifetime of the Patter instance.
        # See FIX #91.
        self._server.record_prewarm_waste = self._record_prewarm_waste  # type: ignore[attr-defined]

        # Run uvicorn in a task so we can resolve ``phone.ready`` once it
        # finishes its startup phase. ``server.start()`` itself awaits
        # ``server.serve()`` which blocks until shutdown — so without the
        # task wrapper we'd never get a chance to resolve.
        serve_task = asyncio.create_task(self._server.start(port=port))
        try:
            # Poll uvicorn's ``started`` flag (set after the listen socket
            # is bound and the lifespan startup phase completes).
            deadline_loop = asyncio.get_running_loop()
            start = deadline_loop.time()
            while deadline_loop.time() - start < 30.0:
                if serve_task.done():
                    # Server failed during startup — propagate the error.
                    await serve_task  # raises
                inner = getattr(self._server, "_server", None)
                if inner is not None and getattr(inner, "started", False):
                    break
                await asyncio.sleep(0.05)
            else:
                raise TimeoutError(
                    "Embedded server did not reach 'started' state within 30s"
                )

            # Tunnel reachability self-test: cloudflared returns the URL
            # the moment its control plane has issued it, but the public
            # DNS edge can take several extra seconds to start serving
            # the trycloudflare.com hostname. Until that propagation
            # completes, Twilio (and any other webhook caller) gets HTTP
            # 502 "Unknown host" and the call is torn down before it
            # ever reaches the WS media stream. We block ``phone.ready``
            # until DNS resolves through the public resolvers Twilio's
            # edge uses, then add a short grace window for cloudflared's
            # origin bridge to stabilise. See
            # ``_wait_for_tunnel_publicly_reachable`` for the rationale
            # behind DNS-only vs full HTTP probing.
            if self._tunnel_handle is not None:
                await _wait_for_tunnel_publicly_reachable(config.webhook_url)

            self._resolve_ready(config.webhook_url)
            # A loop is running now — ship any events buffered at construction.
            self._telemetry.flush_pending()
        except BaseException as exc:
            self._reject_ready(exc)
            serve_task.cancel()
            if self._tunnel_handle is not None:
                self._tunnel_handle.stop()
                self._tunnel_handle = None
                from dataclasses import replace as _replace

                self._local_config = _replace(self._local_config, webhook_url="")
                self._tunnel_owns_webhook_url = False
            raise
        await serve_task

    async def test(
        self,
        agent: Agent,
        on_message: Callable[[dict], Awaitable[str]] | None = None,
        on_call_start: Callable[[dict], Awaitable[None]] | None = None,
        on_call_end: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        """Start an interactive terminal test session.

        Simulates a phone call without telephony, STT, or TTS — pure
        text input/output.  When no ``on_message`` handler is provided and
        an ``openai_key`` is configured, the built-in LLM loop is used.

        Args:
            agent: The ``Agent`` to test.
            on_message: Optional message handler (same as ``serve()``).
            on_call_start: Optional call start callback.
            on_call_end: Optional call end callback.
        """
        if not isinstance(agent, Agent):
            raise TypeError(
                f"agent must be an Agent instance, got {type(agent).__name__}."
            )

        from getpatter.test_mode import TestSession

        session = TestSession()
        await session.run(
            agent=agent,
            openai_key=self._local_config.openai_key,
            on_message=on_message,
            on_call_start=on_call_start,
            on_call_end=on_call_end,
        )

    def _attach_span_exporter(self, exporter: Any, *, side: str = "uut") -> None:
        """Wire an OTel span exporter into the SDK's tracer provider.

        Public-but-underscore: consumed by ``patter-agent-runner`` via
        ``getattr(phone, "_attach_span_exporter")``. The leading underscore
        signals it is not part of the customer-facing API surface.

        Args:
            exporter: Any OTel ``SpanExporter`` (e.g. ``InMemorySpanExporter``,
                ``OTLPSpanExporter``, or the runner's ``PatterSpanExporter``).
            side: ``"driver"`` or ``"uut"``. Stamped on every cost/latency
                span emitted during this Patter instance's call lifecycle.
        """
        from getpatter.observability.attributes import attach_span_exporter

        attach_span_exporter(self, exporter, side=side)

    # === Async context manager ===

    async def __aenter__(self) -> "Patter":
        """Enter an async context. Returns ``self`` WITHOUT starting the
        server — ``serve()`` blocks until shutdown, so it cannot run here.

        The value of ``async with`` is the guaranteed ``disconnect()`` on exit
        (see ``__aexit__``): it tears down the embedded server, any auto-started
        tunnel, and in-flight prewarm/TTS work so a still-running TTS WebSocket
        cannot keep the user billed after the block ends. Pattern::

            async with Patter(carrier=Twilio(), phone_number="+15550000000") as phone:
                await phone.serve(agent=agent)          # inbound, or
                result = await phone.call(to="+1555...", agent=agent, wait=True)
            # disconnect() has run here — nothing left running.
        """
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Exit the async context — always tears down via ``disconnect()``.

        Runs on the normal path AND when the body raises, so resources are
        released either way. ``disconnect()`` is idempotent, so an explicit
        ``disconnect()`` inside the block is still safe.
        """
        await self.disconnect()

    async def disconnect(self) -> None:
        """Stop the embedded server and any auto-started tunnel.

        Safe to call multiple times. Leaves the instance reusable: a
        subsequent ``serve()`` works as if the previous lifecycle never
        happened (clears tunnel-owned ``webhook_url`` and recreates the
        ``ready`` / ``tunnel_ready`` Futures).

        Also cancels any in-flight prewarm-first-message synth tasks and
        TTL eviction timers, then clears the prewarm cache. Without this
        a still-running TTS WS keeps the user billed long after SDK
        teardown, and stale entries leak across ``serve`` /
        ``disconnect`` cycles. See FIX #93.
        """
        # Ship buffered telemetry and WAIT for delivery (bounded) before
        # teardown. A fire-and-forget flush here lost the final events of
        # short-lived scripts: the process exited right after disconnect(),
        # the loop closed cancelling the flush task mid-POST, and
        # call_completed (duration/cost/latency) never reached the wire.
        # drain() keeps the instance reusable (no close) — a subsequent
        # serve() still emits.
        await self._telemetry.drain()

        # Cancel and drain any in-flight prewarm work BEFORE tearing the
        # server down so the synth tasks see a clean cancellation point
        # and don't end up writing bytes to a cache we're about to drop.
        for t in list(self._prewarm_tasks):
            t.cancel()
        for t in list(self._prewarm_ttl_tasks.values()):
            t.cancel()
        if self._prewarm_tasks:
            await asyncio.gather(*self._prewarm_tasks, return_exceptions=True)
        if self._prewarm_ttl_tasks:
            await asyncio.gather(
                *self._prewarm_ttl_tasks.values(), return_exceptions=True
            )
        self._prewarm_tasks.clear()
        self._prewarm_ttl_tasks.clear()
        self._prewarm_audio.clear()
        self._prewarm_consumed.clear()
        # Cancel parked-connection TTL tasks and force-close any
        # remaining parked sockets so we don't leak across
        # ``serve`` / ``disconnect`` cycles.
        for t in list(self._prewarmed_conn_tasks.values()):
            if not t.done():
                t.cancel()
        if self._prewarmed_conn_tasks:
            await asyncio.gather(
                *self._prewarmed_conn_tasks.values(), return_exceptions=True
            )
        self._prewarmed_conn_tasks.clear()
        for slot in list(self._prewarmed_connections.values()):
            for handle in slot.values():
                try:
                    await _safe_close_handle(handle)
                except Exception:
                    pass
        self._prewarmed_connections.clear()
        if self._server:
            # Fail any in-flight call(wait=True) awaiters before the server
            # goes away — otherwise they'd hang until their backstop timeout
            # since no terminal signal can reach a stopped server.
            completions = getattr(self._server, "_completions", None)
            if isinstance(completions, dict) and completions:
                for fut in list(completions.values()):
                    if not fut.done():
                        fut.set_exception(
                            PatterConnectionError(
                                "Patter.disconnect() called while a "
                                "call(wait=True) was still in flight."
                            )
                        )
                completions.clear()
            await self._server.stop()
            self._server = None
        if self._tunnel_handle:
            self._tunnel_handle.stop()
            self._tunnel_handle = None
        # Clear tunnel-owned hostname so the next ``serve()`` does not trip
        # the ``Cannot use both tunnel=True and webhook_url`` guard. Static /
        # explicit ``webhook_url`` values stay in place — they were not ours
        # to drop.
        if self._tunnel_owns_webhook_url:
            from dataclasses import replace

            self._local_config = replace(self._local_config, webhook_url="")
            self._tunnel_owns_webhook_url = False
        # Drop the deferred handles so a follow-up ``serve()`` recreates them
        # fresh. Without this, the next ``await phone.ready`` would resolve
        # immediately with the stale hostname from the previous lifecycle.
        self._ready = None
        self._tunnel_ready = None
        if self._local_config.webhook_url:
            self._tunnel_ready_pre_resolved = self._local_config.webhook_url
        else:
            self._tunnel_ready_pre_resolved = None

    def pop_prewarm_audio(self, call_id: str) -> bytes | None:
        """Pop and return the pre-synthesised first-message audio for ``call_id``.

        Returns ``None`` when ``agent.prewarm_first_message`` was not set
        for the originating outbound call, or when the synth was still in
        flight at the moment the carrier emitted ``start`` (treated as a
        prewarm miss — the StreamHandler falls back to live TTS).

        Called by the per-call StreamHandler at the start of the
        firstMessage emit. Returning bytes here lets the handler skip the
        live TTS synthesis and stream the cached buffer directly.

        Marks ``call_id`` as consumed regardless of cache hit/miss so a
        slow synth task that finishes after this call drops its bytes
        instead of orphaning them in ``_prewarm_audio``. See FIX #92.
        """
        self._prewarm_consumed.add(call_id)
        # Cancel any pending TTL eviction — the slot is being drained
        # naturally now.
        ttl = self._prewarm_ttl_tasks.pop(call_id, None)
        if ttl is not None and not ttl.done():
            ttl.cancel()
        return self._prewarm_audio.pop(call_id, None)

    def _record_prewarm_waste(self, call_id: str) -> None:
        """Log a WARN if a prewarmed greeting was paid for but never used.

        Called from :meth:`disconnect`, :meth:`end_call`, and from the
        carrier status / hangup webhook handlers when a call terminates
        before the media stream starts. The TTS bill for
        ``agent.first_message`` has already been incurred by the
        background synth task, so the user should know — opt-in feature
        with a known cost surface.

        Idempotent: the second call for the same ``call_id`` is a no-op,
        so the status callback firing first and ``end_call`` running
        afterwards (or vice-versa) does not double-WARN.
        """
        # Always drain any parked provider WS — they're cheap to discard
        # and we don't want to leak open sockets when the call dies.
        self.close_prewarmed_connections(call_id)
        # Idempotency guard — once consumed (cache hit, cache miss, or a
        # prior waste record) the slot is gone and there is nothing to
        # warn about a second time.
        if call_id in self._prewarm_consumed:
            self._prewarm_audio.pop(call_id, None)
            return
        self._prewarm_consumed.add(call_id)
        ttl = self._prewarm_ttl_tasks.pop(call_id, None)
        if ttl is not None and not ttl.done():
            ttl.cancel()
        bytes_ = self._prewarm_audio.pop(call_id, None)
        if bytes_:
            logger.warning(
                "Prewarm wasted for call %s — first-message TTS already paid "
                "(~%d bytes synthesised) but call ended before pickup.",
                call_id,
                len(bytes_),
            )

    async def end_call(self, call_sid: str) -> None:
        """Terminate an active call on the configured carrier.

        Posts a hangup to the carrier (Twilio
        ``Calls(call_sid).update(status='completed')`` or Telnyx
        ``/v2/calls/{call_control_id}/actions/hangup``) so the bridge tears
        down gracefully — the SDK's WebSocket handler then fires
        ``on_call_end`` with the final ``CallMetrics`` before the WS closes.

        Use this when the host application needs to end a call programmatically
        without going through the LLM tool-call path (e.g. an admin override,
        a watchdog, or an integration test runner).

        Args:
            call_sid: Carrier-issued call identifier (Twilio Call SID or
                Telnyx call_control_id) returned from a previous
                ``Patter.call(...)`` or captured in the ``on_call_start``
                callback's payload.

        Raises:
            ValueError: ``call_sid`` is empty or no carrier is configured.
        """
        if not call_sid:
            raise ValueError("call_sid must be a non-empty string")
        # If the call had a prewarmed first-message that was never consumed
        # (call ended before pickup), surface the wasted spend to the user.
        self._record_prewarm_waste(call_sid)
        cfg = self._local_config
        telephony = cfg.telephony_provider
        if telephony == "twilio":
            if not cfg.twilio_sid or not cfg.twilio_token:
                raise ValueError(
                    "Twilio credentials not configured on this Patter instance"
                )
            from twilio.rest import Client  # type: ignore[import-not-found]

            client = Client(cfg.twilio_sid, cfg.twilio_token)
            await asyncio.to_thread(
                lambda: client.calls(call_sid).update(status="completed")
            )
        elif telephony == "telnyx":
            if not cfg.telnyx_key:
                raise ValueError(
                    "Telnyx credentials not configured on this Patter instance"
                )
            import telnyx  # type: ignore[import-not-found]

            api = telnyx.api_requestor.APIRequestor(api_key=cfg.telnyx_key)
            await asyncio.to_thread(
                lambda: api.request("post", f"/v2/calls/{call_sid}/actions/hangup")
            )
        elif telephony == "plivo":
            if not cfg.plivo_auth_id or not cfg.plivo_auth_token:
                raise ValueError(
                    "Plivo credentials not configured on this Patter instance"
                )
            from getpatter.providers.plivo_adapter import PlivoAdapter

            adapter = PlivoAdapter(
                auth_id=cfg.plivo_auth_id, auth_token=cfg.plivo_auth_token
            )
            try:
                await adapter.end_call(call_sid)
            finally:
                await adapter.close()
        else:
            raise ValueError(
                f"end_call() requires a configured carrier; got telephony_provider={telephony!r}"
            )


async def _wait_for_tunnel_publicly_reachable(
    hostname: str,
    total_timeout_s: float = 30.0,
    grace_s: float = 5.0,
) -> None:
    """Wait for a freshly-minted cloudflared quick-tunnel hostname to be
    publicly resolvable, bypassing the OS resolver cache.

    Queries 1.1.1.1 (Cloudflare) and 8.8.8.8 (Google) directly over UDP
    instead of going through the OS resolver — this is necessary because
    macOS mDNSResponder caches NXDOMAIN aggressively, so the first
    ``getaddrinfo`` call after a fresh tunnel comes up keeps returning
    ENOTFOUND long after the public DNS edge has the record. The
    public-resolver path is also the one Twilio's edge takes, so a
    positive result here is a true proxy for "Twilio can reach us".

    Why DNS-only and not full HTTP: trycloudflare quick tunnels frequently
    fail same-host loopback (the local machine resolving its own tunnel
    back through Cloudflare's edge can race NAT / IPv4 vs IPv6 resolver
    paths) even when the URL is reachable from external hosts. Twilio's
    edge resolves the hostname from public DNS — so DNS resolution is the
    right proxy for "Twilio can reach us".

    Why the grace window: between "DNS resolves" and "cloudflared origin
    bridge is ready to forward HTTP/WSS", there is a 1–4 s gap during
    which Cloudflare returns 502 on HTTP and silently drops WSS upgrades.
    The HTTP path is usually ready first; the WSS upgrade path takes
    longer because it goes through a different cloudflared edge route.
    Empirically 5 s covers >99 % of cases (was 2.5 s, dropped failure
    rate from ~5 % to <1 % — see BUGS.md 2026-05-06 cartesia-openai-openai
    attempt 1 entry).

    Without this guard, Twilio races the propagation and the first call
    is silently torn down: HTTP webhooks succeed (`/voice` TwiML, AMD
    callback) but Twilio's WSS upgrade for the media stream fails, the
    call drops at pickup with no audio.
    """
    import struct

    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout_s
    attempt = 0
    last_err: BaseException | None = None

    def _resolve_one(server: str) -> str | None:
        """Send a minimal A-record DNS query to *server*:53 and return
        the first IPv4 address from the answer section, or None if the
        server returned NXDOMAIN / NOERROR-no-answer."""
        import socket as _socket

        # Build a minimal DNS query: 12-byte header + qname + qtype/qclass.
        txid = 0x4242
        flags = 0x0100  # standard query, recursion desired
        header = struct.pack(">HHHHHH", txid, flags, 1, 0, 0, 0)
        qname = (
            b"".join(
                bytes([len(part)]) + part.encode("ascii")
                for part in hostname.split(".")
            )
            + b"\x00"
        )
        question = qname + struct.pack(">HH", 1, 1)  # A, IN
        packet = header + question

        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            sock.settimeout(2.0)
            sock.sendto(packet, (server, 53))
            data, _ = sock.recvfrom(4096)
        finally:
            sock.close()

        # Parse: skip header + question, walk answer RRs looking for type=1.
        if len(data) < 12:
            return None
        ancount = struct.unpack(">H", data[6:8])[0]
        if ancount == 0:
            return None
        # Skip question section (qname is compressed-free here, terminates at \x00).
        pos = 12
        while pos < len(data) and data[pos] != 0:
            pos += data[pos] + 1
        pos += 1 + 4  # null terminator + qtype/qclass
        # Walk answer RRs.
        for _ in range(ancount):
            # Name (may be a compression pointer 0xc0xx).
            if data[pos] & 0xC0 == 0xC0:
                pos += 2
            else:
                while pos < len(data) and data[pos] != 0:
                    pos += data[pos] + 1
                pos += 1
            rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[pos : pos + 10])
            pos += 10
            if rtype == 1 and rdlen == 4:  # A record
                addr = ".".join(str(b) for b in data[pos : pos + 4])
                return addr
            pos += rdlen
        return None

    def _resolve_via_public_dns() -> str | None:
        for server in ("1.1.1.1", "8.8.8.8"):
            try:
                addr = _resolve_one(server)
                if addr:
                    return addr
            except Exception:
                continue
        return None

    while loop.time() < deadline:
        attempt += 1
        try:
            addr = await loop.run_in_executor(None, _resolve_via_public_dns)
            if addr:
                logger.info(
                    "Tunnel DNS resolved → %s (attempt %d); waiting %.1fs grace",
                    addr,
                    attempt,
                    grace_s,
                )
                await asyncio.sleep(grace_s)
                return
            last_err = RuntimeError("no A record returned")
        except Exception as err:
            last_err = err
        # Backoff: 250 ms, 400 ms, 640 ms, 1.0 s, capped at 2 s.
        delay = min(0.25 * (1.6 ** (attempt - 1)), 2.0)
        await asyncio.sleep(delay)
    raise TimeoutError(
        f"Tunnel hostname {hostname} did not resolve within "
        f"{total_timeout_s}s. Last error: {last_err!r}"
    )
