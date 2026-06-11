"""Unit tests for the v0.5.0 instance-based Patter client surface.

Exercises the instance-based dispatch layered on top of the :class:`Patter`
client:

* ``Patter(carrier=Twilio(...))`` / ``Patter(carrier=Telnyx(...))`` routing.
* ``phone.agent(engine=OpenAIRealtime(...))`` /
  ``phone.agent(engine=ElevenLabsConvAI(...))`` dispatch.
* ``phone.agent(stt=<STTProvider>, tts=<TTSProvider>)`` instance form.
* ``phone.agent(tools=[Tool(...)])`` and ``phone.agent(guardrails=[Guardrail(...)])``.
* ``Patter(tunnel=CloudflareTunnel()/Static(...))`` directives.
"""

from __future__ import annotations

import pytest

from getpatter import (
    ElevenLabsConvAI,
    Guardrail,
    OpenAIRealtime,
    Patter,
    Telnyx,
    Tool,
    Twilio,
    guardrail,
    tool,
)
from getpatter.stt import deepgram as deepgram_stt
from getpatter.tts import elevenlabs as elevenlabs_tts
from getpatter.tunnels import CloudflareTunnel, Ngrok, Static


# ---------------------------------------------------------------------------
# Carrier dispatch
# ---------------------------------------------------------------------------


