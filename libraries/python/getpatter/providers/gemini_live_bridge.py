"""Carrier-codec shim between Patter's Realtime stream handler and Gemini Live.

``OpenAIRealtimeStreamHandler`` speaks the carrier's negotiated codec on both
legs: for every telephony carrier that is mu-law 8 kHz (``g711_ulaw``). The
OpenAI / xAI adapters negotiate that codec with the provider, so the handler can
hand their bytes straight through.

Gemini Live cannot: it only accepts PCM16 mono at its input rate and only emits
PCM16 mono at its output rate. Rather than rewrite
:class:`~getpatter.providers.gemini_live.GeminiLiveAdapter` (which stays a
faithful, standalone Gemini client) the conversion lives here, at the boundary —
the same split the TypeScript SDK makes in ``server.ts``' ``buildAIAdapter``.

The shim re-exposes the adapter surface the handler drives
(``connect`` / ``send_audio`` / ``send_text`` / ``send_function_result`` /
``cancel_response`` / ``truncate_playback`` / ``update_session`` /
``receive_events`` / ``close``) and touches only the ``audio`` frames.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from getpatter.providers.gemini_live import (
    DEFAULT_INPUT_SAMPLE_RATE_HZ,
    DEFAULT_OUTPUT_SAMPLE_RATE_HZ,
    GeminiLiveAdapter,
    GeminiLiveEventType,
)

logger = logging.getLogger("getpatter.gemini_live")

__all__ = [
    "CARRIER_AUDIO_FORMAT",
    "CARRIER_SAMPLE_RATE_HZ",
    "GeminiLiveTelephonyAdapter",
    "build_gemini_live_adapter",
]

# The stream handler's ``audio_format`` value that means "mu-law 8 kHz on both
# legs" — what every Patter carrier (Twilio, Telnyx, Plivo) negotiates.
CARRIER_AUDIO_FORMAT = "g711_ulaw"
CARRIER_SAMPLE_RATE_HZ = 8000


class GeminiLiveTelephonyAdapter:
    """Wraps :class:`GeminiLiveAdapter` with carrier mu-law transcoding.

    With ``audio_format == "g711_ulaw"`` the shim decodes inbound mu-law 8 kHz
    to PCM16 and resamples it up to the model's input rate, and mu-law-encodes
    the model's PCM16 output back down to 8 kHz. Any other ``audio_format`` is
    passed through untouched — the caller is then responsible for handing the
    adapter PCM16 at ``input_sample_rate``.

    The resamplers are created once per session and reused so ``audioop.ratecv``
    keeps its filter state across chunks (a fresh resampler per chunk clicks at
    every boundary).
    """

    def __init__(
        self,
        api_key: str,
        *,
        audio_format: str = CARRIER_AUDIO_FORMAT,
        input_sample_rate: int = DEFAULT_INPUT_SAMPLE_RATE_HZ,
        output_sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE_HZ,
        **adapter_kwargs: Any,
    ) -> None:
        self._audio_format = audio_format
        self._input_sample_rate = int(input_sample_rate)
        self._output_sample_rate = int(output_sample_rate)
        self._inbound_resampler: Any = None
        self._outbound_resampler: Any = None
        self.inner = GeminiLiveAdapter(
            api_key=api_key,
            input_sample_rate=self._input_sample_rate,
            output_sample_rate=self._output_sample_rate,
            **adapter_kwargs,
        )

    def __repr__(self) -> str:
        return (
            f"GeminiLiveTelephonyAdapter(audio_format={self._audio_format!r}, "
            f"inner={self.inner!r})"
        )

    @property
    def _transcoding(self) -> bool:
        """True when the carrier leg is mu-law and needs conversion."""
        return self._audio_format == CARRIER_AUDIO_FORMAT

    # ---- Lifecycle -----------------------------------------------------

    async def connect(self) -> None:
        await self.inner.connect()

    async def close(self) -> None:
        await self.inner.close()
        for resampler in (self._inbound_resampler, self._outbound_resampler):
            if resampler is not None:
                resampler.flush()
        self._inbound_resampler = None
        self._outbound_resampler = None

    # ---- Audio ---------------------------------------------------------

    async def send_audio(self, audio: bytes) -> None:
        """Forward one carrier chunk to Gemini, transcoding when needed."""
        if not self._transcoding:
            await self.inner.send_audio(audio)
            return
        pcm = self._decode_inbound(audio)
        # The resampler buffers an odd trailing byte, so early chunks can be
        # empty. Sending an empty media frame is a wire error, not a no-op.
        if pcm:
            await self.inner.send_audio(pcm)

    def _decode_inbound(self, mulaw8: bytes) -> bytes:
        """mu-law 8 kHz -> PCM16 mono at ``input_sample_rate``."""
        from getpatter.audio.transcoding import StatefulResampler, mulaw_to_pcm16

        pcm8 = mulaw_to_pcm16(mulaw8)
        if self._input_sample_rate == CARRIER_SAMPLE_RATE_HZ:
            return pcm8
        if self._inbound_resampler is None:
            self._inbound_resampler = StatefulResampler(
                src_rate=CARRIER_SAMPLE_RATE_HZ, dst_rate=self._input_sample_rate
            )
        return self._inbound_resampler.process(pcm8)

    def _encode_outbound(self, pcm: bytes) -> bytes:
        """PCM16 mono at ``output_sample_rate`` -> mu-law 8 kHz."""
        from getpatter.audio.transcoding import StatefulResampler, pcm16_to_mulaw

        if self._output_sample_rate == CARRIER_SAMPLE_RATE_HZ:
            return pcm16_to_mulaw(pcm)
        if self._outbound_resampler is None:
            self._outbound_resampler = StatefulResampler(
                src_rate=self._output_sample_rate, dst_rate=CARRIER_SAMPLE_RATE_HZ
            )
        return pcm16_to_mulaw(self._outbound_resampler.process(pcm))

    async def receive_events(self) -> AsyncIterator[tuple[str, Any]]:
        """Yield the adapter's events with ``audio`` payloads transcoded."""
        async for ev_type, ev_data in self.inner.receive_events():
            if not (self._transcoding and ev_type == GeminiLiveEventType.AUDIO.value):
                yield (ev_type, ev_data)
                continue
            mulaw = self._encode_outbound(ev_data)
            if mulaw:
                yield (ev_type, mulaw)

    # ---- Conversation --------------------------------------------------

    async def send_text(self, text: str) -> None:
        await self.inner.send_text(text)

    async def send_function_result(self, call_id: str, result: str) -> None:
        await self.inner.send_function_result(call_id, result)

    async def cancel_response(self) -> None:
        await self.inner.cancel_response()

    async def truncate_playback(self) -> None:
        """No-op: Gemini Live has no client-driven playback truncation.

        The handler calls this on barge-in for server-managed sessions. Gemini
        Live interrupts on its own server-side VAD and exposes no truncate on
        the v1alpha wire protocol, so there is nothing to send.
        """
        logger.debug("Gemini Live: truncate_playback is implicit via VAD")

    async def update_session(self, **kwargs: Any) -> None:
        """Warn and continue: Gemini Live has no mid-session config update.

        Only the multi-agent handoff path calls this (to swap instructions and
        the tool list). Gemini Live v1alpha fixes both at ``connect`` time, so
        the swap cannot be applied — the call keeps running under the original
        agent rather than dying on an ``AttributeError``. Logged loudly because
        the handoff silently does less than the caller asked for.
        """
        logger.warning(
            "Gemini Live cannot update session config mid-call (%s) — "
            "the handoff keeps the original prompt and tools.",
            ", ".join(sorted(kwargs)) or "no fields",
        )


