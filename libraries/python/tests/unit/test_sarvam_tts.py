"""Unit tests for the Sarvam AI TTS provider.

Mock the aiohttp boundary; everything else (payload assembly, per-model field
gating, base64 decoding, telephony factories, env-var fallback) runs against
real code. NO PII: tests never assert on synthesized audio as text beyond the
mock round-trip bytes.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import patch

import pytest

from getpatter.audio.format import AudioFormat
from getpatter.providers.sarvam_tts import (
    SARVAM_BASE_URL,
    SarvamLanguage,
    SarvamModel,
    SarvamTTS,
)
from getpatter.tts import sarvam as sarvam_ns


class _FakeResponse:
    def __init__(
        self,
        status: int,
        payload: dict[str, Any] | None = None,
        body: str = "",
    ) -> None:
        self.status = status
        self._payload = payload if payload is not None else {}
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._payload

    async def text(self) -> str:
        return self._body

    async def read(self) -> bytes:
        return self._body.encode()

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.last_url: str | None = None
        self.last_json: dict[str, Any] | None = None
        self.last_headers: dict[str, str] | None = None
        self.closed = False

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],  # noqa: A002 - aiohttp signature compat
        timeout: Any = None,
    ) -> _FakeResponse:
        self.last_url = url
        self.last_headers = headers
        self.last_json = json
        return self.response

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: Any = None,
    ) -> _FakeResponse:
        self.last_url = url
        self.last_headers = headers
        return self.response

    async def close(self) -> None:
        self.closed = True


def _audio_response(*payloads: bytes) -> _FakeResponse:
    """Build a Sarvam REST JSON response carrying base64 ``audios``."""
    audios = [base64.b64encode(p).decode() for p in payloads]
    return _FakeResponse(200, {"request_id": "req-1", "audios": audios})


@pytest.mark.unit
class TestPayloadAndAuth:
    async def test_posts_to_endpoint_with_subscription_key_header(self) -> None:
        fake = _FakeSession(_audio_response(b"hello", b"world"))
        tts = SarvamTTS(api_key="key", session=fake)  # type: ignore[arg-type]

        out = b"".join([c async for c in tts.synthesize("namaste")])

        assert out == b"helloworld"
        assert fake.last_url == SARVAM_BASE_URL
        assert fake.last_headers is not None
        assert fake.last_headers["api-subscription-key"] == "key"
        assert fake.last_headers["Content-Type"] == "application/json"

    async def test_default_payload_uses_bulbul_v3_linear16_16k(self) -> None:
        fake = _FakeSession(_audio_response(b"x"))
        tts = SarvamTTS(api_key="key", session=fake)  # type: ignore[arg-type]
        async for _ in tts.synthesize("hi"):
            pass

        body = fake.last_json
        assert body is not None
        assert body["model"] == SarvamModel.BULBUL_V3.value
        assert body["speaker"] == "shubh"
        assert body["target_language_code"] == SarvamLanguage.ENGLISH.value
        assert body["output_audio_codec"] == "linear16"
        assert body["speech_sample_rate"] == 16000
        # No optional / per-model fields unless explicitly configured.
        for key in ("pace", "pitch", "loudness", "temperature",
                    "enable_preprocessing", "dict_id"):
            assert key not in body

    async def test_language_selection_sets_target_language_code(self) -> None:
        fake = _FakeSession(_audio_response(b"x"))
        tts = SarvamTTS(api_key="key", session=fake, language="hi-IN")  # type: ignore[arg-type]
        async for _ in tts.synthesize("नमस्ते"):
            pass
        assert fake.last_json is not None
        assert fake.last_json["target_language_code"] == "hi-IN"

    async def test_pace_sent_when_set(self) -> None:
        fake = _FakeSession(_audio_response(b"x"))
        tts = SarvamTTS(api_key="key", session=fake, pace=1.25)  # type: ignore[arg-type]
        async for _ in tts.synthesize("hi"):
            pass
        assert fake.last_json is not None
        assert fake.last_json["pace"] == 1.25

    async def test_v2_only_params_gated_to_bulbul_v2(self) -> None:
        fake = _FakeSession(_audio_response(b"x"))
        tts = SarvamTTS(
            api_key="key",
            session=fake,  # type: ignore[arg-type]
            model="bulbul:v2",
            speaker="anushka",
            pitch=0.2,
            loudness=1.5,
            enable_preprocessing=True,
            # temperature is v3-only — must be dropped on v2.
            temperature=0.9,
        )
        async for _ in tts.synthesize("hi"):
            pass
        body = fake.last_json
        assert body is not None
        assert body["model"] == "bulbul:v2"
        assert body["pitch"] == 0.2
        assert body["loudness"] == 1.5
        assert body["enable_preprocessing"] is True
        assert "temperature" not in body
        assert "dict_id" not in body

    async def test_v3_only_params_gated_to_bulbul_v3(self) -> None:
        fake = _FakeSession(_audio_response(b"x"))
        tts = SarvamTTS(
            api_key="key",
            session=fake,  # type: ignore[arg-type]
            model="bulbul:v3",
            temperature=0.8,
            dict_id="dict-42",
            # pitch / loudness are v2-only — must be dropped on v3.
            pitch=0.2,
            loudness=1.5,
            enable_preprocessing=True,
        )
        async for _ in tts.synthesize("hi"):
            pass
        body = fake.last_json
        assert body is not None
        assert body["temperature"] == 0.8
        assert body["dict_id"] == "dict-42"
        assert "pitch" not in body
        assert "loudness" not in body
        assert "enable_preprocessing" not in body

    async def test_non_200_raises_with_body_excerpt(self) -> None:
        fake = _FakeSession(_FakeResponse(429, body="rate limited"))
        tts = SarvamTTS(api_key="key", session=fake)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match=r"Sarvam TTS error 429"):
            async for _ in tts.synthesize("hi"):
                pass

    async def test_malformed_body_yields_nothing(self) -> None:
        fake = _FakeSession(_FakeResponse(200, {"request_id": "x"}))
        tts = SarvamTTS(api_key="key", session=fake)  # type: ignore[arg-type]
        out = [c async for c in tts.synthesize("hi")]
        assert out == []

    def test_requires_api_key_or_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SARVAM_API_KEY", raising=False)
        with pytest.raises(ValueError, match="SARVAM_API_KEY"):
            SarvamTTS()

    def test_repr_does_not_leak_key(self) -> None:
        tts = SarvamTTS(api_key="super-secret")
        assert "super-secret" not in repr(tts)


@pytest.mark.unit
class TestTelephonyFactories:
    def test_for_twilio_emits_mulaw_8k_natively(self) -> None:
        tts = SarvamTTS.for_twilio(api_key="x")
        assert tts.sample_rate == 8000
        assert str(tts.codec) == "mulaw"

    async def test_for_twilio_payload_requests_mulaw_8k(self) -> None:
        fake = _FakeSession(_audio_response(b"x"))
        tts = SarvamTTS.for_twilio(api_key="x")
        tts._session = fake  # type: ignore[assignment]
        tts._owns_session = False
        async for _ in tts.synthesize("hi"):
            pass
        body = fake.last_json
        assert body is not None
        assert body["output_audio_codec"] == "mulaw"
        assert body["speech_sample_rate"] == 8000

    def test_for_twilio_declares_mulaw_source_format(self) -> None:
        tts = SarvamTTS.for_twilio(api_key="x")
        assert tts.source_audio_format() == AudioFormat(
            encoding="mulaw", sample_rate=8000
        )

    def test_for_twilio_respects_overrides(self) -> None:
        tts = SarvamTTS.for_twilio(api_key="x", language="ta-IN", speaker="kavya")
        assert tts.sample_rate == 8000
        assert str(tts.codec) == "mulaw"
        assert str(tts.language) == "ta-IN"
        assert tts.speaker == "kavya"

    def test_for_twilio_ignores_caller_sample_rate(self) -> None:
        tts = SarvamTTS.for_twilio(api_key="x", sample_rate=24000)
        assert tts.sample_rate == 8000

    def test_for_telnyx_emits_mulaw_8k_natively(self) -> None:
        tts = SarvamTTS.for_telnyx(api_key="x")
        assert tts.sample_rate == 8000
        assert str(tts.codec) == "mulaw"

    def test_constructor_default_unchanged(self) -> None:
        tts = SarvamTTS(api_key="x")
        assert tts.sample_rate == 16000
        assert str(tts.codec) == "linear16"
        assert tts.source_audio_format() == AudioFormat(
            encoding="pcm_s16le", sample_rate=16000
        )


@pytest.mark.unit
class TestNamespacePublicTTS:
    def test_requires_api_key_or_env_var(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os as _os

            _os.environ.pop("SARVAM_API_KEY", None)
            with pytest.raises(ValueError, match="SARVAM_API_KEY"):
                sarvam_ns.TTS()

    def test_env_var_fallback(self) -> None:
        with patch.dict("os.environ", {"SARVAM_API_KEY": "env-key"}, clear=False):
            tts = sarvam_ns.TTS()
            assert tts.api_key == "env-key"

    def test_explicit_api_key_wins(self) -> None:
        with patch.dict("os.environ", {"SARVAM_API_KEY": "env-key"}, clear=False):
            tts = sarvam_ns.TTS(api_key="explicit")
            assert tts.api_key == "explicit"

    def test_namespace_for_twilio_uses_env_key(self) -> None:
        with patch.dict("os.environ", {"SARVAM_API_KEY": "env-key"}, clear=False):
            tts = sarvam_ns.TTS.for_twilio()
            assert tts.sample_rate == 8000
            assert str(tts.codec) == "mulaw"
            assert tts.api_key == "env-key"

    def test_namespace_defaults_bulbul_v3(self) -> None:
        with patch.dict("os.environ", {"SARVAM_API_KEY": "env-key"}, clear=False):
            tts = sarvam_ns.TTS()
            assert str(tts.model) == "bulbul:v3"
            assert tts.speaker == "shubh"
