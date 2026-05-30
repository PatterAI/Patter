"""Default provider pricing and merge utilities.

Pricing reflects public provider rates as of 2026. Each provider entry
carries provider-level defaults (the model Patter ships with by default)
plus an optional ``models`` dict mapping model identifier → per-model
overrides. The cost-calc functions take an optional ``model`` arg and
auto-resolve the rate via :func:`_resolve_provider_rates` (longest-prefix
match for versioned model IDs). When the agent's adapter exposes
``self.model`` and the metrics layer threads it through, the dashboard
bills with model accuracy out of the box — no manual override needed.

User overrides via ``Patter(pricing={...})`` keep working as before. To
add a new model rate without touching the SDK source, override the
provider entry with a merged ``models`` dict, e.g.::

    Patter(pricing={
        "elevenlabs": {"models": {"my_custom_model": {"price": 0.075}}}
    })

.. note::
    These are **estimates** based on publicly listed prices and may
    become stale as providers update their rates. Always check the
    provider's pricing page for authoritative numbers, or pass your own
    overrides via ``Patter(pricing={...})``.
"""

from __future__ import annotations

from enum import StrEnum

PRICING_VERSION: str = "2026.3"
PRICING_LAST_UPDATED: str = "2026-05-08"


def _resolve_provider_rates(provider_config: dict, model: str | None) -> dict:
    """Merge model-specific overrides on top of provider-level defaults.

    Each ``DEFAULT_PRICING`` entry can carry an optional ``models`` dict
    keyed by model identifier — values inside override the surrounding
    provider defaults for that single model. Lookup order:

    1. Exact model match in ``provider_config["models"]``.
    2. Longest-prefix match (lets ``claude-haiku-4-5-20251001`` resolve
       against ``claude-haiku-4-5`` and ``gpt-realtime-2-2026-05`` against
       ``gpt-realtime-2``).
    3. Provider defaults (the surrounding dict, ``models`` stripped).

    The returned dict is a fresh shallow copy of the provider defaults
    with model overrides applied — callers can index it like the legacy
    flat config without worrying about mutation.
    """
    base = {k: v for k, v in provider_config.items() if k != "models"}
    models = provider_config.get("models") or {}
    if not model or not models:
        return base
    override = models.get(model)
    if override is None:
        best_key = ""
        for key in models:
            if model.startswith(key) and len(key) > len(best_key):
                best_key = key
        if best_key:
            override = models[best_key]
    if override:
        base.update(override)
    return base


class PricingUnit(StrEnum):
    """Billing units used by ``DEFAULT_PRICING`` entries.

    Subclassing :class:`str` keeps the values JSON-serialisable and
    backwards-compatible with consumers that still compare against the
    raw strings (``config.get("unit") == "minute"``).
    """

    MINUTE = "minute"
    THOUSAND_CHARS = "1k_chars"
    TOKEN = "token"


