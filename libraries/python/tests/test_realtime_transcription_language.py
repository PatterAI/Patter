"""Unit tests for the ``transcription_language`` knob on the OpenAI Realtime
adapters and its threading from the engine markers down to the session config.

``transcription_language`` (ISO-639-1) pins the Realtime session's input
transcription to one language instead of auto-detecting per utterance —
auto-detect mislabels short / noisy phone speech (an Italian "sì" comes back
tagged Spanish or English). It is optional and display-only: omitting it keeps
today's auto-detect behaviour, so the language key must be ABSENT from the
session config when unset.

No WebSocket is opened — these assert the dicts produced by the real
session-config builders directly, so no external boundary is mocked and no
``mocked`` marker is needed.
"""

from __future__ import annotations

import os

from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter
from getpatter.providers.openai_realtime_2 import OpenAIRealtime2Adapter


def _v1(**kwargs) -> OpenAIRealtimeAdapter:
    return OpenAIRealtimeAdapter(api_key="sk-test", **kwargs)


def _ga(**kwargs) -> OpenAIRealtime2Adapter:
    return OpenAIRealtime2Adapter(api_key="sk-test", **kwargs)


# ---------------------------------------------------------------------------
# Adapter session-config builders
# ---------------------------------------------------------------------------


def test_v1_transcription_language_pins_input_transcription() -> None:
    config = _v1(transcription_language="it")._build_session_config()
    assert config["input_audio_transcription"]["language"] == "it"


def test_v1_transcription_language_omitted_when_unset() -> None:
    config = _v1()._build_session_config()
    # Auto-detect (today's behaviour): no language key emitted.
    assert "language" not in config["input_audio_transcription"]


def test_ga_transcription_language_nested_under_audio_input() -> None:
    config = _ga(transcription_language="it")._build_ga_session_config()
    assert config["audio"]["input"]["transcription"]["language"] == "it"


def test_ga_transcription_language_omitted_when_unset() -> None:
    config = _ga()._build_ga_session_config()
    assert "language" not in config["audio"]["input"]["transcription"]


def test_v1_transcription_language_independent_of_model() -> None:
    # The language key sits alongside the model key; setting one must not drop
    # the other.
    config = _v1(
        transcription_language="es",
        input_audio_transcription_model="gpt-realtime-whisper",
    )._build_session_config()
    transcription = config["input_audio_transcription"]
    assert transcription["language"] == "es"
    assert transcription["model"] == "gpt-realtime-whisper"


# ---------------------------------------------------------------------------
# Engine-marker → Agent → adapter threading
# ---------------------------------------------------------------------------


def _phone():
    from getpatter import Patter, Twilio

    os.environ.setdefault("OPENAI_API_KEY", "sk-test")
    return Patter(
        carrier=Twilio(account_sid="ACtest", auth_token="x"),
        phone_number="+15555550100",
    )


def test_engine_marker_threads_language_to_agent_realtime_2() -> None:
    from getpatter import OpenAIRealtime2

    eng = OpenAIRealtime2(api_key="sk-test", transcription_language="it")
    assert eng.transcription_language == "it"
    agent = _phone().agent(engine=eng, system_prompt="hi")
    assert agent.openai_realtime_transcription_language == "it"


def test_engine_marker_threads_language_to_agent_realtime_v1() -> None:
    from getpatter import OpenAIRealtime

    eng = OpenAIRealtime(api_key="sk-test", transcription_language="en")
    assert eng.transcription_language == "en"
    agent = _phone().agent(engine=eng, system_prompt="hi")
    assert agent.openai_realtime_transcription_language == "en"


def test_agent_language_defaults_to_none_without_engine_field() -> None:
    from getpatter import OpenAIRealtime2

    agent = _phone().agent(
        engine=OpenAIRealtime2(api_key="sk-test"), system_prompt="hi"
    )
    assert agent.openai_realtime_transcription_language is None
