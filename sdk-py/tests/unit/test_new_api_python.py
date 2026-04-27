"""Unit tests for the Phase 1a v0.5.0 public API surface.

Covers:

* ``getpatter.stt.*.STT``, ``getpatter.tts.*.TTS`` — env fallback on api_key,
  explicit-key instantiation, and helpful errors when neither is set.
* ``getpatter.carriers.twilio.Carrier`` / ``getpatter.carriers.telnyx.Carrier``
  — credentials dataclasses with ``.kind`` discriminator.
* ``getpatter.engines.openai.Realtime`` / ``getpatter.engines.elevenlabs.ConvAI``
  — marker dataclasses.
* ``getpatter.tunnels.{Ngrok,CloudflareTunnel,Static}``.
* ``patter.Tool`` / ``patter.tool()`` validation.
* ``patter.Guardrail`` / ``patter.guardrail()`` — frozen dataclass with a
  factory alias.
* Flat aliases (``Twilio``, ``Telnyx``, ``OpenAIRealtime``, ``ElevenLabsConvAI``).
"""

from __future__ import annotations

from typing import Callable

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _scrub_env(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    for key in keys:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# STT adapters
# ---------------------------------------------------------------------------


class TestDeepgramSTT:
    def test_explicit_api_key(self) -> None:
        from getpatter.stt import deepgram

        stt = deepgram.STT(api_key="dg_explicit")
        assert stt.api_key == "dg_explicit"
        assert stt.language == "en"
        assert stt.model == "nova-3"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.stt import deepgram

        monkeypatch.setenv("DEEPGRAM_API_KEY", "dg_env")
        stt = deepgram.STT()
        assert stt.api_key == "dg_env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.stt import deepgram

        _scrub_env(monkeypatch, "DEEPGRAM_API_KEY")
        with pytest.raises(ValueError, match="DEEPGRAM_API_KEY"):
            deepgram.STT()


class TestWhisperSTT:
    def test_explicit_api_key(self) -> None:
        from getpatter.stt import whisper

        stt = whisper.STT(api_key="sk-explicit")
        assert stt.api_key == "sk-explicit"
        assert stt.model == "whisper-1"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.stt import whisper

        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        stt = whisper.STT()
        assert stt.api_key == "sk-env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.stt import whisper

        _scrub_env(monkeypatch, "OPENAI_API_KEY")
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            whisper.STT()


class TestCartesiaSTT:
    def test_explicit_api_key(self) -> None:
        from getpatter.stt import cartesia

        stt = cartesia.STT(api_key="ct_explicit")
        # CartesiaSTT uses a private attribute.
        assert stt._api_key == "ct_explicit"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.stt import cartesia

        monkeypatch.setenv("CARTESIA_API_KEY", "ct_env")
        stt = cartesia.STT()
        assert stt._api_key == "ct_env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.stt import cartesia

        _scrub_env(monkeypatch, "CARTESIA_API_KEY")
        with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
            cartesia.STT()


class TestSonioxSTT:
    def test_explicit_api_key(self) -> None:
        from getpatter.stt import soniox

        stt = soniox.STT(api_key="sx_explicit")
        assert stt.api_key == "sx_explicit"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.stt import soniox

        monkeypatch.setenv("SONIOX_API_KEY", "sx_env")
        stt = soniox.STT()
        assert stt.api_key == "sx_env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.stt import soniox

        _scrub_env(monkeypatch, "SONIOX_API_KEY")
        with pytest.raises(ValueError, match="SONIOX_API_KEY"):
            soniox.STT()


class TestSpeechmaticsSTT:
    """Speechmatics requires the optional SDK; tolerate its absence."""

    def test_explicit_api_key(self) -> None:
        pytest.importorskip("speechmatics.voice")
        from getpatter.stt import speechmatics

        stt = speechmatics.STT(api_key="sm_explicit")
        assert stt.api_key == "sm_explicit"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("speechmatics.voice")
        from getpatter.stt import speechmatics

        monkeypatch.setenv("SPEECHMATICS_API_KEY", "sm_env")
        stt = speechmatics.STT()
        assert stt.api_key == "sm_env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.stt import speechmatics

        _scrub_env(monkeypatch, "SPEECHMATICS_API_KEY")
        with pytest.raises(ValueError, match="SPEECHMATICS_API_KEY"):
            speechmatics.STT()


class TestAssemblyAISTT:
    def test_explicit_api_key(self) -> None:
        from getpatter.stt import assemblyai

        stt = assemblyai.STT(api_key="ay_explicit")
        assert stt._api_key == "ay_explicit"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.stt import assemblyai

        monkeypatch.setenv("ASSEMBLYAI_API_KEY", "ay_env")
        stt = assemblyai.STT()
        assert stt._api_key == "ay_env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.stt import assemblyai

        _scrub_env(monkeypatch, "ASSEMBLYAI_API_KEY")
        with pytest.raises(ValueError, match="ASSEMBLYAI_API_KEY"):
            assemblyai.STT()


# ---------------------------------------------------------------------------
# TTS adapters
# ---------------------------------------------------------------------------


class TestElevenLabsTTS:
    def test_explicit_api_key(self) -> None:
        from getpatter.tts import elevenlabs

        tts = elevenlabs.TTS(api_key="el_explicit")
        assert tts.api_key == "el_explicit"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.tts import elevenlabs

        monkeypatch.setenv("ELEVENLABS_API_KEY", "el_env")
        tts = elevenlabs.TTS()
        assert tts.api_key == "el_env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.tts import elevenlabs

        _scrub_env(monkeypatch, "ELEVENLABS_API_KEY")
        with pytest.raises(ValueError, match="ELEVENLABS_API_KEY"):
            elevenlabs.TTS()


class TestOpenAITTS:
    def test_explicit_api_key(self) -> None:
        from getpatter.tts import openai as openai_tts

        tts = openai_tts.TTS(api_key="sk-explicit")
        assert tts.api_key == "sk-explicit"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.tts import openai as openai_tts

        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        tts = openai_tts.TTS()
        assert tts.api_key == "sk-env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.tts import openai as openai_tts

        _scrub_env(monkeypatch, "OPENAI_API_KEY")
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            openai_tts.TTS()


class TestCartesiaTTS:
    def test_explicit_api_key(self) -> None:
        from getpatter.tts import cartesia as cartesia_tts

        tts = cartesia_tts.TTS(api_key="ct_explicit")
        assert tts.api_key == "ct_explicit"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.tts import cartesia as cartesia_tts

        monkeypatch.setenv("CARTESIA_API_KEY", "ct_env")
        tts = cartesia_tts.TTS()
        assert tts.api_key == "ct_env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.tts import cartesia as cartesia_tts

        _scrub_env(monkeypatch, "CARTESIA_API_KEY")
        with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
            cartesia_tts.TTS()

    def test_default_model_is_sonic_3(self) -> None:
        """Default model must be ``sonic-3`` (current Cartesia GA, ~90 ms TTFB)."""
        from getpatter.tts import cartesia as cartesia_tts

        tts = cartesia_tts.TTS(api_key="ct_explicit")
        assert tts.model == "sonic-3"

    def test_default_api_version_is_2025_04_16(self) -> None:
        """API version pin must match the current GA snapshot."""
        from getpatter.tts import cartesia as cartesia_tts

        tts = cartesia_tts.TTS(api_key="ct_explicit")
        assert tts.api_version == "2025-04-16"

    def test_low_level_default_model_is_sonic_3(self) -> None:
        """The low-level provider class must also default to ``sonic-3``."""
        from getpatter.providers.cartesia_tts import CartesiaTTS

        tts = CartesiaTTS(api_key="ct_explicit")
        assert tts.model == "sonic-3"

    def test_model_override(self) -> None:
        """Callers can still pin ``sonic-2`` for backwards compatibility."""
        from getpatter.tts import cartesia as cartesia_tts

        tts = cartesia_tts.TTS(api_key="ct_explicit", model="sonic-2")
        assert tts.model == "sonic-2"


class TestRimeTTS:
    def test_explicit_api_key(self) -> None:
        from getpatter.tts import rime

        tts = rime.TTS(api_key="rm_explicit")
        assert tts.api_key == "rm_explicit"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.tts import rime

        monkeypatch.setenv("RIME_API_KEY", "rm_env")
        tts = rime.TTS()
        assert tts.api_key == "rm_env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.tts import rime

        _scrub_env(monkeypatch, "RIME_API_KEY")
        with pytest.raises(ValueError, match="RIME_API_KEY"):
            rime.TTS()


class TestLMNTTTS:
    def test_explicit_api_key(self) -> None:
        from getpatter.tts import lmnt

        tts = lmnt.TTS(api_key="lm_explicit")
        assert tts.api_key == "lm_explicit"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.tts import lmnt

        monkeypatch.setenv("LMNT_API_KEY", "lm_env")
        tts = lmnt.TTS()
        assert tts.api_key == "lm_env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.tts import lmnt

        _scrub_env(monkeypatch, "LMNT_API_KEY")
        with pytest.raises(ValueError, match="LMNT_API_KEY"):
            lmnt.TTS()


# ---------------------------------------------------------------------------
# Carriers
# ---------------------------------------------------------------------------


class TestTwilioCarrier:
    def test_explicit_credentials(self) -> None:
        from getpatter.carriers import twilio

        c = twilio.Carrier(account_sid="AC_explicit", auth_token="tok_explicit")
        assert c.account_sid == "AC_explicit"
        assert c.auth_token == "tok_explicit"
        assert c.kind == "twilio"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.carriers import twilio

        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_env")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok_env")
        c = twilio.Carrier()
        assert c.account_sid == "AC_env"
        assert c.auth_token == "tok_env"

    def test_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.carriers import twilio

        _scrub_env(monkeypatch, "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
        with pytest.raises(ValueError, match="TWILIO_ACCOUNT_SID"):
            twilio.Carrier()


class TestTelnyxCarrier:
    def test_explicit_credentials(self) -> None:
        from getpatter.carriers import telnyx

        c = telnyx.Carrier(api_key="tel_explicit", connection_id="conn_x")
        assert c.api_key == "tel_explicit"
        assert c.connection_id == "conn_x"
        assert c.kind == "telnyx"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.carriers import telnyx

        monkeypatch.setenv("TELNYX_API_KEY", "tel_env")
        monkeypatch.setenv("TELNYX_CONNECTION_ID", "conn_env")
        monkeypatch.setenv("TELNYX_PUBLIC_KEY", "pub_env")
        c = telnyx.Carrier()
        assert c.api_key == "tel_env"
        assert c.connection_id == "conn_env"
        assert c.public_key == "pub_env"

    def test_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.carriers import telnyx

        _scrub_env(monkeypatch, "TELNYX_API_KEY", "TELNYX_CONNECTION_ID", "TELNYX_PUBLIC_KEY")
        with pytest.raises(ValueError, match="TELNYX_API_KEY"):
            telnyx.Carrier()


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------


class TestOpenAIRealtimeEngine:
    def test_explicit_api_key(self) -> None:
        from getpatter.engines import openai as eng_openai

        engine = eng_openai.Realtime(api_key="sk-explicit", voice="nova")
        assert engine.api_key == "sk-explicit"
        assert engine.voice == "nova"
        assert engine.kind == "openai_realtime"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.engines import openai as eng_openai

        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        engine = eng_openai.Realtime()
        assert engine.api_key == "sk-env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.engines import openai as eng_openai

        _scrub_env(monkeypatch, "OPENAI_API_KEY")
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            eng_openai.Realtime()


class TestElevenLabsConvAIEngine:
    def test_explicit_api_key(self) -> None:
        from getpatter.engines import elevenlabs as eng_el

        engine = eng_el.ConvAI(api_key="el_explicit", agent_id="ag_1")
        assert engine.api_key == "el_explicit"
        assert engine.agent_id == "ag_1"
        assert engine.kind == "elevenlabs_convai"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.engines import elevenlabs as eng_el

        monkeypatch.setenv("ELEVENLABS_API_KEY", "el_env")
        monkeypatch.setenv("ELEVENLABS_AGENT_ID", "ag_env")
        engine = eng_el.ConvAI()
        assert engine.api_key == "el_env"
        assert engine.agent_id == "ag_env"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter.engines import elevenlabs as eng_el

        _scrub_env(monkeypatch, "ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID")
        with pytest.raises(ValueError, match="ELEVENLABS_API_KEY"):
            eng_el.ConvAI()


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------


class TestTunnels:
    def test_ngrok_default(self) -> None:
        from getpatter.tunnels import Ngrok

        t = Ngrok()
        assert t.hostname is None
        assert t.kind == "ngrok"

    def test_ngrok_with_hostname(self) -> None:
        from getpatter.tunnels import Ngrok

        t = Ngrok(hostname="abc.ngrok.io")
        assert t.hostname == "abc.ngrok.io"

    def test_cloudflare_tunnel(self) -> None:
        from getpatter.tunnels import CloudflareTunnel

        t = CloudflareTunnel()
        assert t.kind == "cloudflare"

    def test_static_requires_hostname(self) -> None:
        from getpatter.tunnels import Static

        with pytest.raises(ValueError, match="hostname"):
            Static(hostname="")

    def test_static_holds_hostname(self) -> None:
        from getpatter.tunnels import Static

        t = Static(hostname="agent.example.com")
        assert t.hostname == "agent.example.com"
        assert t.kind == "static"


# ---------------------------------------------------------------------------
# Tool / tool()
# ---------------------------------------------------------------------------


class TestTool:
    def test_keyword_with_handler(self) -> None:
        from getpatter import Tool, tool

        def handler(args: dict, ctx: dict) -> str:
            return "ok"

        t = tool(name="do_thing", description="Do it", handler=handler)
        assert isinstance(t, Tool)
        assert t.name == "do_thing"
        assert t.handler is handler
        assert t.webhook_url == ""

    def test_keyword_with_webhook(self) -> None:
        from getpatter import tool

        t = tool(name="do_thing", webhook_url="https://example.com/hook")
        assert t.webhook_url == "https://example.com/hook"
        assert t.handler is None

    def test_rejects_both_handler_and_webhook(self) -> None:
        from getpatter import Tool

        def handler(args: dict, ctx: dict) -> str:
            return "ok"

        with pytest.raises(ValueError, match="exactly one"):
            Tool(name="x", handler=handler, webhook_url="https://x/y")

    def test_rejects_neither_handler_nor_webhook(self) -> None:
        from getpatter import Tool

        with pytest.raises(ValueError, match="handler.*webhook_url|webhook_url.*handler"):
            Tool(name="x")

    def test_rejects_empty_name(self) -> None:
        from getpatter import Tool

        def handler(args: dict, ctx: dict) -> str:
            return "ok"

        with pytest.raises(ValueError, match="non-empty name"):
            Tool(name="", handler=handler)

    def test_decorator_form_returns_tool(self) -> None:
        from getpatter import Tool, tool

        @tool
        def get_weather(location: str, unit: str = "celsius") -> str:
            """Get the current weather for a location.

            Args:
                location: City or zip code.
                unit: Temperature unit (celsius or fahrenheit).
            """
            return f"Sunny in {location}, 22°{unit[0].upper()}"

        assert isinstance(get_weather, Tool)
        assert get_weather.name == "get_weather"
        assert "weather" in get_weather.description.lower()
        assert get_weather.parameters is not None
        assert "location" in get_weather.parameters["properties"]
        assert callable(get_weather.handler)

    def test_factory_keyword_requires_name(self) -> None:
        from getpatter import tool

        def handler(args: dict, ctx: dict) -> str:
            return "ok"

        with pytest.raises(ValueError, match="'name'"):
            tool(handler=handler)


# ---------------------------------------------------------------------------
# Guardrail
# ---------------------------------------------------------------------------


class TestGuardrail:
    def test_top_level_importable(self) -> None:
        from getpatter import Guardrail

        g = Guardrail(name="no-medical", blocked_terms=["prescription"])
        assert g.name == "no-medical"
        assert g.blocked_terms == ["prescription"]
        # Frozen dataclass — mutation should fail.
        with pytest.raises(Exception):
            g.name = "other"  # type: ignore[misc]

    def test_factory_alias(self) -> None:
        from getpatter import Guardrail, guardrail

        g = guardrail(name="custom", check=lambda text: "bad" in text)
        assert isinstance(g, Guardrail)
        assert g.name == "custom"
        assert callable(g.check)

# ---------------------------------------------------------------------------
# Flat aliases
# ---------------------------------------------------------------------------


class TestFlatAliases:
    def test_aliases_resolve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from getpatter import (
            ElevenLabsConvAI,
            OpenAIRealtime,
            Telnyx,
            Twilio,
        )
        from getpatter.carriers.telnyx import Carrier as TelnyxCarrier
        from getpatter.carriers.twilio import Carrier as TwilioCarrier
        from getpatter.engines.elevenlabs import ConvAI
        from getpatter.engines.openai import Realtime

        assert Twilio is TwilioCarrier
        assert Telnyx is TelnyxCarrier
        assert OpenAIRealtime is Realtime
        assert ElevenLabsConvAI is ConvAI
