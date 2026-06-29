"""Inbound audio processing chain for pipeline mode (slice 1 of the
``PipelineStreamHandler`` decomposition — see
``docs/architecture/pipeline-stages.md``).

Owns the stateless-to-STT half of ``on_audio_received``:

    decode (mulaw -> PCM16) -> high-pass/DC-block (opt) -> resample
    -> AEC near-end (opt) -> ``agent.audio_filter``/NS (opt)
    -> AGC (opt) -> VAD frame feed

and returns the processed frame plus at most one VAD event per frame. The
handler keeps everything downstream for this slice (VAD-event handling,
self-hearing gate, inbound ring buffer, ``before_send_to_stt`` hook, STT
feed) so the change stays reviewable.

Stage-order contract (fixed) — canonical WebRTC APM order
``HPF -> AEC -> NS -> VAD -> AGC`` adapted for telephony (AGC pulled ahead
of VAD/STT so the recogniser sees a level-normalised signal):

* The high-pass / DC-block runs FIRST, on the decoded signal BEFORE the
  resampler, so DC and sub-cutoff rumble never reach the echo canceller or
  bias the resampler's interpolation.
* AEC runs before the noise suppressor so the suppressor never disturbs the
  canceller's far-end/near-end alignment.
* ``audio_filter`` (noise suppression) runs AFTER AEC and BEFORE VAD, per the
  :class:`getpatter.providers.base.AudioFilter` docstring ("integrated ...
  before VAD and STT") — the VAD then benefits from the cleaned signal.
* AGC runs AFTER noise suppression and BEFORE VAD/STT: normalising on the
  cleaned signal avoids amplifying suppressed noise, and the level-normalised
  frame helps both the VAD energy gate and the STT recogniser.

Every new stage (HPF, AGC) is OPT-IN, provider-agnostic (runs once,
independent of the chosen STT), and fail-open: a raising stage degrades to
passthrough of its input, logs one WARNING, then DEBUG, and keeps being
attempted — exactly the policy the ``audio_filter`` wrapper uses (a transient
hiccup must not permanently strip processing, a permanent one must never break
the call audio path).

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
    """HPF -> resample -> AEC -> audio_filter -> AGC -> VAD for inbound audio.

    Args:
        input_is_mulaw_8k: When ``True``, frames are G.711 mu-law @ 8 kHz
            (Twilio always; Telnyx when ``streaming_start`` negotiated PCMU)
            and are decoded + upsampled through a per-call
            :class:`~getpatter.audio.transcoding.StatefulResampler` so the
            ratecv filter state survives chunk boundaries. When ``False``
            the frame is assumed to already be linear PCM16 at
            ``input_sample_rate``.
        get_aec: Late-bound accessor for the optional
            :class:`~getpatter.audio.aec.NlmsEchoCanceller`.
        get_audio_filter: Late-bound accessor for the optional
            :class:`~getpatter.providers.base.AudioFilter`
            (``agent.audio_filter`` — Krisp / DeepFilterNet).
        get_vad: Late-bound accessor for the active
            :class:`~getpatter.providers.base.VADProvider`
            (``agent.vad`` or the auto-loaded Silero instance).
        input_sample_rate: Sample rate (Hz) of the inbound PCM16 stream when
            ``input_is_mulaw_8k`` is ``False``. Default ``16000``. When it is
            NOT 16 kHz the chain resamples to 16 kHz through a per-call
            ``StatefulResampler`` instead of forwarding the bytes at the wrong
            rate (a PCM16 @ 8 kHz carrier previously reached STT pitched up an
            octave). Ignored for the mu-law path (always 8 kHz -> 16 kHz).
        high_pass_hz: Optional high-pass / DC-block cutoff (Hz). When set, a
            stateful :class:`~getpatter.audio.high_pass.BiquadHighPass` runs as
            the FIRST stage, on the decoded signal before the resampler.
            ``None`` (default) disables it.
        agc: Optional automatic gain control. ``None`` (default) disables it;
            an :class:`~getpatter.models.AgcConfig` enables a stateful
            :class:`~getpatter.audio.agc.Agc` after noise suppression and
            before VAD/STT.
    """

    def __init__(
        self,
        *,
        input_is_mulaw_8k: bool,
        get_aec: Callable[[], object | None],
        get_audio_filter: Callable[[], "AudioFilter | None"],
        get_vad: Callable[[], "VADProvider | None"],
        input_sample_rate: int = PIPELINE_SAMPLE_RATE,
        high_pass_hz: int | None = None,
        agc: object | None = None,
    ) -> None:
        if input_sample_rate <= 0:
            raise ValueError(f"input_sample_rate must be > 0; got {input_sample_rate}.")
        self._input_is_mulaw_8k = input_is_mulaw_8k
        self._input_sample_rate = int(input_sample_rate)
        self._get_aec = get_aec
        self._get_audio_filter = get_audio_filter
        self._get_vad = get_vad
        # Lazily created on the first mulaw frame (mirrors the handler's
        # historical lazy import) so PCM-native deployments never touch
        # audioop.
        self._resampler_8k_to_16k: "StatefulResampler | None" = None
        # Lazily created when a non-mulaw PCM stream arrives at a rate other
        # than 16 kHz (``input_sample_rate`` -> 16 kHz).
        self._pcm_resampler: "StatefulResampler | None" = None
        # Warn-once latches for the fail-open stage wrappers.
        self._filter_warned = False
        self._hpf_warned = False
        self._agc_warned = False

        # The high-pass runs on the decoded signal BEFORE the resampler, so its
        # operating rate is the carrier rate (8 kHz for mu-law, otherwise
        # ``input_sample_rate``). Built eagerly here; a bad cutoff disables the
        # stage (fail-open) rather than breaking call construction.
        self._hpf = None
        if high_pass_hz is not None:
            hpf_rate = 8000 if input_is_mulaw_8k else self._input_sample_rate
            try:
                from getpatter.audio.high_pass import BiquadHighPass

                self._hpf = BiquadHighPass(
                    cutoff_hz=float(high_pass_hz), sample_rate=hpf_rate
                )
            except Exception as exc:  # noqa: BLE001 - fail-open
                logger.warning(
                    "high_pass_hz=%s could not be initialised at %d Hz "
                    "(stage disabled): %s",
                    high_pass_hz,
                    hpf_rate,
                    exc,
                )

        # AGC runs at the pipeline rate (16 kHz) after noise suppression.
        self._agc = None
        if agc is not None:
            try:
                from getpatter.audio.agc import Agc

                self._agc = Agc(
                    sample_rate=PIPELINE_SAMPLE_RATE,
                    target_rms_dbfs=agc.target_rms_dbfs,
                    max_gain_db=agc.max_gain_db,
                    speech_floor_dbfs=agc.speech_floor_dbfs,
                    attack_ms=agc.attack_ms,
                    release_ms=agc.release_ms,
                    limiter_ceiling=agc.limiter_ceiling,
                )
            except Exception as exc:  # noqa: BLE001 - fail-open
                logger.warning("agc could not be initialised (stage disabled): %s", exc)

    async def process(self, audio_bytes: bytes) -> InputFrame:
        """Run one inbound media frame through decode -> AEC -> filter -> VAD.

        Never raises for filter failures (fail-open passthrough, warn once).
        VAD failures are swallowed per-frame at DEBUG — parity with the
        pre-extraction handler behaviour.
        """
        # ---- decode -> HPF -> resample ----------------------------------------
        # Decode mu-law (or take PCM as-is), then run the optional high-pass /
        # DC-block on the decoded signal at the CARRIER rate, then normalise to
        # 16 kHz. Running the HPF before the resampler keeps DC / rumble out of
        # the interpolation and matches the canonical APM order.
        if self._input_is_mulaw_8k:
            from getpatter.audio.transcoding import mulaw_to_pcm16

            pcm = mulaw_to_pcm16(audio_bytes)
            pcm = self._apply_high_pass(pcm)
            if self._resampler_8k_to_16k is None:
                from getpatter.audio.transcoding import create_resampler_8k_to_16k

                self._resampler_8k_to_16k = create_resampler_8k_to_16k()
            pcm = self._resampler_8k_to_16k.process(pcm)
        else:
            pcm = self._apply_high_pass(audio_bytes)
            if self._input_sample_rate != PIPELINE_SAMPLE_RATE:
                # PCM16 at a non-16 kHz rate: resample instead of forwarding the
                # bytes at the wrong rate (which pitches the caller up/down and
                # corrupts STT alignment).
                if self._pcm_resampler is None:
                    from getpatter.audio.transcoding import StatefulResampler

                    self._pcm_resampler = StatefulResampler(
                        src_rate=self._input_sample_rate,
                        dst_rate=PIPELINE_SAMPLE_RATE,
                    )
                pcm = self._pcm_resampler.process(pcm)

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

        # ---- AGC ---- normalise level AFTER noise suppression, BEFORE VAD/STT.
        # Speech-selective + peak-limited; fail-open like audio_filter.
        if self._agc is not None:
            try:
                gained = self._agc.process(pcm)
            except Exception as exc:  # noqa: BLE001 - fail-open
                self._warn_agc_once(exc)
            else:
                if isinstance(gained, (bytes, bytearray)):
                    pcm = bytes(gained)
                else:  # pragma: no cover - defensive
                    self._warn_agc_once(
                        TypeError(
                            f"Agc.process returned {type(gained).__name__}, expected bytes"
                        )
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

        return InputFrame(pcm=pcm, vad_event=vad_event, vad_configured=vad is not None)

    def flush(self) -> None:
        """Flush and discard the inbound resampler tails (call teardown)."""
        if self._resampler_8k_to_16k is not None:
            self._resampler_8k_to_16k.flush()
            self._resampler_8k_to_16k = None
        if self._pcm_resampler is not None:
            self._pcm_resampler.flush()
            self._pcm_resampler = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _apply_high_pass(self, pcm: bytes) -> bytes:
        """Run the optional high-pass stage (fail-open passthrough, warn once)."""
        if self._hpf is None:
            return pcm
        try:
            out = self._hpf.process(pcm)
        except Exception as exc:  # noqa: BLE001 - fail-open
            if not self._hpf_warned:
                self._hpf_warned = True
                logger.warning(
                    "high-pass stage failed; passing audio through unfiltered "
                    "(further failures logged at DEBUG): %s",
                    exc,
                )
            else:
                logger.debug("high-pass stage failed (passthrough): %s", exc)
            return pcm
        return out if isinstance(out, (bytes, bytearray)) else pcm

    def _warn_agc_once(self, exc: Exception) -> None:
        """WARN on the first AGC failure, DEBUG afterwards (fail-open)."""
        if not self._agc_warned:
            self._agc_warned = True
            logger.warning(
                "AGC stage failed; passing audio through without gain "
                "(further failures logged at DEBUG): %s",
                exc,
            )
        else:
            logger.debug("AGC stage failed (passthrough): %s", exc)

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
