"""
Smart-turn v3 semantic turn detector (ONNX).

Audio-native end-of-utterance model from the pipecat-ai project
(https://github.com/pipecat-ai/smart-turn, Apache-2.0 — ~8 M params,
<100 ms CPU inference). Unlike a VAD — which only knows *whether* the
caller is producing sound — smart-turn looks at the prosody of the last
few seconds of speech and predicts whether the caller has *finished
their turn* or is merely pausing mid-sentence ("My phone number is…").

Wiring
------
Pass an instance as ``Agent(turn_detector=...)``. The pipeline stream
handler then defers the STT finalize that normally fires on a VAD
``speech_end`` until the model agrees the turn is complete (probability
≥ :attr:`SmartTurnDetector.threshold`), holding for at most
``Agent.max_semantic_hold_ms`` (default 1200 ms) so a turn can never
hang on a model that keeps saying "incomplete".

Model file
----------
The ONNX weights are NOT bundled with the SDK (~30 MB). Download a
``smart-turn-v3*.onnx`` file from
https://huggingface.co/pipecat-ai/smart-turn-v3 and point the SDK at it
via the ``PATTER_SMART_TURN_MODEL`` environment variable or the
``model_path=`` argument of :meth:`SmartTurnDetector.load`.

Preprocessing (matches ``pipecat-ai/smart-turn`` ``inference.py`` exactly)
--------------------------------------------------------------------------
The v3 ONNX graph takes Whisper log-mel features, not a raw waveform:

1. int16 LE PCM → float32 in [-1, 1] (÷ 32768).
2. Keep the LAST 8 s of 16 kHz audio; left-pad with zeros to exactly
   128 000 samples so the speech sits at the END of the window
   (``truncate_audio_to_last_n_seconds``).
3. Zero-mean / unit-variance normalize the full padded window
   (``WhisperFeatureExtractor(..., do_normalize=True)``).
4. Whisper log-mel: 400-point Hann STFT (hop 160, reflect-padded,
   centered), 80 Slaney-scale mel filters, ``log10`` with 1e-10 floor,
   clamp to ``max - 8``, scale ``(x + 4) / 4`` and drop the trailing
   frame → ``(80, 800)`` float32.
5. ``session.run(None, {"input_features": features[None]})`` — the
   graph applies the sigmoid internally and returns the end-of-turn
   probability directly. ``probability > 0.5`` ⇒ turn complete.

``onnxruntime`` is imported lazily and inference runs in a
``loop.run_in_executor`` worker thread so the event loop stays
responsive — the same pattern as :mod:`getpatter.providers.silero_vad`.
Install the optional deps with ``pip install getpatter[turn-detector]``.
"""

# mypy: disable-error-code=unused-ignore

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import numpy as np
except ImportError:  # pragma: no cover — exercised via the maybe_load tests
    # numpy is part of the optional ``turn-detector`` extra. Keep the module
    # importable without it so :meth:`SmartTurnDetector.maybe_load` can
    # degrade gracefully (warn + return None) instead of crashing the app
    # at import time. :meth:`SmartTurnDetector.load` raises a descriptive
    # ImportError before any helper below can dereference ``np``.
    np = None  # type: ignore[assignment]

from getpatter.providers.base import TurnDetectorProvider

if TYPE_CHECKING:
    import onnxruntime  # type: ignore

logger = logging.getLogger(__name__)

# Environment variable consulted by :meth:`SmartTurnDetector.load` when no
# explicit ``model_path`` is given.
SMART_TURN_MODEL_ENV_VAR = "PATTER_SMART_TURN_MODEL"

# Smart-turn v3 input contract — 16 kHz mono, up to 8 s of context.
SMART_TURN_SAMPLE_RATE = 16000
SMART_TURN_MAX_SECONDS = 8
SMART_TURN_MAX_SAMPLES = SMART_TURN_SAMPLE_RATE * SMART_TURN_MAX_SECONDS  # 128 000

# Default decision threshold per the smart-turn v3 docs
# (``prediction = 1 if probability > 0.5 else 0``).
DEFAULT_SMART_TURN_THRESHOLD = 0.5

# Mirror of silero_vad.SLOW_INFERENCE_THRESHOLD — warn when one predict()
# takes longer than this many seconds (smart-turn v3 is ~12-60 ms on CPU).
SLOW_INFERENCE_THRESHOLD = 0.2