DEFAULT_PRICING: dict[str, dict] = {
    # STT — per minute of audio processed.
    # Provider defaults reflect the model Patter ships with by default.
    # Per-model rates live under ``models`` and are auto-resolved when the
    # adapter exposes its model identifier (see ``_resolve_provider_rates``).
    "deepgram": {
        "unit": PricingUnit.MINUTE,
        # Default = Nova-3 streaming monolingual ($0.0048/min, current Pay-
        # As-You-Go promotional rate). Source: https://deepgram.com/pricing
        # (verified 2026-05-11). The promo replaces the standard $0.0077/min
        # quoted at Nova-3 launch and is the rate customers actually pay
        # today; revisit when Deepgram removes the "Limited-time
        # promotional rates on streaming" banner.
        "price": 0.0048,
        "models": {
            # Nova-3 family — current flagship.
            "nova-3": {"price": 0.0048},
            "nova-3-multilingual": {"price": 0.0058},
            # Flux family — new event-driven turn-taking STT (2026 launch).
            "flux": {"price": 0.0065},
            "flux-english": {"price": 0.0065},
            "flux-multilingual": {"price": 0.0078},
            # Legacy Nova-2 / Nova-1 — still supported but no longer
            # featured on the public pricing page; rates kept as last
            # verified ($0.0058 / $0.0043 per min).
            "nova-2": {"price": 0.0058},
            "nova": {"price": 0.0043},
            # Whisper Cloud via Deepgram — separate tier.
            "whisper-large": {"price": 0.0048},
            "whisper-medium": {"price": 0.0048},
        },
    },
    "whisper": {
        "unit": PricingUnit.MINUTE,
        # Default = whisper-1 REST ($0.006/min).
        "price": 0.006,
        "models": {
            "whisper-1": {"price": 0.006},
            # GPT-4o transcribe family — same REST endpoint, different rates.
            "gpt-4o-transcribe": {"price": 0.006},
            "gpt-4o-mini-transcribe": {"price": 0.003},
            # Streaming Whisper variant used inside Realtime sessions.
            "gpt-realtime-whisper": {"price": 0.017},
        },
    },
    # OpenAI standalone transcription endpoint (separate ``provider_key``
    # from ``whisper`` so the dashboard can distinguish them).
    "openai_transcribe": {
        "unit": PricingUnit.MINUTE,
        "price": 0.006,
        "models": {
            "gpt-4o-transcribe": {"price": 0.006},
            "gpt-4o-mini-transcribe": {"price": 0.003},
            "whisper-1": {"price": 0.006},
        },
    },
    # AssemblyAI Universal-Streaming: $0.15/hr = $0.0025/min
    "assemblyai": {"unit": PricingUnit.MINUTE, "price": 0.0025},
    # Cartesia ink-whisper streaming STT: ~$0.15/hr on usage plans
    "cartesia_stt": {"unit": PricingUnit.MINUTE, "price": 0.0025},
    # Soniox real-time STT: $0.12/hr = $0.002/min
    "soniox": {"unit": PricingUnit.MINUTE, "price": 0.002},
    # Speechmatics Pro tier: $0.24/hr = $0.0040/min (new users land here).
    # Previous $0.0173 reflected a retired Standard tier; users were
    # being over-billed ~4.3x.
    "speechmatics": {"unit": PricingUnit.MINUTE, "price": 0.004},
    # TTS — per 1,000 characters synthesized.
    # Source: https://elevenlabs.io/pricing/api (verified 2026-05-11). The
    # per-1K-character API/overage rate is flat across all plan tiers (Free
    # through Business); only the included character bundle varies by plan.
    "elevenlabs": {
        "unit": PricingUnit.THOUSAND_CHARS,
        # Default = eleven_flash_v2_5 (the Patter default model) at $0.05/1k.
        "price": 0.05,
        "models": {
            "eleven_flash_v2_5": {"price": 0.05},
            "eleven_turbo_v2_5": {"price": 0.05},
            "eleven_multilingual_v2": {"price": 0.10},
            "eleven_monolingual_v1": {"price": 0.10},
            "eleven_v3": {"price": 0.10},
        },
    },
    # ElevenLabs WebSocket streaming TTS shares pricing with REST.
    "elevenlabs_ws": {
        "unit": PricingUnit.THOUSAND_CHARS,
        "price": 0.05,
        "models": {
            "eleven_flash_v2_5": {"price": 0.05},
            "eleven_turbo_v2_5": {"price": 0.05},
            "eleven_multilingual_v2": {"price": 0.10},
            "eleven_v3": {"price": 0.10},
        },
    },
    "openai_tts": {
        "unit": PricingUnit.THOUSAND_CHARS,
        # Default = tts-1 ($0.015/1k chars).
        "price": 0.015,
        "models": {
            "tts-1": {"price": 0.015},
            "tts-1-hd": {"price": 0.030},
            # gpt-4o-mini-tts is billed by tokens upstream but published per
            # 1k chars equivalent here for parity with the rest of the table.
            "gpt-4o-mini-tts": {"price": 0.012},
        },
    },
    # Legacy alias preserved for backward compat with users who set
    # provider_key="openai_tts_hd" in their own adapters.
    "openai_tts_hd": {"unit": PricingUnit.THOUSAND_CHARS, "price": 0.030},
    "cartesia_tts": {
        "unit": PricingUnit.THOUSAND_CHARS,
        # Default = Sonic-2 (current Cartesia flagship) at ~$0.030/1k chars.
        "price": 0.030,
        "models": {
            "sonic-2": {"price": 0.030},
            "sonic-1": {"price": 0.030},
            "sonic-english": {"price": 0.030},
            "sonic-multilingual": {"price": 0.030},
        },
    },
    "rime": {
        "unit": PricingUnit.THOUSAND_CHARS,
        # Default = mistv2 ($0.030/1k chars).
        "price": 0.030,
        "models": {
            "mistv2": {"price": 0.030},
            "mist": {"price": 0.030},
            "arcana": {"price": 0.040},
        },
    },
    "lmnt": {
        "unit": PricingUnit.THOUSAND_CHARS,
        # Default = aurora ($0.050/1k chars).
        "price": 0.050,
        "models": {
            "aurora": {"price": 0.050},
            "blizzard": {"price": 0.050},
        },
    },
    "inworld": {
        "unit": PricingUnit.THOUSAND_CHARS,
        # Default = inworld-tts-2 (placeholder rate — verify against tier).
        "price": 0.020,
        "models": {
            "inworld-tts-2": {"price": 0.020},
            "inworld-tts-1.5-max": {"price": 0.025},
            "inworld-tts-1.5": {"price": 0.025},
        },
    },
    # OpenAI Realtime — per token (actual tokens from response.done usage).
    # Provider defaults match ``gpt-4o-mini-realtime-preview`` /
    # ``gpt-realtime-mini`` (the Patter default). Per-model overrides under
    # ``models`` are auto-resolved when the realtime adapter's model is
    # threaded through ``calculate_realtime_cost(usage, pricing, model=...)``.
    "openai_realtime": {
        "unit": PricingUnit.TOKEN,
        # Default rates: gpt-realtime-mini / gpt-4o-mini-realtime-preview
        #   audio  input  $10  / M  ->  0.00001    per token
        #   audio  output $20  / M  ->  0.00002    per token
        #   text   input  $0.60/ M  ->  0.0000006  per token
        #   text   output $2.40/ M  ->  0.0000024  per token
        "audio_input_per_token": 0.00001,
        "audio_output_per_token": 0.00002,
        "text_input_per_token": 0.0000006,
        "text_output_per_token": 0.0000024,
        # Prompt caching rates (official): audio cached $0.30/M ~= 3% of full,
        # text cached $0.06/M = 10% of full. OpenAI bills the cached portion
        # of input_token_details.{audio,text}_tokens at these reduced rates.
        "cached_audio_input_per_token": 0.0000003,
        "cached_text_input_per_token": 0.00000006,
        "models": {
            # gpt-realtime (GA, August 2025): audio in $32/M, audio out $64/M,
            # text in $4/M, text out $16/M, cached audio $0.40/M, cached text
            # $0.40/M. Roughly 3x the mini for audio; matches the published
            # platform.openai.com/docs/pricing as of 2026-05.
            "gpt-realtime": {
                "audio_input_per_token": 0.000032,
                "audio_output_per_token": 0.000064,
                "text_input_per_token": 0.000004,
                "text_output_per_token": 0.000016,
                "cached_audio_input_per_token": 0.0000004,
                "cached_text_input_per_token": 0.0000004,
            },
            # gpt-realtime-2 (most-capable): audio in $32/M, audio out $64/M,
            # text in $4/M, text out $24/M, cached $0.40/M (audio + text).
            "gpt-realtime-2": {
                "audio_input_per_token": 0.000032,
                "audio_output_per_token": 0.000064,
                "text_input_per_token": 0.000004,
                "text_output_per_token": 0.000024,
                "cached_audio_input_per_token": 0.0000004,
                "cached_text_input_per_token": 0.0000004,
            },
            # gpt-realtime-mini and gpt-4o-mini-realtime-preview share the
            # provider defaults. Listed explicitly so tooling can introspect.
            "gpt-realtime-mini": {
                "audio_input_per_token": 0.00001,
                "audio_output_per_token": 0.00002,
                "text_input_per_token": 0.0000006,
                "text_output_per_token": 0.0000024,
                "cached_audio_input_per_token": 0.0000003,
                "cached_text_input_per_token": 0.00000006,
            },
            "gpt-4o-mini-realtime-preview": {
                "audio_input_per_token": 0.00001,
                "audio_output_per_token": 0.00002,
                "text_input_per_token": 0.0000006,
                "text_output_per_token": 0.0000024,
                "cached_audio_input_per_token": 0.0000003,
                "cached_text_input_per_token": 0.00000006,
            },
            # gpt-4o-realtime-preview (legacy preview, ~10x mini for audio):
            # audio in $100/M, audio out $200/M, text in $5/M, text out $20/M.
            "gpt-4o-realtime-preview": {
                "audio_input_per_token": 0.0001,
                "audio_output_per_token": 0.0002,
                "text_input_per_token": 0.000005,
                "text_output_per_token": 0.000020,
                "cached_audio_input_per_token": 0.0000020,
                "cached_text_input_per_token": 0.0000025,
            },
        },
    },
    # Telephony — per minute of call duration.
    # twilio default = US inbound local (the 99% case for voice agents
    # receiving calls on a local number). For US toll-free inbound ($0.022/min)
    # or US outbound local ($0.0140/min), override via Patter(pricing={...}).
    "twilio": {"unit": PricingUnit.MINUTE, "price": 0.0085, "round_up": True},
    # Telnyx — direction-aware rates as of 2026-05-11.
    # Sources:
    #   https://telnyx.com/pricing/elastic-sip
    #   https://telnyx.com/pricing/voice-api
    # US inbound (DID / local termination, Pay-As-You-Go): $0.0035/min
    # US outbound (Pay-As-You-Go, mid-range of $0.005-$0.009): $0.007/min
    # Billing granularity is per-MINUTE (Telnyx rounds partial minutes up
    # on the invoice; prior internal docs incorrectly claimed per-second).
    # The legacy ``telnyx`` key is preserved at the outbound rate as a
    # safe fallback for users who override ``pricing={"telnyx": {...}}``
    # without knowing the direction; the metrics layer currently uses
    # this flat key (direction is not threaded through to
    # ``calculate_telephony_cost``). Direction-aware billing can be
    # enabled by override-only: ``Patter(pricing={"telnyx":
    # {"unit": "minute", "price": 0.0035}})`` to bill all inbound at
    # the lower rate.
    "telnyx": {"unit": PricingUnit.MINUTE, "price": 0.007},
    "telnyx_inbound": {"unit": PricingUnit.MINUTE, "price": 0.0035},
    "telnyx_outbound": {"unit": PricingUnit.MINUTE, "price": 0.007},
    # Plivo — official US pay-as-you-go voice rates (per minute; Plivo rounds
    # partial minutes up like Twilio). Source: https://www.plivo.com/voice/pricing/
    #   US local inbound:   $0.0055/min
    #   US local outbound:  $0.0115/min
    #   US toll-free inbound: $0.0180/min (override via Patter(pricing=...))
    # The flat ``plivo`` key defaults to inbound local (the 99% case for an
    # inbound voice agent); the metrics layer uses it since direction is not
    # threaded through ``calculate_telephony_cost``. The actual billed amount
    # is reconciled post-call from the Plivo CDR (``total_amount``).
    "plivo": {"unit": PricingUnit.MINUTE, "price": 0.0055, "round_up": True},
    "plivo_inbound": {"unit": PricingUnit.MINUTE, "price": 0.0055, "round_up": True},
    "plivo_outbound": {"unit": PricingUnit.MINUTE, "price": 0.0115, "round_up": True},
}


