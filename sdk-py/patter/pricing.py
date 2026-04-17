"""Default provider pricing and merge utilities.

Pricing is based on public provider rates (as of early 2025).
Developers can override any provider's pricing via ``Patter(pricing={...})``.

.. note::
    These are **estimates** based on publicly listed prices and may become
    stale as providers update their rates.  Always check the provider's
    pricing page for authoritative numbers, or pass your own overrides
    via ``Patter(pricing={...})``.
"""

from __future__ import annotations

PRICING_VERSION: str = "2025.1"
PRICING_LAST_UPDATED: str = "2025-01-15"

DEFAULT_PRICING: dict[str, dict] = {
    # STT — per minute of audio processed
    "deepgram": {"unit": "minute", "price": 0.0043},
    "whisper": {"unit": "minute", "price": 0.006},
    # TTS — per 1,000 characters synthesized
    "elevenlabs": {"unit": "1k_chars", "price": 0.18},
    "openai_tts": {"unit": "1k_chars", "price": 0.015},
    # OpenAI Realtime — per token (actual tokens from response.done usage)
    "openai_realtime": {
        "unit": "token",
        "audio_input_per_token": 0.0001,
        "audio_output_per_token": 0.0004,
        "text_input_per_token": 0.000005,
        "text_output_per_token": 0.00002,
    },
    # Telephony — per minute of call duration
    "twilio": {"unit": "minute", "price": 0.013},
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

    input_details = usage.get("input_token_details", {})
    output_details = usage.get("output_token_details", {})

    cost = 0.0
    cost += input_details.get("audio_tokens", 0) * config.get("audio_input_per_token", 0)
    cost += input_details.get("text_tokens", 0) * config.get("text_input_per_token", 0)
    cost += output_details.get("audio_tokens", 0) * config.get("audio_output_per_token", 0)
    cost += output_details.get("text_tokens", 0) * config.get("text_output_per_token", 0)
    return cost


def calculate_telephony_cost(
    provider: str, duration_seconds: float, pricing: dict
) -> float:
    """Calculate telephony cost from call duration."""
    config = pricing.get(provider, {})
    if config.get("unit") == "minute":
        return (duration_seconds / 60.0) * config.get("price", 0.0)
    return 0.0
