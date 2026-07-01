"""Unit tests for the bring-your-own-license denoiser registry.

These exercise :mod:`getpatter.providers.denoiser` (the string-keyed model-id
selection surfaced as ``Agent.denoiser``) in isolation from the proprietary
``krisp-audio`` SDK by injecting a FAKE ``krisp_audio`` module that carries
both the Noise-Cancellation (``NcInt16``) and Background-Voice-Cancellation
(``BvcInt16``) session classes before importing the Patter providers.

MOCK: no real inference. The fake sessions record which Krisp session class
was chosen so the tests can assert the correct session type per model id.
Tests that require the real SDK + license key must be run manually (see
``test_krisp_filter.py``).
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

# The byte-transform test synthesises PCM frames with numpy; skip the whole
# file cleanly on runners without the optional extra (mirrors
# ``test_krisp_filter.py``).
pytest.importorskip(
    "numpy", reason="denoiser tests that synthesise PCM require numpy"
)


# ---------------------------------------------------------------------------
# Fake krisp_audio module with BOTH NC and BVC session families
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


class _FakeSession:
    """Identity session that records which Krisp family created it."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        # Identity transform so byte round-trips are verifiable.
        self.process = MagicMock(side_effect=lambda samples, level: samples)


class _FakeNcInt16:
    @staticmethod
    def create(_cfg):
        return _FakeSession("nc")


class _FakeBvcInt16:
    @staticmethod
    def create(_cfg):
        return _FakeSession("bvc")


class _FakeModelInfo:
    def __init__(self) -> None:
        self.path: str | None = None


class _FakeNcSessionConfig:
    def __init__(self) -> None:
        self.inputSampleRate = None
        self.outputSampleRate = None
        self.inputFrameDuration = None
        self.modelInfo = None


class _FakeBvcSessionConfig:
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
    mod.BvcSessionConfig = _FakeBvcSessionConfig
    mod.BvcInt16 = _FakeBvcInt16
    mod.globalInit = MagicMock()
    mod.globalDestroy = MagicMock()
    mod.getVersion = MagicMock(return_value=_FakeVersion)
    return mod


_PATTER_KRISP_MODULES = (
    "getpatter.providers.denoiser",
    "getpatter.providers.krisp_filter",
    "getpatter.providers.krisp_instance",
)