def _clone_provider_entry(entry: dict) -> dict:
    """Deep-ish copy of a provider pricing entry (one level into ``models``)."""
    out: dict = {}
    for k, v in entry.items():
        if k == "models" and isinstance(v, dict):
            out[k] = {mk: dict(mv) for mk, mv in v.items()}
        else:
            out[k] = v
    return out


def merge_pricing(overrides: dict | None) -> dict:
    """Merge user overrides into a copy of DEFAULT_PRICING.

    Performs a per-provider shallow merge with one exception: the nested
    ``models`` dict is itself merged shallowly (per-model entries replace
    the default entry but unmentioned models keep their built-in rates).
    A user override of ``{"deepgram": {"models": {"nova-2": {"price": 0.01}}}}``
    keeps every other Deepgram model rate intact.
    """
    merged = {k: _clone_provider_entry(v) for k, v in DEFAULT_PRICING.items()}
    if not overrides:
        return merged
    for provider, values in overrides.items():
        if provider not in merged:
            merged[provider] = _clone_provider_entry(values)
            continue
        target = merged[provider]
        for k, v in values.items():
            if (
                k == "models"
                and isinstance(v, dict)
                and isinstance(target.get("models"), dict)
            ):
                # Per-model overlay — keep models the user did NOT mention.
                merged_models = dict(target["models"])
                for mk, mv in v.items():
                    merged_models[mk] = dict(mv) if isinstance(mv, dict) else mv
                target["models"] = merged_models
            else:
                target[k] = v
    return merged