class TestCarrierDispatch:
    def test_twilio_carrier_routes_credentials(self) -> None:
        phone = Patter(
            carrier=Twilio(account_sid="AC_test", auth_token="tok"),
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        assert phone._local_config.telephony_provider == "twilio"
        assert phone._local_config.twilio_sid == "AC_test"
        assert phone._local_config.twilio_token == "tok"

    def test_telnyx_carrier_routes_credentials(self) -> None:
        phone = Patter(
            carrier=Telnyx(api_key="KEY", connection_id="200"),
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        assert phone._local_config.telephony_provider == "telnyx"
        assert phone._local_config.telnyx_key == "KEY"
        assert phone._local_config.telnyx_connection_id == "200"

    def test_invalid_carrier_type_raises(self) -> None:
        with pytest.raises(TypeError, match="Twilio.*Telnyx"):
            Patter(carrier="twilio", phone_number="+15550001234")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Engine dispatch
# ---------------------------------------------------------------------------


class TestEngineDispatch:
    def _phone(self) -> Patter:
        return Patter(
            carrier=Twilio(account_sid="AC", auth_token="tok"),
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )

    def test_openai_realtime_engine_sets_provider(self) -> None:
        phone = self._phone()
        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-engine", voice="nova"),
            system_prompt="hi",
        )
        assert agent.provider == "openai_realtime"
        assert agent.voice == "nova"
        # The engine's api_key is propagated into LocalConfig so OpenAI Realtime
        # dispatch can pull it without the user passing it to Patter() twice.
        assert phone._local_config.openai_key == "sk-engine"

    def test_elevenlabs_convai_engine_sets_provider(self) -> None:
        phone = self._phone()
        agent = phone.agent(
            engine=ElevenLabsConvAI(api_key="el_test", agent_id="agt_abc"),
            system_prompt="hi",
        )
        assert agent.provider == "elevenlabs_convai"
        assert phone._local_config.elevenlabs_key == "el_test"

    def test_invalid_engine_type_raises(self) -> None:
        phone = self._phone()
        with pytest.raises(TypeError, match="OpenAIRealtime.*ElevenLabsConvAI"):
            phone.agent(engine="openai_realtime", system_prompt="hi")  # type: ignore[arg-type]

    def test_pipeline_inferred_from_stt_tts(self) -> None:
        """Omitting engine but providing stt+tts picks pipeline mode."""
        phone = self._phone()
        agent = phone.agent(
            system_prompt="hi",
            stt=deepgram_stt.STT(api_key="dg_test"),
            tts=elevenlabs_tts.TTS(api_key="el_test"),
        )
        assert agent.provider == "pipeline"


# ---------------------------------------------------------------------------
# STT / TTS dispatch
# ---------------------------------------------------------------------------


class TestSTTTTSDispatch:
    def _phone(self) -> Patter:
        return Patter(
            carrier=Twilio(account_sid="AC", auth_token="tok"),
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )

    def test_stt_instance_is_stored_directly(self) -> None:
        phone = self._phone()
        stt = deepgram_stt.STT(api_key="dg_test")
        tts = elevenlabs_tts.TTS(api_key="el_test", voice_id="rachel")
        agent = phone.agent(
            system_prompt="hi",
            stt=stt,
            tts=tts,
        )
        assert agent.stt is stt
        assert agent.tts is tts

    def test_stt_string_raises_type_error(self) -> None:
        """Legacy string form was removed in v0.5.0."""
        phone = self._phone()
        with pytest.raises(TypeError, match="STTProvider"):
            phone.agent(
                system_prompt="hi",
                stt="deepgram",
                tts=elevenlabs_tts.TTS(api_key="el"),
            )

    def test_tts_string_raises_type_error(self) -> None:
        phone = self._phone()
        with pytest.raises(TypeError, match="TTSProvider"):
            phone.agent(
                system_prompt="hi",
                stt=deepgram_stt.STT(api_key="dg"),
                tts="elevenlabs",
            )

    def test_stt_provider_is_cloned_per_call(self) -> None:
        """An STTProvider instance is CLONED per call (same type/config,
        fresh connection state) — sharing one stateful adapter across
        concurrent calls bled transcripts between callers. TTS instances
        still flow through untouched."""
        from getpatter.telephony.common import (
            _create_stt_from_config,
            _create_tts_from_config,
        )

        stt = deepgram_stt.STT(api_key="dg_bypass")
        tts = elevenlabs_tts.TTS(api_key="el_bypass")
        resolved = _create_stt_from_config(stt)
        assert resolved is not stt
        assert type(resolved) is type(stt)
        assert resolved.api_key == stt.api_key
        # Two calls → two distinct adapters.
        assert _create_stt_from_config(stt) is not resolved
        assert _create_tts_from_config(tts) is tts


# ---------------------------------------------------------------------------
# Tools / guardrails
# ---------------------------------------------------------------------------


class TestToolsAndGuardrails:
    def _phone(self) -> Patter:
        return Patter(
            carrier=Twilio(account_sid="AC", auth_token="tok"),
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )

    def test_tool_instance_is_converted_to_dict(self) -> None:
        phone = self._phone()

        def ping(args: dict, ctx: dict) -> str:
            return "pong"

        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-x"),
            system_prompt="hi",
            tools=[Tool(name="ping", handler=ping)],
        )
        # Public collection fields are immutable tuples (frozen-dataclass contract).
        assert isinstance(agent.tools, tuple)
        assert agent.tools[0]["name"] == "ping"
        assert agent.tools[0]["handler"] is ping
        # Default parameters filled in.
        assert agent.tools[0]["parameters"] == {"type": "object", "properties": {}}

    def test_tool_dict_is_rejected(self) -> None:
        phone = self._phone()

        def ping(args: dict, ctx: dict) -> str:
            return "pong"

        with pytest.raises(TypeError, match="Tool instance"):
            phone.agent(
                engine=OpenAIRealtime(api_key="sk-x"),
                system_prompt="hi",
                tools=[{"name": "ping", "handler": ping}],
            )

    def test_tool_factory_keyword_form(self) -> None:
        phone = self._phone()
        t = tool(name="hook", webhook_url="https://example.com/h")
        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-x"),
            system_prompt="hi",
            tools=[t],
        )
        assert agent.tools[0]["webhook_url"] == "https://example.com/h"

    def test_tool_decorator_form(self) -> None:
        phone = self._phone()

        @tool
        def fetch_time(tz: str) -> str:
            """Return the current time in the given timezone."""
            return f"time in {tz}"

        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-x"),
            system_prompt="hi",
            tools=[fetch_time],
        )
        assert agent.tools[0]["name"] == "fetch_time"
        assert "tz" in agent.tools[0]["parameters"]["properties"]

    def test_guardrail_instance_is_converted_to_dict(self) -> None:
        phone = self._phone()
        g = Guardrail(name="no-foo", blocked_terms=["foo"])
        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-x"),
            system_prompt="hi",
            guardrails=[g],
        )
        # Public collection fields are immutable tuples (frozen-dataclass contract).
        assert isinstance(agent.guardrails, tuple)
        assert agent.guardrails[0]["name"] == "no-foo"
        assert agent.guardrails[0]["blocked_terms"] == ("foo",)

    def test_guardrail_factory_alias(self) -> None:
        phone = self._phone()
        g = guardrail(name="custom", check=lambda text: "bad" in text.lower())
        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-x"),
            system_prompt="hi",
            guardrails=[g],
        )
        assert callable(agent.guardrails[0]["check"])

    def test_guardrail_dict_is_rejected(self) -> None:
        phone = self._phone()
        with pytest.raises(TypeError, match="Guardrail instance"):
            phone.agent(
                engine=OpenAIRealtime(api_key="sk-x"),
                system_prompt="hi",
                guardrails=[{"name": "legacy", "blocked_terms": ["x"]}],
            )


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------


