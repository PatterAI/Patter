"""Anonymous capture of the composed voice stack (carrier + STT + TTS + LLM).

For a **pipeline** agent the interesting telemetry is *which providers and models
were composed*. Each provider adapter already exposes a stable ``provider_key``
class constant (minification-safe, the canonical vendor id) and stores its
configured model string. This module turns those into two anonymous fields per
layer: a low-cardinality **vendor** (closed allowlist) and a sanitized **model**
token.

PII safety for the model token (the one field that is not a closed enum): a model
string can embed customer data — fine-tuned ids (``ft:gpt-4o:acme-corp:...``),
self-hosted endpoints/paths, or custom names. The sanitizer therefore emits
``"{vendor}-other"`` for anything containing ``:`` / ``/`` / whitespace / unusual
characters or longer than 40 chars, so a brand or org name can never reach the
wire. Known public model ids (``nova-3``, ``gpt-4o``, ``claude-haiku-4-5``…) pass
through as ``"{vendor}-{token}"`` with the trailing release date stripped so date
variants collapse together. Keep this byte-for-byte identical to the TypeScript
``stack.ts``.
"""

from __future__ import annotations

import re
from typing import Any

# Closed vendor allowlist for the per-layer ``*_provider`` fields. A provider_key
# not resolvable to one of these collapses to "other"; an absent layer is "none".
STACK_VENDORS: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "google",
        "cerebras",
        "groq",
        "deepgram",
        "elevenlabs",
        "cartesia",
        "whisper",
        "soniox",
        "assemblyai",
        "speechmatics",
        "lmnt",
        "rime",
        "inworld",
        "telnyx",
        "other",
    }
)

# provider_key values that carry a layer suffix or alias → canonical vendor.
_VENDOR_ALIASES: dict[str, str] = {
    "cartesia_stt": "cartesia",
    "cartesia_tts": "cartesia",
    "openai_tts": "openai",
    "openai_transcribe": "openai",
    "elevenlabs_ws": "elevenlabs",
    "telnyx_stt": "telnyx",
    "telnyx_tts": "telnyx",
}

# A sanitized model token: starts alphanumeric, then [a-z0-9.-], <= 41 chars total.
_MODEL_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,40}$")
# Raw model is only "safe" if it is short and uses [a-z0-9._-] (blocks ":" "/" …).
_RAW_UNSAFE_RE = re.compile(r"[^a-z0-9._-]")
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def vendor_of(provider_key: str | None) -> str:
    """Map a provider's ``provider_key`` to the closed vendor allowlist."""
    if not provider_key:
        return "other"
    v = _VENDOR_ALIASES.get(provider_key, provider_key)
    return v if v in STACK_VENDORS else "other"


def model_token(vendor: str, raw_model: str | None) -> str:
    """Sanitize a raw model string into ``"{vendor}-{token}"`` (or ``-other``).

    Returns ``"{vendor}-other"`` for anything that could carry PII (fine-tuned
    ids, self-hosted paths, custom names) — anything with ``:`` / ``/`` /
    whitespace / non ``[a-z0-9._-]`` chars, or longer than 40 chars.
    """
    if not raw_model:
        return f"{vendor}-other"
    m = raw_model.strip().lower()
    if len(m) > 40 or _RAW_UNSAFE_RE.search(m):
        return f"{vendor}-other"
    token = _DATE_SUFFIX_RE.sub("", m.replace("_", "-")).strip("-.")
    return f"{vendor}-{token}" if token else f"{vendor}-other"


def _provider_key(obj: Any) -> str | None:
    key = getattr(obj, "provider_key", None)
    return key if isinstance(key, str) and key else None


def _model_of(obj: Any) -> str:
    """Best-effort read of a provider's configured model string."""
    for attr in ("model", "model_id"):
        v = getattr(obj, attr, None)
        if isinstance(v, str) and v:
            return v
    # Some adapters keep it private or under an options object.
    v = getattr(obj, "_model", None)
    if isinstance(v, str) and v:
        return v
    opts = getattr(obj, "_opts", None)
    v = getattr(opts, "model", None) if opts is not None else None
    return v if isinstance(v, str) and v else ""


def _layer_dims(obj: Any, provider_field: str, model_field: str) -> dict[str, str]:
    """Return ``{provider_field, model_field}`` for one stack layer (or empty)."""
    if obj is None:
        return {}
    vendor = vendor_of(_provider_key(obj))
    return {provider_field: vendor, model_field: model_token(vendor, _model_of(obj))}


def stack_dimensions(stt: Any, tts: Any, llm: Any) -> dict[str, str]:
    """Anonymous per-layer vendor+model dims for a composed (pipeline) agent.

    Layers that are absent (e.g. realtime/convai engines) are simply omitted —
    ``build_event`` drops missing dimensions, so no ``"none"`` noise is emitted.
    """
    dims: dict[str, str] = {}
    dims.update(_layer_dims(stt, "stt_provider", "stt_model"))
    dims.update(_layer_dims(tts, "tts_provider", "tts_model"))
    dims.update(_layer_dims(llm, "llm_provider", "llm_model"))
    return dims
