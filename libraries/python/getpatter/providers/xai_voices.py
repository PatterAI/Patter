"""xAI built-in voice catalog for the Patter SDK.

Single source of truth for the 26 built-in xAI voice ids, shared by
:class:`~getpatter.providers.xai_tts.XaiTTS` and
:class:`~getpatter.providers.xai_realtime.XaiRealtimeAdapter` (the Voice Agent
API draws on the same roster). Both modules import :data:`XAI_DEFAULT_VOICE`
and :func:`normalize_xai_voice` from here instead of hardcoding them, so the
roster has one place to update.

Built-in ids are lowercase and case-insensitive on the xAI side (docs.x.ai).
Custom cloned voices (see
:func:`~getpatter.providers.xai_tts.create_custom_voice`) return an opaque
``voice_id`` string that is NOT part of this roster — :func:`normalize_xai_voice`
only folds case for a KNOWN built-in id and otherwise leaves the value
untouched (besides trimming whitespace), so a custom voice id is never
rejected or reshaped.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class XaiVoice:
    """Metadata for one built-in xAI voice (from the xAI voice catalog)."""

    id: str
    tone: str
    use_cases: tuple[str, ...]


# Roster order: ``eve`` (the documented default) first, then the remaining 25
# alphabetically by id — mirrors the table in docs/*/providers/xai-tts.mdx.
XAI_VOICES: tuple[XaiVoice, ...] = (
    XaiVoice("eve", "Energetic and upbeat", ()),
    XaiVoice(
        "altair",
        "Elegant, refined, and effortlessly premium",
        ("Advertising", "Narration"),
    ),
    XaiVoice("ara", "Warm and friendly", ()),
    XaiVoice(
        "atlas", "Confident, commanding, and reassuring", ("Sales", "Assistant")
    ),
    XaiVoice("carina", "Soft, empathetic, and soothing", ("Wellness", "Support")),
    XaiVoice(
        "castor",
        "Charismatic, down-to-earth, and easygoing",
        ("Sales", "Support"),
    ),
    XaiVoice(
        "celeste",
        "Compassionate, confident, and reassuring",
        ("Support", "Assistant"),
    ),
    XaiVoice("cosmo", "Bright, curious, and easy to follow", ("Education", "Podcast")),
    XaiVoice(
        "helios",
        "Upbeat, energetic, and endlessly versatile",
        ("Assistant", "Wellness"),
    ),
    XaiVoice(
        "helix", "Bold, dynamic, and adrenaline-fueled", ("Commentary", "Podcast")
    ),
    XaiVoice(
        "iris", "Friendly, upbeat, and naturally charming", ("Sales", "Support")
    ),
    XaiVoice(
        "kepler",
        "Inventive, forward-thinking, and charismatic",
        ("Advertising", "Podcast"),
    ),
    XaiVoice("leo", "Authoritative and strong", ()),
    XaiVoice(
        "lumen", "Warm, articulate, and engaging", ("Education", "Advertising")
    ),
    XaiVoice(
        "luna", "Gentle, patient, and deeply nurturing", ("Education", "Assistant")
    ),
    XaiVoice("lux", "Grounded, calm, and quietly wise", ("Wellness", "Narration")),
    XaiVoice("naksh", "Warm, thoughtful, and wise", ("Assistant", "Support")),
    XaiVoice("orion", "Rich, cinematic, and resonant", ("Narration", "Audiobooks")),
    XaiVoice(
        "perseus",
        "Strong, confident, and trustworthy",
        ("Advertising", "Narration"),
    ),
    XaiVoice("rex", "Confident and clear", ()),
    XaiVoice(
        "rigel",
        "Precise, professional, and calmly confident",
        ("Assistant", "Support"),
    ),
    XaiVoice("sal", "Smooth and balanced", ()),
    XaiVoice(
        "sirius", "Quick-witted, clever, and playful", ("Commentary", "Characters")
    ),
    XaiVoice("ursa", "Friendly, warm, and steadfast", ("Assistant", "Podcast")),
    XaiVoice(
        "zagan",
        "Powerful, dramatic, and unmistakable",
        ("Characters", "Narration"),
    ),
    XaiVoice("zenith", "Sharp, focused, and driven", ("Sales", "Advertising")),
)

XAI_VOICE_IDS: tuple[str, ...] = tuple(v.id for v in XAI_VOICES)

# xAI's documented default voice. Derived from the roster (``XAI_VOICES[0]``)
# rather than duplicated as a second literal, so the "eve first" ordering
# invariant above is the single source of truth.
XAI_DEFAULT_VOICE: str = XAI_VOICES[0].id

# Case-insensitive lookup built once from the roster above.
_VOICES_BY_ID: dict[str, XaiVoice] = {v.id.lower(): v for v in XAI_VOICES}


def is_xai_builtin_voice(voice: str) -> bool:
    """Return True if *voice* names one of the built-in roster voices.

    Case-insensitive and trims surrounding whitespace, matching how xAI
    resolves the ``voice`` / ``voice_id`` field on its API. A custom cloned
    voice id returns False here — it is still a legal value, just not one
    with roster metadata.
    """
    return voice.strip().lower() in _VOICES_BY_ID


def normalize_xai_voice(voice: str) -> str:
    """Prepare a ``voice`` value for the xAI wire format.

    Built-in roster ids are lowercase and case-insensitive on the xAI side, so
    a known id is trimmed and lowercased before being sent. A custom cloned
    ``voice_id`` (see :func:`~getpatter.providers.xai_tts.create_custom_voice`)
    is an arbitrary, case-sensitive opaque string — it must NEVER be rejected
    or reshaped, so only surrounding whitespace is trimmed.
    """
    trimmed = voice.strip()
    lowered = trimmed.lower()
    return lowered if lowered in _VOICES_BY_ID else trimmed


def get_xai_voice(voice_id: str) -> XaiVoice | None:
    """Look up roster metadata for a built-in voice id, else ``None``.

    Case-insensitive and trims surrounding whitespace; returns ``None`` for a
    custom cloned voice id since it carries no roster metadata.
    """
    return _VOICES_BY_ID.get(voice_id.strip().lower())
