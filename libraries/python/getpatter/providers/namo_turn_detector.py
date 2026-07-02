"""
NAMO Turn Detector v1 — text-based semantic end-of-utterance model (ONNX).

Open, clean-weights end-of-turn (EOT) detector from the NAMO Turn Detector
v1 project (VideoSDK, Apache-2.0). Unlike an audio-native detector such as
:class:`~getpatter.providers.smart_turn.SmartTurnDetector` — which scores the
*prosody* of the last few seconds of caller audio — NAMO is a text
transformer: it reads the rolling conversation transcript (the last few
turns plus the caller's current in-flight utterance) and predicts whether
the caller has *finished their turn* or is merely pausing mid-sentence
("My email is john at ... and then ...").

Wiring
------
Pass an instance as ``Agent(turn_detector=...)`` exactly like smart-turn.
The pipeline stream handler defers the STT finalize that normally fires on
a VAD ``speech_end`` until the model agrees the turn is complete
(probability >= :attr:`NamoTurnDetector.threshold`), holding for at most
``Agent.max_semantic_hold_ms`` (default 1200 ms) so a turn can never hang.
The handler assembles the rolling transcript and passes it to
:meth:`predict` as the ``transcript`` keyword argument (audio is ignored).

Model + tokenizer
-----------------
Weights are NOT bundled with the SDK. Download a NAMO Turn Detector v1
sequence-classification model (an ``.onnx`` file plus its Hugging Face
tokenizer files) and point the SDK at the ``.onnx`` file via the
``PATTER_NAMO_MODEL`` environment variable or the ``model_path=`` argument
of :meth:`NamoTurnDetector.load`. The tokenizer is loaded (with
``transformers.AutoTokenizer``) from the directory containing the model
file, or from ``PATTER_NAMO_TOKENIZER`` / the ``tokenizer_path=`` argument.
If ``tokenizer_path`` resolves to a Hugging Face Hub repo id rather than a
local directory, pin the download to an immutable commit/tag via the
``PATTER_NAMO_REVISION`` environment variable or the ``revision=`` argument
of :meth:`NamoTurnDetector.load` — it is a no-op for the documented local
default.

NAMO ships per-language models, each with its own recommended decision
threshold — select the model for your language and set :attr:`threshold`
to that language's value (default 0.5).

Inference
---------
1. Tokenize the rolling transcript (HF tokenizer, truncated to
   :attr:`max_tokens`) → ``input_ids`` / ``attention_mask`` int64 tensors.
2. ``session.run(None, {"input_ids": …, "attention_mask": …})`` → logits.
3. Read the completion probability: a 2-class head is soft-maxed and the
   COMPLETE class is taken; a single-logit head is passed through a
   sigmoid. ``probability >= threshold`` ⇒ turn complete.

``onnxruntime`` and ``transformers`` are imported lazily (the optional
``namo-turn-detector`` extra) and inference runs in a
``loop.run_in_executor`` worker thread so the event loop stays responsive —
the same pattern as :mod:`getpatter.providers.smart_turn`. Install the
optional deps with ``pip install 'getpatter[namo-turn-detector]'``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    import numpy as np
except ImportError:  # pragma: no cover — exercised via the maybe_load tests
    # numpy is part of the optional ``namo-turn-detector`` extra. Keep the
    # module importable without it so :meth:`NamoTurnDetector.maybe_load`
    # degrades gracefully (warn + return None) rather than crashing the app
    # at import time. :meth:`NamoTurnDetector.load` raises a descriptive
    # ImportError before any helper below can dereference ``np``.
    np = None  # type: ignore[assignment]

from getpatter.providers.base import TurnDetectorProvider

if TYPE_CHECKING:
    import onnxruntime  # type: ignore

logger = logging.getLogger(__name__)

# Environment variables consulted by :meth:`NamoTurnDetector.load` when no
# explicit ``model_path`` / ``tokenizer_path`` is given.
NAMO_MODEL_ENV_VAR = "PATTER_NAMO_MODEL"
NAMO_TOKENIZER_ENV_VAR = "PATTER_NAMO_TOKENIZER"

# Hugging Face Hub revision (commit SHA or tag) to pin the tokenizer download
# to when ``tokenizer_path`` resolves to a repo id. No effect on the
# documented default — a local directory shipped alongside the model file.
NAMO_REVISION_ENV_VAR = "PATTER_NAMO_REVISION"

# Default decision threshold. NAMO ships per-language models, each with its
# own recommended value — override via ``load(threshold=...)``.
DEFAULT_NAMO_THRESHOLD = 0.5

# Rolling transcript is truncated to this many tokens before inference so the
# detector always sees a bounded prompt (matches the ~128-token window the
# handler assembles).
DEFAULT_NAMO_MAX_TOKENS = 128

# Index of the "turn complete" class in a 2-class classification head.
COMPLETE_LABEL_INDEX = 1

# Warn when one predict() takes longer than this many seconds (NAMO v1 is a
# small transformer — tens of ms on CPU). Mirrors smart_turn.
SLOW_INFERENCE_THRESHOLD = 0.2

_DOWNLOAD_HINT = (
    "Download a NAMO Turn Detector v1 model (an .onnx file plus its Hugging "
    "Face tokenizer files) and either set the "
    f"{NAMO_MODEL_ENV_VAR} environment variable to the .onnx path or pass "
    "model_path= to NamoTurnDetector.load(). The model is not bundled with "
    "the SDK."
)


class NamoProviderTag(StrEnum):
    """Provider/model identifier strings exposed via the public properties."""

    MODEL = "namo-turn-detector-v1"
    PROVIDER = "ONNX"


def resolve_namo_model_path(model_path: "Path | str | None") -> Path:
    """Resolve the NAMO ONNX file from the arg or env var.

    Resolution order: explicit ``model_path`` argument, then the
    ``PATTER_NAMO_MODEL`` environment variable. Raises :class:`ValueError`
    when neither is set and :class:`FileNotFoundError` when the resolved
    path does not exist or is not a file — both errors explain how to
    provision the model.
    """
    if model_path is None:
        env_path = os.environ.get(NAMO_MODEL_ENV_VAR, "").strip()
        if not env_path:
            raise ValueError(
                "NamoTurnDetector has no model file configured. " + _DOWNLOAD_HINT
            )
        model_path = env_path
    path = Path(model_path)
    if not path.exists():
        raise FileNotFoundError(
            f"NAMO model file not found: {path}. " + _DOWNLOAD_HINT
        )
    if not path.is_file():
        raise FileNotFoundError(
            f"NAMO model path is not a file: {path}. " + _DOWNLOAD_HINT
        )
    return path


def resolve_namo_tokenizer_path(
    tokenizer_path: "Path | str | None", model_path: Path
) -> str:
    """Resolve the tokenizer source (directory or repo id).

    Resolution order: explicit ``tokenizer_path``, then the
    ``PATTER_NAMO_TOKENIZER`` environment variable, then the directory
    holding the model file (NAMO ships the tokenizer alongside the weights).
    """
    if tokenizer_path is not None:
        return str(tokenizer_path)
    env_tok = os.environ.get(NAMO_TOKENIZER_ENV_VAR, "").strip()
    if env_tok:
        return env_tok
    return str(model_path.parent)


def resolve_namo_revision(revision: "str | None") -> "str | None":
    """Resolve the Hugging Face Hub revision to pin the tokenizer to.

    Resolution order: explicit ``revision`` argument, then the
    ``PATTER_NAMO_REVISION`` environment variable. Returns ``None`` when
    neither is set — the documented default (``tokenizer_path`` resolving to
    a local directory shipped alongside the model) has no revision concept;
    set this when ``tokenizer_path`` instead resolves to a Hugging Face Hub
    repo id, so the download is pinned to an immutable commit/tag rather than
    a mutable branch.
    """
    if revision is not None:
        return revision
    env_revision = os.environ.get(NAMO_REVISION_ENV_VAR, "").strip()
    return env_revision or None


def load_namo_tokenizer(
    tokenizer_path: "Path | str", *, revision: "str | None" = None
) -> Any:
    """Load the Hugging Face tokenizer for NAMO.

    Lazy-imports ``transformers`` (the ``namo-turn-detector`` extra) and
    raises a descriptive :class:`ImportError` — naming the install command —
    when it is absent, so a missing optional dep never surfaces as an opaque
    ``ModuleNotFoundError`` deep in a call.

    ``revision`` (see :func:`resolve_namo_revision`) pins the Hugging Face
    Hub commit/tag when ``tokenizer_path`` is a repo id; it is a no-op when
    ``tokenizer_path`` is the documented local directory.
    """
    try:
        from transformers import AutoTokenizer  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "NamoTurnDetector requires the 'transformers' package (Hugging "
            "Face tokenizers), which is not installed. Install the optional "
            "extra with `pip install 'getpatter[namo-turn-detector]'`."
        ) from exc
    return AutoTokenizer.from_pretrained(
        str(tokenizer_path), revision=revision
    )  # nosec B615 -- revision is threaded through (PATTER_NAMO_REVISION / NamoTurnDetector.load(revision=...)) for the Hub-repo-id case; the documented default source is a local operator-provided model directory, not an unpinned Hub download.


def new_namo_session(
    force_cpu: bool, model_path: "Path | str | None" = None
) -> "onnxruntime.InferenceSession":
    """Create an ``onnxruntime.InferenceSession`` for the NAMO model.

    Lazy-imports ``onnxruntime`` (the ``namo-turn-detector`` extra) and
    applies the same single-threaded, non-spinning session options the
    bundled Silero VAD / smart-turn use so a per-call detector never
    busy-spins a core.
    """
    path = resolve_namo_model_path(model_path)

    if np is None:
        raise ImportError(
            "NamoTurnDetector requires numpy, which is not installed. "
            "Install the optional extra with "
            "`pip install 'getpatter[namo-turn-detector]'`."
        )
    try:
        import onnxruntime  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "NamoTurnDetector requires onnxruntime, which is not installed. "
            "Install the optional extra with "
            "`pip install 'getpatter[namo-turn-detector]'`."
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


def completion_probability(logits: Any) -> float:
    """Turn-complete probability from a NAMO classification head.

    A single-logit head is passed through a sigmoid; a 2-(or more)-class
    head is soft-maxed and the :data:`COMPLETE_LABEL_INDEX` class is taken.
    The result is clamped to ``[0, 1]``.
    """
    arr = np.asarray(logits, dtype=np.float64).reshape(-1)
    if arr.size == 0:
        return 0.0
    if arr.size == 1:
        probability = float(1.0 / (1.0 + np.exp(-arr[0])))
    else:
        shifted = arr - arr.max()
        exps = np.exp(shifted)
        probs = exps / exps.sum()
        idx = COMPLETE_LABEL_INDEX if COMPLETE_LABEL_INDEX < probs.size else probs.size - 1
        probability = float(probs[idx])
    return min(1.0, max(0.0, probability))


@dataclass
class _NamoOptions:
    threshold: float
    max_tokens: int


class NamoTurnDetector(TurnDetectorProvider):
    """Text-based semantic end-of-utterance detector backed by NAMO v1 (ONNX).

    Construct via :meth:`load` (raises with actionable instructions when the
    optional deps or the model/tokenizer are missing) or :meth:`maybe_load`
    (warns once and returns ``None`` instead, so the agent degrades to plain
    VAD-silence endpointing rather than crashing)::

        detector = NamoTurnDetector.load()               # PATTER_NAMO_MODEL
        # or: NamoTurnDetector.load(model_path="…/model.onnx")
        # or: NamoTurnDetector.maybe_load()               # None when unprovisioned

        agent = Patter.agent(..., turn_detector=detector)

    :meth:`predict` ignores the audio window and scores the rolling
    ``transcript`` the pipeline handler assembles (the last few turns plus
    the caller's current in-flight utterance). The handler compares the
    returned probability against :attr:`threshold` (default 0.5; set it to
    your NAMO language model's recommended value).

    The model is stateless, so a single instance may be shared across
    concurrent calls.
    """

    @classmethod
    def load(
        cls,
        *,
        threshold: float = DEFAULT_NAMO_THRESHOLD,
        model_path: "Path | str | None" = None,
        tokenizer_path: "Path | str | None" = None,
        revision: "str | None" = None,
        force_cpu: bool = True,
        max_tokens: int = DEFAULT_NAMO_MAX_TOKENS,
    ) -> "NamoTurnDetector":
        """Load the NAMO v1 ONNX model + tokenizer and return a ready detector.

        Args:
            threshold: End-of-turn probability at/above which the turn is
                considered complete. Default ``0.5`` — override with the
                per-language value recommended for your NAMO model.
            model_path: Path to the NAMO ``.onnx`` file. When ``None``, the
                ``PATTER_NAMO_MODEL`` environment variable is consulted; if
                that is unset a :class:`ValueError` explains how to provision
                the model.
            tokenizer_path: Directory (or repo id) for the Hugging Face
                tokenizer. When ``None``, ``PATTER_NAMO_TOKENIZER`` is
                consulted, falling back to the directory containing the model
                file.
            revision: Hugging Face Hub revision (commit SHA or tag) to pin
                the tokenizer download to when ``tokenizer_path`` resolves to
                a repo id. When ``None``, ``PATTER_NAMO_REVISION`` is
                consulted. No effect for the documented default — a local
                directory shipped alongside the model file.
            force_cpu: Restrict ONNX Runtime to the CPU execution provider.
            max_tokens: Truncate the rolling transcript to this many tokens
                before inference (default 128).
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be within [0.0, 1.0]")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        path = resolve_namo_model_path(model_path)
        resolved_tokenizer = resolve_namo_tokenizer_path(tokenizer_path, path)
        resolved_revision = resolve_namo_revision(revision)
        tokenizer = load_namo_tokenizer(resolved_tokenizer, revision=resolved_revision)
        session = new_namo_session(force_cpu, model_path=path)
        return cls(
            session=session,
            tokenizer=tokenizer,
            opts=_NamoOptions(threshold=threshold, max_tokens=max_tokens),
        )

    @classmethod
    def maybe_load(
        cls,
        *,
        threshold: float = DEFAULT_NAMO_THRESHOLD,
        model_path: "Path | str | None" = None,
        tokenizer_path: "Path | str | None" = None,
        revision: "str | None" = None,
        force_cpu: bool = True,
        max_tokens: int = DEFAULT_NAMO_MAX_TOKENS,
    ) -> "NamoTurnDetector | None":
        """Like :meth:`load`, but degrade instead of raise.

        Returns ``None`` — after a single clear warning — when semantic turn
        detection is not provisioned: the optional ``namo-turn-detector``
        dependencies (numpy / onnxruntime / transformers) are missing, no
        model file is configured, or the configured model/tokenizer cannot be
        loaded. Intended for deployments where the detector is a soft
        upgrade::

            agent = patter.agent(
                ...,
                turn_detector=NamoTurnDetector.maybe_load(),
            )

        ``turn_detector=None`` keeps the plain VAD-silence endpointing, so the
        agent starts (and the call behaves) exactly as if the feature were
        never enabled — it never crashes the app.

        A ``threshold`` outside ``[0, 1]`` (or a non-positive ``max_tokens``)
        still raises: that is a configuration bug, not a provisioning gap.
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be within [0.0, 1.0]")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        try:
            return cls.load(
                threshold=threshold,
                model_path=model_path,
                tokenizer_path=tokenizer_path,
                revision=revision,
                force_cpu=force_cpu,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — degrade, never crash startup
            logger.warning(
                "Semantic turn detection unavailable — falling back to plain "
                "VAD-silence endpointing: %s",
                exc,
            )
            return None

    @classmethod
    def from_session(
        cls,
        *,
        session: "onnxruntime.InferenceSession",
        tokenizer: Any,
        threshold: float = DEFAULT_NAMO_THRESHOLD,
        max_tokens: int = DEFAULT_NAMO_MAX_TOKENS,
    ) -> "NamoTurnDetector":
        """Construct directly from a session + tokenizer (bypasses loading).

        Used by tests to inject a fake ONNX session and fake tokenizer, and
        by callers that manage the model/tokenizer lifecycle themselves.
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be within [0.0, 1.0]")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        return cls(
            session=session,
            tokenizer=tokenizer,
            opts=_NamoOptions(threshold=threshold, max_tokens=max_tokens),
        )

    def __init__(
        self,
        *,
        session: "onnxruntime.InferenceSession",
        tokenizer: Any,
        opts: _NamoOptions,
    ) -> None:
        self._session = session
        self._tokenizer = tokenizer
        self._opts = opts
        self._closed = False

    @property
    def model(self) -> str:
        """Identifier of the underlying model (``namo-turn-detector-v1``)."""
        return NamoProviderTag.MODEL.value

    @property
    def provider(self) -> str:
        """Identifier of the runtime backend (``ONNX``)."""
        return NamoProviderTag.PROVIDER.value

    @property
    def max_tokens(self) -> int:
        """Rolling-transcript token budget consumed per prediction."""
        return self._opts.max_tokens

    @property
    def threshold(self) -> float:
        """End-of-turn probability at/above which the turn is complete."""
        return self._opts.threshold

    async def predict(
        self, pcm16_16k_window: bytes, *, transcript: str | None = None
    ) -> float:
        """End-of-turn probability for the rolling conversation transcript.

        Args:
            pcm16_16k_window: Ignored. NAMO is a text model — it scores the
                transcript, not the audio. Accepted for contract parity with
                audio-native detectors.
            transcript: The rolling conversation text (last few turns plus
                the caller's current in-flight utterance) assembled by the
                pipeline handler. Truncated to :attr:`max_tokens` tokens.

        Returns:
            Probability in ``[0, 1]`` that the turn is COMPLETE. Returns
            ``0.0`` (turn incomplete) when the transcript is empty — nothing
            to score yet, so the handler keeps holding.
        """
        if self._closed:
            raise RuntimeError("NamoTurnDetector is closed")
        text = (transcript or "").strip()
        if not text:
            return 0.0

        loop = asyncio.get_running_loop()
        start_time = time.perf_counter()
        probability = await loop.run_in_executor(None, self._infer, text)
        inference_duration = time.perf_counter() - start_time
        if inference_duration > SLOW_INFERENCE_THRESHOLD:
            logger.warning(
                "NAMO turn-detector inference slower than expected",
                extra={"inference_duration": inference_duration},
            )
        return probability

    def _encode(self, text: str) -> "tuple[np.ndarray, np.ndarray]":
        """Tokenize *text* into ``(input_ids, attention_mask)`` int64 (1, n)."""
        enc = self._tokenizer(
            text,
            truncation=True,
            max_length=self._opts.max_tokens,
            return_tensors="np",
        )
        input_ids = np.asarray(enc["input_ids"], dtype=np.int64).reshape(1, -1)
        if "attention_mask" in enc:
            attention_mask = np.asarray(
                enc["attention_mask"], dtype=np.int64
            ).reshape(1, -1)
        else:
            attention_mask = np.ones_like(input_ids)
        return input_ids, attention_mask

    def _infer(self, text: str) -> float:
        """Blocking tokenize + inference, executed in a worker thread."""
        input_ids, attention_mask = self._encode(text)
        feeds = {"input_ids": input_ids, "attention_mask": attention_mask}
        outputs = self._session.run(None, feeds)
        return completion_probability(outputs[0])

    async def close(self) -> None:
        """Release the ONNX session and tokenizer. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # onnxruntime sessions don't expose an explicit close; drop the refs
        # so the session and tokenizer can be garbage collected.
        self._session = None  # type: ignore[assignment]
        self._tokenizer = None