class TestTunnelDirective:
    def test_cloudflare_tunnel_stored(self) -> None:
        phone = Patter(
            carrier=Twilio(account_sid="AC", auth_token="tok"),
            phone_number="+15550001234",
            tunnel=CloudflareTunnel(),
        )
        assert isinstance(phone._tunnel_directive, CloudflareTunnel)

    def test_static_tunnel_equivalent_to_webhook_url(self) -> None:
        phone = Patter(
            carrier=Twilio(account_sid="AC", auth_token="tok"),
            phone_number="+15550001234",
            tunnel=Static(hostname="abc.ngrok.io"),
        )
        assert phone._local_config.webhook_url == "abc.ngrok.io"

    def test_ngrok_with_hostname_sets_webhook_url(self) -> None:
        phone = Patter(
            carrier=Twilio(account_sid="AC", auth_token="tok"),
            phone_number="+15550001234",
            tunnel=Ngrok(hostname="mynumber.ngrok.io"),
        )
        assert phone._local_config.webhook_url == "mynumber.ngrok.io"

    def test_ngrok_without_hostname_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="ngrok"):
            Patter(
                carrier=Twilio(account_sid="AC", auth_token="tok"),
                phone_number="+15550001234",
                tunnel=Ngrok(),
            )

    def test_tunnel_true_still_works_as_shorthand(self) -> None:
        """The ``Patter(..., tunnel=True)`` shorthand → CloudflareTunnel()."""
        phone = Patter(
            carrier=Twilio(account_sid="AC", auth_token="tok"),
            phone_number="+15550001234",
            tunnel=True,
        )
        assert isinstance(phone._tunnel_directive, CloudflareTunnel)

    def test_tunnel_conflicts_with_webhook_url(self) -> None:
        with pytest.raises(ValueError, match="webhook_url"):
            Patter(
                carrier=Twilio(account_sid="AC", auth_token="tok"),
                phone_number="+15550001234",
                webhook_url="other.ngrok.io",
                tunnel=Static(hostname="abc.ngrok.io"),
            )


# ---------------------------------------------------------------------------
# 4-line quickstart smoke test
# ---------------------------------------------------------------------------


class TestQuickstartSmoke:
    def test_four_line_quickstart_env_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_env")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok_env")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

        phone = Patter(carrier=Twilio(), phone_number="+15550001234")
        agent = phone.agent(engine=OpenAIRealtime(), system_prompt="hi")

        assert agent.provider == "openai_realtime"
        assert phone._local_config.twilio_sid == "AC_env"
        assert phone._local_config.openai_key == "sk-env"


# ---------------------------------------------------------------------------
# Engine voice/model merge precedence
# ---------------------------------------------------------------------------


