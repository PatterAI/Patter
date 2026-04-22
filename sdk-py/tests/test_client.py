import pytest
from unittest.mock import AsyncMock, patch
from getpatter import (
    DeepgramSTT,
    ElevenLabsTTS,
    OpenAIRealtime,
    Patter,
    Twilio,
)
from getpatter.providers import deepgram, elevenlabs
from getpatter.exceptions import PatterConnectionError


def test_client_init():
    phone = Patter(api_key="pt_test123")
    assert phone.api_key == "pt_test123"


def test_client_init_custom_backend():
    phone = Patter(api_key="pt_test123", backend_url="wss://custom.server.com")
    assert phone._backend_url == "wss://custom.server.com"


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


# === Managed mode (simple) ===

@pytest.mark.asyncio
async def test_managed_connect():
    """Managed mode: just on_message, no provider details."""
    phone = Patter(api_key="pt_test123")
    handler = AsyncMock(return_value="response")
    with patch.object(phone, "_register_number", new_callable=AsyncMock) as mock_reg:
        with patch.object(phone._connection, "connect", new_callable=AsyncMock) as mock_conn:
            await phone.connect(on_message=handler)
            mock_reg.assert_not_called()  # No registration in managed mode
            mock_conn.assert_called_once_with(
                on_message=handler, on_call_start=None, on_call_end=None,
            )


@pytest.mark.asyncio
async def test_managed_call_auto_connects():
    """Managed mode: call() auto-connects if on_message provided."""
    phone = Patter(api_key="pt_test123")
    handler = AsyncMock(return_value="response")
    with patch.object(phone._connection, "connect", new_callable=AsyncMock):
        with patch.object(phone._connection, "request_call", new_callable=AsyncMock) as mock_call:
            await phone.call(to="+39123", on_message=handler, first_message="Ciao")
            mock_call.assert_called_once()


# === Self-hosted mode (bring your own keys) ===

@pytest.mark.asyncio
async def test_selfhosted_connect_registers_number():
    """Self-hosted mode: provider + keys → registers number then connects."""
    phone = Patter(api_key="pt_test123")
    handler = AsyncMock(return_value="response")
    with patch.object(phone, "_register_number", new_callable=AsyncMock) as mock_reg:
        with patch.object(phone._connection, "connect", new_callable=AsyncMock) as mock_conn:
            await phone.connect(
                on_message=handler,
                provider="twilio",
                provider_key="AC_test",
                number="+16590000000",
                stt=deepgram(api_key="dg_test"),
                tts=elevenlabs(api_key="el_test"),
            )
            mock_reg.assert_called_once()
            mock_conn.assert_called_once()


# === Error cases ===

@pytest.mark.asyncio
async def test_call_raises_if_not_connected_no_handler():
    """call() without on_message and not connected raises error."""
    phone = Patter(api_key="pt_test123")
    with pytest.raises(PatterConnectionError):
        await phone.call(to="+39123")


# === Agent/Number/Call Management ===

@pytest.mark.asyncio
async def test_create_agent_method():
    phone = Patter(api_key="pt_test")
    assert hasattr(phone, "create_agent")

@pytest.mark.asyncio
async def test_buy_number_method():
    phone = Patter(api_key="pt_test")
    assert hasattr(phone, "buy_number")

@pytest.mark.asyncio
async def test_assign_agent_method():
    phone = Patter(api_key="pt_test")
    assert hasattr(phone, "assign_agent")

@pytest.mark.asyncio
async def test_list_calls_method():
    phone = Patter(api_key="pt_test")
    assert hasattr(phone, "list_calls")
