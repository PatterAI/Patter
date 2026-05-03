"""Smoke tests for the legacy internal STTConfig/TTSConfig builders.

These helpers live in ``getpatter.providers`` and are still called by
``client.agent()`` when the user passes legacy kwargs. Newer code should use
the public classes (``getpatter.stt.deepgram.STT``, ``getpatter.tts.openai.TTS``,
etc.) — those have their own tests in ``tests/unit/test_new_api_python.py``.
"""

from getpatter.providers import deepgram, whisper, elevenlabs
from getpatter.providers import openai_tts as _openai_tts_module
from getpatter.models import STTConfig, TTSConfig

# The submodule ``getpatter.providers.openai_tts`` shadows the legacy
# ``openai_tts()`` builder function when the tts namespace modules are
# imported, so fetch the function directly from the providers package dict.
import getpatter.providers as _providers_pkg

_openai_tts_fn = None
for _name, _obj in vars(_providers_pkg).items():
    if _name == "openai_tts" and callable(_obj) and not isinstance(_obj, type(_providers_pkg)):
        _openai_tts_fn = _obj
        break


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


def test_openai_tts_public_class_builds():
    """The public ``OpenAITTS`` class is the supported way to build an
    OpenAI TTS adapter — covered end-to-end in the new-api tests. Here we
    just prove the top-level alias resolves correctly."""
    from getpatter import OpenAITTS

    tts = OpenAITTS(api_key="sk_test", voice="nova")
    assert tts.voice == "nova"
