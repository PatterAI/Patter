"""Telemetry event schema and allowlist enforcement.

Only coarse, anonymous, low-cardinality fields ever leave the process. Two layers
of defense run before any event is built:

* **Key allowlist** (``ALLOWED_DIMENSIONS``) — a dimension whose key is not listed
  is dropped.
* **Value allowlist** (``DIMENSION_VALUES``) — for every enum dimension, a value
  that is not on its closed set is coerced to ``"other"``. This makes a raw custom
  tool name, custom integration id, or unexpected string **structurally
  impossible** to emit, even from a buggy caller. The relay re-validates both
  layers server-side as defense in depth.

NEVER add a field that could carry PII or call content: phone numbers, call SIDs,
transcripts, audio, prompts, tool arguments, API keys, customer identifiers, file
paths, hostnames, IPs, or any free text. See ``docs/internal`` design plan.

Bump ``SCHEMA_VERSION`` whenever the event/field/value set changes so the relay
can apply version-appropriate validation.
"""

from __future__ import annotations

import platform
import re
import sys
from typing import Any

from getpatter.telemetry.env import is_ci, is_test
from getpatter.telemetry.install_id import install_id, run_id
from getpatter.telemetry.stack import STACK_VENDORS

SCHEMA_VERSION = 8

# --- Event names (the only values the ``event`` field may take) -------------
EVENT_SDK_INITIALIZED = "sdk_initialized"
EVENT_FIRST_RUN = "first_run"
EVENT_CLI_COMMAND = "cli_command"
EVENT_FEATURE_USED = "feature_used"
EVENT_AGENT_CONFIGURED = "agent_configured"
EVENT_CALL_STARTED = "call_started"
EVENT_CALL_COMPLETED = "call_completed"
# Activation-blocker signal: emitted once when required runtime config is missing
# so the instance cannot proceed. In the NEVER-sampled set (always delivered).
EVENT_CONFIG_INCOMPLETE = "config_incomplete"

_ALLOWED_EVENTS = frozenset(
    {
        EVENT_SDK_INITIALIZED,
        EVENT_FIRST_RUN,
        EVENT_CLI_COMMAND,
        EVENT_FEATURE_USED,
        EVENT_AGENT_CONFIGURED,
        EVENT_CALL_STARTED,
        EVENT_CALL_COMPLETED,
        EVENT_CONFIG_INCOMPLETE,
    }
)