@pytest.fixture
def fake_krisp(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``krisp_audio`` module and drop cached Patter imports."""
    fake = _build_fake_module()
    monkeypatch.setitem(sys.modules, "krisp_audio", fake)
    for mod in _PATTER_KRISP_MODULES:
        sys.modules.pop(mod, None)
    return fake


def _reset_sdk_manager() -> None:
    from getpatter.providers.krisp_instance import KrispSDKManager

    KrispSDKManager._reference_count = 0
    KrispSDKManager._initialized = False


def _make_model(tmp_path, name: str):
    """Create a fake ``.kef`` model file and return the containing directory."""
    (tmp_path / name).write_bytes(b"fake-model")
    return tmp_path


# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_registry_exposes_the_two_krisp_model_ids(fake_krisp):
    from getpatter.providers.denoiser import DENOISER_IDS, DENOISERS

    assert set(DENOISER_IDS) == {"krisp-viva-tel-v2", "krisp-bvc-o-pro-v3"}
    assert set(DENOISERS) == set(DENOISER_IDS)


@pytest.mark.unit
def test_unknown_denoiser_id_raises_clear_error(fake_krisp):
    from getpatter.providers.denoiser import resolve_denoiser

    with pytest.raises(ValueError, match="Unknown denoiser"):
        resolve_denoiser("krisp-does-not-exist")


# ---------------------------------------------------------------------------
# Session-type selection per model id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_viva_tel_selects_noise_cancellation_session(
    fake_krisp, tmp_path, monkeypatch
):
    from getpatter.providers.denoiser import resolve_denoiser

    _reset_sdk_manager()
    models_dir = _make_model(tmp_path, "krisp-viva-tel-v2.kef")
    monkeypatch.setenv("KRISP_MODELS_DIR", str(models_dir))

    flt = resolve_denoiser("krisp-viva-tel-v2")
    # The VIVA/telephony model must load through the plain NC session family.
    assert flt._session.kind == "nc"  # type: ignore[attr-defined]


@pytest.mark.unit
def test_bvc_pro_selects_background_voice_cancellation_session(
    fake_krisp, tmp_path, monkeypatch
):
    from getpatter.providers.denoiser import resolve_denoiser

    _reset_sdk_manager()
    models_dir = _make_model(tmp_path, "krisp-bvc-o-pro-v3.kef")
    monkeypatch.setenv("KRISP_MODELS_DIR", str(models_dir))

    flt = resolve_denoiser("krisp-bvc-o-pro-v3")
    # The BVC model must select the Background-Voice-Cancellation session,
    # NOT the plain NcInt16 path.
    assert flt._session.kind == "bvc"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Resolved filter is a working AudioFilter
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_resolved_denoiser_is_a_working_audio_filter(
    fake_krisp, tmp_path, monkeypatch
):
    import numpy as np

    from getpatter.providers.base import AudioFilter
    from getpatter.providers.denoiser import resolve_denoiser

    _reset_sdk_manager()
    models_dir = _make_model(tmp_path, "krisp-viva-tel-v2.kef")
    monkeypatch.setenv("KRISP_MODELS_DIR", str(models_dir))

    flt = resolve_denoiser("krisp-viva-tel-v2")
    assert isinstance(flt, AudioFilter)

    # 10 ms @ 16 kHz = 160 samples int16.
    pcm_in = np.arange(160, dtype=np.int16).tobytes()
    pcm_out = await flt.process(pcm_in, 16000)
    assert isinstance(pcm_out, bytes)
    assert len(pcm_out) == len(pcm_in)

    await flt.close()


@pytest.mark.unit
async def test_resolved_bvc_denoiser_transforms_bytes(
    fake_krisp, tmp_path, monkeypatch
):
    import numpy as np

    from getpatter.providers.denoiser import resolve_denoiser

    _reset_sdk_manager()
    models_dir = _make_model(tmp_path, "krisp-bvc-o-pro-v3.kef")
    monkeypatch.setenv("KRISP_MODELS_DIR", str(models_dir))

    flt = resolve_denoiser("krisp-bvc-o-pro-v3")
    pcm_in = np.arange(160, dtype=np.int16).tobytes()
    pcm_out = await flt.process(pcm_in, 16000)
    assert pcm_out == pcm_in  # identity fake session round-trips
    await flt.close()


# ---------------------------------------------------------------------------
# Bring-your-own-license failure surfaces
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_model_dir_names_the_env_var(fake_krisp, monkeypatch):
    from getpatter.providers.denoiser import resolve_denoiser

    _reset_sdk_manager()
    monkeypatch.delenv("KRISP_MODELS_DIR", raising=False)

    with pytest.raises(RuntimeError, match="KRISP_MODELS_DIR"):
        resolve_denoiser("krisp-viva-tel-v2")


@pytest.mark.unit
def test_missing_model_file_raises_file_not_found(
    fake_krisp, tmp_path, monkeypatch
):
    from getpatter.providers.denoiser import resolve_denoiser

    _reset_sdk_manager()
    # Directory exists but the required ``.kef`` is absent.
    monkeypatch.setenv("KRISP_MODELS_DIR", str(tmp_path))

    with pytest.raises(FileNotFoundError):
        resolve_denoiser("krisp-viva-tel-v2")


@pytest.mark.unit
def test_missing_sdk_raises_actionable_error(tmp_path, monkeypatch):
    """When ``krisp-audio`` is absent, resolving must name the SDK + license."""
    import builtins
    import importlib

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "krisp_audio":
            raise ModuleNotFoundError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    for mod in _PATTER_KRISP_MODULES:
        sys.modules.pop(mod, None)

    # A valid model dir + file so we get PAST the dir check and hit the SDK
    # guard inside KrispVivaFilter.
    (tmp_path / "krisp-viva-tel-v2.kef").write_bytes(b"x")
    monkeypatch.setenv("KRISP_MODELS_DIR", str(tmp_path))

    denoiser = importlib.import_module("getpatter.providers.denoiser")
    with pytest.raises(RuntimeError, match="Krisp SDK not installed"):
        denoiser.resolve_denoiser("krisp-viva-tel-v2")


# ---------------------------------------------------------------------------
# Handler wiring — Agent(denoiser=...) resolves at start and is honoured
# ---------------------------------------------------------------------------


class _StubFilter:
    """Minimal AudioFilter that tags bytes and records close()."""

    def __init__(self, tag: bytes) -> None:
        self.tag = tag
        self.closed = False

    async def process(self, pcm_chunk: bytes, sample_rate: int) -> bytes:
        return self.tag + pcm_chunk

    async def close(self) -> None:
        self.closed = True


def _make_pipeline_handler(agent):
    from collections import deque
    from unittest.mock import AsyncMock, MagicMock

    from getpatter.stream_handler import PipelineStreamHandler

    handler = PipelineStreamHandler(
        agent=agent,
        audio_sender=AsyncMock(),
        call_id="call-denoiser",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="p",
        metrics=None,
        for_twilio=False,
        input_is_mulaw_8k=False,
        conversation_history=deque(maxlen=10),
        transcript_entries=deque(maxlen=10),
    )
    handler._stt = MagicMock()
    handler._stt.send_audio = AsyncMock()
    # ``set_source_format`` is a sync call in ``start()``; keep it a plain mock
    # so the AsyncMock audio_sender does not emit an un-awaited-coroutine warning.
    handler.audio_sender.set_source_format = MagicMock()
    return handler


@pytest.mark.unit
async def test_handler_resolves_denoiser_and_closes_it(
    fake_krisp, tmp_path, monkeypatch
):
    """A string ``denoiser`` id is resolved to a real (fake-backed) Krisp
    filter at start, applied to inbound audio, and the SDK-owned filter is
    closed on cleanup."""
    from tests.conftest import fake_pcm_frame, make_agent

    _reset_sdk_manager()
    models_dir = _make_model(tmp_path, "krisp-viva-tel-v2.kef")
    monkeypatch.setenv("KRISP_MODELS_DIR", str(models_dir))

    handler = _make_pipeline_handler(
        make_agent(denoiser="krisp-viva-tel-v2", audio_filter=None)
    )

    await handler.start()
    resolved = handler._resolved_denoiser
    # Resolved to the real Krisp filter wrapper (backed by the fake NC session).
    assert resolved is not None
    assert resolved._session.kind == "nc"  # type: ignore[attr-defined]
    assert resolved._sdk_acquired is True  # type: ignore[attr-defined]

    # A 20 ms @ 16 kHz PCM frame reframes into two 10 ms Krisp frames; the
    # identity fake session round-trips the bytes to STT unchanged.
    frame = fake_pcm_frame(duration_ms=20)
    await handler.on_audio_received(frame)
    handler._stt.send_audio.assert_awaited_once_with(frame)

    await handler.cleanup()
    # The SDK-owned denoiser was closed (ref released) and dropped.
    assert resolved._sdk_acquired is False  # type: ignore[attr-defined]
    assert handler._resolved_denoiser is None


@pytest.mark.unit
async def test_explicit_audio_filter_wins_over_denoiser(
    fake_krisp, tmp_path, monkeypatch
):
    """When both ``audio_filter`` and ``denoiser`` are set, the explicit
    instance is used and the denoiser is never resolved."""
    from tests.conftest import fake_pcm_frame, make_agent

    _reset_sdk_manager()

    explicit = _StubFilter(b"EXPLICIT:")

    def _boom(name):  # pragma: no cover - must never be called
        raise AssertionError("denoiser must not be resolved when audio_filter set")

    monkeypatch.setattr("getpatter.providers.denoiser.resolve_denoiser", _boom)

    handler = _make_pipeline_handler(
        make_agent(denoiser="krisp-bvc-o-pro-v3", audio_filter=explicit)
    )
    await handler.start()
    assert handler._resolved_denoiser is None

    frame = fake_pcm_frame(duration_ms=20)
    await handler.on_audio_received(frame)
    handler._stt.send_audio.assert_awaited_once_with(b"EXPLICIT:" + frame)

    await handler.cleanup()
    # The user-owned explicit filter is NOT closed by the handler.
    assert explicit.closed is False
