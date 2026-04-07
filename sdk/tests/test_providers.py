from patter.providers import deepgram, whisper, elevenlabs, openai_tts
from patter.models import STTConfig, TTSConfig


def test_deepgram_returns_stt_config():
    config = deepgram(api_key="dg_test")
    assert isinstance(config, STTConfig)
    assert config.provider == "deepgram"


def test_deepgram_with_language():
    config = deepgram(api_key="dg_test", language="it")
    assert config.language == "it"


def test_whisper_returns_stt_config():
    config = whisper(api_key="sk_test")
    assert isinstance(config, STTConfig)


def test_elevenlabs_returns_tts_config():
    config = elevenlabs(api_key="el_test", voice="aria")
    assert isinstance(config, TTSConfig)
    assert config.voice == "aria"


def test_openai_tts_returns_tts_config():
    config = openai_tts(api_key="sk_test", voice="nova")
    assert isinstance(config, TTSConfig)
    assert config.voice == "nova"


def test_openai_tts_default_voice():
    config = openai_tts(api_key="sk_test")
    assert config.voice == "alloy"