# Every enum dimension maps to its closed value set. Any value not in the set is
# coerced to ``"other"`` inside :func:`build_event`. Keep this byte-for-byte
# identical to ``DIMENSION_VALUES`` in the TypeScript ``events.ts``.
DIMENSION_VALUES: dict[str, frozenset[str]] = {
    "carrier": frozenset({"twilio", "telnyx", "plivo", "none"}),
    "tunnel": frozenset({"static", "configured", "none"}),
    "engine": frozenset({"realtime", "convai", "pipeline"}),
    "provider": frozenset(
        {
            "openai",
            "elevenlabs",
            "deepgram",
            "cartesia",
            "cerebras",
            "anthropic",
            "google",
            "whisper",
            "other",
        }
    ),
    # agent_configured dimensions
    "custom_tool_count_bucket": frozenset({"0", "1", "2_3", "4_6", "7_12", "13_plus"}),
    "integration": frozenset({"openclaw", "mcp", "hermes", "other", "none"}),
    "integration_kind": frozenset({"consult", "mcp", "none"}),
    "mcp_server_count_bucket": frozenset({"0", "1", "2_3", "4_plus"}),
    # call_started / call_completed: inbound vs outbound — a core usage split.
    "direction": frozenset({"inbound", "outbound", "none"}),
    # cli_command: which CLI subcommand was invoked (never args/flags values).
    "cli_command": frozenset(
        {"dashboard", "eval", "hermes", "openclaw", "telemetry", "none", "other"}
    ),
    # call_completed: the call's terminal outcome
    "outcome": frozenset({"completed", "error", "no_answer", "busy", "failed"}),
    # call_completed: the terminal error code (mirrors exceptions.ErrorCode, plus
    # "other" for non-Patter errors). Never the error message.
    "error_code": frozenset(
        {
            "config",
            "connection",
            "auth",
            "timeout",
            "rate_limit",
            "webhook_verification",
            "input_validation",
            "provider_error",
            "provision",
            "internal",
            "other",
        }
    ),
    # feature_used (pipeline): per-layer vendor of the composed stack. A
    # provider_key not on the closed allowlist collapses to "other"; an absent
    # layer is simply omitted (the value set keeps "none" only as a safety token).
    "stt_provider": STACK_VENDORS | {"none"},
    "tts_provider": STACK_VENDORS | {"none"},
    "llm_provider": STACK_VENDORS | {"none"},
    # sdk_initialized: anonymous deploy-shape (presence-only env/file probes).
    "invoked_by_agent": frozenset(
        {"claude", "cursor", "copilot", "gemini", "windsurf", "other", "none"}
    ),
    "serverless": frozenset(
        {"lambda", "cloud_run", "vercel", "azure_functions", "none"}
    ),
    "cloud": frozenset({"aws", "gcp", "azure", "fly", "none"}),
    "package_manager": frozenset(
        {"npm", "pnpm", "yarn", "bun", "pip", "uv", "poetry", "pipenv", "conda", "none"}
    ),
    "days_since_install_bucket": frozenset({"0", "1_7", "8_30", "30_plus"}),
    # agent_configured: feature-adoption (Realtime tuning).
    "noise_reduction": frozenset({"near_field", "far_field", "none"}),
    "turn_detection": frozenset({"default", "custom", "none"}),
    # call_completed: how many conversational turns the call had.
    "turn_count_bucket": frozenset({"0", "1", "2_3", "4_6", "7_12", "13_plus"}),
    # config_incomplete: coarse category of the absent required runtime config
    # blocking activation. Never the key value, env var name, or any PII.
    "missing": frozenset({"carrier_credentials", "llm_key", "engine_config", "other"}),
    # call_completed: which layer the terminal error originated in (derived from
    # CallMetrics.error_code/outcome). "none" when the call ended cleanly.
    "error_layer": frozenset(
        {"stt", "llm", "tts", "carrier", "config", "internal", "none", "other"}
    ),
    # call_completed: coarse reason the call ended (derived from terminal
    # outcome/error/AMD state). hangup_local/hangup_remote only when the side is
    # reliably known; else fall back to completed (clean end) or other.
    "disconnect_reason": frozenset(
        {
            "hangup_local",
            "hangup_remote",
            "error",
            "timeout",
            "no_answer",
            "busy",
            "completed",
            "other",
        }
    ),
    # call_started / call_completed: coarse age of the install at call time
    # (install-id file mtime vs now). "unknown" when the timestamp can't be read.
    "time_to_first_call_bucket": frozenset(
        {"lt_1h", "1h_1d", "1d_7d", "gt_7d", "unknown"}
    ),
}