class TestEngineModelVoiceMerge:
    """Explicit kwargs > engine marker > defaults.

    Mirrors the TypeScript resolution ``opts.model ?? engineModel ??
    "gpt-realtime-mini"`` (and the same for voice): an explicit ``model=`` /
    ``voice=`` kwarg always wins, even when its value equals the documented
    default — the engine fills the slot only when the kwarg is omitted.
    """

    def _phone(self) -> Patter:
        return Patter(
            carrier=Twilio(account_sid="AC", auth_token="tok"),
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )

    def test_explicit_model_wins_over_engine_model(self) -> None:
        phone = self._phone()
        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-engine", model="gpt-realtime-2"),
            system_prompt="hi",
            model="gpt-realtime-mini",
        )
        assert agent.model == "gpt-realtime-mini"

    def test_engine_model_fills_unset_kwarg(self) -> None:
        phone = self._phone()
        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-engine", model="gpt-realtime-2"),
            system_prompt="hi",
        )
        assert agent.model == "gpt-realtime-2"

    def test_explicit_voice_wins_over_engine_voice(self) -> None:
        phone = self._phone()
        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-engine", voice="nova"),
            system_prompt="hi",
            voice="alloy",
        )
        assert agent.voice == "alloy"

    def test_engine_voice_fills_unset_kwarg(self) -> None:
        phone = self._phone()
        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-engine", voice="nova"),
            system_prompt="hi",
        )
        assert agent.voice == "nova"

    def test_defaults_without_engine_or_kwargs(self) -> None:
        """Old-shape call (no voice=/model=) keeps today's documented defaults.

        Pipeline mode: no engine marker touches the slots, so the sentinel
        fallback must land on ``"alloy"`` / ``"gpt-realtime-mini"`` unchanged.
        """
        phone = self._phone()
        agent = phone.agent(
            system_prompt="hi",
            stt=deepgram_stt.STT(api_key="dg_test"),
            tts=elevenlabs_tts.TTS(api_key="el_test"),
        )
        assert agent.voice == "alloy"
        assert agent.model == "gpt-realtime-mini"

    def test_defaults_with_bare_engine(self) -> None:
        """Engine constructed with its own defaults yields the same defaults."""
        phone = self._phone()
        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-engine"),
            system_prompt="hi",
        )
        assert agent.voice == "alloy"
        assert agent.model == "gpt-realtime-mini"


class TestAgentFactoryBargeInKwargs:
    """``barge_in_mode`` / ``barge_in_confirm_ms`` are factory kwargs (TS parity).

    TypeScript ``AgentOptions`` exposes ``bargeInMode`` / ``bargeInConfirmMs``;
    the Python factory must accept the same knobs instead of forcing callers
    through ``dataclasses.replace`` on the returned Agent.
    """

    def _phone(self) -> Patter:
        return Patter(
            carrier=Twilio(account_sid="AC", auth_token="tok"),
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )

    def test_factory_passes_barge_in_mode_and_confirm_ms(self) -> None:
        phone = self._phone()
        agent = phone.agent(
            system_prompt="hi",
            stt=deepgram_stt.STT(api_key="dg_test"),
            tts=elevenlabs_tts.TTS(api_key="el_test"),
            barge_in_mode="pause_resume",
            barge_in_confirm_ms=900,
        )
        assert agent.barge_in_mode == "pause_resume"
        assert agent.barge_in_confirm_ms == 900

    def test_factory_defaults_match_dataclass(self) -> None:
        phone = self._phone()
        agent = phone.agent(
            system_prompt="hi",
            stt=deepgram_stt.STT(api_key="dg_test"),
            tts=elevenlabs_tts.TTS(api_key="el_test"),
        )
        assert agent.barge_in_mode == "cancel"
        assert agent.barge_in_confirm_ms == 1500

    def test_invalid_barge_in_mode_rejected(self) -> None:
        phone = self._phone()
        with pytest.raises(ValueError, match="barge_in_mode"):
            phone.agent(
                system_prompt="hi",
                stt=deepgram_stt.STT(api_key="dg_test"),
                tts=elevenlabs_tts.TTS(api_key="el_test"),
                barge_in_mode="silence",
            )


class TestProviderRealtimeEnvKey:
    """``provider="openai_realtime"`` honours ``OPENAI_API_KEY`` from the env.

    The error message has always promised "or set OPENAI_API_KEY in the
    environment" and TypeScript accepts the env var — Python must too, AND the
    key must be backfilled into the local config so the call path actually
    uses it (accepting at validation but dialing with an empty key would be a
    dead call).
    """

    def _phone(self) -> Patter:
        return Patter(
            carrier=Twilio(account_sid="AC", auth_token="tok"),
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )

    def test_env_key_accepted_and_backfilled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        phone = self._phone()
        agent = phone.agent(system_prompt="hi", provider="openai_realtime")
        assert agent.provider == "openai_realtime"
        assert phone._local_config.openai_key == "sk-env"

    def test_no_key_anywhere_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        phone = self._phone()
        with pytest.raises(ValueError, match="OpenAI API key"):
            phone.agent(system_prompt="hi", provider="openai_realtime")
