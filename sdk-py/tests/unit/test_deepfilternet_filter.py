"""Unit tests for :mod:`getpatter.providers.deepfilternet_filter`.

These tests inject fake ``deep-filter`` / ``torch`` / ``numpy`` modules via
``sys.modules`` so the wrapper can be exercised without downloading the real
DeepFilterNet weights (~60 MB) or installing the ~2 GB torch runtime.

MOCK: no real inference.  Real RMS-before/after assertions require the
pre-trained model and are skipped by default.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

import pytest

# DeepFilterNet is an optional extra (`pip install getpatter[deepfilternet]`);
# skip the whole module when numpy is absent on CI runners with base deps only.
np = pytest.importorskip("numpy")


# ---------------------------------------------------------------------------
# Fake deep-filter + torch
# ---------------------------------------------------------------------------


def _install_fake_modules(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Install fake ``df.enhance`` and ``torch`` modules.

    Returns the underlying MagicMocks so individual tests can assert on calls.
    """
    # torch
    torch_mod = types.ModuleType("torch")

    class _FakeTensor:
        def __init__(self, data):
            self._data = np.asarray(data)

        def unsqueeze(self, _dim):  # noqa: D401 - mimics torch API
            return _FakeTensor(self._data[np.newaxis, :])

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self._data

    def _from_numpy(arr):
        return _FakeTensor(arr)

    torch_mod.from_numpy = _from_numpy  # type: ignore[attr-defined]
    torch_mod.Tensor = _FakeTensor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch_mod)

    # df + df.enhance
    df_mod = types.ModuleType("df")
    df_enhance_mod = types.ModuleType("df.enhance")

    fake_model = MagicMock(name="deepfilternet_model")
    fake_state = MagicMock(name="df_state")
    init_df = MagicMock(return_value=(fake_model, fake_state, ""))

    # The fake enhancer flips the sign of the audio so tests can detect it ran.
    def enhance(_model, _state, tensor, **_kwargs):  # noqa: ARG001
        return _FakeTensor(-tensor._data)  # type: ignore[attr-defined]

    enhance_mock = MagicMock(side_effect=enhance)

    df_enhance_mod.enhance = enhance_mock  # type: ignore[attr-defined]
    df_enhance_mod.init_df = init_df  # type: ignore[attr-defined]
    df_mod.enhance = df_enhance_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "df", df_mod)
    monkeypatch.setitem(sys.modules, "df.enhance", df_enhance_mod)

    # Reload patter module.
    sys.modules.pop("getpatter.providers.deepfilternet_filter", None)

    return {"init_df": init_df, "enhance": enhance_mock}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_raises_when_deps_missing(monkeypatch):
    """Without ``df.enhance`` or torch, instantiation must raise RuntimeError."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in {"df.enhance", "torch"}:
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sys.modules.pop("df.enhance", None)
    sys.modules.pop("torch", None)
    sys.modules.pop("getpatter.providers.deepfilternet_filter", None)

    from getpatter.providers.deepfilternet_filter import DeepFilterNetFilter

    with pytest.raises(RuntimeError, match="DeepFilterNet not installed"):
        DeepFilterNetFilter()


@pytest.mark.unit
async def test_process_calls_enhance_and_returns_bytes(monkeypatch):
    """MOCK: no real inference.  Verifies bytes<->float round-trip + call chain."""
    mocks = _install_fake_modules(monkeypatch)

    from getpatter.providers.deepfilternet_filter import DeepFilterNetFilter

    flt = DeepFilterNetFilter()
    assert mocks["init_df"].call_count == 1

    # 20 ms @ 16 kHz = 320 samples.
    pcm = (np.ones(320, dtype=np.int16) * 1000).tobytes()
    out = await flt.process(pcm, 16000)

    assert isinstance(out, bytes)
    # Length matches the input duration (same sample rate in/out).
    assert len(out) == len(pcm)
    assert mocks["enhance"].called
    await flt.close()


@pytest.mark.unit
async def test_process_empty_chunk(monkeypatch):
    _install_fake_modules(monkeypatch)
    from getpatter.providers.deepfilternet_filter import DeepFilterNetFilter

    flt = DeepFilterNetFilter()
    assert await flt.process(b"", 16000) == b""
    await flt.close()


@pytest.mark.unit
async def test_process_after_close_raises(monkeypatch):
    _install_fake_modules(monkeypatch)
    from getpatter.providers.deepfilternet_filter import DeepFilterNetFilter

    flt = DeepFilterNetFilter()
    await flt.close()
    with pytest.raises(RuntimeError, match="closed"):
        await flt.process(b"\x00\x00", 16000)


@pytest.mark.unit
async def test_native_sample_rate_skips_resampling(monkeypatch):
    """At 48 kHz input, up/down-sampling is a no-op."""
    _install_fake_modules(monkeypatch)
    from getpatter.providers.deepfilternet_filter import DeepFilterNetFilter

    flt = DeepFilterNetFilter()
    # 10 ms @ 48 kHz = 480 samples.
    pcm = np.zeros(480, dtype=np.int16).tobytes()
    out = await flt.process(pcm, 48000)
    assert len(out) == len(pcm)
    await flt.close()


# ---------------------------------------------------------------------------
# Real-model tests (skipped by default)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("PATTER_DEEPFILTERNET_REAL") != "1",
    reason="Requires real DeepFilterNet3 model (~60 MB) + torch (~2 GB).",
)
async def test_real_rms_before_after():
    """RMS of white noise should decrease after DeepFilterNet enhancement.

    Enable with ``PATTER_DEEPFILTERNET_REAL=1`` once torch + deep-filter are
    installed.
    """
    from getpatter.providers.deepfilternet_filter import DeepFilterNetFilter

    flt = DeepFilterNetFilter()
    rng = np.random.default_rng(0)
    noise = (rng.normal(0, 0.1, 48000) * 32767).astype(np.int16).tobytes()

    def rms(b: bytes) -> float:
        arr = np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768
        return float(np.sqrt(np.mean(arr**2)))

    enhanced = await flt.process(noise, 48000)
    assert rms(enhanced) <= rms(noise)
    await flt.close()