# Whisper feature-extractor constants (WhisperFeatureExtractor defaults).
_N_FFT = 400
_HOP_LENGTH = 160
_N_MELS = 80
_MEL_FLOOR = 1e-10
_NORM_EPS = 1e-7

_DOWNLOAD_HINT = (
    "Download a smart-turn-v3 ONNX file from "
    "https://huggingface.co/pipecat-ai/smart-turn-v3 and either set the "
    f"{SMART_TURN_MODEL_ENV_VAR} environment variable to its path or pass "
    "model_path= to SmartTurnDetector.load(). The model is not bundled "
    "with the SDK (~30 MB)."
)


class SmartTurnProviderTag(StrEnum):
    """Provider/model identifier strings exposed via the public properties."""

    MODEL = "smart-turn-v3"
    PROVIDER = "ONNX"


# ---------------------------------------------------------------------------
# Whisper log-mel feature extraction (numpy port of WhisperFeatureExtractor)
# ---------------------------------------------------------------------------

_mel_filterbank_cache: "np.ndarray | None" = None
_hann_window_cache: "np.ndarray | None" = None


def _hertz_to_mel_slaney(freq: "np.ndarray") -> "np.ndarray":
    """Hertz → mel using the Slaney scale (linear < 1 kHz, log above)."""
    min_log_hertz = 1000.0
    min_log_mel = 15.0
    logstep = 27.0 / np.log(6.4)
    mels = 3.0 * freq / 200.0
    log_region = freq >= min_log_hertz
    # ``np.where`` evaluates both branches — guard the log against freq=0.
    safe = np.where(log_region, freq, min_log_hertz)
    return np.where(
        log_region, min_log_mel + np.log(safe / min_log_hertz) * logstep, mels
    )


def _mel_to_hertz_slaney(mels: "np.ndarray") -> "np.ndarray":
    """Mel → Hertz, inverse of :func:`_hertz_to_mel_slaney`."""
    min_log_hertz = 1000.0
    min_log_mel = 15.0
    logstep = np.log(6.4) / 27.0
    freq = 200.0 * mels / 3.0
    log_region = mels >= min_log_mel
    return np.where(
        log_region, min_log_hertz * np.exp(logstep * (mels - min_log_mel)), freq
    )