def calculate_stt_cost(
    provider: str,
    audio_seconds: float,
    pricing: dict,
    model: str | None = None,
) -> float:
    """Calculate STT cost from audio duration.

    When ``model`` is supplied and the provider entry has a matching
    ``models`` override, the per-model rate is used; otherwise falls back
    to the provider-level rate (legacy behavior, model=None).
    """
    config = pricing.get(provider, {})
    rates = _resolve_provider_rates(config, model)
    if rates.get("unit") == "minute":
        return (audio_seconds / 60.0) * rates.get("price", 0.0)
    return 0.0


def calculate_tts_cost(
    provider: str,
    character_count: int,
    pricing: dict,
    model: str | None = None,
) -> float:
    """Calculate TTS cost from character count.

    When ``model`` is supplied and the provider entry has a matching
    ``models`` override, the per-model rate is used; otherwise falls back
    to the provider-level rate (legacy behavior, model=None).
    """
    config = pricing.get(provider, {})
    rates = _resolve_provider_rates(config, model)
    if rates.get("unit") == "1k_chars":
        return (character_count / 1000.0) * rates.get("price", 0.0)
    return 0.0


def calculate_realtime_cost(
    usage: dict,
    pricing: dict,
    model: str | None = None,
) -> float:
    """Calculate OpenAI Realtime cost from token usage in ``response.done``.

    Args:
        usage: The ``response.usage`` dict from an OpenAI ``response.done``
            event.  Expected keys: ``input_token_details``,
            ``output_token_details``.
        pricing: Merged pricing dict.

    Returns:
        Total cost in USD for this response.
    """
    config = pricing.get("openai_realtime", {})
    rates = _resolve_provider_rates(config, model)
    if rates.get("unit") != "token":
        return 0.0

    # Guard against OpenAI sending ``"input_token_details": null`` — dict.get
    # returns None in that case and the chained .get() would crash.
    input_details = usage.get("input_token_details") or {}
    output_details = usage.get("output_token_details") or {}
    details = input_details.get("cached_tokens_details") or {}

    cached_audio_rate = rates.get(
        "cached_audio_input_per_token", rates.get("audio_input_per_token", 0)
    )
    cached_text_rate = rates.get(
        "cached_text_input_per_token", rates.get("text_input_per_token", 0)
    )

    total_audio_in = input_details.get("audio_tokens", 0)
    total_text_in = input_details.get("text_tokens", 0)

    # Prefer cached_tokens_details breakdown. When absent (some Azure OpenAI
    # responses) fall back to the top-level cached_tokens scalar and pro-rate
    # by the audio/text split so the discount still applies.
    if details and ("audio_tokens" in details or "text_tokens" in details):
        cached_audio_in = min(details.get("audio_tokens", 0), total_audio_in)
        cached_text_in = min(details.get("text_tokens", 0), total_text_in)
    elif input_details.get("cached_tokens", 0) > 0:
        cached_total = input_details["cached_tokens"]
        total_in = total_audio_in + total_text_in
        ratio = (cached_total / total_in) if total_in > 0 else 0
        cached_audio_in = min(round(total_audio_in * ratio), total_audio_in)
        cached_text_in = min(round(total_text_in * ratio), total_text_in)
    else:
        cached_audio_in = 0
        cached_text_in = 0

    cost = 0.0
    cost += (total_audio_in - cached_audio_in) * rates.get("audio_input_per_token", 0)
    cost += cached_audio_in * cached_audio_rate
    cost += (total_text_in - cached_text_in) * rates.get("text_input_per_token", 0)
    cost += cached_text_in * cached_text_rate
    cost += output_details.get("audio_tokens", 0) * rates.get(
        "audio_output_per_token", 0
    )
    cost += output_details.get("text_tokens", 0) * rates.get("text_output_per_token", 0)
    # Clamp ≥0 — mis-configured cached rates can never produce negative bill.
    return max(0.0, cost)


