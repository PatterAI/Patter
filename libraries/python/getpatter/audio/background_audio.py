"""Background audio player (hold music / ambient cues) for Patter.

Mixes a background audio source (hold music, office ambience, etc.) into the
outbound PCM stream produced by ``PipelineStreamHandler``. Public surface:

    player = BackgroundAudioPlayer(BuiltinAudioClip.HOLD_MUSIC, volume=0.1, loop=True)
    await player.start()
    mixed = await player.mix(agent_pcm, sample_rate=16000)
    ...
    await player.stop()

Implementation notes:

* Mixing is delegated to :class:`getpatter.audio.pcm_mixer.PcmMixer`, a
  numpy-only synchronous mixer (~80 lines) that operates on raw int16 PCM.
* ``.ogg`` decoding uses :mod:`soundfile` (libsndfile) and is resampled to the
  caller's sample rate with lightweight linear interpolation. This avoids the
  deprecated ``audioop`` module and keeps the dependency surface small.
* :class:`AudioConfig` supports probability-weighted random selection so
  callers can configure a list of candidate clips with weights.

Optional dependencies (``pip install 'getpatter[background-audio]'``):
``numpy``, ``soundfile``.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
from importlib import resources
from typing import NamedTuple, Union

from getpatter.providers.base import BackgroundAudioPlayer as _BaseBackgroundAudioPlayer
from getpatter.audio.pcm_mixer import PcmMixer

try:  # numpy + soundfile are optional
    import numpy as np
    import soundfile as sf

    _AUDIO_DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in envs without deps
    _AUDIO_DEPS_AVAILABLE = False


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Builtin audio clips
# ---------------------------------------------------------------------------


class BuiltinAudioClip(enum.Enum):
    """Enumerates the audio clips bundled with Patter.

    Original licences for redistributed audio assets are recorded in
    ``patter/resources/audio/NOTICE``.
    """

    CITY_AMBIENCE = "city-ambience.ogg"
    FOREST_AMBIENCE = "forest-ambience.ogg"
    OFFICE_AMBIENCE = "office-ambience.ogg"
    CROWDED_ROOM = "crowded-room.ogg"
    KEYBOARD_TYPING = "keyboard-typing.ogg"
    KEYBOARD_TYPING2 = "keyboard-typing2.ogg"
    HOLD_MUSIC = "hold_music.ogg"

    def path(self) -> str:
        """Return an absolute filesystem path to the clip.

        ``importlib.resources.files`` is used so the path resolves correctly
        whether the package is installed as a wheel, editable install, or
        zipapp.  For zipapps the file is extracted into a cache directory.
        """
        ref = resources.files("getpatter.resources.audio") / self.value
        # ``as_file`` returns a context manager that may extract a file from
        # a zipapp.  Enter it immediately — callers only need the filesystem
        # path during the single synchronous decode step below.
        with resources.as_file(ref) as p:
            return str(p)


# ---------------------------------------------------------------------------
# Public config types
# ---------------------------------------------------------------------------


AudioSource = Union[str, BuiltinAudioClip]


class AudioConfig(NamedTuple):
    """Definition for a single background audio source.

    Attributes
    ----------
    source:
        A :class:`BuiltinAudioClip` value or a filesystem path to an audio
        file readable by ``soundfile`` (``.ogg``, ``.wav``, ``.flac``).
    volume:
        Playback volume in ``[0.0, 1.0]``.  Applied as a pre-mix gain before
        the player's ``volume`` ratio.
    probability:
        Weight used by :class:`BackgroundAudioPlayer` when selecting one of
        several configs.  A sum of probabilities below ``1.0`` means there is
        a chance no sound is selected (pure silence).
    """

    source: AudioSource
    volume: float = 1.0
    probability: float = 1.0


# ---------------------------------------------------------------------------
# PlayHandle (trimmed from upstream — we keep the async-await shape)
# ---------------------------------------------------------------------------


class PlayHandle:
    """Awaitable handle returned by :meth:`BackgroundAudioPlayer.start`.

    The handle completes when playback finishes naturally (non-looping
    sources) or when :meth:`stop` is invoked.  Non-looping handles are mostly
    useful for integration tests — in production the player loops hold music
    until the surrounding call ends.
    """

    def __init__(self) -> None:
        self._done: asyncio.Future[None] = asyncio.get_event_loop().create_future()

    def done(self) -> bool:
        """Return ``True`` when playback has finished or :meth:`stop` was called."""
        return self._done.done()

    def stop(self) -> None:
        """Mark this handle complete; idempotent if already done."""
        if not self._done.done():
            self._done.set_result(None)

    async def wait_for_playout(self) -> None:
        """Await playback completion (shielded against task cancellation)."""
        await asyncio.shield(self._done)

    def __await__(self):
        """Allow ``await handle`` as shorthand for :meth:`wait_for_playout`."""
        return self.wait_for_playout().__await__()


# ---------------------------------------------------------------------------
# BackgroundAudioPlayer
# ---------------------------------------------------------------------------


class BackgroundAudioPlayer(_BaseBackgroundAudioPlayer):
    """Mix a background audio clip into an outbound PCM stream.

    Parameters
    ----------
    source:
        Background source.  Accepts a :class:`BuiltinAudioClip`, a file path,
        or a list of :class:`AudioConfig` (in which case one entry is chosen
        at start time using probability-weighted random selection).
    volume:
        Master mix ratio in ``[0.0, 1.0]``.  The default of ``0.1`` is a safe
        value for hold-music behind active speech.
    loop:
        If ``True`` the background source restarts when exhausted.  Hold
        music typically sets ``loop=True``; short cues (``keyboard-typing``)
        usually set ``loop=False`` and rely on a fresh player per cue.
    """

    def __init__(
        self,
        source: BuiltinAudioClip | str | list[AudioConfig],
        *,
        volume: float = 0.1,
        loop: bool = False,
    ) -> None:
        if not _AUDIO_DEPS_AVAILABLE:
            raise ImportError(
                "getpatter.audio.background_audio requires numpy and "
                "soundfile. Install the 'background-audio' extra: "
                "pip install 'getpatter[background-audio]'."
            )

        if not 0.0 <= volume <= 1.0:
            raise ValueError(f"volume must be in [0.0, 1.0], got {volume}")

        self._source_spec = source
        self._volume = volume
        self._loop = loop

        self._mixer = PcmMixer()
        self._lock = asyncio.Lock()
        self._started = False
        self._handle: PlayHandle | None = None

        # Decoded PCM cache: the source is decoded once at ``start()`` time
        # and stored as int16 mono numpy arrays keyed by sample rate.
        self._source_sr: int | None = None
        self._source_pcm: np.ndarray | None = None
        self._resample_cache: dict[int, np.ndarray] = {}

        # Position in samples within _source_pcm.  Updated by ``mix()``.
        self._position: int = 0

    # ------------------------------------------------------------------
    # Source selection
    # ------------------------------------------------------------------

    @staticmethod
    def _select_sound_from_list(sounds: list[AudioConfig]) -> AudioConfig | None:
        """Pick one ``AudioConfig`` from *sounds* using its probabilities.

        Returns ``None`` when the sum of probabilities is below 1.0 and the
        roll falls in the "silence" band.
        """
        total = sum(s.probability for s in sounds)
        if total <= 0:
            return None

        if total < 1.0 and random.random() > total:
            return None

        normalize_factor = 1.0 if total <= 1.0 else total
        r = random.random() * min(total, 1.0)
        cumulative = 0.0
        for sound in sounds:
            if sound.probability <= 0:
                continue
            cumulative += sound.probability / normalize_factor
            if r <= cumulative:
                return sound

        return sounds[-1]

    def _resolve_source(
        self,
    ) -> tuple[str, float] | None:
        """Resolve the user-supplied source spec into ``(path, volume)``.

        Returns ``None`` if the source is a list and probability selection
        yielded silence.
        """
        spec = self._source_spec
        if isinstance(spec, BuiltinAudioClip):
            return spec.path(), 1.0
        if isinstance(spec, str):
            return spec, 1.0
        if isinstance(spec, list):
            picked = self._select_sound_from_list(spec)
            if picked is None:
                return None
            if isinstance(picked.source, BuiltinAudioClip):
                return picked.source.path(), picked.volume
            return picked.source, picked.volume
        raise TypeError(f"Unsupported background audio source: {type(spec).__name__}")

    # ------------------------------------------------------------------
    # Decode / resample
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_file(path: str) -> tuple[np.ndarray, int]:
        """Decode *path* into ``(int16 mono array, sample_rate)``.

        ``soundfile`` returns float32 in the range ``[-1, 1]``.  We convert to
        int16 and collapse multi-channel streams to mono by averaging — a
        sensible default for telephony-grade audio.
        """
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        # data shape: (frames, channels)
        if data.shape[1] > 1:
            data = data.mean(axis=1)
        else:
            data = data[:, 0]
        # float32 [-1, 1] -> int16
        scaled = np.clip(data * 32768.0, -32768.0, 32767.0).astype(np.int16)
        return scaled, int(sr)

    def _resample(self, target_sr: int) -> np.ndarray:
        """Return a cached copy of the source resampled to *target_sr*.

        Uses linear interpolation — adequate for background hold music and
        ambient cues that are heavily attenuated (``volume <= 0.1``).  A high
        quality polyphase resampler would add scipy as a dependency, which is
        disproportionate for this use case.
        """
        assert self._source_pcm is not None and self._source_sr is not None
        if target_sr == self._source_sr:
            return self._source_pcm

        cached = self._resample_cache.get(target_sr)
        if cached is not None:
            return cached

        src = self._source_pcm.astype(np.float32)
        src_sr = self._source_sr
        ratio = target_sr / src_sr
        new_len = int(src.shape[0] * ratio)
        if new_len <= 0:
            return np.zeros(0, dtype=np.int16)

        # Positions in the *source* index space corresponding to each output
        # sample.  np.interp handles fractional indexing via linear interp.
        idx = np.linspace(0, src.shape[0] - 1, num=new_len, dtype=np.float64)
        resampled = np.interp(idx, np.arange(src.shape[0]), src)
        result = np.clip(resampled, -32768.0, 32767.0).astype(np.int16)
        self._resample_cache[target_sr] = result
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Decode the background source and arm the mixer.

        Safe to call multiple times — subsequent calls are no-ops while the
        player is active.  Always pair with :meth:`stop` to release cached
        resampled buffers.
        """
        async with self._lock:
            if self._started:
                return

            resolved = self._resolve_source()
            if resolved is None:
                # Silence variant selected from an AudioConfig list.
                self._source_pcm = np.zeros(0, dtype=np.int16)
                self._source_sr = 16000  # arbitrary; won't be used
                self._started = True
                self._handle = PlayHandle()
                return

            path, source_volume = resolved

            # Apply the per-source volume *at decode time* so subsequent mix
            # calls only need a single master ratio.
            pcm, sr = await asyncio.to_thread(self._decode_file, path)
            if source_volume != 1.0:
                pcm = np.clip(
                    pcm.astype(np.float32) * float(source_volume),
                    -32768.0,
                    32767.0,
                ).astype(np.int16)

            self._source_pcm = pcm
            self._source_sr = sr
            self._position = 0
            self._started = True
            self._handle = PlayHandle()

    async def mix(self, agent_pcm: bytes, sample_rate: int) -> bytes:
        """Mix the next background chunk into *agent_pcm*.

        Returns a PCM buffer of exactly ``len(agent_pcm)`` bytes.  When the
        player is stopped, not started, or configured with ``volume == 0``,
        the agent bytes are returned unchanged — the mix is a safe no-op.
        """
        if not self._started or self._handle is None or self._handle.done():
            return agent_pcm
        if self._volume == 0.0:
            return agent_pcm
        if self._source_pcm is None or self._source_pcm.size == 0:
            return agent_pcm

        samples_needed = len(agent_pcm) // 2
        if samples_needed == 0:
            return agent_pcm

        bg = self._resample(sample_rate)
        if bg.size == 0:
            return agent_pcm

        # Collect ``samples_needed`` samples, looping or zero-padding as
        # configured.
        if self._loop:
            # Contiguous view via modulo indexing.
            indices = (np.arange(samples_needed) + self._position) % bg.shape[0]
            chunk = bg[indices]
            self._position = int((self._position + samples_needed) % bg.shape[0])
        else:
            remaining = bg.shape[0] - self._position
            if remaining <= 0:
                # Source exhausted — mark the play handle done so future
                # mix() calls return agent audio unchanged without reloading.
                if self._handle is not None:
                    self._handle.stop()
                return agent_pcm

            take = min(remaining, samples_needed)
            chunk = np.zeros(samples_needed, dtype=np.int16)
            chunk[:take] = bg[self._position : self._position + take]
            self._position += take
            if self._position >= bg.shape[0] and self._handle is not None:
                self._handle.stop()

        bg_bytes = chunk.tobytes()
        return self._mixer.mix(agent_pcm, bg_bytes, ratio=self._volume)

    async def stop(self) -> None:
        """Stop playback and release cached PCM buffers."""
        async with self._lock:
            if not self._started:
                return
            if self._handle is not None and not self._handle.done():
                self._handle.stop()
            self._source_pcm = None
            self._resample_cache.clear()
            self._position = 0
            self._started = False


__all__ = [
    "AudioConfig",
    "AudioSource",
    "BackgroundAudioPlayer",
    "BuiltinAudioClip",
    "PlayHandle",
]
