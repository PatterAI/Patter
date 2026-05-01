"""§3 Live Appendix cells — 03 Pipeline STT."""

from __future__ import annotations

_REQUIRED = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TARGET_PHONE_NUMBER", "DEEPGRAM_API_KEY", "OPENAI_API_KEY"]


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
            "Calls a real number through the Pipeline engine using Deepgram STT. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "with _setup.cell('live_preflight', tier=4, required=['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'DEEPGRAM_API_KEY'], env=env) as ok:\n"
            "    if ok:\n"
            "        print(f'  carrier:  Twilio {env.twilio_number}  →  {env.target_number}')\n"
            "        print(f'  STT:      Deepgram  (nova-2-general)')\n"
            "        print(f'  webhook:  {env.public_webhook_url or \"(ngrok auto-launch)\"}')\n",
        ),
        _md("### Live Pipeline STT call *(T4)*\n"),
        _code(
            "live_stt_call",
            f"from getpatter import Patter, Twilio, DeepgramSTT, OpenAILLM, OpenAITTS\n"
            f"with _setup.cell('live_stt_call', tier=4, required={_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid=env.twilio_sid, auth_token=env.twilio_token),\n"
            "            phone_number=env.twilio_number,\n"
            "            webhook_url=env.public_webhook_url,\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='Greet the caller and say goodbye.',\n"
            "            stt=DeepgramSTT(api_key=env.deepgram_key),\n"
            "            llm=OpenAILLM(api_key=env.openai_key, model='gpt-4o-mini'),\n"
            "            tts=OpenAITTS(api_key=env.openai_key, voice='alloy'),\n"
            "        )\n"
            "        try:\n"
            "            await p.call(env.target_number, agent=agent, first_message='Hello from Patter STT demo.',\n"
            "                         ring_timeout=env.max_call_seconds)\n"
            "            print('✓ Pipeline STT call completed')\n"
            "        finally:\n"
            "            _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Calls a real number through the Pipeline engine using Deepgram STT. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "await cell('live_preflight', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'DEEPGRAM_API_KEY'], env }, () => {\n"
            "  console.log(`  carrier:  Twilio ${env.twilioNumber}  →  ${env.targetNumber}`);\n"
            "  console.log('  STT:      Deepgram  (nova-2-general)');\n"
            "  console.log(`  webhook:  ${env.publicWebhookUrl || '(ngrok auto-launch)'}`);\n"
            "});\n",
        ),
        _md("### Live Pipeline STT call *(T4)*\n"),
        _code(
            "live_stt_call",
            'import { Patter, Twilio, DeepgramSTT, OpenAILLM, OpenAITTS } from "getpatter";\n'
            "await cell('live_stt_call', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'DEEPGRAM_API_KEY', 'OPENAI_API_KEY'], env }, async () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: env.twilioSid, authToken: env.twilioToken }),\n"
            "    phoneNumber: env.twilioNumber,\n"
            "    webhookUrl: env.publicWebhookUrl,\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'Greet the caller and say goodbye.',\n"
            "    stt: new DeepgramSTT({ apiKey: env.deepgramKey }),\n"
            "    llm: new OpenAILLM({ apiKey: env.openaiKey, model: 'gpt-4o-mini' }),\n"
            "    tts: new OpenAITTS({ apiKey: env.openaiKey, voice: 'alloy' }),\n"
            "  });\n"
            "  try {\n"
            "    await p.call(env.targetNumber, { agent, firstMessage: 'Hello from Patter STT demo.', ringTimeout: env.maxCallSeconds });\n"
            "    console.log('✓ Pipeline STT call completed');\n"
            "  } finally {\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