def _mel_filterbank() -> "np.ndarray":
    """80-filter Slaney-normalized triangular mel filterbank, shape (201, 80).

    Numpy port of ``transformers.audio_utils.mel_filter_bank`` with
    ``norm="slaney"``, ``mel_scale="slaney"``, ``min_frequency=0.0``,
    ``max_frequency=8000.0`` — the exact filterbank WhisperFeatureExtractor
    builds for 16 kHz audio. Cached after the first call.
    """
    global _mel_filterbank_cache
    if _mel_filterbank_cache is not None:
        return _mel_filterbank_cache

    num_frequency_bins = 1 + _N_FFT // 2  # 201
    fft_freqs = np.linspace(0, SMART_TURN_SAMPLE_RATE // 2, num_frequency_bins)

    mel_min = _hertz_to_mel_slaney(np.array(0.0))
    mel_max = _hertz_to_mel_slaney(np.array(SMART_TURN_SAMPLE_RATE / 2))
    mel_freqs = np.linspace(mel_min, mel_max, _N_MELS + 2)
    filter_freqs = _mel_to_hertz_slaney(mel_freqs)  # (82,)

    filter_diff = np.diff(filter_freqs)  # (81,)
    slopes = filter_freqs[np.newaxis, :] - fft_freqs[:, np.newaxis]  # (201, 82)
    down_slopes = -slopes[:, :-2] / filter_diff[:-1]
    up_slopes = slopes[:, 2:] / filter_diff[1:]
    fb = np.maximum(np.zeros(1), np.minimum(down_slopes, up_slopes))  # (201, 80)

    # Slaney-style energy normalization.
    enorm = 2.0 / (filter_freqs[2 : _N_MELS + 2] - filter_freqs[:_N_MELS])
    fb *= enorm[np.newaxis, :]

    _mel_filterbank_cache = fb.astype(np.float64)
    return _mel_filterbank_cache


def _hann_window() -> "np.ndarray":
    """Periodic 400-point Hann window (``window_function(400, "hann")``)."""
    global _hann_window_cache
    if _hann_window_cache is None:
        n = np.arange(_N_FFT)
        _hann_window_cache = (0.5 - 0.5 * np.cos(2.0 * np.pi * n / _N_FFT)).astype(
            np.float64
        )
    return _hann_window_cache


def prepare_input_window(samples: "np.ndarray") -> "np.ndarray":
    """Truncate/left-pad *samples* to exactly 8 s and normalize.

    Mirrors smart-turn's ``truncate_audio_to_last_n_seconds`` (keep the
    END of the audio, pad zeros at the BEGINNING) followed by the
    feature extractor's ``do_normalize=True`` zero-mean / unit-variance
    pass over the full padded window. Returns float64 of shape
    ``(128000,)``.
    """
    samples = np.asarray(samples, dtype=np.float64).reshape(-1)
    if samples.shape[0] > SMART_TURN_MAX_SAMPLES:
        samples = samples[-SMART_TURN_MAX_SAMPLES:]
    elif samples.shape[0] < SMART_TURN_MAX_SAMPLES:
        padding = SMART_TURN_MAX_SAMPLES - samples.shape[0]
        samples = np.pad(samples, (padding, 0), mode="constant", constant_values=0)
    return (samples - samples.mean()) / np.sqrt(samples.var() + _NORM_EPS)


def compute_whisper_log_mel_features(window: "np.ndarray") -> "np.ndarray":
    """Whisper log-mel features for a prepared 8 s window → ``(80, 800)``.

    Numpy port of ``WhisperFeatureExtractor._np_extract_fbank_features``
    (the preprocessing smart-turn v3 runs before the ONNX graph): reflect-
    padded centered STFT (n_fft 400, hop 160, periodic Hann), power
    spectrum, Slaney mel projection, ``log10`` with a 1e-10 floor, clamp
    to ``max - 8.0``, scale ``(x + 4) / 4``, and drop the trailing frame.

    Args:
        window: float array of exactly ``SMART_TURN_MAX_SAMPLES`` samples
            (see :func:`prepare_input_window`).
    """
    window = np.asarray(window, dtype=np.float64).reshape(-1)
    if window.shape[0] != SMART_TURN_MAX_SAMPLES:
        raise ValueError(
            f"expected {SMART_TURN_MAX_SAMPLES} samples, got {window.shape[0]}; "
            "run prepare_input_window() first"
        )

    half = _N_FFT // 2
    padded = np.pad(window, (half, half), mode="reflect")
    # (num_frames, 400) frame matrix without copying the waveform per frame.
    frames = np.lib.stride_tricks.sliding_window_view(padded, _N_FFT)[::_HOP_LENGTH]
    stft = np.fft.rfft(frames * _hann_window(), n=_N_FFT, axis=1)  # (801, 201)
    power = np.abs(stft) ** 2

    mel_spec = power @ _mel_filterbank()  # (801, 80)
    log_spec = np.log10(np.maximum(mel_spec, _MEL_FLOOR)).T  # (80, 801)
    log_spec = log_spec[:, :-1]  # Whisper drops the final frame → (80, 800)
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0).astype(np.float32)


def _features_from_pcm16(pcm16_window: bytes) -> "np.ndarray":
    """Raw int16 LE PCM bytes → smart-turn ``input_features`` (1, 80, 800)."""
    samples_i16 = np.frombuffer(pcm16_window, dtype=np.int16)
    # ÷32768 matches smart-turn / pipecat int16→float conversion. The exact
    # scale is irrelevant after zero-mean/unit-variance normalization, but we
    # keep it identical for byte-level parity with the reference pipeline.
    samples = samples_i16.astype(np.float64) / 32768.0
    window = prepare_input_window(samples)
    features = compute_whisper_log_mel_features(window)
    return features[np.newaxis, :, :]  # (1, 80, 800)


# ---------------------------------------------------------------------------
# ONNX session helpers (mirrors silero_onnx.new_inference_session)
# ---------------------------------------------------------------------------