# Dimensions that may accompany an event. Enum dimensions are the keys of
# ``DIMENSION_VALUES``; numeric dimensions pass through as-is (no value coercion).
# ``latency_ms`` (raw milliseconds) and ``duration_seconds`` (raw seconds) are
# operational metrics — the coarse-bucket privacy posture (which exists for cost
# and for names) does not apply to them, so they are sent at full resolution.
# The per-stage call_completed latencies (``stt_latency_ms``, ``llm_ttft_ms``,
# ``tts_first_byte_ms``, ``eou_latency_ms``) are read off the existing
# CallMetrics LatencyBreakdown — whole ints, omitted when the stage didn't run.
# ``sample_rate`` is the client-side sampling rate in (0,1] for high-frequency
# call events (analytics weight = 1/sample_rate).
_NUMERIC_DIMENSIONS = frozenset(
    {
        "builtin_tool_count",
        "latency_ms",
        "duration_seconds",
        "cost_usd",
        "stt_latency_ms",
        "llm_ttft_ms",
        "tts_first_byte_ms",
        "eou_latency_ms",
        "sample_rate",
    }
)
# feature_used (pipeline): the sanitized per-layer model token, e.g.
# ``deepgram-nova-3`` / ``anthropic-claude-haiku-4-5``. NOT a closed enum — the
# token is produced by :func:`telemetry.stack.model_token`, which already coerces
# anything that could carry PII (fine-tuned ids, self-hosted paths, custom names)
# to ``"{vendor}-other"``. :func:`build_event` re-checks the shape as defense in
# depth. The relay applies the same regex guard server-side.
# String dimensions are validated by a safe-shape regex (not a closed enum):
# the per-layer model tokens plus ``previous_sdk_version`` (a version string for
# the upgrade funnel). The regex also matches versions like ``0.6.3``.
_STRING_DIMENSIONS = frozenset(
    {"stt_model", "tts_model", "llm_model", "previous_sdk_version"}
)
_MODEL_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,40}$")
# Boolean dimensions: a feature is on/off. Kept only if the value is a real bool.
_BOOL_DIMENSIONS = frozenset(
    {"container", "preambles_used", "per_tool_timeouts_set", "llm_fallback_configured"}
)
# ID dimensions: a random SDK-generated per-call correlation id — never the
# carrier call SID. The hex32 shape is enforced as defense in depth (the SDK
# already generates a clean ``uuid.uuid4().hex``); the relay re-checks it.
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ID_DIMENSIONS = frozenset({"call_uid"})
ALLOWED_DIMENSIONS = (
    frozenset(DIMENSION_VALUES)
    | _NUMERIC_DIMENSIONS
    | _STRING_DIMENSIONS
    | _BOOL_DIMENSIONS
    | _ID_DIMENSIONS
)


def _os_family() -> str:
    """Coarse OS family only — ``linux`` / ``darwin`` / ``windows``."""
    return (platform.system() or "unknown").lower()


def _arch() -> str:
    machine = (platform.machine() or "unknown").lower()
    if machine in {"x86_64", "amd64", "x64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "other"  # keep arch low-cardinality (no raw uname strings on the wire)


def _runtime_version() -> str:
    """Runtime version bucketed to ``major.minor`` (no patch — avoid fingerprinting)."""
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def build_event(
    name: str,
    *,
    sdk_version: str,
    dimensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a telemetry event payload containing only allowlisted fields/values.

    Raises ``ValueError`` for an unknown event name (a programmer error caught by
    the caller, which swallows it). Unknown dimension keys are dropped; off-list
    enum dimension values are coerced to ``"other"``.
    """
    if name not in _ALLOWED_EVENTS:
        raise ValueError(f"unknown telemetry event: {name!r}")

    event: dict[str, Any] = {
        "event": name,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id(),
        "install_id": install_id(),
        "sdk": "python",
        "sdk_version": sdk_version,
        "os": _os_family(),
        "arch": _arch(),
        "runtime": "cpython",
        "runtime_version": _runtime_version(),
        "ci": is_ci() or is_test(),
    }

    for key, value in (dimensions or {}).items():
        if key not in ALLOWED_DIMENSIONS or value is None:
            continue
        allowed_values = DIMENSION_VALUES.get(key)
        if allowed_values is not None and value not in allowed_values:
            value = "other"  # off-list enum value can never reach the wire raw
        elif key in _STRING_DIMENSIONS:
            # Sanitized model / version token: enforce the safe shape; drop
            # anything else (the SDK already guarantees this, but never trust input).
            if not (isinstance(value, str) and _MODEL_TOKEN_RE.match(value)):
                continue
        elif key in _ID_DIMENSIONS:
            # Random SDK-generated per-call correlation id — never the carrier
            # call SID. Kept only if it is a str matching the hex32 shape; an
            # off-shape value is DROPPED (never coerced). Relay re-checks.
            if not (isinstance(value, str) and _ID_RE.match(value)):
                continue
        elif key in _BOOL_DIMENSIONS and not isinstance(value, bool):
            continue
        elif key in _NUMERIC_DIMENSIONS and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            # Numeric dims must be numbers: a buggy caller passing a string
            # under latency_ms/cost_usd would otherwise ship free text to
            # the wire — the one gap in the two-layer allowlist.
            continue
        # Scalars only — no nested objects, no free-text payloads.
        if isinstance(value, (str, bool, int, float)):
            event[key] = value

    return event