def build_gemini_live_adapter(
    *,
    agent: Any,
    api_key: str,
    instructions: str,
    tools: list[dict],
    audio_format: str,
) -> GeminiLiveTelephonyAdapter:
    """Construct the Gemini Live adapter for a call from the agent config.

    Reads the engine-supplied knobs off ``agent.gemini_live`` (populated by
    ``Patter._unpack_engine``) and forwards only the keys that were set, so the
    adapter's own defaults stay authoritative. Mirrors the TypeScript
    ``buildAIAdapter`` ``provider === 'gemini_live'`` branch.
    """
    if not api_key:
        raise ValueError(
            "Gemini Live mode requires a Google API key. Pass "
            "engine=GeminiLive(api_key='...') or set GEMINI_API_KEY (or "
            "GOOGLE_API_KEY) in the environment."
        )
    config: dict = dict(getattr(agent, "gemini_live", None) or {})
    kwargs: dict = {
        "audio_format": audio_format,
        "instructions": instructions,
        "tools": tools,
    }
    if agent.model:
        kwargs["model"] = agent.model
    if agent.voice:
        kwargs["voice"] = agent.voice
    # Engine ``language`` wins over the agent-level one: it is the more
    # specific setting and the caller had to name the engine to set it.
    language = config.pop("language", None) or getattr(agent, "language", None)
    if language:
        kwargs["language"] = language
    kwargs.update(config)
    return GeminiLiveTelephonyAdapter(api_key, **kwargs)