def calculate_realtime_cached_savings(
    usage: dict,
    pricing: dict,
    model: str | None = None,
) -> float:
    """How much would have been paid if the cached portion of input tokens had
    been billed at the full rate. Used to expose a "saved from prompt caching"
    figure on the dashboard.
    """
    config = pricing.get("openai_realtime", {})
    rates = _resolve_provider_rates(config, model)
    if rates.get("unit") != "token":
        return 0.0
    input_details = usage.get("input_token_details") or {}
    cached = input_details.get("cached_tokens_details") or {}

    cached_audio_rate = rates.get(
        "cached_audio_input_per_token", rates.get("audio_input_per_token", 0)
    )
    cached_text_rate = rates.get(
        "cached_text_input_per_token", rates.get("text_input_per_token", 0)
    )

    total_audio = input_details.get("audio_tokens", 0)
    total_text = input_details.get("text_tokens", 0)

    # Prefer cached_tokens_details breakdown. When absent (e.g. Azure OpenAI
    # responses that only provide the top-level cached_tokens scalar), fall
    # back to pro-rating by the audio/text split — mirrors the same fallback
    # in calculate_realtime_cost so savings and cost figures stay consistent.
    if cached and ("audio_tokens" in cached or "text_tokens" in cached):
        cached_audio = min(cached.get("audio_tokens", 0), total_audio)
        cached_text = min(cached.get("text_tokens", 0), total_text)
    elif input_details.get("cached_tokens", 0) > 0:
        cached_total = input_details["cached_tokens"]
        total_in = total_audio + total_text
        ratio = (cached_total / total_in) if total_in > 0 else 0
        cached_audio = min(round(total_audio * ratio), total_audio)
        cached_text = min(round(total_text * ratio), total_text)
    else:
        cached_audio = 0
        cached_text = 0

    full_cost = cached_audio * rates.get(
        "audio_input_per_token", 0
    ) + cached_text * rates.get("text_input_per_token", 0)
    discounted_cost = cached_audio * cached_audio_rate + cached_text * cached_text_rate
    # Clamp >= 0. If a user overrides cached_*_input_per_token to a rate HIGHER
    # than full, the diff becomes negative -- meaningless as a savings figure,
    # so we return 0 instead of a negative number. Matches TS parity.
    return max(0.0, full_cost - discounted_cost)


