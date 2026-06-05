"""Embedded HTTP/WebSocket server for local mode."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import os
import re
import signal
import time
import uuid
from collections import defaultdict
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Response, WebSocket

from getpatter.local_config import LocalConfig
from getpatter.models import Agent, MachineDetectionResult
from getpatter.services.call_log import (
    CallLogger,
    alog_call_end,
    alog_call_start,
    alog_turn,
    resolve_log_root,
)
from getpatter.utils.log_sanitize import mask_phone_number, sanitize_log_value

logger = logging.getLogger("getpatter")

# Hostnames that resolve to private/internal infrastructure even when not
# literal IPs.  Mirrors libraries/typescript/src/server.ts:117-124.
_BLOCKED_WEBHOOK_HOSTNAMES = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.azure.com",
    }
)

# Maximum concurrent WebSocket connections allowed from a single client IP.
# Mirrors libraries/typescript/src/server.ts:1041 (MAX_WS_PER_IP = 10).
MAX_WS_PER_IP = 10

# Hosts that are loopback-only (not reachable from another machine). Used by
# the dashboard auto-token gate to decide whether the server is "exposed".
# A non-empty ``webhook_url`` or ``PATTER_BIND_HOST`` whose hostname is NOT in
# this set is treated as publicly reachable. Mirrors the TS counterpart.
_LOOPBACK_HOSTS = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "::1",
        "::ffff:127.0.0.1",
    }
)


def _is_loopback_host(host: str) -> bool:
    """Return True when *host* refers to loopback only (not externally reachable).

    Strips an optional scheme, ``[...]`` IPv6 brackets, and a trailing port,
    then matches against :data:`_LOOPBACK_HOSTS` and the full ``127.0.0.0/8``
    range. A ``webhook_url`` like ``"abc.trycloudflare.com"`` returns ``False``
    (publicly reachable); ``"127.0.0.1:8000"`` returns ``True``. Mirrors the
    TypeScript ``isLoopbackHost`` byte-for-byte so the exposure gate classifies
    hosts identically across SDKs.
    """
    if not host:
        return True
    raw = host.strip()
    # Drop scheme if present (webhook_url is normally bare hostname, but be safe)
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    # Drop any path / query
    raw = raw.split("/", 1)[0]
    raw = raw.lower()
    # IPv6 in brackets, optionally with a port: ``[::1]:8000``
    if raw.startswith("["):
        inner = raw[1:].split("]", 1)[0]
        return inner in _LOOPBACK_HOSTS
    # Strip a trailing :port for IPv4 / hostname (a bare IPv6 has many colons)
    if raw.count(":") == 1:
        raw = raw.split(":", 1)[0]
    if raw in _LOOPBACK_HOSTS:
        return True
    # The entire 127.0.0.0/8 block is loopback (RFC 1122), not just 127.0.0.1.
    parts = raw.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        try:
            octets = [int(p) for p in parts]
        except ValueError:  # pragma: no cover - defensive
            return False
        if octets[0] == 127 and all(0 <= o <= 255 for o in octets):
            return True
    return False


def validate_webhook_url(url: str) -> bool:
    """Return True when *url* is safe to fetch (SSRF protection).

    Blocks:
      * Non-HTTP(S) schemes (``file:``, ``javascript:``, etc.)
      * IPv4 loopback / private / link-local / reserved ranges
        (127/8, 10/8, 172.16/12, 192.168/16, 169.254/16, 0/8)
      * IPv6 loopback (``::1``, ``::``), unique-local ``fc00::/7`` and
        link-local ``fe80::/10``
      * Localhost aliases and cloud-metadata hostnames

    Mirrors :func:`validateWebhookUrl` in
    ``libraries/typescript/src/server.ts`` and the existing
    :func:`getpatter.tools.tool_executor._validate_webhook_url`.
    Returns False rather than raising so callers can decide how to react.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    raw_host = parsed.hostname or ""
    if not raw_host:
        return False
    host = raw_host.strip("[]").lower()
    if host in _BLOCKED_WEBHOOK_HOSTNAMES:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Hostname (not a literal IP) — DNS resolution at fetch time can
        # still hit private space, but we avoid blocking the event loop
        # with a sync resolver here.  This matches the TS counterpart.
        return True
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return False
    return True


def _client_ip_for_ws(websocket: WebSocket) -> str:
    """Best-effort remote IP for a WebSocket, normalising IPv4-mapped IPv6."""
    client = websocket.client
    if client is None:
        return "unknown"
    raw = client.host or "unknown"
    return re.sub(r"^::ffff:", "", raw)


def _classify_twilio_amd(answered_by: str) -> str:
    """Map a Twilio ``AnsweredBy`` value to the carrier-agnostic
    classification (``human`` / ``machine`` / ``fax`` / ``unknown``).

    Anything unrecognised collapses to ``unknown`` rather than raising —
    Twilio occasionally adds new AMD outcomes and we don't want a webhook
    to 500 because of an unknown enum value. Mirrors the TS helper in
    ``libraries/typescript/src/server.ts``.
    """
    if answered_by == "human":
        return "human"
    if answered_by.startswith("machine_"):
        return "machine"
    if answered_by == "fax":
        return "fax"
    return "unknown"


def _classify_telnyx_amd(result: str) -> str:
    """Map a Telnyx ``call.machine.detection.ended.result`` value to the
    carrier-agnostic classification. Telnyx uses ``human`` / ``machine``
    (and historically ``machine_detected``) / ``not_sure`` / ``fax``.
    Mirrors the TS helper in ``libraries/typescript/src/server.ts``.
    """
    if result == "human":
        return "human"
    if result in ("machine", "machine_detected"):
        return "machine"
    if result == "fax":
        return "fax"
    return "unknown"


def _twilio_status_to_outcome(call_status: str) -> str:
    """Map a no-media Twilio terminal ``CallStatus`` to a ``CallResult``
    outcome. Only called for statuses that imply the call never reached the
    media stream (``no-answer`` / ``busy`` / ``failed`` / ``canceled``);
    connected calls resolve via ``on_call_end`` instead.
    """
    s = (call_status or "").lower()
    if s == "no-answer":
        return "no_answer"
    if s == "busy":
        return "busy"
    return "failed"  # failed / canceled / any other terminal no-media status


def _telnyx_hangup_outcome(cause: str) -> str | None:
    """Map a Telnyx ``hangup_cause`` to a no-media ``CallResult`` outcome, or
    ``None`` when the cause implies the call connected (``normal_clearing``).

    Connected calls return ``None`` here so they resolve via ``on_call_end``
    with the full transcript + metrics rather than being prematurely closed
    as a no-media outcome.
    """
    c = (cause or "").lower()
    if c in ("no_answer", "timeout", "no_user_response"):
        return "no_answer"
    if c in ("user_busy", "busy"):
        return "busy"
    if c in ("call_rejected", "rejected", "destination_out_of_order"):
        return "failed"
    return None


def _validate_telnyx_signature(
    raw_body: bytes,
    signature: str,
    timestamp: str,
    public_key: str,
    tolerance_sec: int = 300,
) -> bool:
    """Verify a Telnyx webhook Ed25519 signature.

    Signed payload is ``timestamp + "|" + raw_body``. Returns False when any
    step fails (missing deps, bad base64, stale timestamp, bad signature).
    """
    if not signature or not timestamp or not public_key:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    # Telnyx sends ``telnyx-timestamp`` as seconds since epoch (per docs:
    # https://developers.telnyx.com/docs/messaging/webhooks#webhook-signing).
    # Heuristic: any value below 1e12 is seconds (a 2026 epoch in seconds is
    # ~1.77e9, while milliseconds is ~1.77e12), so promote to ms before
    # comparing. Stays correct if Telnyx ever switches the unit.
    ts_ms = ts * 1000 if ts < 1_000_000_000_000 else ts
    now_ms = int(time.time() * 1000)
    age_ms = now_ms - ts_ms
    # ``abs`` tolerates small negative skew (timestamp slightly ahead of the
    # local clock when the webhook host is a touch behind Telnyx) while still
    # enforcing the ±tolerance anti-replay window.
    if abs(age_ms) > tolerance_sec * 1000:
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.serialization import load_der_public_key
    except ImportError:
        logger.warning(
            "cryptography package not installed — cannot verify Telnyx signature. "
            "Install with: pip install cryptography"
        )
        return False
    try:
        key_bytes = base64.b64decode(public_key)
        key = load_der_public_key(key_bytes)
        payload = timestamp.encode("utf-8") + b"|" + raw_body
    except (ValueError, TypeError):
        return False
    except Exception:
        return False
    # The telnyx-signature-ed25519 header may contain multiple
    # comma-separated signatures during key rotation.  Accept the webhook
    # if any one of them verifies.  Fail-closed when none match.
    for raw_sig in signature.split(","):
        raw_sig = raw_sig.strip()
        if not raw_sig:
            continue
        try:
            sig_bytes = base64.b64decode(raw_sig)
            key.verify(sig_bytes, payload)
            return True
        except (InvalidSignature, ValueError, TypeError):
            continue
        except Exception:
            continue
    return False


