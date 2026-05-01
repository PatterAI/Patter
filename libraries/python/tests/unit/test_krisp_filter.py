"""Unit tests for the Krisp VIVA filter port.

These tests exercise the Patter wrapper in isolation from the proprietary
``krisp-audio`` SDK by injecting a fake ``krisp_audio`` module before
importing :mod:`getpatter.providers.krisp_filter` / :mod:`getpatter.providers.krisp_instance`.

MOCK: no real inference.  Tests that require the real SDK + license key are
documented in the module docstring and must be run manually.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

# A few tests below import numpy locally to synthesise PCM frames; those tests
# are skipped gracefully on CI runners without the optional extras installed.
# Module-level importorskip makes the entire file skip cleanly when numpy
# is absent, matching the pattern already used in test_pcm_mixer.py and
# test_silero_vad.py.
pytest.importorskip("numpy", reason="Krisp filter tests that synthesise PCM require numpy")


# ---------------------------------------------------------------------------
# Fake krisp_audio module fixture
# ---------------------------------------------------------------------------


class _FakeSamplingRate:
    Sr8000Hz = object()
    Sr16000Hz = object()
    Sr24000Hz = object()
    Sr32000Hz = object()
    Sr44100Hz = object()
    Sr48000Hz = object()


class _FakeFrameDuration:
    Fd10ms = object()
    Fd15ms = object()
    Fd20ms = object()
    Fd30ms = object()
    Fd32ms = object()


class _FakeLogLevel:
    Off = 0


class _FakeVersion:
    major = 1
    minor = 0
    patch = 0


class _FakeNcSession:
    def __init__(self) -> None:
        self.process = MagicMock(side_effect=lambda samples, level: samples)


class _FakeNcInt16:
    @staticmethod
    def create(_cfg):
        return _FakeNcSession()


class _FakeModelInfo:
    def __init__(self) -> None:
        self.path: str | None = None


class _FakeNcSessionConfig:
    def __init__(self) -> None:
        self.inputSampleRate = None
        self.outputSampleRate = None
        self.inputFrameDuration = None
        self.modelInfo = None


def _build_fake_module() -> types.ModuleType:
    mod = types.ModuleType("krisp_audio")
    mod.SamplingRate = _FakeSamplingRate
    mod.FrameDuration = _FakeFrameDuration
    mod.LogLevel = _FakeLogLevel
    mod.ModelInfo = _FakeModelInfo
    mod.NcSessionConfig = _FakeNcSessionConfig
    mod.NcInt16 = _FakeNcInt16
    mod.globalInit = MagicMock()
    mod.globalDestroy = MagicMock()
    mod.getVersion = MagicMock(return_value=_FakeVersion)
    return mod


@pytest.fixture
def fake_krisp(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``krisp_audio`` module and reload Patter modules."""
    fake = _build_fake_module()
    monkeypatch.setitem(sys.modules, "krisp_audio", fake)
    # Drop any cached imports so the providers pick up the fake module.
    for mod in (
        "getpatter.providers.krisp_instance",
        "getpatter.providers.krisp_filter",
    ):
        sys.modules.pop(mod, None)
    return fake


# ---------------------------------------------------------------------------
# KrispSDKManager
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sdk_manager_acquire_initialises_once(fake_krisp):
    from getpatter.providers.krisp_instance import KrispSDKManager

    # Reset counters between tests because the manager is a classmethod-based
    # singleton.
    KrispSDKManager._reference_count = 0
    KrispSDKManager._initialized = False

    KrispSDKManager.acquire()
    KrispSDKManager.acquire()

    assert fake_krisp.globalInit.call_count == 1
    assert KrispSDKManager.get_reference_count() == 2

    KrispSDKManager.release()
    KrispSDKManager.release()
    assert fake_krisp.globalDestroy.call_count == 1
    assert KrispSDKManager.get_reference_count() == 0


@pytest.mark.unit
def test_sdk_manager_acquire_raises_when_sdk_missing(monkeypatch: pytest.MonkeyPatch):
    """When krisp_audio is absent, acquire must raise RuntimeError."""
    import importlib

    monkeypatch.setitem(sys.modules, "krisp_audio", None)
    sys.modules.pop("getpatter.providers.krisp_instance", None)

    # Force ImportError during the lazy import.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "krisp_audio":
            raise ModuleNotFoundError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    module = importlib.import_module("getpatter.providers.krisp_instance")
    assert module.KRISP_AUDIO_AVAILABLE is False
    with pytest.raises(RuntimeError, match="Krisp SDK not installed"):
        module.KrispSDKManager.acquire()


