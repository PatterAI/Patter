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

PRICING_VERSION: str = "2026.1"
PRICING_LAST_UPDATED: str = "2026-04-23"

DEFAULT_PRICING: dict[str, dict] = {
    # STT — per minute of audio processed.
    # Deepgram Nova-3 streaming (monolingual) — $0.0077/min. The previous
    # $0.0043 was the batch rate; streaming is ~80% more expensive.
    # Multilingual Nova-3 is $0.0092/min — override when needed.
    "deepgram": {"unit": "minute", "price": 0.0077},
    "whisper": {"unit": "minute", "price": 0.006},
    # AssemblyAI Universal-Streaming: $0.15/hr = $0.0025/min
    "assemblyai": {"unit": "minute", "price": 0.0025},
    # Cartesia ink-whisper streaming STT: ~$0.15/hr on usage plans
    "cartesia_stt": {"unit": "minute", "price": 0.0025},
    # Soniox real-time STT: $0.12/hr = $0.002/min
    "soniox": {"unit": "minute", "price": 0.002},
    # Speechmatics Standard tier: $1.04/hr base
    "speechmatics": {"unit": "minute", "price": 0.0173},
    # TTS — per 1,000 characters synthesized.
    # ElevenLabs default model is eleven_flash_v2_5 at $0.06/1k via direct API.
    # The previous $0.18 matched only the Creator plan overage rate.
    "elevenlabs": {"unit": "1k_chars", "price": 0.06},
    "openai_tts": {"unit": "1k_chars", "price": 0.015},
    "openai_tts_hd": {"unit": "1k_chars", "price": 0.030},
    # Cartesia Sonic TTS: ~$0.030/1k chars on usage plans
    "cartesia_tts": {"unit": "1k_chars", "price": 0.030},
    # Rime mist v2: $0.030/1k chars pay-as-you-go
    "rime": {"unit": "1k_chars", "price": 0.030},
    # LMNT aurora/blizzard: $0.050/1k chars Indie overage
    "lmnt": {"unit": "1k_chars", "price": 0.050},
    # OpenAI Realtime — per token (actual tokens from response.done usage).
    # Calibrated for gpt-4o-mini-realtime-preview (the Patter default):
    #   audio  input  $10  / M  ->  0.00001    per token
    #   audio  output $20  / M  ->  0.00002    per token
    #   text   input  $0.60/ M  ->  0.0000006  per token
    #   text   output $2.40/ M  ->  0.0000024  per token
    # For gpt-4o-realtime-preview multiply by ~10, for gpt-realtime by ~3.
    "openai_realtime": {
        "unit": "token",
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
    "twilio": {"unit": "minute", "price": 0.0085},
    "telnyx": {"unit": "minute", "price": 0.007},
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
    cached = input_details.get("cached_tokens_details") or {}

    cached_audio_rate = config.get(
        "cached_audio_input_per_token", config.get("audio_input_per_token", 0)
    )
    cached_text_rate = config.get(
        "cached_text_input_per_token", config.get("text_input_per_token", 0)
    )

    total_audio_in = input_details.get("audio_tokens", 0)
    total_text_in = input_details.get("text_tokens", 0)
    cached_audio_in = min(cached.get("audio_tokens", 0), total_audio_in)
    cached_text_in = min(cached.get("text_tokens", 0), total_text_in)

    cost = 0.0
    cost += (total_audio_in - cached_audio_in) * config.get("audio_input_per_token", 0)
    cost += cached_audio_in * cached_audio_rate
    cost += (total_text_in - cached_text_in) * config.get("text_input_per_token", 0)
    cost += cached_text_in * cached_text_rate
    cost += output_details.get("audio_tokens", 0) * config.get("audio_output_per_token", 0)
    cost += output_details.get("text_tokens", 0) * config.get("text_output_per_token", 0)
    return cost


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

    full_cost = (
        cached_audio * config.get("audio_input_per_token", 0)
        + cached_text * config.get("text_input_per_token", 0)
    )
    discounted_cost = cached_audio * cached_audio_rate + cached_text * cached_text_rate
    return full_cost - discounted_cost


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
