"""
Lightweight PCM mixer used by :mod:`getpatter.services.background_audio`.

Replaces ``livekit.rtc.AudioMixer`` from the LiveKit Agents framework.  Patter
does not depend on ``livekit-rtc`` and streams raw 16-bit little-endian PCM
frames directly through its ``PipelineStreamHandler``, so a full buffered
mixer with its own capture/clock loop is unnecessary.  This module provides a
synchronous, pure-numpy mix of two PCM buffers that is deterministic and
trivial to unit-test.

Intended usage::

    mixer = PcmMixer()
    out = mixer.mix(agent_pcm, bg_pcm, ratio=0.1)

All buffers are mono, 16-bit signed little-endian PCM.  The sample rate is
carried by the caller (the mix itself is sample-rate agnostic — it aligns
samples by length only).

``numpy`` is an optional dependency (installed via the ``background-audio``
extra); importing this module without it raises ``ImportError`` with a
helpful message.
"""

from __future__ import annotations

from typing import Final

try:  # numpy is optional
    import numpy as np

    _NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in envs without numpy
    _NUMPY_AVAILABLE = False


_INT16_MIN: Final[int] = -32768
_INT16_MAX: Final[int] = 32767
_BYTES_PER_SAMPLE: Final[int] = 2


def _require_numpy() -> None:
    if not _NUMPY_AVAILABLE:
        raise ImportError(
            "getpatter.services.pcm_mixer requires numpy. Install the "
            "'background-audio' extra: pip install 'getpatter[background-audio]'."
        )


class PcmMixer:
    """Mix two 16-bit mono PCM streams with a volume ratio.

    Parameters
    ----------
    clip:
        If ``True`` (default) samples are clipped to the int16 range after
        mixing.  Disabling clipping is only useful for tests that want to
        inspect raw overflow.
    """

    __slots__ = ("_clip",)

    def __init__(self, *, clip: bool = True) -> None:
        _require_numpy()
        self._clip = clip

    def mix(
        self,
        agent_pcm: bytes,
        bg_pcm: bytes,
        ratio: float = 0.1,
    ) -> bytes:
        """Return ``agent_pcm + bg_pcm * ratio`` as 16-bit LE PCM bytes.

        The background buffer is truncated or zero-padded to match the agent
        buffer length so the caller always receives a chunk of exactly
        ``len(agent_pcm)`` bytes back.  This guarantees the mixer is a no-op
        on the outbound PCM timing budget.

        Parameters
        ----------
        agent_pcm:
            Foreground PCM bytes.  Must be 16-bit little-endian mono with an
            even byte length.
        bg_pcm:
            Background PCM bytes.  May be empty — in that case the agent
            buffer is returned unchanged.
        ratio:
            Gain applied to ``bg_pcm`` in ``[0.0, 1.0]``.  Values outside this
            range are accepted but not recommended (caller is responsible for
            choosing a sensible volume).
        """
        if len(agent_pcm) % _BYTES_PER_SAMPLE != 0:
            raise ValueError(
                "agent_pcm must be a whole number of 16-bit samples "
                f"(got {len(agent_pcm)} bytes)"
            )
        if len(bg_pcm) % _BYTES_PER_SAMPLE != 0:
            raise ValueError(
                "bg_pcm must be a whole number of 16-bit samples "
                f"(got {len(bg_pcm)} bytes)"
            )

        if not agent_pcm:
            return agent_pcm

        if not bg_pcm or ratio == 0.0:
            return bytes(agent_pcm)

        agent = np.frombuffer(agent_pcm, dtype=np.int16).astype(np.int32)
        bg = np.frombuffer(bg_pcm, dtype=np.int16).astype(np.int32)

        if bg.shape[0] < agent.shape[0]:
            padded = np.zeros(agent.shape[0], dtype=np.int32)
            padded[: bg.shape[0]] = bg
            bg = padded
        elif bg.shape[0] > agent.shape[0]:
            bg = bg[: agent.shape[0]]

        # Scale background and add.  Use float for the multiply so we retain
        # fractional energy before rounding back to int.
        bg_scaled = np.rint(bg.astype(np.float32) * float(ratio)).astype(np.int32)
        mixed = agent + bg_scaled

        if self._clip:
            np.clip(mixed, _INT16_MIN, _INT16_MAX, out=mixed)

        return mixed.astype(np.int16).tobytes()


def mix_pcm(agent: bytes, bg: bytes, ratio: float) -> bytes:
    """Standalone PCM mixer — mirrors the TypeScript ``mixPcm(agent, bg, ratio)``.

    Thin wrapper over :class:`PcmMixer`. The ``ratio`` argument is mandatory
    (matching the TS signature); pass ``0.0`` to get the agent buffer back
    unchanged.
    """
    return PcmMixer().mix(agent, bg, ratio=ratio)


__all__ = ["PcmMixer", "mix_pcm"]