# ---------------------------------------------------------------------------
# Chat/completion LLM pricing (per 1M tokens)
# ---------------------------------------------------------------------------
#
# Rates reflect publicly listed provider pricing as of PRICING_LAST_UPDATED.
# ``input`` / ``output`` are dollars per 1M tokens. Anthropic adds
# ``cache_read`` (~10% of full input) and ``cache_write`` (~125% of full input)
# for prompt caching. Groq / Cerebras / Google do not publicly expose cache
# rates for these models, so only input/output are populated.
LLM_PRICING: dict[str, dict[str, dict[str, float]]] = {
    "openai": {
        # Chat Completions LLM pricing (not Realtime — see DEFAULT_PRICING["openai_realtime"]).
        # Rates: per 1M tokens as of 2026-04-24.
        "gpt-4o": {"input": 2.50, "output": 10.00, "cache_read": 1.25},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075},
        "gpt-4.1": {"input": 3.00, "output": 12.00, "cache_read": 0.75},
        "gpt-4.1-mini": {"input": 0.80, "output": 3.20, "cache_read": 0.20},
        "o3": {"input": 2.00, "output": 8.00, "cache_read": 0.50},
        "o4-mini": {"input": 1.10, "output": 4.40, "cache_read": 0.275},
    },
    "anthropic": {
        "claude-opus-4-7": {
            "input": 15.0,
            "output": 75.0,
            "cache_read": 1.5,
            "cache_write": 18.75,
        },
        "claude-sonnet-4-6": {
            "input": 3.0,
            "output": 15.0,
            "cache_read": 0.3,
            "cache_write": 3.75,
        },
        "claude-haiku-4-5": {
            "input": 1.0,
            "output": 5.0,
            "cache_read": 0.1,
            "cache_write": 1.25,
        },
    },
    "google": {
        "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
        "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
        "gemini-live-2.5-flash-native-audio": {"input": 0.30, "output": 2.50},
    },
    "groq": {
        # Rates as of 2026-05-08; verify against groq.com/pricing.
        # Production-tier ``llama-3.3-70b-versatile`` is the Patter default for
        # Groq. The remaining models are reachable via ``model="..."`` and were
        # silently billing $0 before this entry was added.
        "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
        "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
        "llama-3.3-70b-specdec": {"input": 0.59, "output": 0.99},
        "llama3-70b-8192": {"input": 0.59, "output": 0.79},
        "llama3-8b-8192": {"input": 0.05, "output": 0.08},
        "mixtral-8x7b-32768": {"input": 0.27, "output": 0.27},
        "gemma2-9b-it": {"input": 0.20, "output": 0.20},
    },
    "cerebras": {
        # Rates as of 2026-05-11 verified against the canonical per-model docs
        # pages at ``https://inference-docs.cerebras.ai/models/<model>``. The
        # previous 2026-05-08 update overcharged across the board (gpt-oss-120b
        # 2.4x input, qwen-3-235b 1.67x input) because it conflated the launch
        # blog quotes with the "Exploration pricing" banner now shown on each
        # model page. Each entry below cites the docs URL it was sourced from.
        "gpt-oss-120b": {"input": 0.35, "output": 0.75},
        "llama3.1-8b": {"input": 0.10, "output": 0.10},
        "llama-3.3-70b": {"input": 0.85, "output": 1.20},
        "qwen-3-32b": {"input": 0.40, "output": 0.80},
        "qwen-3-235b-a22b-instruct-2507": {"input": 0.60, "output": 1.20},
        "qwen-3-coder-480b": {"input": 2.00, "output": 2.00},
        "zai-glm-4.7": {"input": 0.85, "output": 1.20},
    },
}