def resolve_smart_turn_model_path(model_path: "Path | str | None") -> Path:
    """Resolve the smart-turn ONNX file from the arg or env var.

    Resolution order: explicit ``model_path`` argument, then the
    ``PATTER_SMART_TURN_MODEL`` environment variable. Raises
    :class:`ValueError` when neither is set and :class:`FileNotFoundError`
    when the resolved path does not exist — both errors explain how to
    download the model from Hugging Face.
    """
    if model_path is None:
        env_path = os.environ.get(SMART_TURN_MODEL_ENV_VAR, "").strip()
        if not env_path:
            raise ValueError(
                "SmartTurnDetector has no model file configured. " + _DOWNLOAD_HINT
            )
        model_path = env_path
    path = Path(model_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Smart-turn model file not found: {path}. " + _DOWNLOAD_HINT
        )
    if not path.is_file():
        raise FileNotFoundError(
            f"Smart-turn model path is not a file: {path}. " + _DOWNLOAD_HINT
        )
    return path


def new_smart_turn_session(
    force_cpu: bool, model_path: "Path | str | None" = None
) -> "onnxruntime.InferenceSession":
    """Create an ``onnxruntime.InferenceSession`` for the smart-turn model.

    Lazy-imports ``onnxruntime`` (the ``turn-detector`` extra) and applies
    the same single-threaded, non-spinning session options the bundled
    Silero VAD uses so a per-call detector never busy-spins a core.
    """
    path = resolve_smart_turn_model_path(model_path)

    if np is None:
        raise ImportError(
            "SmartTurnDetector requires numpy, which is not installed. "
            "Install the optional extra with "
            "`pip install 'getpatter[turn-detector]'`."
        )
    try:
        import onnxruntime  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "SmartTurnDetector requires onnxruntime, which is not installed. "
            "Install the optional extra with "
            "`pip install 'getpatter[turn-detector]'`."
        ) from exc

    opts = onnxruntime.SessionOptions()
    opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
    opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 1
    opts.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    opts.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL

    if force_cpu and "CPUExecutionProvider" in onnxruntime.get_available_providers():
        return onnxruntime.InferenceSession(
            str(path), providers=["CPUExecutionProvider"], sess_options=opts
        )
    return onnxruntime.InferenceSession(str(path), sess_options=opts)


@dataclass
class _SmartTurnOptions:
    threshold: float


