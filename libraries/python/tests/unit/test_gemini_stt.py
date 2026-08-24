"""Tests for the Gemini multimodal STT provider.

Only the ``google-genai`` client is faked (it is the paid vendor boundary).
Turn buffering, the real WAV container the adapter uploads, prompt assembly,
transcript mapping, the queue/sentinel drain and the error path all execute for
real. See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import io
import wave
from typing import Any
from unittest.mock import patch

import pytest

from getpatter.providers.base import Transcript
from getpatter.providers.gemini_stt import (
    GEMINI_STT_DEFAULT_MODEL,
    GEMINI_STT_MAX_OUTPUT_TOKENS,
    TONE_PROMPT,
    GeminiSTT,
)
from getpatter.stt.gemini import STT as GeminiNamespacedSTT

pytestmark = pytest.mark.mocked

API_KEY = "test-gemini-key"
SAMPLE_RATE = 16000


# ---------------------------------------------------------------------------
# google-genai fakes — only the vendor client is simulated.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModels:
    def __init__(self, replies: list[str], error: Exception | None) -> None:
        self._replies = list(replies)
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        reply = self._replies.pop(0) if self._replies else ""
        return _FakeResponse(reply)


class _FakeAio:
    def __init__(self, models: _FakeModels) -> None:
        self.models = models


class _FakeClient:
    def __init__(
        self, replies: list[str] | None = None, error: Exception | None = None
    ) -> None:
        self.models = _FakeModels(replies or [], error)
        self.aio = _FakeAio(self.models)


def _silence(num_bytes: int) -> bytes:
    return b"\x00" * num_bytes


async def _drain(stt: GeminiSTT) -> list[Transcript]:
    return [t async for t in stt.receive_transcripts()]


def _uploaded_wav(call: dict[str, Any]) -> bytes:
    parts = call["contents"][0]["parts"]
    return parts[1]["inline_data"]["data"]


# ---------------------------------------------------------------------------
# Credentials / construction
# ---------------------------------------------------------------------------


def test_the_api_key_falls_back_to_gemini_api_key(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert GeminiSTT().api_key == "from-env"


def test_a_missing_api_key_raises_with_both_env_names(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY / GOOGLE_API_KEY"):
        GeminiSTT()


def test_the_repr_never_leaks_the_api_key() -> None:
    assert API_KEY not in repr(GeminiSTT(API_KEY))


def test_the_defaults_match_the_typescript_adapter() -> None:
    stt = GeminiSTT(API_KEY)
    assert stt.model == GEMINI_STT_DEFAULT_MODEL == "gemini-2.5-flash"
    assert stt.sample_rate == SAMPLE_RATE
    assert GeminiSTT.provider_key == GeminiNamespacedSTT.provider_key == "gemini_stt"


async def test_clone_gives_each_call_its_own_buffer() -> None:
    stt = GeminiSTT(API_KEY)
    with patch(
        "getpatter.providers.gemini_stt.build_genai_client", return_value=_FakeClient()
    ):
        await stt.connect()
        await stt.send_audio(_silence(320))
        twin = stt.clone()
        assert isinstance(twin, GeminiSTT)
        assert twin is not stt
        assert twin._buffer == bytearray()
        await stt.close()
        await _drain(stt)


# ---------------------------------------------------------------------------
# Turn semantics
# ---------------------------------------------------------------------------


async def test_buffered_audio_is_only_sent_when_the_turn_ends() -> None:
    client = _FakeClient(["[tone: calm] hello"])
    with patch(
        "getpatter.providers.gemini_stt.build_genai_client", return_value=client
    ):
        stt = GeminiSTT(API_KEY)
        await stt.connect()
        await stt.send_audio(_silence(SAMPLE_RATE * 2))  # 1 s of audio
        assert client.models.calls == []  # no request before speech-end

        await stt.finalize()
        await stt.close()
        transcripts = await _drain(stt)

    assert len(client.models.calls) == 1
    assert [t.text for t in transcripts] == ["[tone: calm] hello"]


async def test_the_upload_is_a_real_wav_of_the_whole_utterance() -> None:
    client = _FakeClient(["[tone: flat] one two"])
    with patch(
        "getpatter.providers.gemini_stt.build_genai_client", return_value=client
    ):
        stt = GeminiSTT(API_KEY)
        await stt.connect()
        await stt.send_audio(_silence(3200))
        await stt.send_audio(_silence(3200))
        await stt.finalize()
        await stt.close()
        await _drain(stt)

    with wave.open(io.BytesIO(_uploaded_wav(client.models.calls[0])), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == SAMPLE_RATE
        assert wf.getnframes() == 6400 // 2  # both chunks in one request


async def test_the_request_carries_the_tone_prompt_and_output_cap() -> None:
    client = _FakeClient(["[tone: warm] hi"])
    with patch(
        "getpatter.providers.gemini_stt.build_genai_client", return_value=client
    ):
        stt = GeminiSTT(API_KEY)
        await stt.connect()
        await stt.send_audio(_silence(3200))
        await stt.finalize()
        await stt.close()
        await _drain(stt)

    call = client.models.calls[0]
    assert call["model"] == GEMINI_STT_DEFAULT_MODEL
    parts = call["contents"][0]["parts"]
    assert parts[0] == {"text": TONE_PROMPT}
    assert parts[1]["inline_data"]["mime_type"] == "audio/wav"
    assert call["config"]["max_output_tokens"] == GEMINI_STT_MAX_OUTPUT_TOKENS


async def test_each_turn_produces_one_final_transcript() -> None:
    client = _FakeClient(["[tone: calm] first", "[tone: tense] second"])
    with patch(
        "getpatter.providers.gemini_stt.build_genai_client", return_value=client
    ):
        stt = GeminiSTT(API_KEY)
        await stt.connect()
        await stt.send_audio(_silence(3200))
        await stt.finalize()
        await stt.send_audio(_silence(3200))
        await stt.finalize()
        await stt.close()
        transcripts = await _drain(stt)

    assert [t.text for t in transcripts] == [
        "[tone: calm] first",
        "[tone: tense] second",
    ]
    assert all(
        t.is_final and t.speech_final and t.confidence == 1.0 for t in transcripts
    )


async def test_finalize_without_buffered_audio_sends_nothing() -> None:
    client = _FakeClient(["never used"])
    with patch(
        "getpatter.providers.gemini_stt.build_genai_client", return_value=client
    ):
        stt = GeminiSTT(API_KEY)
        await stt.connect()
        await stt.finalize()
        await stt.close()
        assert await _drain(stt) == []
    assert client.models.calls == []


async def test_close_flushes_a_turn_that_never_got_a_speech_end() -> None:
    client = _FakeClient(["[tone: tired] trailing words"])
    with patch(
        "getpatter.providers.gemini_stt.build_genai_client", return_value=client
    ):
        stt = GeminiSTT(API_KEY)
        await stt.connect()
        await stt.send_audio(_silence(3200))
        await stt.close()
        transcripts = await _drain(stt)

    assert [t.text for t in transcripts] == ["[tone: tired] trailing words"]


async def test_audio_sent_before_connect_is_ignored() -> None:
    client = _FakeClient(["unused"])
    with patch(
        "getpatter.providers.gemini_stt.build_genai_client", return_value=client
    ):
        stt = GeminiSTT(API_KEY)
        await stt.send_audio(_silence(3200))
        await stt.finalize()
        await stt.close()
        assert await _drain(stt) == []
    assert client.models.calls == []


# ---------------------------------------------------------------------------
# Failure handling — STT must never kill the call
# ---------------------------------------------------------------------------


async def test_an_empty_model_reply_produces_no_transcript() -> None:
    client = _FakeClient(["   "])
    with patch(
        "getpatter.providers.gemini_stt.build_genai_client", return_value=client
    ):
        stt = GeminiSTT(API_KEY)
        await stt.connect()
        await stt.send_audio(_silence(3200))
        await stt.finalize()
        await stt.close()
        assert await _drain(stt) == []


async def test_a_model_error_is_logged_and_the_stream_survives() -> None:
    client = _FakeClient(error=RuntimeError("429 rate limited"))
    with patch(
        "getpatter.providers.gemini_stt.build_genai_client", return_value=client
    ):
        stt = GeminiSTT(API_KEY)
        await stt.connect()
        await stt.send_audio(_silence(3200))
        await stt.finalize()
        await stt.close()
        assert await _drain(stt) == []


# ---------------------------------------------------------------------------
# Config-driven construction
# ---------------------------------------------------------------------------


def test_the_stt_config_factory_wires_the_adapter() -> None:
    from getpatter.models import STTConfig
    from getpatter.telephony.common import _create_stt_from_config

    stt = _create_stt_from_config(
        STTConfig(
            provider="gemini_stt",
            api_key=API_KEY,
            options={"sample_rate": 8000},
        )
    )
    assert isinstance(stt, GeminiSTT)
    assert stt.sample_rate == 8000
