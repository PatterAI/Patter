"""Inbound audio processing chain for pipeline mode (slice 1 of the
``PipelineStreamHandler`` decomposition — see
``docs/architecture/pipeline-stages.md``).

Owns the stateless-to-STT half of ``on_audio_received``:

    decode (mulaw -> PCM16) -> resample 8 kHz -> 16 kHz (stateful)
    -> AEC near-end -> ``agent.audio_filter`` -> VAD frame feed

and returns the processed frame plus at most one VAD event per frame. The
handler keeps everything downstream for this slice (VAD-event handling,
self-hearing gate, inbound ring buffer, ``before_send_to_stt`` hook, STT
feed) so the change stays reviewable.

Stage-order contract (fixed):

* AEC runs FIRST so the noise suppressor never disturbs the canceller's
  far-end/near-end alignment.
* ``audio_filter`` runs AFTER AEC and BEFORE VAD, per the
  :class:`getpatter.providers.base.AudioFilter` docstring ("integrated ...
  before VAD and STT") — the VAD then benefits from the cleaned signal.

The filter wrapper is fail-open with a warn-once policy: a raising (or
non-bytes-returning) filter degrades to passthrough of the pre-filter PCM,
logs one WARNING, keeps logging at DEBUG, and keeps being attempted — a
transient provider hiccup must not permanently strip noise suppression, and a
permanent one must never break the call audio path.

AEC / audio-filter / VAD are resolved through late-bound getter callables
rather than captured at construction: ``PipelineStreamHandler`` populates
``_aec`` / ``_auto_vad`` during ``start()`` (after the chain may already
exist) and the unit suites assign them directly on handler instances.

Mirrors TypeScript ``src/services/input-chain.ts``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from getpatter.audio.transcoding import StatefulResampler
    from getpatter.providers.base import AudioFilter, VADEvent, VADProvider

logger = logging.getLogger("getpatter")

# The pipeline's internal processing rate: inbound carrier audio is always
# normalised to PCM16 mono @ 16 kHz before AEC / filter / VAD / STT.
PIPELINE_SAMPLE_RATE: int = 16000


@dataclass(frozen=True)
class InputFrame:
    """Result of pushing one carrier media frame through the input chain.

    Attributes:
        pcm: The decoded / resampled / AEC'd / filtered PCM16 16 kHz bytes —
            exactly what should reach the self-hearing gate and STT.
        vad_event: The VAD event emitted for this frame, if any.
        vad_configured: ``True`` when a VAD provider was consulted for this
            frame — the handler's self-hearing gate only applies then.
    """

    pcm: bytes
    vad_event: "VADEvent | None"
    vad_configured: bool


class InputProcessingChain:
    """Decode -> AEC -> audio_filter -> VAD for one call's inbound audio.

    Args:
        input_is_mulaw_8k: When ``True``, frames are G.711 mu-law @ 8 kHz
            (Twilio always; Telnyx when ``streaming_start`` negotiated PCMU)
            and are decoded + upsampled through a per-call
            :class:`~getpatter.audio.transcoding.StatefulResampler` so the
            ratecv filter state survives chunk boundaries. When ``False``
            the frame is assumed to already be PCM16 @ 16 kHz and passes
            through untouched.
        get_aec: Late-bound accessor for the optional
            :class:`~getpatter.audio.aec.NlmsEchoCanceller`.
        get_audio_filter: Late-bound accessor for the optional
            :class:`~getpatter.providers.base.AudioFilter`
            (``agent.audio_filter`` — Krisp / DeepFilterNet).
        get_vad: Late-bound accessor for the active
            :class:`~getpatter.providers.base.VADProvider`
            (``agent.vad`` or the auto-loaded Silero instance).
    """

    def __init__(
        self,
        *,
        input_is_mulaw_8k: bool,
        get_aec: Callable[[], object | None],
        get_audio_filter: Callable[[], "AudioFilter | None"],
        get_vad: Callable[[], "VADProvider | None"],
    ) -> None:
        self._input_is_mulaw_8k = input_is_mulaw_8k
        self._get_aec = get_aec
        self._get_audio_filter = get_audio_filter
        self._get_vad = get_vad
        # Lazily created on the first mulaw frame (mirrors the handler's
        # historical lazy import) so PCM-native deployments never touch
        # audioop.
        self._resampler_8k_to_16k: "StatefulResampler | None" = None
        # Warn-once latch for the fail-open audio_filter wrapper.
        self._filter_warned = False

    async def process(self, audio_bytes: bytes) -> InputFrame:
        """Run one inbound media frame through decode -> AEC -> filter -> VAD.

        Never raises for filter failures (fail-open passthrough, warn once).
        VAD failures are swallowed per-frame at DEBUG — parity with the
        pre-extraction handler behaviour.
        """
        # ---- decode + resample -------------------------------------------------
        if self._input_is_mulaw_8k:
            from getpatter.audio.transcoding import mulaw_to_pcm16

            if self._resampler_8k_to_16k is None:
                from getpatter.audio.transcoding import create_resampler_8k_to_16k

                self._resampler_8k_to_16k = create_resampler_8k_to_16k()
            pcm = self._resampler_8k_to_16k.process(mulaw_to_pcm16(audio_bytes))
        else:
            pcm = audio_bytes

        # ---- AEC ---- subtract estimated TTS bleed before filter/VAD/STT.
        # Pass-through until the canceller has enough far-end history to
        # fill its filter window (~128 ms), then converges over the next
        # 0.5-2 s of TTS-only frames.
        aec = self._get_aec()
        if aec is not None:
            pcm = aec.process_near_end(pcm)  # type: ignore[attr-defined]

        # ---- audio_filter ---- noise suppression (Krisp / DeepFilterNet).
        # AFTER AEC, BEFORE VAD per the AudioFilter ABC contract. Fail-open:
        # a broken filter must never take down the call audio path.
        audio_filter = self._get_audio_filter()
        if audio_filter is not None:
            try:
                filtered = await audio_filter.process(pcm, PIPELINE_SAMPLE_RATE)
            except Exception as exc:
                self._warn_filter_once(audio_filter, exc)
            else:
                if isinstance(filtered, (bytes, bytearray)):
                    pcm = bytes(filtered)
                else:
                    self._warn_filter_once(
                        audio_filter,
                        TypeError(
                            f"process() returned {type(filtered).__name__}, expected bytes"
                        ),
                    )

        # ---- VAD ---- feed the (filtered) frame; at most one event back.
        vad = self._get_vad()
        vad_event: "Optional[VADEvent]" = None
        if vad is not None:
            try:
                vad_event = await vad.process_frame(pcm, PIPELINE_SAMPLE_RATE)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("VAD process_frame failed: %s", exc)
                vad_event = None

        return InputFrame(
            pcm=pcm, vad_event=vad_event, vad_configured=vad is not None
        )

    def flush(self) -> None:
        """Flush and discard the inbound resampler tail (call teardown)."""
        if self._resampler_8k_to_16k is not None:
            self._resampler_8k_to_16k.flush()
            self._resampler_8k_to_16k = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _warn_filter_once(self, audio_filter: object, exc: Exception) -> None:
        """WARN on the first filter failure, DEBUG afterwards (fail-open)."""
        if not self._filter_warned:
            self._filter_warned = True
            logger.warning(
                "audio_filter %s failed; passing audio through unfiltered "
                "(further failures logged at DEBUG): %s",
                type(audio_filter).__name__,
                exc,
            )
        else:
            logger.debug(
                "audio_filter %s failed (passthrough): %s",
                type(audio_filter).__name__,
                exc,
            )
