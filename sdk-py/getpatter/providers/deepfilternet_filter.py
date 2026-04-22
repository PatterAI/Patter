# Copyright 2025 PatterAI
#
# Licensed under the MIT License.  See the repository root ``LICENSE`` file for
# the full license text.

"""DeepFilterNet open-source :class:`AudioFilter` implementation.

Wraps the ``deep-filter`` (MIT) PyPI package, which exposes the pre-trained
DeepFilterNet3 deep-learning noise suppressor.  DeepFilterNet works natively at
48 kHz; PCM at common telephony rates (8 kHz/16 kHz) is up-sampled for
inference and down-sampled back before being returned, so callers see a filter
that preserves the input sample rate.

The heavy imports (``deepfilternet``, ``torch``, ``numpy``) are deferred until
the filter is actually instantiated so merely importing this module has no
cost.
"""

from __future__ import annotations

import logging
from typing import Any

from getpatter.providers.base import AudioFilter

logger = logging.getLogger(__name__)

# DeepFilterNet operates at 48 kHz.  Any other rate is resampled around
# inference.
_DEEPFILTERNET_SR: int = 48000

_INSTALL_MESSAGE = (
    "DeepFilterNet not installed: pip install 'getpatter[deepfilternet]' "
    "(requires torch and the deep-filter package)"
)


class DeepFilterNetFilter(AudioFilter):
    """OSS noise-suppression filter powered by DeepFilterNet3.

    Parameters
    ----------
    model_base_dir:
        Optional path passed to :func:`df.enhance.init_df`.  When ``None`` the
        bundled default DeepFilterNet3 model is downloaded / loaded from the
        package's cache directory on first use.
    atten_lim_db:
        Optional attenuation limit in dB forwarded to ``df.enhance.enhance``.
        When ``None`` the model default is used.
    """

    def __init__(
        self,
        model_base_dir: str | None = None,
        atten_lim_db: float | None = None,
    ) -> None:
        try:
            import numpy as np  # noqa: F401 - imported here to fail fast
            import torch  # noqa: F401
            from df.enhance import enhance, init_df
        except ImportError as e:
            raise RuntimeError(_INSTALL_MESSAGE) from e

        self._model_base_dir = model_base_dir
        self._atten_lim_db = atten_lim_db
        self._enhance = enhance

        try:
            # ``init_df`` returns ``(model, df_state, suffix)`` in
            # DeepFilterNet >= 0.5.  We only need the first two.
            result = init_df(model_base_dir) if model_base_dir else init_df()
        except Exception as e:
            logger.error("DeepFilterNet init_df failed: %s", e)
            raise RuntimeError(f"DeepFilterNet init failed: {e}") from e

        if not isinstance(result, tuple) or len(result) < 2:
            raise RuntimeError(
                "Unexpected DeepFilterNet init_df() return shape: "
                f"{type(result).__name__}"
            )

        self._model: Any = result[0]
        self._df_state: Any = result[1]
        self._closed: bool = False

        # DeepFilterNet expects 48 kHz audio; expose the native rate for
        # callers/tests that want to skip resampling.
        self._native_sr: int = _DEEPFILTERNET_SR
        logger.info(
            "DeepFilterNet filter initialised (native_sr=%s Hz, model_dir=%s)",
            self._native_sr,
            model_base_dir,
        )

    @staticmethod
    def _pcm16_to_float32(pcm: bytes) -> Any:
        import numpy as np

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        # Normalise into [-1, 1] as expected by torch/DeepFilterNet.
        return samples / 32768.0

    @staticmethod
    def _float32_to_pcm16(samples: Any) -> bytes:
        import numpy as np

        clipped = np.clip(samples, -1.0, 1.0)
        pcm = (clipped * 32767.0).astype(np.int16)
        return pcm.tobytes()

    @staticmethod
    def _resample(samples: Any, src_sr: int, dst_sr: int) -> Any:
        """Linear-interpolation resampler.

        DeepFilterNet inference dominates runtime; a simple numpy resampler is
        sufficient here and avoids a hard dependency on ``scipy``/``librosa``.
        """
        import numpy as np

        if src_sr == dst_sr:
            return samples
        src_len = samples.shape[0]
        if src_len == 0:
            return samples
        dst_len = int(round(src_len * dst_sr / src_sr))
        if dst_len <= 0:
            return np.zeros(0, dtype=samples.dtype)
        x_src = np.linspace(0.0, 1.0, num=src_len, endpoint=False)
        x_dst = np.linspace(0.0, 1.0, num=dst_len, endpoint=False)
        return np.interp(x_dst, x_src, samples).astype(samples.dtype)

    async def process(self, pcm_chunk: bytes, sample_rate: int) -> bytes:
        """Run DeepFilterNet enhancement on the given PCM chunk."""
        if self._closed:
            raise RuntimeError("DeepFilterNetFilter is closed")
        if not pcm_chunk:
            return pcm_chunk

        import numpy as np
        import torch

        samples = self._pcm16_to_float32(pcm_chunk)
        if samples.size == 0:
            return pcm_chunk

        # Up-sample to 48 kHz for inference when required.
        inference_samples = self._resample(samples, sample_rate, self._native_sr)

        audio_tensor = torch.from_numpy(inference_samples).unsqueeze(0)
        try:
            if self._atten_lim_db is not None:
                enhanced = self._enhance(
                    self._model,
                    self._df_state,
                    audio_tensor,
                    atten_lim_db=self._atten_lim_db,
                )
            else:
                enhanced = self._enhance(
                    self._model, self._df_state, audio_tensor
                )
        except Exception as e:
            logger.error("DeepFilterNet enhance failed: %s", e)
            return pcm_chunk

        if hasattr(enhanced, "detach"):
            enhanced_np = enhanced.detach().cpu().numpy()
        else:
            enhanced_np = np.asarray(enhanced)

        # ``enhance`` returns shape (1, N) or (N,).  Normalise to 1-D.
        if enhanced_np.ndim == 2:
            enhanced_np = enhanced_np[0]

        # Down-sample back to the caller's rate.
        restored = self._resample(enhanced_np, self._native_sr, sample_rate)
        return self._float32_to_pcm16(restored)

    async def close(self) -> None:
        """Release model references (GC handles actual teardown)."""
        self._model = None
        self._df_state = None
        self._closed = True
        logger.debug("DeepFilterNet filter closed")
