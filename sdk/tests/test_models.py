from patter.models import IncomingMessage, STTConfig, TTSConfig


def test_incoming_message():
    msg = IncomingMessage(text="ciao come stai", call_id="call_123", caller="+39111222333")
    assert msg.text == "ciao come stai"
    assert msg.call_id == "call_123"


def test_stt_config_deepgram():
    config = STTConfig(provider="deepgram", api_key="dg_test")
    assert config.to_dict() == {"provider": "deepgram", "api_key": "dg_test", "language": "en"}


def test_stt_config_whisper():
    config = STTConfig(provider="whisper", api_key="sk_test", language="it")
    assert config.to_dict() == {"provider": "whisper", "api_key": "sk_test", "language": "it"}


def test_tts_config_elevenlabs():
    config = TTSConfig(provider="elevenlabs", api_key="el_test", voice="aria")
    assert config.to_dict() == {"provider": "elevenlabs", "api_key": "el_test", "voice": "aria"}


def test_tts_config_openai():
    config = TTSConfig(provider="openai", api_key="sk_test", voice="nova")
    assert config.to_dict() == {"provider": "openai", "api_key": "sk_test", "voice": "nova"}
