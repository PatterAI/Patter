import pytest
from getpatter import (
    DeepgramSTT,
    ElevenLabsTTS,
    Patter,
    Twilio,
)


def test_api_key_raises_not_implemented():
    """Cloud mode is not yet available — api_key= raises NotImplementedError."""
    with pytest.raises(NotImplementedError, match="Patter Cloud is not yet available"):
        Patter(api_key="pt_test123")


def test_backend_url_raises_not_implemented():
    with pytest.raises(NotImplementedError, match="Patter Cloud is not yet available"):
        Patter(backend_url="wss://custom.server.com")


def test_local_mode_constructs():
    """Local-mode construction with carrier + phone_number succeeds."""
    phone = Patter(
        carrier=Twilio(account_sid="AC_test", auth_token="tok"),
        phone_number="+15550001234",
    )
    assert phone is not None


def test_agent_stt_instance_stored():
    phone = Patter(
        carrier=Twilio(account_sid="AC_test", auth_token="tok"),
        phone_number="+15550001234",
    )
    stt = DeepgramSTT(api_key="dg_test")
    tts = ElevenLabsTTS(api_key="el_test")
    ag = phone.agent(system_prompt="hi", stt=stt, tts=tts)
    assert ag.provider == "pipeline"
    assert ag.stt is stt
    assert ag.tts is tts


def test_agent_tts_instance_stored():
    phone = Patter(
        carrier=Twilio(account_sid="AC_test", auth_token="tok"),
        phone_number="+15550001234",
    )
    stt = DeepgramSTT(api_key="dg_test")
    tts = ElevenLabsTTS(api_key="el_test", voice_id="aria")
    ag = phone.agent(system_prompt="hi", stt=stt, tts=tts, voice="aria")
    assert ag.tts is tts


def test_agent_rejects_non_stt_provider_type():
    phone = Patter(
        carrier=Twilio(account_sid="AC_test", auth_token="tok"),
        phone_number="+15550001234",
    )
    with pytest.raises(TypeError, match="STTProvider"):
        phone.agent(system_prompt="hi", stt="deepgram")


def test_agent_rejects_non_tts_provider_type():
    phone = Patter(
        carrier=Twilio(account_sid="AC_test", auth_token="tok"),
        phone_number="+15550001234",
    )
    with pytest.raises(TypeError, match="TTSProvider"):
        phone.agent(system_prompt="hi", stt=DeepgramSTT(api_key="dg"), tts="elevenlabs")
