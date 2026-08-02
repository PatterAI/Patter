"""Tests for the Fish Audio HTTP TTS provider.

These run against a **real in-process aiohttp server** bound to 127.0.0.1 —
only the Fish endpoint itself is simulated. Everything from the adapter inward
(header assembly, JSON payload construction, prosody nesting, chunked response
streaming, error surfacing, warmup, cost accounting) executes for real over a
real socket. See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest
from aiohttp import web

from getpatter.audio.format import AudioFormat
from getpatter.providers.fish_audio_tts import (
    FishAudioFormat,
    FishAudioLatency,
    FishAudioModel,
    FishAudioTTS,
)

pytestmark = pytest.mark.mocked


class _Recorder:
    """Captures what the adapter actually put on the wire."""

    def __init__(self) -> None:
        self.tts_headers: dict[str, str] = {}
        self.tts_body: dict[str, Any] = {}
        self.model_query: dict[str, str] = {}
        self.model_hits = 0
        self.status = 200
        self.chunks: list[bytes] = [b"\x01\x02", b"\x03\x04", b"\x05\x06"]
        self.error_body = ""


@pytest.fixture
async def fish_server() -> AsyncIterator[tuple[str, _Recorder]]:
    rec = _Recorder()

    async def tts_handler(request: web.Request) -> web.StreamResponse:
        rec.tts_headers = dict(request.headers)
        rec.tts_body = await request.json()
        if rec.status != 200:
            return web.Response(status=rec.status, text=rec.error_body)
        resp = web.StreamResponse(status=200)
        resp.content_type = "application/octet-stream"
        await resp.prepare(request)
        for chunk in rec.chunks:
            await resp.write(chunk)
        await resp.write_eof()
        return resp

    async def model_handler(request: web.Request) -> web.Response:
        rec.model_hits += 1
        rec.model_query = dict(request.query)
        return web.json_response({"items": [], "total": 0})

    app = web.Application()
    app.router.add_post("/v1/tts", tts_handler)
    app.router.add_get("/model", model_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    try:
        yield f"http://{host}:{port}", rec
    finally:
        await runner.cleanup()


def _tts(base: str, **kwargs: Any) -> FishAudioTTS:
    return FishAudioTTS(api_key="fish-test-key", base_url=f"{base}/v1/tts", **kwargs)


async def _collect(tts: FishAudioTTS, text: str) -> bytes:
    out = bytearray()
    async for chunk in tts.synthesize(text):
        out.extend(chunk)
    return bytes(out)


async def test_streams_every_chunk_the_server_emits(fish_server) -> None:
    base, rec = fish_server
    tts = _tts(base)
    try:
        assert await _collect(tts, "ciao") == b"\x01\x02\x03\x04\x05\x06"
    finally:
        await tts.close()


async def test_model_travels_in_a_header_not_the_body(fish_server) -> None:
    base, rec = fish_server
    tts = _tts(base, model=FishAudioModel.S2_PRO)
    try:
        await _collect(tts, "ciao")
    finally:
        await tts.close()
    assert rec.tts_headers["model"] == "s2-pro"
    assert rec.tts_headers["Authorization"] == "Bearer fish-test-key"
    assert rec.tts_headers["Content-Type"] == "application/json"
    assert "model" not in rec.tts_body


async def test_defaults_are_s21pro_pcm_16k_balanced(fish_server) -> None:
    base, rec = fish_server
    tts = _tts(base)
    try:
        await _collect(tts, "hello")
    finally:
        await tts.close()
    assert rec.tts_headers["model"] == "s2.1-pro"
    assert rec.tts_body == {
        "text": "hello",
        "format": "pcm",
        "latency": "balanced",
        "sample_rate": 16000,
    }


async def test_unset_knobs_are_omitted_so_fish_applies_its_own_defaults(
    fish_server,
) -> None:
    base, rec = fish_server
    tts = _tts(base)
    try:
        await _collect(tts, "hello")
    finally:
        await tts.close()
    for absent in (
        "temperature",
        "top_p",
        "chunk_length",
        "normalize",
        "prosody",
        "reference_id",
        "max_new_tokens",
    ):
        assert absent not in rec.tts_body, f"{absent} should not be sent when unset"


async def test_voice_string_maps_to_reference_id(fish_server) -> None:
    base, rec = fish_server
    tts = _tts(base, voice="ref-abc123")
    try:
        await _collect(tts, "hello")
    finally:
        await tts.close()
    assert rec.tts_body["reference_id"] == "ref-abc123"


async def test_voice_sequence_maps_to_multi_speaker_reference_ids(fish_server) -> None:
    base, rec = fish_server
    tts = _tts(base, voice=("spk-a", "spk-b"))
    try:
        await _collect(tts, "<|speaker:0|>Hi<|speaker:1|>Yo")
    finally:
        await tts.close()
    assert rec.tts_body["reference_id"] == ["spk-a", "spk-b"]


async def test_flat_speed_volume_loudness_collapse_into_nested_prosody(
    fish_server,
) -> None:
    base, rec = fish_server
    tts = _tts(base, speed=1.2, volume=-3.0, normalize_loudness=False)
    try:
        await _collect(tts, "hello")
    finally:
        await tts.close()
    assert rec.tts_body["prosody"] == {
        "speed": 1.2,
        "volume": -3.0,
        "normalize_loudness": False,
    }


async def test_sample_rate_is_sent_for_pcm_and_wav_but_not_mp3(fish_server) -> None:
    base, rec = fish_server
    for fmt, expected in (
        (FishAudioFormat.PCM, True),
        (FishAudioFormat.WAV, True),
        (FishAudioFormat.MP3, False),
        (FishAudioFormat.OPUS, False),
    ):
        tts = _tts(base, format=fmt, sample_rate=24000)
        try:
            await _collect(tts, "hello")
        finally:
            await tts.close()
        assert ("sample_rate" in rec.tts_body) is expected, f"format={fmt}"


async def test_latency_mode_is_forwarded(fish_server) -> None:
    base, rec = fish_server
    tts = _tts(base, latency=FishAudioLatency.LOW)
    try:
        await _collect(tts, "hello")
    finally:
        await tts.close()
    assert rec.tts_body["latency"] == "low"


async def test_non_200_raises_with_status_and_body(fish_server) -> None:
    base, rec = fish_server
    rec.status = 402
    rec.error_body = json.dumps({"detail": "insufficient credit"})
    tts = _tts(base)
    try:
        with pytest.raises(RuntimeError, match="Fish Audio TTS error 402"):
            await _collect(tts, "hello")
    finally:
        await tts.close()


async def test_warmup_hits_the_free_model_listing_not_the_billed_endpoint(
    fish_server,
) -> None:
    base, rec = fish_server
    tts = FishAudioTTS(api_key="fish-test-key", base_url=f"{base}/v1/tts")
    # Point the module-level warmup URL at the local server for this test.
    import getpatter.providers.fish_audio_tts as mod

    original = mod.FISH_AUDIO_MODELS_URL
    mod.FISH_AUDIO_MODELS_URL = f"{base}/model"
    try:
        await tts.warmup()
    finally:
        mod.FISH_AUDIO_MODELS_URL = original
        await tts.close()
    assert rec.model_hits == 1
    assert rec.model_query == {"page_size": "1"}
    # The billed synthesis endpoint was never touched.
    assert rec.tts_body == {}


async def test_warmup_never_raises_when_the_host_is_unreachable() -> None:
    tts = FishAudioTTS(api_key="fish-test-key", base_url="http://127.0.0.1:1/v1/tts")
    import getpatter.providers.fish_audio_tts as mod

    original = mod.FISH_AUDIO_MODELS_URL
    mod.FISH_AUDIO_MODELS_URL = "http://127.0.0.1:1/model"
    try:
        await tts.warmup()  # must not raise
    finally:
        mod.FISH_AUDIO_MODELS_URL = original
        await tts.close()


def test_source_audio_format_tracks_the_configured_sample_rate() -> None:
    assert FishAudioTTS(api_key="k").source_audio_format() == AudioFormat(
        encoding="pcm_s16le", sample_rate=16000
    )
    assert FishAudioTTS(api_key="k", sample_rate=24000).source_audio_format() == (
        AudioFormat(encoding="pcm_s16le", sample_rate=24000)
    )


def test_for_twilio_requests_8k_pcm_so_the_pipeline_skips_the_resample() -> None:
    tts = FishAudioTTS.for_twilio(api_key="k")
    assert tts.format == FishAudioFormat.PCM
    assert tts.source_audio_format() == AudioFormat(
        encoding="pcm_s16le", sample_rate=8000
    )


def test_for_telnyx_keeps_the_16k_pipeline_rate() -> None:
    tts = FishAudioTTS.for_telnyx(api_key="k")
    assert tts.source_audio_format() == AudioFormat(
        encoding="pcm_s16le", sample_rate=16000
    )


def test_carrier_factories_preserve_other_options() -> None:
    tts = FishAudioTTS.for_twilio(api_key="k", model="s2-pro", voice="ref-1", speed=1.1)
    assert (tts.model, tts.voice, tts.speed) == ("s2-pro", "ref-1", 1.1)


def test_api_key_falls_back_to_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "env-key")
    assert FishAudioTTS().api_key == "env-key"


def test_missing_api_key_raises_a_pointed_error(monkeypatch) -> None:
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
    with pytest.raises(ValueError, match="FISH_AUDIO_API_KEY"):
        FishAudioTTS()


def test_repr_never_leaks_the_api_key() -> None:
    assert "super-secret" not in repr(FishAudioTTS(api_key="super-secret"))


async def test_cost_is_metered_in_utf8_bytes_not_characters(fish_server) -> None:
    """Fish bills UTF-8 bytes; a CJK string costs ~3x its character count."""
    base, _rec = fish_server
    captured: list[dict] = []

    import getpatter.observability.attributes as attrs

    original = attrs.record_patter_attrs
    attrs.record_patter_attrs = lambda payload: captured.append(payload)  # type: ignore[assignment]
    tts = _tts(base)
    try:
        await _collect(tts, "你好世界")  # 4 chars, 12 UTF-8 bytes
    finally:
        attrs.record_patter_attrs = original  # type: ignore[assignment]
        await tts.close()

    assert captured, "synthesis should record a cost attribute"
    assert captured[0]["patter.cost.tts_chars"] == 12
    assert captured[0]["patter.tts.provider"] == "fish_audio"