def calculate_llm_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Calculate LLM cost from token counts using :data:`LLM_PRICING`.

    Args:
        provider: Provider key in :data:`LLM_PRICING` (``"anthropic"``,
            ``"google"``, ``"groq"``, ``"cerebras"``).
        model: Model identifier under the provider (e.g.
            ``"claude-haiku-4-5"``).
        input_tokens: Non-cached input tokens billed at the full rate.
            Callers should subtract ``cache_read_tokens`` before passing
            this value when they also pass cache_read_tokens separately.
        output_tokens: Output tokens billed at the output rate.
        cache_read_tokens: Input tokens served from Anthropic's prompt
            cache; billed at the reduced ``cache_read`` rate.
        cache_write_tokens: Input tokens that populated the cache this
            call; billed at the ``cache_write`` rate.

    Returns:
        Total cost in USD. Returns ``0.0`` when the provider/model is not
        listed so unknown models never produce bogus line items.
    """
    provider_table = LLM_PRICING.get(provider, {})
    rates = provider_table.get(model, {})
    if not rates:
        # Fall back to the longest matching prefix in the provider's
        # rate table. Lets us handle versioned model IDs like
        # ``claude-haiku-4-5-20251001`` against a base entry of
        # ``claude-haiku-4-5`` without forcing an exact match.
        best_key = ""
        for key in provider_table:
            if model.startswith(key) and len(key) > len(best_key):
                best_key = key
        if best_key:
            rates = provider_table.get(best_key, {})
        if not rates:
            return 0.0

    # Per-1M-tokens rates are divided by 1_000_000 per token.
    cost = 0.0
    cost += (input_tokens / 1_000_000.0) * rates.get("input", 0.0)
    cost += (output_tokens / 1_000_000.0) * rates.get("output", 0.0)
    cost += (cache_read_tokens / 1_000_000.0) * rates.get("cache_read", 0.0)
    cost += (cache_write_tokens / 1_000_000.0) * rates.get("cache_write", 0.0)
    return max(0.0, cost)


def calculate_telephony_cost(
    provider: str, duration_seconds: float, pricing: dict
) -> float:
    """Calculate telephony cost from call duration.

    Twilio bills in whole-minute increments (any partial minute rounded up
    per twilio.com/help/223132307). Telnyx bills per-second. Detection is
    by provider name.
    """
    import math

    config = pricing.get(provider, {})
    if config.get("unit") != "minute":
        return 0.0
    if config.get("round_up"):
        minutes = math.ceil(duration_seconds / 60.0)
    else:
        minutes = duration_seconds / 60.0
    return minutes * config.get("price", 0.0)
