"""Krisp VIVA SDK singleton manager with reference counting.

Provides a singleton manager for the Krisp VIVA SDK with reference counting,
ensuring proper initialization and cleanup when multiple components (filters)
use the SDK.

The Krisp SDK is a proprietary component that must be installed separately via
``pip install krisp-audio`` and requires a valid license key provided through
the ``KRISP_VIVA_SDK_LICENSE_KEY`` environment variable.
"""

from __future__ import annotations

import logging
import os
from enum import IntEnum
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


class KrispSampleRate(IntEnum):
    """Sample rates accepted by the Krisp VIVA SDK ``SamplingRate`` enum."""

    HZ_8000 = 8000
    HZ_16000 = 16000
    HZ_24000 = 24000
    HZ_32000 = 32000
    HZ_44100 = 44100
    HZ_48000 = 48000


class KrispFrameDuration(IntEnum):
    """Frame durations (ms) accepted by the Krisp VIVA SDK ``FrameDuration`` enum."""

    MS_10 = 10
    MS_15 = 15
    MS_20 = 20
    MS_30 = 30
    MS_32 = 32


# Lazy/optional import of the proprietary Krisp Audio SDK.  We deliberately do
# not raise at import time; the error surface is deferred to ``acquire`` so
# callers can decide whether to fall back to a different filter.
try:
    import krisp_audio  # type: ignore[import-not-found]

    KRISP_AUDIO_AVAILABLE = True

    # Mapping of sample rates (Hz) to Krisp SDK SamplingRate enums
    KRISP_SAMPLE_RATES: dict[int, Any] = {
        8000: krisp_audio.SamplingRate.Sr8000Hz,
        16000: krisp_audio.SamplingRate.Sr16000Hz,
        24000: krisp_audio.SamplingRate.Sr24000Hz,
        32000: krisp_audio.SamplingRate.Sr32000Hz,
        44100: krisp_audio.SamplingRate.Sr44100Hz,
        48000: krisp_audio.SamplingRate.Sr48000Hz,
    }

    KRISP_FRAME_DURATIONS: dict[int, Any] = {
        10: krisp_audio.FrameDuration.Fd10ms,
        15: krisp_audio.FrameDuration.Fd15ms,
        20: krisp_audio.FrameDuration.Fd20ms,
        30: krisp_audio.FrameDuration.Fd30ms,
        32: krisp_audio.FrameDuration.Fd32ms,
    }
except ModuleNotFoundError:
    krisp_audio = None  # type: ignore[assignment]
    KRISP_AUDIO_AVAILABLE = False
    KRISP_SAMPLE_RATES = {}
    KRISP_FRAME_DURATIONS = {}


KRISP_NOT_INSTALLED_MESSAGE = (
    "Krisp SDK not installed: pip install krisp-audio "
    "and set KRISP_VIVA_SDK_LICENSE_KEY"
)


def int_to_krisp_frame_duration(frame_duration_ms: int) -> Any:
    """Translate an integer frame duration (ms) into the Krisp enum value."""
    if frame_duration_ms not in KRISP_FRAME_DURATIONS:
        supported_durations = ", ".join(
            str(duration) for duration in sorted(KRISP_FRAME_DURATIONS.keys())
        )
        raise ValueError(
            f"Unsupported frame duration: {frame_duration_ms} ms. "
            f"Supported durations: {supported_durations} ms"
        )
    return KRISP_FRAME_DURATIONS[frame_duration_ms]


def int_to_krisp_sample_rate(sample_rate: int) -> Any:
    """Translate an integer sample rate (Hz) into the Krisp enum value."""
    if sample_rate not in KRISP_SAMPLE_RATES:
        supported_rates = ", ".join(
            str(rate) for rate in sorted(KRISP_SAMPLE_RATES.keys())
        )
        raise ValueError(
            f"Unsupported sample rate: {sample_rate} Hz. "
            f"Supported rates: {supported_rates} Hz"
        )
    return KRISP_SAMPLE_RATES[sample_rate]


class KrispSDKManager:
    """Singleton manager for Krisp VIVA SDK with reference counting.

    This manager ensures the Krisp SDK is initialized only once and properly
    cleaned up when all components are done using it. It uses reference counting
    to track active users (filters).

    Thread-safe implementation using a lock for all operations.

    The license key must be provided via the ``KRISP_VIVA_SDK_LICENSE_KEY``
    environment variable.
    """

    _initialized: bool = False
    _lock: Lock = Lock()
    _reference_count: int = 0

    @staticmethod
    def _log_callback(log_message: str, log_level: Any) -> None:
        """Thread-safe callback for Krisp SDK logging."""
        logger.debug("[Krisp %s] %s", log_level, log_message)

    @staticmethod
    def licensing_error_callback(error: Any, error_message: str) -> None:
        """Callback invoked by the Krisp SDK on licensing errors."""
        logger.error("[Krisp Licensing Error: %s] %s", error, error_message)

    @classmethod
    def _get_license_key(cls) -> str:
        """Return the license key from ``KRISP_VIVA_SDK_LICENSE_KEY``."""
        return os.getenv("KRISP_VIVA_SDK_LICENSE_KEY", "")

    @classmethod
    def acquire(cls) -> None:
        """Acquire a reference to the SDK (initializes if needed).

        Call this when creating a filter instance. The SDK will be initialized
        on the first call.

        Raises:
            RuntimeError: If the ``krisp-audio`` package is not installed.
            Exception: If SDK initialization fails (propagated from krisp_audio).
        """
        if not KRISP_AUDIO_AVAILABLE or krisp_audio is None:
            raise RuntimeError(KRISP_NOT_INSTALLED_MESSAGE)

        with cls._lock:
            # Initialize SDK on first acquire
            if cls._reference_count == 0:
                try:
                    license_key = cls._get_license_key()
                    krisp_audio.globalInit(
                        "",
                        license_key,
                        cls.licensing_error_callback,
                        cls._log_callback,
                        krisp_audio.LogLevel.Off,
                    )
                    cls._initialized = True

                    version = krisp_audio.getVersion()
                    logger.debug(
                        "Krisp Audio SDK initialized - Version: %s.%s.%s",
                        version.major,
                        version.minor,
                        version.patch,
                    )

                except Exception as e:
                    cls._initialized = False
                    logger.error("Krisp SDK initialization failed: %s", e)
                    raise

            cls._reference_count += 1
            logger.debug("Krisp SDK reference count: %s", cls._reference_count)

    @classmethod
    def release(cls) -> None:
        """Release a reference to the SDK (destroys if last reference).

        Call this when destroying a filter instance. The SDK will be cleaned up
        when the last reference is released.
        """
        with cls._lock:
            if cls._reference_count > 0:
                cls._reference_count -= 1
                logger.debug("Krisp SDK reference count: %s", cls._reference_count)

                # Destroy SDK when last reference is released
                if cls._reference_count == 0 and cls._initialized:
                    try:
                        if krisp_audio is not None:
                            krisp_audio.globalDestroy()
                        cls._initialized = False
                        logger.debug(
                            "Krisp Audio SDK destroyed (all references released)"
                        )
                    except Exception as e:
                        logger.error("Error during Krisp SDK cleanup: %s", e)
                        cls._initialized = False

    @classmethod
    def is_initialized(cls) -> bool:
        """Return True when the SDK is currently initialized."""
        with cls._lock:
            return cls._initialized

    @classmethod
    def get_reference_count(cls) -> int:
        """Return the current reference count."""
        with cls._lock:
            return cls._reference_count