# ---------------------------------------------------------------------------
# KrispVivaFilter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filter_requires_model_path(fake_krisp, monkeypatch: pytest.MonkeyPatch):
    from getpatter.providers.krisp_filter import KrispVivaFilter
    from getpatter.providers.krisp_instance import KrispSDKManager

    KrispSDKManager._reference_count = 0
    KrispSDKManager._initialized = False
    monkeypatch.delenv("KRISP_VIVA_FILTER_MODEL_PATH", raising=False)

    with pytest.raises(ValueError, match="Model path"):
        KrispVivaFilter()


@pytest.mark.unit
def test_filter_rejects_non_kef_extension(fake_krisp, tmp_path, monkeypatch):
    from getpatter.providers.krisp_filter import KrispVivaFilter
    from getpatter.providers.krisp_instance import KrispSDKManager

    KrispSDKManager._reference_count = 0
    KrispSDKManager._initialized = False

    bad = tmp_path / "model.bin"
    bad.write_bytes(b"x")
    with pytest.raises(ValueError, match="kef"):
        KrispVivaFilter(model_path=str(bad))


@pytest.mark.unit
async def test_filter_process_delegates_to_krisp(fake_krisp, tmp_path, monkeypatch):
    """MOCK: no real inference.  Verifies bytes<->numpy round-trip and call chain."""
    import numpy as np

    from getpatter.providers.krisp_filter import KrispVivaFilter
    from getpatter.providers.krisp_instance import KrispSDKManager

    KrispSDKManager._reference_count = 0
    KrispSDKManager._initialized = False

    model = tmp_path / "noisenet.kef"
    model.write_bytes(b"fake-model")

    flt = KrispVivaFilter(
        model_path=str(model),
        frame_duration_ms=10,
        sample_rate=16000,
    )
    assert flt.enabled is True

    # 10 ms @ 16 kHz = 160 samples × int16
    pcm_in = np.arange(160, dtype=np.int16).tobytes()
    pcm_out = await flt.process(pcm_in, 16000)

    assert isinstance(pcm_out, bytes)
    assert len(pcm_out) == len(pcm_in)
    assert flt._session.process.call_count == 1  # type: ignore[attr-defined]

    await flt.close()
    assert flt._sdk_acquired is False


@pytest.mark.unit
async def test_filter_disable_passthrough(fake_krisp, tmp_path):
    import numpy as np

    from getpatter.providers.krisp_filter import KrispVivaFilter
    from getpatter.providers.krisp_instance import KrispSDKManager

    KrispSDKManager._reference_count = 0
    KrispSDKManager._initialized = False

    model = tmp_path / "m.kef"
    model.write_bytes(b"x")
    flt = KrispVivaFilter(model_path=str(model), frame_duration_ms=10, sample_rate=16000)
    flt.disable()
    pcm = np.zeros(160, dtype=np.int16).tobytes()
    assert await flt.process(pcm, 16000) == pcm
    await flt.close()


@pytest.mark.unit
def test_filter_raises_when_sdk_unavailable(monkeypatch):
    """Without krisp-audio, direct instantiation must fail with a clear message."""
    import importlib
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "krisp_audio":
            raise ModuleNotFoundError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sys.modules.pop("getpatter.providers.krisp_instance", None)
    sys.modules.pop("getpatter.providers.krisp_filter", None)

    module = importlib.import_module("getpatter.providers.krisp_filter")
    with pytest.raises(RuntimeError, match="Krisp SDK not installed"):
        module.KrispVivaFilter(model_path="whatever.kef")


# ---------------------------------------------------------------------------
# Real SDK integration (skipped by default)
# ---------------------------------------------------------------------------


import os  # noqa: E402


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("KRISP_VIVA_SDK_LICENSE_KEY")
    or not os.getenv("KRISP_VIVA_FILTER_MODEL_PATH"),
    reason="Krisp SDK / license / model not available",
)
async def test_filter_real_sdk_smoke():
    """Smoke test against the real Krisp SDK (requires license + model file)."""
    from getpatter.providers.krisp_filter import KrispVivaFilter

    flt = KrispVivaFilter(frame_duration_ms=10, sample_rate=16000)
    import numpy as np

    pcm = np.zeros(160, dtype=np.int16).tobytes()
    out = await flt.process(pcm, 16000)
    assert len(out) == len(pcm)
    await flt.close()
