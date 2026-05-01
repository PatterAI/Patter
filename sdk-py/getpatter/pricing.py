"""Default provider pricing and merge utilities.

Pricing reflects public provider rates as of 2026. These defaults are
calibrated for the default models Patter ships with — notably
``gpt-4o-mini-realtime-preview`` for OpenAI Realtime. If you pick a
different model (e.g. ``gpt-4o-realtime-preview`` or ``gpt-realtime``),
override the ``openai_realtime`` entry via the ``pricing`` option on
``Patter()`` so the dashboard cost display matches what OpenAI actually
bills.

.. note::
    These are **estimates** based on publicly listed prices and may
    become stale as providers update their rates. Always check the
    provider's pricing page for authoritative numbers, or pass your own
    overrides via ``Patter(pricing={...})``.
"""

from __future__ import annotations

from enum import StrEnum

PRICING_VERSION: str = "2026.2"
PRICING_LAST_UPDATED: str = "2026-04-24"


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
    # Deepgram Nova-3 streaming (monolingual) — $0.0077/min. The previous
    # $0.0043 was the batch rate; streaming is ~80% more expensive.
    # Multilingual Nova-3 is $0.0092/min — override when needed.
    "deepgram": {"unit": PricingUnit.MINUTE, "price": 0.0077},
    "whisper": {"unit": PricingUnit.MINUTE, "price": 0.006},
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
    # ElevenLabs default model is eleven_flash_v2_5 at $0.06/1k via direct API.
    # The previous $0.18 matched only the Creator plan overage rate.
    "elevenlabs": {"unit": PricingUnit.THOUSAND_CHARS, "price": 0.06},
    "openai_tts": {"unit": PricingUnit.THOUSAND_CHARS, "price": 0.015},
    "openai_tts_hd": {"unit": PricingUnit.THOUSAND_CHARS, "price": 0.030},
    # Cartesia Sonic TTS: ~$0.030/1k chars on usage plans
    "cartesia_tts": {"unit": PricingUnit.THOUSAND_CHARS, "price": 0.030},
    # Rime mist v2: $0.030/1k chars pay-as-you-go
    "rime": {"unit": PricingUnit.THOUSAND_CHARS, "price": 0.030},
    # LMNT aurora/blizzard: $0.050/1k chars Indie overage
    "lmnt": {"unit": PricingUnit.THOUSAND_CHARS, "price": 0.050},
    # OpenAI Realtime — per token (actual tokens from response.done usage).
    # Calibrated for gpt-4o-mini-realtime-preview (the Patter default):
    #   audio  input  $10  / M  ->  0.00001    per token
    #   audio  output $20  / M  ->  0.00002    per token
    #   text   input  $0.60/ M  ->  0.0000006  per token
    #   text   output $2.40/ M  ->  0.0000024  per token
    # For gpt-4o-realtime-preview multiply by ~10, for gpt-realtime by ~3.
    "openai_realtime": {
        "unit": PricingUnit.TOKEN,
        "audio_input_per_token": 0.00001,
        "audio_output_per_token": 0.00002,
        "text_input_per_token": 0.0000006,
        "text_output_per_token": 0.0000024,
        # Prompt caching rates (official): audio cached $0.30/M ~= 3% of full,
        # text cached $0.06/M = 10% of full. OpenAI bills the cached portion
        # of input_token_details.{audio,text}_tokens at these reduced rates.
        "cached_audio_input_per_token": 0.0000003,
        "cached_text_input_per_token": 0.00000006,
    },
    # Telephony — per minute of call duration.
    # twilio default = US inbound local (the 99% case for voice agents
    # receiving calls on a local number). For US toll-free inbound ($0.022/min)
    # or US outbound local ($0.0140/min), override via Patter(pricing={...}).
    "twilio": {"unit": PricingUnit.MINUTE, "price": 0.0085},
    "telnyx": {"unit": PricingUnit.MINUTE, "price": 0.007},
}