def _classify_plivo_amd(result: str) -> str:
    """Map a Plivo AMD result to the carrier-agnostic classification
    (``human`` / ``machine`` / ``fax`` / ``unknown``).

    Plivo's async machine-detection callback reports the outcome via a
    result field; values vary by API version so we match the common shapes
    defensively (``human`` / ``person`` → human, anything starting with
    ``machine`` plus ``answering_machine`` / ``amd`` / ``true`` → machine).
    Anything unrecognised collapses to ``unknown`` rather than raising.
    Mirrors the TS helper in ``libraries/typescript/src/server.ts``.
    """
    r = (result or "").strip().lower()
    if r in ("human", "person"):
        return "human"
    if r.startswith("machine") or r in ("answering_machine", "amd", "true"):
        return "machine"
    if r == "fax":
        return "fax"
    return "unknown"


def _validate_plivo_signature(
    url: str,
    nonce: str,
    signature: str,
    auth_token: str,
    params: dict | None = None,
    method: str = "POST",
) -> bool:
    """Verify a Plivo V3 webhook signature.

    Mirrors the algorithm in plivo-python's ``signature_v3`` module:

    * **POST**: ``signed = url + sorted_post_params + "." + nonce`` where
      POST params are sorted alphabetically by key (case-sensitive) and
      concatenated as ``key1value1key2value2…`` with no delimiters.
    * **GET**:  ``signed = url + "." + nonce`` — query params live in the
      URL already, so no separate concatenation.

    HMAC-SHA256 keyed on the account ``auth_token``, base64-encoded. The
    ``X-Plivo-Signature-V3`` header may carry multiple comma-separated
    signatures during key rotation; accept if any matches. Returns False
    when any step fails (missing inputs, no match).
    """
    if not signature or not nonce or not auth_token:
        return False
    import hashlib
    import hmac

    base = url
    if method.upper() == "POST" and params:
        # Plivo SDK ``get_sorted_params_string``: sort keys, concat ``k+v``.
        base += "".join(f"{k}{params[k]}" for k in sorted(params))
    signed = f"{base}.{nonce}"
    expected = base64.b64encode(
        hmac.new(
            auth_token.encode("utf-8"),
            signed.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    for raw_sig in signature.split(","):
        raw_sig = raw_sig.strip()
        if raw_sig and hmac.compare_digest(raw_sig, expected):
            return True
    return False


class EmbeddedServer:
    """Self-contained server that handles Twilio/Telnyx webhooks and streams.

    Usage::

        server = EmbeddedServer(config=local_cfg, agent=my_agent)
        server.on_call_start = my_start_handler
        await server.start(port=8000)
    """

    def __init__(
        self,
        config: LocalConfig,
        agent: Agent,
        recording: bool = False,
        voicemail_message: str = "",
        pricing: dict | None = None,
        dashboard: bool = True,
        dashboard_token: str = "",
        allow_insecure_dashboard: bool = False,
    ) -> None:
        self.config = config
        self.agent = agent
        self.recording = recording
        self.voicemail_message = voicemail_message
        self.pricing = pricing
        self.dashboard = dashboard
        self.dashboard_token = dashboard_token
        # Opt-out from the auto-token protection. When ``True``, the dashboard +
        # call-data API routes are served fully OPEN (no token) even on a
        # publicly-reachable bind, with a loud WARNING. Default ``False`` =>
        # when the server is reachable beyond loopback without a configured
        # ``dashboard_token``, the SDK auto-generates a one-time token so the
        # dashboard is always available but protected with zero config. See
        # ``_dashboard_is_exposed`` and the token resolution in ``_create_app``.
        self.allow_insecure_dashboard = allow_insecure_dashboard
        # The dashboard token actually in effect for this process — resolved in
        # ``_create_app``: the configured ``dashboard_token`` when set, an
        # auto-generated UUID when the bind is exposed and no token was given
        # (unless ``allow_insecure_dashboard``), or ``""`` (OPEN) for loopback
        # local-dev / the insecure opt-out. Read by the startup banner (to print
        # the ready URL incl. ``?token=``) and by tests to authenticate.
        self._effective_dashboard_token = ""
        self._server = None
        self._app = None
        self._active_connections: set[WebSocket] = set()
        self._shutting_down = False
        self.on_call_start = None
        self.on_call_end = None
        self.on_transcript = None
        self.on_message = None
        self.on_metrics = None
        # Per-call AMD result callback set by ``Patter.call()`` for the most
        # recent outbound call. Cleared after firing once per call so a result
        # for a previous call cannot leak into a new caller's callback.
        #
        # ``on_machine_detection`` is the legacy single-slot fallback (last
        # registered callback). ``on_machine_detection_by_call_sid`` keys the
        # callback by the carrier-issued call id (Twilio CallSid / Telnyx
        # call_control_id / Plivo CallUUID) so concurrent outbound calls do
        # not clobber each other's callbacks. Parity with the TypeScript
        # ``onMachineDetectionByCallSid`` map. The webhook handlers prefer the
        # per-call entry and fall back to the single-slot callback; both are
        # one-shot (deleted after firing).
        self.on_machine_detection = None
        self.on_machine_detection_by_call_sid: dict = {}
        # Per-call_id completion futures for ``Patter.call(wait=True)``.
        # Resolved by the FIRST terminal signal: the Twilio/Telnyx status
        # callback for no-media outcomes (no-answer / busy / failed), or
        # ``on_call_end`` for a connected call (answered / voicemail). The
        # AMD classification is recorded per call_id so the connected-call
        # path can distinguish ``answered`` from ``voicemail``. This is what
        # lets ``call(wait=True)`` return a structured ``CallResult`` without
        # the caller hand-wiring ``on_call_end`` to an ``asyncio.Event``.
        self._completions: dict[str, asyncio.Future] = {}
        self._amd_class: dict[str, str] = {}
        # Pre-warm first-message audio accessor wired by ``Patter.serve()``.
        # The per-call StreamHandler invokes this with its ``call_id`` at the
        # start of the firstMessage emit; a non-None return is sent verbatim
        # in place of running TTS again. ``None`` means "no prewarm cache for
        # this call — fall back to live synthesis". Default is a no-op so
        # callers that instantiate ``EmbeddedServer`` directly (tests) work
        # without further setup.
        self.pop_prewarm_audio = lambda _cid: None
        # Pre-warmed provider WebSocket accessor wired by
        # ``Patter.serve()``. The per-call StreamHandler invokes this
        # with its ``call_id`` at pipeline init; a defined return hands
        # off pre-opened STT / TTS / Realtime sockets so the live first
        # turn skips the cold-handshake. ``None`` means "no parked
        # sockets — fall back to fresh ``connect()``".
        self.pop_prewarmed_connections = lambda _cid: None
        # Prewarm waste recorder wired by ``Patter.serve()``. Invoked from
        # the Twilio status callback (no-answer / busy / failed / canceled)
        # and the Telnyx call.hangup / AMD-machine handlers so the cache
        # entry is evicted when the call terminates before the media stream
        # starts. Default is a no-op so direct ``EmbeddedServer`` callers
        # (tests) work without further setup. See FIX #91.
        self.record_prewarm_waste = lambda _cid: None
        self._telnyx_sig_warning_logged = False
        self._metrics_store = None
        # Opt-in per-call filesystem logging. Path is resolved by
        # ``client.py`` from the public ``Patter(persist=...)`` option
        # (with the legacy ``PATTER_LOG_DIR`` env var as fallback). When
        # ``config.persist_root`` is ``None`` the logger is a no-op.
        # Callers that bypass ``client.py`` and instantiate the server
        # directly fall back to the env-var resolver.
        log_root = (
            config.persist_root
            if getattr(config, "persist_root", None) is not None
            else resolve_log_root()
        )
        self._persist_root = log_root  # remember for hydrate() in serve()
        self._call_logger = CallLogger(log_root)
        # Per-client-IP active WebSocket counter for DoS protection.
        # Mirrors TS server.ts:1042 (wsConnectionsByIp).
        self._ws_conn_counts: defaultdict[str, int] = defaultdict(int)

    @property
    def effective_dashboard_token(self) -> str:
        """The dashboard token in effect for this process.

        Resolved in :meth:`_create_app`. An empty string means the dashboard
        is served OPEN (loopback-only local dev, or the
        ``allow_insecure_dashboard`` opt-out); a non-empty value is required to
        authenticate (the explicit ``dashboard_token`` or an auto-generated
        one when the bind is exposed). Public mirror of the TypeScript SDK's
        ``EmbeddedServer.resolvedDashboardToken`` getter — see sdk-parity.
        """
        return self._effective_dashboard_token

    # === Outbound completion registry (call(wait=True)) ===

    def register_completion(self, call_id: str) -> "asyncio.Future":
        """Register (or return) a completion future for an outbound call.

        Called by ``Patter.call(wait=True)`` immediately after the carrier
        accepts the dial — the future resolves to a ``CallResult`` once a
        terminal signal arrives. Idempotent: returns the existing pending
        future if one is already registered for ``call_id``.
        """
        existing = self._completions.get(call_id)
        if existing is not None and not existing.done():
            return existing
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._completions[call_id] = fut
        return fut

    def _resolve_completion(
        self,
        call_id: str,
        *,
        outcome: str,
        status: str,
        data: dict | None = None,
    ) -> None:
        """Resolve a pending completion future with a ``CallResult``.

        No-op when no future is registered for ``call_id`` (the common case —
        most calls are placed without ``wait=True``) or it is already done.
        Builds the result from the ``on_call_end`` payload when ``data`` is
        provided (connected calls carry transcript + ``CallMetrics``); no-media
        outcomes pass ``data=None`` and yield an empty transcript / no cost.
        """
        # Drop any AMD callback that never fired (human answer, no-media
        # outcome, …) so the per-call map does not leak. Runs for every
        # terminal signal — connected via ``_on_call_end`` and no-media via
        # the carrier status callbacks — regardless of ``wait``.
        if call_id:
            self.on_machine_detection_by_call_sid.pop(call_id, None)
        fut = self._completions.get(call_id)
        if fut is None or fut.done():
            return
        from getpatter.models import CallResult

        metrics = data.get("metrics") if data else None
        cost = getattr(metrics, "cost", None)
        duration = float(getattr(metrics, "duration_seconds", 0.0) or 0.0)
        transcript = tuple(data.get("transcript", ()) or ()) if data else ()
        fut.set_result(
            CallResult(
                call_id=call_id,
                outcome=outcome,  # type: ignore[arg-type]
                status=status,
                duration_seconds=duration,
                transcript=transcript,
                cost=cost,
                metrics=metrics if metrics is not None else None,
            )
        )
        self._completions.pop(call_id, None)
        self._amd_class.pop(call_id, None)

    async def _fire_machine_detection(
        self, call_sid: str, result: MachineDetectionResult
    ) -> None:
        """Fire the per-call AMD callback exactly once.

        Prefers the per-callSid entry registered by ``Patter.call()`` so
        concurrent outbound calls do not clobber each other; falls back to
        the legacy single-slot ``on_machine_detection``. Both are one-shot —
        the map entry is removed after firing. User-code exceptions are
        swallowed (logged) so a throwing callback never breaks webhook
        delivery (carriers retry on non-2xx).
        """
        cb = None
        if call_sid:
            cb = self.on_machine_detection_by_call_sid.pop(call_sid, None)
        if cb is None:
            cb = self.on_machine_detection
        if cb is None:
            return
        try:
            cb_ret = cb(result)
            if asyncio.iscoroutine(cb_ret):
                await cb_ret
        except Exception as exc:
            logger.warning("on_machine_detection callback threw: %s", exc)

    def _wrap_callbacks(self):
        """Return (on_call_start, on_call_end, on_metrics, on_transcript_line) wrappers.

        Each wrapper feeds data into the dashboard store first, then calls
        the user-provided callback (if any).  Completed calls are also
        persisted to ``~/.patter/data/calls.jsonl`` and pushed to any
        running standalone dashboard.
        """
        store = self._metrics_store
        user_start = self.on_call_start
        user_end = self.on_call_end
        user_metrics = self.on_metrics
        call_logger = self._call_logger
        agent = self.agent

        def _agent_snapshot() -> dict:
            """Serialise minimal agent identity for log metadata."""
            provider = getattr(agent, "provider", None)
            engine = getattr(agent, "engine", None)
            engine_kind = getattr(engine, "kind", None) if engine is not None else None
            snapshot: dict = {
                "provider": provider,
                "engine": engine_kind,
                "model": getattr(agent, "model", None),
                "voice": getattr(agent, "voice", None),
                "language": getattr(agent, "language", None),
            }
            if (
                getattr(agent, "stt", None) is not None
                and getattr(agent, "tts", None) is not None
                and engine is None
            ):
                snapshot["mode"] = "pipeline"
            return {k: v for k, v in snapshot.items() if v is not None}

        async def _on_call_start(data):
            if store is not None:
                store.record_call_start(data)
            # Notify standalone dashboard so active calls appear immediately.
            # Fire-and-forget via ``asyncio.create_task`` so the call_start
            # fast path never blocks on dashboard responsiveness — even when
            # the dashboard is offline (~1s connect timeout).
            try:
                from getpatter.dashboard.persistence import notify_dashboard

                asyncio.create_task(notify_dashboard(data))
            except Exception:
                pass
            if call_logger.enabled:
                # For outbound calls the bridge has no caller/callee in the
                # WS query string (TwiML for outbound is inline
                # ``<Stream url=".../outbound"/>`` with no <Parameter> tags),
                # so ``data["caller"]`` / ``data["callee"]`` are empty here.
                # The active record in the store was populated by
                # ``record_call_initiated`` at dial time and holds the correct
                # numbers — pull them from there before persisting
                # metadata.json. Without this fallback every outbound call's
                # metadata.json on disk has ``caller=""`` / ``callee=""``.
                call_id_str = data.get("call_id", "") or ""
                data_caller = data.get("caller", "") or ""
                data_callee = data.get("callee", "") or ""
                active_record = (
                    store.get_active(call_id_str) if (store and call_id_str) else None
                ) or {}
                resolved_caller = data_caller or active_record.get("caller", "") or ""
                resolved_callee = data_callee or active_record.get("callee", "") or ""
                await alog_call_start(
                    call_logger,
                    call_id_str,
                    caller=resolved_caller,
                    callee=resolved_callee,
                    direction=(
                        data.get("direction")
                        or active_record.get("direction")
                        or "inbound"
                    ),
                    telephony_provider=data.get("telephony_provider", "") or "",
                    provider_mode=getattr(agent, "provider", "") or "",
                    agent=_agent_snapshot(),
                )
            if user_start is not None:
                return await user_start(data)
            return None

        async def _on_call_end(data):
            if store is not None:
                store.record_call_end(data, metrics=data.get("metrics"))
            # Notify standalone dashboard (if running). Fire-and-forget via
            # ``asyncio.create_task`` so the call_end path never blocks on
            # dashboard responsiveness.
            try:
                from getpatter.dashboard.persistence import notify_dashboard

                asyncio.create_task(notify_dashboard(data))
            except Exception:
                pass
            if call_logger.enabled:
                from dataclasses import asdict, is_dataclass

                metrics_obj = data.get("metrics")
                duration = (
                    getattr(metrics_obj, "duration_seconds", None)
                    if metrics_obj
                    else None
                )
                cost_obj = getattr(metrics_obj, "cost", None) if metrics_obj else None
                cost_dict = asdict(cost_obj) if is_dataclass(cost_obj) else None
                latency_dict = None
                avg = getattr(metrics_obj, "latency_avg", None) if metrics_obj else None
                p95 = getattr(metrics_obj, "latency_p95", None) if metrics_obj else None
                p50 = getattr(metrics_obj, "latency_p50", None) if metrics_obj else None
                p99 = getattr(metrics_obj, "latency_p99", None) if metrics_obj else None
                if (
                    avg is not None
                    or p50 is not None
                    or p95 is not None
                    or p99 is not None
                ):
                    # Persist full LatencyBreakdown per percentile so the
                    # dashboard hydrate path can render stt/llm/tts breakdown
                    # for historical calls. Keep flat ``p50_ms/p95_ms/p99_ms``
                    # for backward compat with consumers that only read totals.
                    latency_dict = {
                        "p50_ms": getattr(p50, "total_ms", None) if p50 else None,
                        "p95_ms": getattr(p95, "total_ms", None) if p95 else None,
                        "p99_ms": getattr(p99, "total_ms", None) if p99 else None,
                        "avg": asdict(avg) if is_dataclass(avg) else None,
                        "p50": asdict(p50) if is_dataclass(p50) else None,
                        "p95": asdict(p95) if is_dataclass(p95) else None,
                        "p99": asdict(p99) if is_dataclass(p99) else None,
                    }
                turns_count = (
                    len(getattr(metrics_obj, "turns", []) or [])
                    if metrics_obj
                    else None
                )
                await alog_call_end(
                    call_logger,
                    data.get("call_id", ""),
                    duration_seconds=duration,
                    turns=turns_count,
                    cost=cost_dict,
                    latency=latency_dict,
                )
            if user_end is not None:
                await user_end(data)
            # Resolve any pending call(wait=True) future. A media-stream end
            # means the call connected: classify ``voicemail`` when AMD tagged
            # the callee as a machine, else ``answered``. Fan-out — this runs
            # regardless of (and after) the user's own on_call_end callback,
            # so wiring a callback no longer monopolises completion signalling.
            cid = data.get("call_id", "")
            if cid:
                cls = self._amd_class.get(cid)
                outcome = "voicemail" if cls == "machine" else "answered"
                self._resolve_completion(
                    cid, outcome=outcome, status="completed", data=data
                )

        async def _on_metrics(data):
            if store is not None:
                store.record_turn(data)
            if call_logger.enabled:
                from dataclasses import asdict, is_dataclass

                turn = data.get("turn")
                turn_dict: dict | None = None
                if is_dataclass(turn):
                    turn_dict = asdict(turn)
                elif isinstance(turn, dict):
                    turn_dict = turn
                if turn_dict is not None:
                    await alog_turn(call_logger, data.get("call_id", ""), turn_dict)
            if user_metrics is not None:
                await user_metrics(data)

        async def _on_transcript_line(data):
            # FIX-5 (issue #154): feed the dashboard store's live per-line
            # transcript path. The Realtime / ConvAI handler fires this the
            # moment each user/assistant line is known (keyed by the reserved
            # turn index) so the dashboard can render and re-order lines before
            # the turn completes. Store-only — never user-facing.
            if store is not None:
                store.record_transcript_line(data)

        return _on_call_start, _on_call_end, _on_metrics, _on_transcript_line

    def _dashboard_is_exposed(self) -> bool:
        """Return True when this server would be reachable beyond loopback.

        Drives the dashboard auto-token gate in :meth:`_create_app`. Returns
        ``True`` if ANY of these signals holds:

        (a)/(b) A public ``webhook_url`` is configured — set either explicitly
            via ``Patter(webhook_url=...)`` or auto-assigned by a tunnel
            (CloudflareTunnel / Ngrok / Static) in ``serve()``. Both collapse
            to "``config.webhook_url`` is non-empty and not a loopback host",
            which is the PARITY-CRITICAL signal shared with the TypeScript SDK.
        (c) ``PATTER_BIND_HOST`` is EXPLICITLY set to a non-loopback host. The
            default bind is ``127.0.0.1`` (loopback, safe); reading that
            default never trips this signal, so normal local dev is unaffected.

        Returns ``False`` for the local-dev path (loopback-only bind, no
        tunnel, no public webhook_url) so that case keeps serving as before.
        """
        # Signals (a) + (b): a public webhook hostname (tunnel- or
        # caller-assigned) means the whole port is reachable publicly.
        webhook_url = getattr(self.config, "webhook_url", "") or ""
        if webhook_url and not _is_loopback_host(webhook_url):
            return True

        # Signal (c): explicit non-loopback PATTER_BIND_HOST override only.
        bind_host = os.environ.get("PATTER_BIND_HOST")
        if bind_host is not None and not _is_loopback_host(bind_host):
            return True

        return False

    def _create_app(self):
        """Build the FastAPI application with webhook + stream routes."""
        from getpatter.telephony.plivo import (
            plivo_stream_bridge,
            plivo_webhook_handler,
        )
        from getpatter.telephony.telnyx import telnyx_stream_bridge
        from getpatter.telephony.twilio import (
            twilio_stream_bridge,
            twilio_webhook_handler,
        )

        app = FastAPI(title="Patter Local Server")

        # --- Dashboard ---
        if self.dashboard:
            from getpatter.dashboard.routes import mount_dashboard
            from getpatter.dashboard.store import MetricsStore

            self._metrics_store = MetricsStore()

            # Hydrate the dashboard from disk so /api/dashboard/calls survives
            # a process restart. CallLogger persists call metadata as JSONL/JSON
            # under the resolved log root; replay those files into the store.
            # No-op when logging is disabled (``persist_root`` is ``None``).
            log_root = self._persist_root
            if log_root is not None:
                try:
                    restored = self._metrics_store.hydrate(str(log_root))
                    if restored > 0:
                        import logging

                        logging.getLogger("getpatter.server").info(
                            "Dashboard hydrated %d call(s) from %s", restored, log_root
                        )
                except Exception as exc:  # pragma: no cover - defensive
                    import logging

                    logging.getLogger("getpatter.server").warning(
                        "Dashboard hydration failed: %s", exc
                    )

            # --- Resolve the effective dashboard token ---
            #
            # The dashboard + call-data API expose call transcripts and
            # metadata (PII). The dashboard is ALWAYS mounted; how it is
            # protected depends on the bind exposure and config:
            #
            #   * explicit ``dashboard_token`` set       => use it (auth required)
            #   * exposed + no token + NOT insecure      => auto-generate a
            #       one-time UUID token (always available, zero-config protected)
            #   * exposed + no token + insecure opt-out  => OPEN (no token), WARN
            #   * loopback-only + no token               => OPEN (no token) —
            #       unchanged zero-friction local-dev behaviour
            #
            # The carrier webhook + media-stream + /health routes always mount
            # too, so calls keep working regardless. Mirrors the TypeScript SDK.
            is_exposed = self._dashboard_is_exposed()

            if self.dashboard_token:
                effective_token = self.dashboard_token
            elif is_exposed and not self.allow_insecure_dashboard:
                # RFC 4122 v4 UUID with dashes (str(), not .hex) so the
                # generated token is byte-for-byte the same shape as the
                # TypeScript SDK's ``crypto.randomUUID()`` — see sdk-parity.
                effective_token = str(uuid.uuid4())
                logger.warning(
                    "Dashboard is reachable beyond 127.0.0.1 without a "
                    "configured token; protecting it with an auto-generated "
                    "token. Set dashboard_token for a stable token, or "
                    "allow_insecure_dashboard=true to serve it open. "
                    "(The ready-to-use URL with the token is printed in the "
                    "startup banner.)"
                )
            elif is_exposed and self.allow_insecure_dashboard:
                effective_token = ""
                logger.warning(
                    "Dashboard served WITHOUT authentication on a "
                    "publicly-reachable bind (allow_insecure_dashboard=True). "
                    "Call transcripts and metadata are exposed to anyone "
                    "who can reach this URL."
                )
            else:
                # Loopback-only, no token: open local-dev path (unchanged).
                effective_token = ""

            self._effective_dashboard_token = effective_token

            mount_dashboard(app, self._metrics_store, token=effective_token)

            from getpatter.api_routes import mount_api

            mount_api(app, self._metrics_store, token=effective_token)

        @app.get("/health")
        async def health():
            return {"status": "ok", "mode": "local"}

        # --- Twilio ---

        async def _read_and_validate_twilio_form(request: Request):
            """Read the form body and verify the X-Twilio-Signature header.

            Returns the parsed form on success, or a 403 Response when the
            signature is present but invalid.  When no auth token is
            configured and ``config.require_signature`` is True (default),
            returns a 503 Response — safety-first posture requires an
            explicit opt-out to accept unsigned webhooks.
            """
            if not self.config.twilio_token and getattr(
                self.config, "require_signature", True
            ):
                logger.error(
                    "Twilio webhook rejected: twilio_token not configured and "
                    "require_signature=True. Set twilio_token, or explicitly "
                    "opt out with LocalConfig(require_signature=False)."
                )
                return Response(status_code=503, content="Webhook signature required")
            if self.config.twilio_token:
                try:
                    from twilio.request_validator import RequestValidator
                except ImportError:
                    # SECURITY: fail closed when the twilio package is missing.
                    # Previously we skipped signature validation and logged a
                    # warning — a deployer who didn't install getpatter with
                    # the twilio extra got an auth bypass. Now we reject.
                    logger.error(
                        "twilio package not installed but twilio_token is set — "
                        "refusing to accept webhook without signature verification. "
                        "Install with: pip install 'getpatter[local]' or "
                        "`pip install twilio`."
                    )
                    return Response(
                        status_code=503, content="Signature validator unavailable"
                    )
                form_data = await request.form()
                validator = RequestValidator(self.config.twilio_token)
                # Use request.url verbatim when it carries .path / .query
                # (Starlette URL in production). Under test harnesses that mock
                # request.url as a plain string, fall back to that string and
                # normalise the scheme to https. Proxy-induced scheme drift
                # stays handled in both cases.
                req_url = request.url
                if hasattr(req_url, "path"):
                    path_and_query = req_url.path
                    if getattr(req_url, "query", ""):
                        path_and_query += "?" + req_url.query
                    url = f"https://{self.config.webhook_url}{path_and_query}"
                else:
                    url = str(req_url).replace("http://", "https://")
                signature = request.headers.get("X-Twilio-Signature", "")
                if not validator.validate(url, dict(form_data), signature):
                    return Response(status_code=403, content="Invalid signature")
                return form_data
            return await request.form()

        @app.post("/webhooks/twilio/voice")
        async def twilio_voice(request: Request):
            form_or_response = await _read_and_validate_twilio_form(request)
            if isinstance(form_or_response, Response):
                return form_or_response
            form_data = form_or_response
            call_sid = form_data.get("CallSid", "")
            # Twilio sends both `From` and `Caller` — `From` is set for direct
            # inbound dials, `Caller` is what Twilio sees on the SIP trunk and
            # is the reliable fallback when the number is anonymised or
            # masked. Same for `To` / `Called`.
            caller = form_data.get("From", "") or form_data.get("Caller", "")
            callee = form_data.get("To", "") or form_data.get("Called", "")
            twiml = twilio_webhook_handler(
                call_sid, caller, callee, self.config.webhook_url
            )
            return Response(content=twiml, media_type="text/xml")

        # Twilio posts here for every status transition of a call
        # (initiated → ringing → in-progress → completed | no-answer |
        # busy | failed | canceled). Keeps the dashboard honest even when
        # the call never reaches the media channel and no media-stream
        # webhook is fired.
        @app.post("/webhooks/twilio/status")
        async def twilio_status_callback(request: Request):
            form_or_response = await _read_and_validate_twilio_form(request)
            if isinstance(form_or_response, Response):
                return form_or_response
            form = form_or_response
            call_sid = form.get("CallSid", "")
            call_status = form.get("CallStatus", "")
            duration = form.get("CallDuration", "") or form.get("Duration", "")
            logger.info(
                "Twilio status %s for call %s (duration=%s)",
                sanitize_log_value(call_status),
                sanitize_log_value(call_sid),
                sanitize_log_value(duration),
            )
            if self._metrics_store is not None and call_sid and call_status:
                extra: dict = {}
                if duration:
                    try:
                        extra["duration_seconds"] = float(duration)
                    except ValueError:
                        pass
                self._metrics_store.update_call_status(call_sid, call_status, **extra)
            # FIX #91 — when the call terminates before the media stream
            # starts (no-answer / busy / failed / canceled), the prewarm
            # cache entry would otherwise leak until ``end_call`` runs.
            # Evict it here so the WARN fires once and the bytes are
            # released regardless of whether the user calls ``end_call``.
            if call_sid and call_status in (
                "no-answer",
                "busy",
                "failed",
                "canceled",
            ):
                try:
                    self.record_prewarm_waste(call_sid)
                except Exception as exc:  # noqa: BLE001 - defensive
                    logger.debug("record_prewarm_waste raised: %s", exc)
                # Resolve any pending call(wait=True) future for a call that
                # never reached media — no on_call_end will fire for these.
                self._resolve_completion(
                    call_sid,
                    outcome=_twilio_status_to_outcome(call_status),
                    status=call_status,
                )
            return Response(content="", status_code=204)

        @app.post("/webhooks/twilio/recording")
        async def twilio_recording_callback(request: Request):
            form_or_response = await _read_and_validate_twilio_form(request)
            if isinstance(form_or_response, Response):
                return form_or_response
            form = form_or_response
            recording_sid = form.get("RecordingSid", "")
            recording_url = form.get("RecordingUrl", "")
            call_sid = form.get("CallSid", "")
            logger.info(
                "Recording %s for call %s: %s",
                sanitize_log_value(recording_sid),
                sanitize_log_value(call_sid),
                sanitize_log_value(recording_url),
            )
            return Response(content="", status_code=204)

        @app.post("/webhooks/twilio/amd")
        async def twilio_amd_callback(request: Request):
            form_or_response = await _read_and_validate_twilio_form(request)
            if isinstance(form_or_response, Response):
                return form_or_response
            form = form_or_response
            answered_by = form.get("AnsweredBy", "")
            call_sid = form.get("CallSid", "")
            logger.info("AMD result for %s: %s", call_sid, answered_by)

            # Record the AMD classification so a later on_call_end can resolve
            # call(wait=True) as ``voicemail`` vs ``answered``.
            if call_sid:
                self._amd_class[call_sid] = _classify_twilio_amd(answered_by)

            # Fire the per-call on_machine_detection callback (if any) BEFORE
            # the voicemail-drop logic so callers see the result regardless
            # of whether a voicemail message was configured. Keyed by CallSid
            # so concurrent outbound calls do not clobber each other. Errors in
            # user code must not break webhook delivery — Twilio retries on
            # non-2xx.
            if call_sid:
                await self._fire_machine_detection(
                    call_sid,
                    MachineDetectionResult(
                        call_id=call_sid,
                        carrier="twilio",
                        classification=_classify_twilio_amd(answered_by),
                        raw=answered_by,
                        detected_at=time.time(),
                    ),
                )

            # FIX #91 — when AMD classifies as machine, the agent's first
            # message will not be played (we drop voicemail or hang up), so
            # the prewarmed greeting is never consumed. Evict the cache
            # entry once so the WARN fires regardless of whether
            # ``voicemail_message`` is configured.
            if answered_by in ("machine_end_beep", "machine_end_silence") and call_sid:
                try:
                    self.record_prewarm_waste(call_sid)
                except Exception as exc:  # noqa: BLE001 - defensive
                    logger.debug("record_prewarm_waste raised: %s", exc)

            if (
                answered_by in ("machine_end_beep", "machine_end_silence")
                and self.voicemail_message
                and self.config.twilio_sid
                and self.config.twilio_token
            ):
                from getpatter.telephony.twilio import (
                    _validate_twilio_sid,
                    _xml_escape,
                )

                if not _validate_twilio_sid(call_sid, "CA"):
                    logger.warning(
                        "AMD callback: invalid CallSid format %r, ignoring", call_sid
                    )
                    return Response(content="", status_code=204)

                import httpx as _httpx

                twiml = f"<Response><Say>{_xml_escape(self.voicemail_message)}</Say><Hangup/></Response>"
                try:
                    async with _httpx.AsyncClient(timeout=10.0) as _http:
                        await _http.post(
                            f"https://api.twilio.com/2010-04-01/Accounts/{self.config.twilio_sid}/Calls/{call_sid}.json",
                            auth=(self.config.twilio_sid, self.config.twilio_token),
                            data={"Twiml": twiml},
                        )
                    logger.info("Voicemail dropped for %s", call_sid)
                except _httpx.TimeoutException as exc:
                    # Mirrors TS server.ts:834 fetch with AbortSignal.timeout(10_000).
                    # Voicemail-drop is best-effort — degrade gracefully rather
                    # than block call-flow when Twilio is slow / unreachable.
                    logger.warning(
                        "Voicemail-drop timed out (>10s); continuing without it: %s",
                        exc,
                    )
                except Exception as exc:
                    logger.warning("Could not drop voicemail: %s", exc)

            return Response(content="", status_code=204)

        @app.websocket("/ws/stream/{call_id}")
        async def twilio_stream_handler(websocket: WebSocket, call_id: str):
            # Per-IP DoS cap (mirrors TS server.ts:1041-1064).
            client_ip = _client_ip_for_ws(websocket)
            if self._ws_conn_counts[client_ip] >= MAX_WS_PER_IP:
                logger.warning(
                    "WebSocket upgrade rejected: too many connections from %s",
                    client_ip,
                )
                # Close before accept = HTTP 429 to the upgrading client.
                await websocket.close(code=1008, reason="Too Many Requests")
                return
            self._ws_conn_counts[client_ip] += 1
            self._active_connections.add(websocket)
            try:
                _start, _end, _metrics, _transcript_line = self._wrap_callbacks()
                await twilio_stream_bridge(
                    websocket=websocket,
                    agent=self.agent,
                    pop_prewarm_audio=self.pop_prewarm_audio,
                    pop_prewarmed_connections=self.pop_prewarmed_connections,
                    openai_key=self.config.openai_key,
                    on_call_start=_start,
                    on_call_end=_end,
                    on_transcript=self.on_transcript,
                    on_message=self.on_message,
                    deepgram_key=self.config.deepgram_key,
                    elevenlabs_key=self.config.elevenlabs_key,
                    twilio_sid=self.config.twilio_sid,
                    twilio_token=self.config.twilio_token,
                    recording=self.recording,
                    on_metrics=_metrics,
                    on_transcript_line=_transcript_line,
                    pricing=self.pricing,
                    report_only_initial_ttfb=self.config.report_only_initial_ttfb,
                    speech_events=getattr(self, "speech_events", None),
                )
            finally:
                self._active_connections.discard(websocket)
                remaining = self._ws_conn_counts[client_ip] - 1
                if remaining <= 0:
                    self._ws_conn_counts.pop(client_ip, None)
                else:
                    self._ws_conn_counts[client_ip] = remaining

        # --- Telnyx ---

        @app.post("/webhooks/telnyx/voice")
        async def telnyx_voice(request: Request):
            raw_body = await request.body()
            telnyx_public_key = getattr(self.config, "telnyx_public_key", "")
            require_sig = getattr(self.config, "require_signature", True)
            if telnyx_public_key:
                signature = request.headers.get("telnyx-signature-ed25519", "")
                timestamp = request.headers.get("telnyx-timestamp", "")
                if not _validate_telnyx_signature(
                    raw_body, signature, timestamp, telnyx_public_key
                ):
                    logger.warning(
                        "Telnyx webhook rejected: invalid or missing Ed25519 signature"
                    )
                    return Response(status_code=403, content="Invalid signature")
            elif require_sig:
                logger.error(
                    "Telnyx webhook rejected: telnyx_public_key not configured "
                    "and require_signature=True. Set telnyx_public_key, or "
                    "explicitly opt out with LocalConfig(require_signature=False)."
                )
                return Response(status_code=503, content="Webhook signature required")
            elif not self._telnyx_sig_warning_logged:
                self._telnyx_sig_warning_logged = True
                logger.warning(
                    "Telnyx webhook signature verification is disabled. "
                    "Set telnyx_public_key in LocalConfig for production use."
                )
            import json as _json

            try:
                body = _json.loads(raw_body)
            except (ValueError, TypeError):
                return Response(status_code=400, content="Invalid JSON body")
            if not isinstance(body.get("data"), dict) or not isinstance(
                body.get("data", {}).get("payload"), dict
            ):
                logger.warning(
                    "Telnyx webhook rejected: missing data.payload structure."
                )
                return Response(status_code=400, content="Invalid webhook structure")
            data = body["data"]
            event_type = data.get("event_type", "")
            payload = data["payload"]
            call_control_id = payload.get("call_control_id", "")
            caller = payload.get("from", "")
            callee = payload.get("to", "")
            if not call_control_id:
                logger.warning("Telnyx webhook rejected: missing call_control_id.")
                return Response(status_code=400, content="Invalid webhook payload")

            # Telnyx Call Control is a REST API — the webhook body is a
            # notification, not a command transport. We react by POSTing
            # actions/answer and actions/streaming_start to the Call Control
            # REST endpoint.
            api_key = self.config.telnyx_key
            if not api_key:
                logger.warning("Telnyx webhook: missing telnyx_key in LocalConfig")
                return Response(status_code=500, content="Missing Telnyx API key")

            import httpx as _httpx

            api_base = "https://api.telnyx.com/v2"
            auth_headers = {"Authorization": f"Bearer {api_key}"}

            # DTMF received during the call — Telnyx fires
            # ``call.dtmf.received`` as a notification webhook (separate from
            # the in-band media-stream ``dtmf`` frame). Acknowledge it so
            # Telnyx does not retry. Mirrors the TS server.
            if event_type == "call.dtmf.received":
                digit = str(payload.get("digit", "")).strip()
                if digit:
                    logger.info(
                        "Telnyx DTMF received (webhook): %s",
                        sanitize_log_value(digit),
                    )
                return Response(status_code=200)

            try:
                if event_type == "call.initiated":
                    # PERF — Telnyx accepts the streaming params inline on
                    # ``actions/answer`` and auto-starts the stream the moment
                    # the leg picks up. Folding ``streaming_start`` into the
                    # answer body removes both the ``call.answered`` webhook
                    # round-trip and a second POST (~100-200 ms saved per
                    # inbound call).
                    from urllib.parse import quote as _quote

                    stream_url = (
                        f"wss://{self.config.webhook_url}/ws/telnyx/stream/{_quote(call_control_id, safe='')}"
                        f"?caller={_quote(caller)}&callee={_quote(callee)}"
                    )
                    logger.info(
                        "Telnyx call.initiated %s — answering with inline stream",
                        call_control_id,
                    )
                    async with _httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(
                            f"{api_base}/calls/{_quote(call_control_id, safe='')}/actions/answer",
                            headers=auth_headers,
                            json={
                                "stream_url": stream_url,
                                # ``inbound_track`` halves WS upstream
                                # bandwidth — outbound echo was always
                                # filtered downstream anyway.
                                "stream_track": "inbound_track",
                                "stream_bidirectional_mode": "rtp",
                                "stream_bidirectional_codec": "PCMU",
                                "stream_bidirectional_sampling_rate": 8000,
                                "stream_bidirectional_target_legs": "self",
                            },
                        )
                        if resp.status_code >= 400:
                            logger.warning(
                                "Telnyx answer failed: %s %s",
                                resp.status_code,
                                resp.text,
                            )
                elif event_type == "call.answered":
                    # No-op: ``call.initiated`` already submitted answer +
                    # streaming_start in a single call. Telnyx still emits
                    # ``call.answered`` as an informational event; acknowledge
                    # without making a redundant POST.
                    logger.debug(
                        "Telnyx call.answered %s — stream already active (inline)",
                        call_control_id,
                    )
                elif event_type == "call.machine.detection.ended":
                    # AMD (answering machine detection) result — mirror the
                    # Twilio voicemail-drop flow when Telnyx reports the leg
                    # was answered by a machine.
                    amd_result = str(payload.get("result", ""))
                    logger.info(
                        "Telnyx AMD result for %s: %s",
                        sanitize_log_value(call_control_id),
                        sanitize_log_value(amd_result),
                    )
                    # Record the AMD classification so a later on_call_end can
                    # resolve call(wait=True) as ``voicemail`` vs ``answered``.
                    if call_control_id:
                        self._amd_class[call_control_id] = _classify_telnyx_amd(
                            amd_result
                        )
                    # Fire the per-call on_machine_detection callback. Same
                    # rationale as the Twilio path above — caller sees the
                    # result even when no voicemail_message is configured,
                    # and errors in user code don't break webhook delivery.
                    # Keyed by call_control_id so concurrent outbound calls
                    # do not clobber each other.
                    if call_control_id:
                        await self._fire_machine_detection(
                            call_control_id,
                            MachineDetectionResult(
                                call_id=call_control_id,
                                carrier="telnyx",
                                classification=_classify_telnyx_amd(amd_result),
                                raw=amd_result,
                                detected_at=time.time(),
                            ),
                        )
                    if self.voicemail_message:
                        from getpatter.telephony.telnyx import handle_amd_result

                        await handle_amd_result(
                            call_control_id=call_control_id,
                            result=amd_result,
                            voicemail_message=self.voicemail_message,
                            telnyx_key=api_key,
                        )
                    # FIX #91 — when AMD classifies as machine the agent's
                    # first message is replaced by ``voicemail_message`` (or
                    # the call simply ends), so the prewarmed greeting is
                    # never consumed. Evict it so the WARN fires once.
                    if call_control_id and amd_result in (
                        "machine",
                        "machine_detected",
                    ):
                        try:
                            self.record_prewarm_waste(call_control_id)
                        except Exception as exc:  # noqa: BLE001 - defensive
                            logger.debug("record_prewarm_waste raised: %s", exc)
                elif event_type == "call.hangup":
                    # FIX #91 — Telnyx fires ``call.hangup`` as the final
                    # status notification. ``hangup_cause`` distinguishes
                    # carrier outcomes (``call_rejected`` / ``busy`` /
                    # ``no_answer`` / ``timeout`` / ``normal_clearing`` /
                    # ``user_busy`` / …). When the call never reached the
                    # media stream the prewarm cache leaks unless we
                    # evict it here.
                    hangup_cause = str(payload.get("hangup_cause", ""))
                    logger.info(
                        "Telnyx call.hangup for %s (cause=%s)",
                        sanitize_log_value(call_control_id),
                        sanitize_log_value(hangup_cause),
                    )
                    if call_control_id:
                        try:
                            self.record_prewarm_waste(call_control_id)
                        except Exception as exc:  # noqa: BLE001 - defensive
                            logger.debug("record_prewarm_waste raised: %s", exc)
                        # Resolve a pending call(wait=True) future only for
                        # no-media hangup causes (no-answer / busy / rejected).
                        # ``normal_clearing`` implies the call connected →
                        # ``None`` here so on_call_end resolves it with the
                        # full transcript instead.
                        no_media_outcome = _telnyx_hangup_outcome(hangup_cause)
                        if no_media_outcome is not None:
                            self._resolve_completion(
                                call_control_id,
                                outcome=no_media_outcome,
                                status=hangup_cause,
                            )
                elif event_type == "call.recording.saved":
                    # Telnyx Call Control recording completion — produced
                    # when a ``record_start`` action is followed by a
                    # ``record_stop`` or the call ends. Mirrors the
                    # Twilio ``/webhooks/twilio/recording`` route which
                    # logs RecordingSid and RecordingUrl.
                    recording_urls = payload.get("recording_urls") or {}
                    public_urls = payload.get("public_recording_urls") or {}
                    recording_url = (
                        recording_urls.get("mp3")
                        or recording_urls.get("wav")
                        or public_urls.get("mp3")
                        or public_urls.get("wav")
                        or ""
                    )
                    logger.info(
                        "Telnyx recording saved for %s: %s",
                        sanitize_log_value(call_control_id),
                        sanitize_log_value(recording_url),
                    )
                else:
                    logger.debug("Telnyx event ignored: %s", event_type)
            except Exception as exc:
                logger.exception("Telnyx webhook handler error: %s", exc)

            return Response(status_code=200)

        @app.websocket("/ws/telnyx/stream/{call_id}")
        async def telnyx_stream_handler(websocket: WebSocket, call_id: str):
            # Per-IP DoS cap (mirrors TS server.ts:1041-1064).
            client_ip = _client_ip_for_ws(websocket)
            if self._ws_conn_counts[client_ip] >= MAX_WS_PER_IP:
                logger.warning(
                    "WebSocket upgrade rejected: too many connections from %s",
                    client_ip,
                )
                await websocket.close(code=1008, reason="Too Many Requests")
                return
            self._ws_conn_counts[client_ip] += 1
            self._active_connections.add(websocket)
            try:
                _start, _end, _metrics, _transcript_line = self._wrap_callbacks()
                await telnyx_stream_bridge(
                    websocket=websocket,
                    agent=self.agent,
                    pop_prewarm_audio=self.pop_prewarm_audio,
                    pop_prewarmed_connections=self.pop_prewarmed_connections,
                    openai_key=self.config.openai_key,
                    on_call_start=_start,
                    on_call_end=_end,
                    on_transcript=self.on_transcript,
                    on_message=self.on_message,
                    deepgram_key=self.config.deepgram_key,
                    elevenlabs_key=self.config.elevenlabs_key,
                    telnyx_key=self.config.telnyx_key,
                    recording=self.recording,
                    on_metrics=_metrics,
                    on_transcript_line=_transcript_line,
                    pricing=self.pricing,
                    report_only_initial_ttfb=self.config.report_only_initial_ttfb,
                )
            finally:
                self._active_connections.discard(websocket)
                remaining = self._ws_conn_counts[client_ip] - 1
                if remaining <= 0:
                    self._ws_conn_counts.pop(client_ip, None)
                else:
                    self._ws_conn_counts[client_ip] = remaining

        # --- Plivo ---

        async def _validate_plivo_request(request: Request):
            """Verify the ``X-Plivo-Signature-V3`` header.

            Returns ``(form_dict, None)`` on success or ``(None, Response)`` to
            short-circuit the route. Mirrors the
            :func:`_read_and_validate_twilio_form` pattern so callers don't
            re-parse the form themselves. V3 signs ``url + sorted_post_params
            + "." + nonce`` for POST and ``url + "." + nonce`` for GET — so the
            form has to be parsed *before* signature validation, not after.
            Fails closed when no ``plivo_auth_token`` is configured and
            ``require_signature`` is True.
            """
            method = request.method.upper()
            form_params: dict = {}
            if method == "POST":
                form_params = {k: str(v) for k, v in (await request.form()).items()}
            auth_token = self.config.plivo_auth_token
            require_sig = getattr(self.config, "require_signature", True)
            if not auth_token:
                if require_sig:
                    logger.error(
                        "Plivo webhook rejected: plivo_auth_token not configured "
                        "and require_signature=True. Set plivo_auth_token, or "
                        "explicitly opt out with LocalConfig(require_signature=False)."
                    )
                    return None, Response(
                        status_code=503, content="Webhook signature required"
                    )
                return form_params, None
            signature = request.headers.get("X-Plivo-Signature-V3", "")
            nonce = request.headers.get("X-Plivo-Signature-V3-Nonce", "")
            # Reconstruct the exact public URL Plivo signed (the answer_url /
            # callback we registered), independent of any proxy/tunnel rewrite.
            req_url = request.url
            if hasattr(req_url, "path"):
                path_and_query = req_url.path
                if getattr(req_url, "query", ""):
                    path_and_query += "?" + req_url.query
                url = f"https://{self.config.webhook_url}{path_and_query}"
            else:
                url = str(req_url).replace("http://", "https://")
            if not _validate_plivo_signature(
                url,
                nonce,
                signature,
                auth_token,
                params=form_params,
                method=method,
            ):
                logger.warning(
                    "Plivo webhook rejected: invalid or missing V3 signature"
                )
                return None, Response(status_code=403, content="Invalid signature")
            return form_params, None

        @app.post("/webhooks/plivo/voice")
        async def plivo_voice(request: Request):
            form, sig_err = await _validate_plivo_request(request)
            if sig_err is not None:
                return sig_err
            # Plivo posts CallUUID + From/To on the answer_url for both inbound
            # and answered-outbound calls. The same route serves both.
            call_uuid = form.get("CallUUID", "")
            caller = form.get("From", "")
            callee = form.get("To", "")
            xml = plivo_webhook_handler(
                call_uuid or "outbound", caller, callee, self.config.webhook_url
            )
            return Response(content=xml, media_type="text/xml")

        @app.post("/webhooks/plivo/status")
        async def plivo_status_callback(request: Request):
            form, sig_err = await _validate_plivo_request(request)
            if sig_err is not None:
                return sig_err
            call_uuid = form.get("CallUUID", "")
            # Plivo's hangup_url posts CallStatus (completed / busy / no-answer
            # / failed / timeout / cancel) once the call ends.
            call_status = form.get("CallStatus", "") or form.get("Status", "")
            duration = form.get("Duration", "") or form.get("BillDuration", "")
            logger.info(
                "Plivo status %s for call %s (duration=%s)",
                sanitize_log_value(call_status),
                sanitize_log_value(call_uuid),
                sanitize_log_value(duration),
            )
            if self._metrics_store is not None and call_uuid and call_status:
                extra: dict = {}
                if duration:
                    try:
                        extra["duration_seconds"] = float(duration)
                    except ValueError:
                        pass
                self._metrics_store.update_call_status(call_uuid, call_status, **extra)
            if call_uuid and call_status in (
                "no-answer",
                "busy",
                "failed",
                "timeout",
                "cancel",
            ):
                try:
                    self.record_prewarm_waste(call_uuid)
                except Exception as exc:  # noqa: BLE001 - defensive
                    logger.debug("record_prewarm_waste raised: %s", exc)
                # Resolve a pending call(wait=True) for a call that never
                # reached media — no on_call_end will fire for these.
                outcome = (
                    "no_answer"
                    if call_status in ("no-answer", "timeout")
                    else "busy"
                    if call_status == "busy"
                    else "failed"
                )
                self._resolve_completion(call_uuid, outcome=outcome, status=call_status)
            return Response(content="", status_code=200)

        @app.post("/webhooks/plivo/amd")
        async def plivo_amd_callback(request: Request):
            form, sig_err = await _validate_plivo_request(request)
            if sig_err is not None:
                return sig_err
            call_uuid = form.get("CallUUID", "")
            # Plivo's async AMD result field name varies by API version —
            # accept the common spellings; _classify_plivo_amd normalises them.
            amd_raw = (
                form.get("Machine", "")
                or form.get("MachineDetection", "")
                or form.get("AnsweredBy", "")
                or form.get("CallStatus", "")
            )
            logger.info("AMD result for %s: %s", call_uuid, amd_raw)
            classification = _classify_plivo_amd(amd_raw)
            # Record the AMD classification so a later on_call_end can resolve
            # a pending call(wait=True) as ``voicemail`` vs ``answered``.
            if call_uuid:
                self._amd_class[call_uuid] = classification

            # Keyed by CallUUID so concurrent outbound calls do not clobber
            # each other; falls back to the legacy single-slot callback.
            if call_uuid:
                await self._fire_machine_detection(
                    call_uuid,
                    MachineDetectionResult(
                        call_id=call_uuid,
                        carrier="plivo",
                        classification=classification,
                        raw=amd_raw,
                        detected_at=time.time(),
                    ),
                )

            if classification == "machine" and call_uuid:
                try:
                    self.record_prewarm_waste(call_uuid)
                except Exception as exc:  # noqa: BLE001 - defensive
                    logger.debug("record_prewarm_waste raised: %s", exc)
                if (
                    self.voicemail_message
                    and self.config.plivo_auth_id
                    and self.config.plivo_auth_token
                ):
                    from getpatter.telephony.plivo import handle_amd_result

                    await handle_amd_result(
                        call_uuid=call_uuid,
                        voicemail_message=self.voicemail_message,
                        auth_id=self.config.plivo_auth_id,
                        auth_token=self.config.plivo_auth_token,
                    )
            return Response(content="", status_code=200)

        @app.api_route("/webhooks/plivo/transfer", methods=["GET", "POST"])
        async def plivo_transfer_xml(request: Request):
            # Returns the ``<Dial>`` XML that the blind-transfer ``aleg_url``
            # redirects the A-leg to. Validated like every other Plivo webhook.
            _form, sig_err = await _validate_plivo_request(request)
            if sig_err is not None:
                return sig_err
            from getpatter.providers.plivo_adapter import _xml_escape
            from getpatter.telephony.common import _validate_e164

            to = request.query_params.get("to", "")
            if not to or not _validate_e164(to):
                logger.warning("Plivo transfer XML: invalid target %r", to)
                return Response(
                    content="<Response><Hangup/></Response>", media_type="text/xml"
                )
            xml = (
                f"<Response><Dial><Number>{_xml_escape(to)}</Number></Dial></Response>"
            )
            return Response(content=xml, media_type="text/xml")

        @app.websocket("/ws/plivo/stream/{call_id}")
        async def plivo_stream_websocket(websocket: WebSocket, call_id: str):
            # Per-IP DoS cap (mirrors the Twilio / Telnyx handlers).
            client_ip = _client_ip_for_ws(websocket)
            if self._ws_conn_counts[client_ip] >= MAX_WS_PER_IP:
                logger.warning(
                    "WebSocket upgrade rejected: too many connections from %s",
                    client_ip,
                )
                await websocket.close(code=1008, reason="Too Many Requests")
                return
            self._ws_conn_counts[client_ip] += 1
            self._active_connections.add(websocket)
            try:
                _start, _end, _metrics, _transcript_line = self._wrap_callbacks()
                await plivo_stream_bridge(
                    websocket=websocket,
                    agent=self.agent,
                    pop_prewarm_audio=self.pop_prewarm_audio,
                    pop_prewarmed_connections=self.pop_prewarmed_connections,
                    openai_key=self.config.openai_key,
                    on_call_start=_start,
                    on_call_end=_end,
                    on_transcript=self.on_transcript,
                    on_message=self.on_message,
                    deepgram_key=self.config.deepgram_key,
                    elevenlabs_key=self.config.elevenlabs_key,
                    plivo_auth_id=self.config.plivo_auth_id,
                    plivo_auth_token=self.config.plivo_auth_token,
                    webhook_host=self.config.webhook_url,
                    recording=self.recording,
                    on_metrics=_metrics,
                    on_transcript_line=_transcript_line,
                    pricing=self.pricing,
                    report_only_initial_ttfb=self.config.report_only_initial_ttfb,
                    speech_events=getattr(self, "speech_events", None),
                )
            finally:
                self._active_connections.discard(websocket)
                remaining = self._ws_conn_counts[client_ip] - 1
                if remaining <= 0:
                    self._ws_conn_counts.pop(client_ip, None)
                else:
                    self._ws_conn_counts[client_ip] = remaining

        self._app = app
        return app

    async def start(self, port: int = 8000) -> None:
        """Start the embedded server.

        Optionally auto-configures the Twilio webhook URL if credentials are
        present in ``LocalConfig``.

        Args:
            port: Local TCP port to bind to (default 8000).
        """
        import uvicorn

        app = self._create_app()

        # Auto-configure Twilio webhook URL if possible
        if (
            self.config.telephony_provider == "twilio"
            and self.config.twilio_sid
            and self.config.webhook_url
        ):
            try:
                from getpatter.providers.twilio_adapter import TwilioAdapter  # type: ignore[import]

                adapter = TwilioAdapter(
                    account_sid=self.config.twilio_sid,
                    auth_token=self.config.twilio_token,
                )
                webhook_url = f"https://{self.config.webhook_url}/webhooks/twilio/voice"
                # SSRF guard: refuse to configure Twilio against private/loopback
                # hosts; mirrors libraries/typescript/src/server.ts:105 helper.
                if not validate_webhook_url(webhook_url):
                    raise ValueError(
                        f"Refusing to configure Twilio with unsafe webhook URL: {webhook_url}"
                    )
                await adapter.configure_number(self.config.phone_number, webhook_url)
                logger.info("Twilio webhook set to %s", webhook_url)
            except Exception as exc:
                logger.warning("Could not auto-configure webhook: %s", exc)
                logger.info(
                    "Set webhook manually to: https://%s/webhooks/twilio/voice",
                    self.config.webhook_url,
                )

        # Auto-configure the Plivo application answer URL if possible. Plivo
        # routes inbound calls through an Application, so this is best-effort;
        # most deployments pre-configure it in the Plivo console.
        if (
            self.config.telephony_provider == "plivo"
            and self.config.plivo_auth_id
            and self.config.webhook_url
        ):
            try:
                from getpatter.providers.plivo_adapter import PlivoAdapter  # type: ignore[import]

                adapter = PlivoAdapter(
                    auth_id=self.config.plivo_auth_id,
                    auth_token=self.config.plivo_auth_token,
                )
                webhook_url = f"https://{self.config.webhook_url}/webhooks/plivo/voice"
                if not validate_webhook_url(webhook_url):
                    raise ValueError(
                        f"Refusing to configure Plivo with unsafe webhook URL: {webhook_url}"
                    )
                await adapter.configure_number(self.config.phone_number, webhook_url)
                await adapter.close()
                logger.info("Plivo answer URL set to %s", webhook_url)
            except Exception as exc:
                logger.warning("Could not auto-configure webhook: %s", exc)
                logger.info(
                    "Set the Plivo application answer URL manually to: "
                    "https://%s/webhooks/plivo/voice",
                    self.config.webhook_url,
                )

        logger.info("Server starting on port %s", port)
        logger.info("Webhook URL: https://%s", self.config.webhook_url)
        logger.info("Phone:   %s", mask_phone_number(self.config.phone_number))
        logger.info("Agent:   %s / %s", self.agent.model, self.agent.voice)

        # Startup-time warning when webhook signature enforcement is active
        # but the verifying credential is missing. Surfacing this at startup
        # prevents deployers from discovering it only via a first 503 response.
        require_sig = getattr(self.config, "require_signature", True)
        if require_sig:
            provider = getattr(self.config, "telephony_provider", "")
            if provider == "twilio" and not self.config.twilio_token:
                logger.warning(
                    "Twilio webhook enforcement ACTIVE but twilio_token is empty "
                    "— webhooks will 503. Set require_signature=False for local dev."
                )
            if provider == "telnyx" and not getattr(
                self.config, "telnyx_public_key", ""
            ):
                logger.warning(
                    "Telnyx webhook enforcement ACTIVE but telnyx_public_key is empty "
                    "— webhooks will 503. Set require_signature=False for local dev."
                )
            if provider == "plivo" and not getattr(self.config, "plivo_auth_token", ""):
                logger.warning(
                    "Plivo webhook enforcement ACTIVE but plivo_auth_token is empty "
                    "— webhooks will 503. Set require_signature=False for local dev."
                )

        # (Earlier versions of this file emitted a "Pipeline mode without
        # VAD" warning here when neither ``agent.engine`` nor ``agent.vad``
        # was set. The warning is now stale: since the auto-VAD work
        # landed in stream_handler.py (``self.auto_vad =
        # await SileroVAD.for_phone_call()`` when ``onnxruntime`` is
        # installed), the SDK silently provides a working VAD per call.
        # The stream handler still logs a single, accurate message in
        # the rare case the auto-load fails — emitting both warnings
        # created false-positive alarm fatigue for operators.)

        # Warn if the agent runs a non-default Realtime model — DEFAULT_PRICING
        # is calibrated for the default Realtime models (gpt-realtime-mini /
        # gpt-4o-mini-realtime-preview, which share the same rates). Other
        # models differ by 3-10x so cost display would under-report.
        model = self.agent.model or ""
        _calibrated = ("gpt-realtime-mini", "gpt-4o-mini-realtime-preview")
        if model and model not in _calibrated and "realtime" in model:
            # Dev-supplied string — sanitize to avoid ANSI/log-injection in
            # the startup warning, matching TS parity.
            logger.warning(
                "Agent uses %r but DEFAULT_PRICING.openai_realtime is "
                "calibrated for the default Realtime models (gpt-realtime-mini "
                "/ gpt-4o-mini-realtime-preview). Pass "
                "Patter(pricing={'openai_realtime': {...}}) to set rates for "
                "this model, otherwise the dashboard cost display will "
                "under-report.",
                sanitize_log_value(model),
            )
        # The dashboard is always served (``_create_app`` resolved the
        # effective token: explicit, auto-generated, or "" when open). Print a
        # ready-to-click URL — with ``?token=`` when a token is in effect so the
        # operator can open the protected dashboard directly, or the plain URL
        # plus an unauthenticated warning when it is served open.
        if self.dashboard:
            token = self._effective_dashboard_token
            if token:
                logger.info(
                    "\n──── Dashboard ─────────────────────────────────────\n"
                    "URL: http://127.0.0.1:%s/?token=%s\n"
                    "────────────────────────────────────────────────────\n",
                    port,
                    token,
                )
            else:
                logger.info(
                    "\n──── Dashboard ─────────────────────────────────────\n"
                    "URL: http://127.0.0.1:%s/\n"
                    "────────────────────────────────────────────────────\n",
                    port,
                )
                logger.warning(
                    "Dashboard is enabled without authentication. "
                    "Set dashboard_token to protect call data. "
                    "This is safe for local development but should "
                    "not be exposed on a public network."
                )

        # Suppress Uvicorn's "Uvicorn running on..." startup message
        # but keep request logs (INFO level) visible
        logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

        # Default bind = 127.0.0.1 (loopback, safest). Set
        # ``PATTER_BIND_HOST=0.0.0.0`` when the SDK runs inside a container
        # whose port must be reachable from the host (e.g. ``docker run -p
        # 8000:8000`` with a tunnel pointing at the host port — Docker's
        # port-mapping cannot forward to a 127.0.0.1 listener inside the
        # container because that's the container's own loopback).
        bind_host = os.environ.get("PATTER_BIND_HOST", "127.0.0.1")
        config = uvicorn.Config(app, host=bind_host, port=port, log_level="info")
        self._server = uvicorn.Server(config)

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        await self._server.serve()

    async def stop(self) -> None:
        """Gracefully stop the embedded server.

        Closes all active WebSocket connections, waits up to 10 seconds
        for in-progress calls to finish, then shuts down the uvicorn server.
        """
        if self._shutting_down:
            return
        self._shutting_down = True

        logger.info(
            "Graceful shutdown initiated — closing %d active connection(s)",
            len(self._active_connections),
        )

        # Signal all active WebSocket connections to close
        for ws in list(self._active_connections):
            try:
                await ws.close(code=1001, reason="Server shutting down")
            except Exception:
                pass

        # Wait up to 10 seconds for active connections to drain
        for _ in range(100):
            if not self._active_connections:
                break
            await asyncio.sleep(0.1)

        if self._active_connections:
            logger.warning(
                "Shutdown timeout — %d connection(s) still active, forcing close",
                len(self._active_connections),
            )

        # Shutdown the uvicorn server
        if self._server:
            self._server.should_exit = True
