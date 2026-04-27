"""§3 Live Appendix cells — 04 Pipeline TTS."""

from __future__ import annotations

_REQUIRED = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TARGET_PHONE_NUMBER", "ELEVENLABS_API_KEY", "OPENAI_API_KEY"]


def _md(*lines: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}


def _code(tag: str, source: str) -> dict:
    return {
        "cell_type": "code",
        "metadata": {"tags": [tag]},
        "source": source.splitlines(keepends=True),
        "execution_count": None,
        "outputs": [],
    }


def section_cells_python() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Calls a real number using ElevenLabs TTS in the Pipeline engine. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "with _setup.cell('live_preflight', tier=4, required=['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'ELEVENLABS_API_KEY'], env=env) as ok:\n"
            "    if ok:\n"
            "        print(f'  carrier:  Twilio {env.twilio_number}  →  {env.target_number}')\n"
            "        print(f'  TTS:      ElevenLabs  voice={env.elevenlabs_voice_id[:8]}...')\n"
            "        print(f'  webhook:  {env.public_webhook_url or \"(ngrok auto-launch)\"}')\n",
        ),
        _md("### Live ElevenLabs TTS call *(T4)*\n"),
        _code(
            "live_tts_call",
            f"from getpatter import Patter, Twilio, DeepgramSTT, OpenAILLM, ElevenLabsTTS\n"
            f"with _setup.cell('live_tts_call', tier=4, required={_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid=env.twilio_sid, auth_token=env.twilio_token),\n"
            "            phone_number=env.twilio_number,\n"
            "            webhook_url=env.public_webhook_url,\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='Greet the caller with a short friendly message and end the call.',\n"
            "            stt=DeepgramSTT(api_key=env.deepgram_key) if env.deepgram_key else None,\n"
            "            llm=OpenAILLM(api_key=env.openai_key, model='gpt-4o-mini'),\n"
            "            tts=ElevenLabsTTS(api_key=env.elevenlabs_key, voice_id=env.elevenlabs_voice_id),\n"
            "        )\n"
            "        try:\n"
            "            await p.call(env.target_number, agent=agent,\n"
            "                         first_message='Hello, this is a Patter ElevenLabs TTS demo.',\n"
            "                         ring_timeout=env.max_call_seconds)\n"
            "            print('✓ ElevenLabs TTS call completed')\n"
            "        finally:\n"
            "            _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Calls a real number using ElevenLabs TTS in the Pipeline engine. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "await cell('live_preflight', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'ELEVENLABS_API_KEY'], env }, () => {\n"
            "  console.log(`  carrier:  Twilio ${env.twilioNumber}  →  ${env.targetNumber}`);\n"
            "  console.log(`  TTS:      ElevenLabs  voice=${env.elevenLabsVoiceId.slice(0, 8)}...`);\n"
            "  console.log(`  webhook:  ${env.publicWebhookUrl || '(ngrok auto-launch)'}`);\n"
            "});\n",
        ),
        _md("### Live ElevenLabs TTS call *(T4)*\n"),
        _code(
            "live_tts_call",
            'import { Patter, Twilio, OpenAILLM, ElevenLabsTTS } from "getpatter";\n'
            "await cell('live_tts_call', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'ELEVENLABS_API_KEY', 'OPENAI_API_KEY'], env }, async () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: env.twilioSid, authToken: env.twilioToken }),\n"
            "    phoneNumber: env.twilioNumber,\n"
            "    webhookUrl: env.publicWebhookUrl,\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'Greet and end the call.',\n"
            "    llm: new OpenAILLM({ apiKey: env.openaiKey, model: 'gpt-4o-mini' }),\n"
            "    tts: new ElevenLabsTTS({ apiKey: env.elevenLabsKey, voiceId: env.elevenLabsVoiceId }),\n"
            "  });\n"
            "  try {\n"
            "    await p.call(env.targetNumber, { agent, firstMessage: 'Hello, ElevenLabs TTS demo.', ringTimeout: env.maxCallSeconds });\n"
            "    console.log('✓ ElevenLabs TTS call completed');\n"
            "  } finally {\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