def merge_pricing(overrides: dict | None) -> dict:
    """Merge user overrides into a copy of DEFAULT_PRICING.

    Performs a shallow per-provider merge: if the user overrides
    ``{"deepgram": {"price": 0.005}}``, the ``"unit"`` key is preserved
    from the default.
    """
    merged = {k: dict(v) for k, v in DEFAULT_PRICING.items()}
    if not overrides:
        return merged
    for provider, values in overrides.items():
        if provider in merged:
            merged[provider].update(values)
        else:
            merged[provider] = dict(values)
    return merged


def calculate_stt_cost(provider: str, audio_seconds: float, pricing: dict) -> float:
    """Calculate STT cost from audio duration."""
    config = pricing.get(provider, {})
    if config.get("unit") == "minute":
        return (audio_seconds / 60.0) * config.get("price", 0.0)
    return 0.0


def calculate_tts_cost(provider: str, character_count: int, pricing: dict) -> float:
    """Calculate TTS cost from character count."""
    config = pricing.get(provider, {})
    if config.get("unit") == "1k_chars":
        return (character_count / 1000.0) * config.get("price", 0.0)
    return 0.0


def calculate_realtime_cost(usage: dict, pricing: dict) -> float:
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
    if config.get("unit") != "token":
        return 0.0

    # Guard against OpenAI sending ``"input_token_details": null`` — dict.get
    # returns None in that case and the chained .get() would crash.
    input_details = usage.get("input_token_details") or {}
    output_details = usage.get("output_token_details") or {}
    details = input_details.get("cached_tokens_details") or {}

    cached_audio_rate = config.get(
        "cached_audio_input_per_token", config.get("audio_input_per_token", 0)
    )
    cached_text_rate = config.get(
        "cached_text_input_per_token", config.get("text_input_per_token", 0)
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
    cost += (total_audio_in - cached_audio_in) * config.get("audio_input_per_token", 0)
    cost += cached_audio_in * cached_audio_rate
    cost += (total_text_in - cached_text_in) * config.get("text_input_per_token", 0)
    cost += cached_text_in * cached_text_rate
    cost += output_details.get("audio_tokens", 0) * config.get(
        "audio_output_per_token", 0
    )
    cost += output_details.get("text_tokens", 0) * config.get(
        "text_output_per_token", 0
    )
    # Clamp ≥0 — mis-configured cached rates can never produce negative bill.
    return max(0.0, cost)


def calculate_realtime_cached_savings(usage: dict, pricing: dict) -> float:
    """How much would have been paid if the cached portion of input tokens had
    been billed at the full rate. Used to expose a "saved from prompt caching"
    figure on the dashboard.
    """
    config = pricing.get("openai_realtime", {})
    if config.get("unit") != "token":
        return 0.0
    input_details = usage.get("input_token_details") or {}
    cached = input_details.get("cached_tokens_details") or {}

    cached_audio_rate = config.get(
        "cached_audio_input_per_token", config.get("audio_input_per_token", 0)
    )
    cached_text_rate = config.get(
        "cached_text_input_per_token", config.get("text_input_per_token", 0)
    )

    total_audio = input_details.get("audio_tokens", 0)
    total_text = input_details.get("text_tokens", 0)
    cached_audio = min(cached.get("audio_tokens", 0), total_audio)
    cached_text = min(cached.get("text_tokens", 0), total_text)

    full_cost = cached_audio * config.get(
        "audio_input_per_token", 0
    ) + cached_text * config.get("text_input_per_token", 0)
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
        "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
        "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
    },
    "cerebras": {
        "llama-3.3-70b": {"input": 0.85, "output": 1.20},
        "qwen-3-32b": {"input": 0.40, "output": 0.80},
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
    if provider == "twilio":
        minutes = math.ceil(duration_seconds / 60.0)
    else:
        minutes = duration_seconds / 60.0
    return minutes * config.get("price", 0.0)