class SmartTurnDetector(TurnDetectorProvider):
    """Semantic end-of-utterance detector backed by smart-turn v3 (ONNX).

    Construct via :meth:`load` (raises with actionable instructions when
    the optional deps or the model file are missing) or :meth:`maybe_load`
    (warns once and returns ``None`` instead, so the agent degrades to
    plain VAD-silence endpointing rather than crashing)::

        detector = SmartTurnDetector.load()          # PATTER_SMART_TURN_MODEL
        # or: SmartTurnDetector.load(model_path="…/smart-turn-v3.0.onnx")
        # or: SmartTurnDetector.maybe_load()         # None when unprovisioned

        agent = Patter.agent(..., turn_detector=detector)

    :meth:`predict` takes the most recent window of mono int16 LE PCM at
    16 kHz (up to 8 s — longer windows are truncated to the last 8 s,
    shorter ones are left-padded) and returns the probability in
    ``[0, 1]`` that the caller has finished their turn. The pipeline
    handler compares it against :attr:`threshold` (default 0.5).

    The model is stateless (no streaming RNN state), so a single instance
    may be shared across concurrent calls; ``onnxruntime`` sessions are
    thread-safe for ``run()``.
    """

    @classmethod
    def load(
        cls,
        *,
        threshold: float = DEFAULT_SMART_TURN_THRESHOLD,
        model_path: "Path | str | None" = None,
        force_cpu: bool = True,
    ) -> "SmartTurnDetector":
        """Load the smart-turn v3 ONNX model and return a ready detector.

        Args:
            threshold: End-of-turn probability at/above which the turn is
                considered complete. Default ``0.5`` per the smart-turn v3
                reference (``prediction = 1 if probability > 0.5``).
            model_path: Path to the ``smart-turn-v3*.onnx`` file. When
                ``None``, the ``PATTER_SMART_TURN_MODEL`` environment
                variable is consulted; if that is unset too, a
                :class:`ValueError` explains how to download the model
                from https://huggingface.co/pipecat-ai/smart-turn-v3.
            force_cpu: Restrict ONNX Runtime to the CPU execution provider
                (the model is designed for <100 ms CPU inference).
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be within [0.0, 1.0]")
        session = new_smart_turn_session(force_cpu, model_path=model_path)
        return cls(session=session, opts=_SmartTurnOptions(threshold=threshold))

    @classmethod
    def maybe_load(
        cls,
        *,
        threshold: float = DEFAULT_SMART_TURN_THRESHOLD,
        model_path: "Path | str | None" = None,
        force_cpu: bool = True,
    ) -> "SmartTurnDetector | None":
        """Like :meth:`load`, but degrade instead of raise.

        Returns ``None`` — after a single clear warning — when semantic
        turn detection is not provisioned: the optional ``turn-detector``
        dependencies (numpy / onnxruntime) are missing, no model file is
        configured, or the configured file cannot be loaded. Intended for
        deployments where the detector is a soft upgrade::

            agent = patter.agent(
                ...,
                turn_detector=SmartTurnDetector.maybe_load(),
            )

        ``turn_detector=None`` keeps the plain VAD-silence endpointing, so
        the agent starts (and the call behaves) exactly as if the feature
        were never enabled — it never crashes the app.

        A ``threshold`` outside ``[0, 1]`` still raises :class:`ValueError`:
        that is a configuration bug, not a provisioning gap.
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be within [0.0, 1.0]")
        try:
            return cls.load(
                threshold=threshold, model_path=model_path, force_cpu=force_cpu
            )
        except Exception as exc:  # noqa: BLE001 — degrade, never crash startup
            logger.warning(
                "Semantic turn detection unavailable — falling back to plain "
                "VAD-silence endpointing: %s",
                exc,
            )
            return None

    def __init__(
        self,
        *,
        session: "onnxruntime.InferenceSession",
        opts: _SmartTurnOptions,
    ) -> None:
        self._session = session
        self._opts = opts
        self._closed = False

    @property
    def model(self) -> str:
        """Identifier of the underlying model (``smart-turn-v3``)."""
        return SmartTurnProviderTag.MODEL.value

    @property
    def provider(self) -> str:
        """Identifier of the runtime backend (``ONNX``)."""
        return SmartTurnProviderTag.PROVIDER.value

    @property
    def sample_rate(self) -> int:
        """Input sample rate the model expects (16 000 Hz)."""
        return SMART_TURN_SAMPLE_RATE

    @property
    def max_window_seconds(self) -> int:
        """Maximum audio context the model consumes per prediction (8 s)."""
        return SMART_TURN_MAX_SECONDS

    @property
    def threshold(self) -> float:
        """End-of-turn probability at/above which the turn is complete."""
        return self._opts.threshold

    async def predict(self, pcm16_16k_window: bytes) -> float:
        """End-of-turn probability for the given recent-audio window.

        Args:
            pcm16_16k_window: Mono int16 little-endian PCM at 16 kHz —
                ideally the full audio of the caller's current turn, up
                to 8 s (the handler keeps a rolling 8 s buffer). Longer
                input is truncated to the most recent 8 s; shorter input
                is left-padded with silence, matching the reference
                preprocessing exactly.

        Returns:
            Probability in ``[0, 1]`` that the turn is COMPLETE (the
            graph applies the sigmoid internally). Returns ``0.0`` for an
            empty window.
        """
        if self._closed:
            raise RuntimeError("SmartTurnDetector is closed")
        if len(pcm16_16k_window) < 2:
            return 0.0

        loop = asyncio.get_running_loop()
        start_time = time.perf_counter()
        probability = await loop.run_in_executor(
            None, _run_inference, self._session, pcm16_16k_window
        )
        inference_duration = time.perf_counter() - start_time
        if inference_duration > SLOW_INFERENCE_THRESHOLD:
            logger.warning(
                "smart-turn inference slower than expected",
                extra={"inference_duration": inference_duration},
            )
        return probability

    async def close(self) -> None:
        """Release the ONNX session. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # onnxruntime sessions don't expose an explicit close; drop the ref
        # so the session can be garbage collected.
        self._session = None  # type: ignore[assignment]


def _run_inference(
    session: "onnxruntime.InferenceSession", pcm16_window: bytes
) -> float:
    """Blocking feature-extraction + inference, executed in a worker thread."""
    input_features = _features_from_pcm16(pcm16_window)
    outputs = session.run(None, {"input_features": input_features})
    # Output is the sigmoid probability, shape (1, 1).
    probability = float(np.asarray(outputs[0]).reshape(-1)[0])
    # Defensive clamp — the graph already emits a sigmoid in (0, 1).
    return min(1.0, max(0.0, probability))
