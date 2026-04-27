"""§3 Live Appendix cells — 06 Telephony Twilio."""

from __future__ import annotations

_REQUIRED = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TARGET_PHONE_NUMBER", "OPENAI_API_KEY"]


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
            "Tests Twilio call flow including AMD detection. Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "with _setup.cell('live_preflight', tier=4, required=['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER'], env=env) as ok:\n"
            "    if ok:\n"
            "        print(f'  carrier:  Twilio {env.twilio_number}  →  {env.target_number}')\n"
            "        print(f'  webhook:  {env.public_webhook_url or \"(ngrok auto-launch)\"}')\n"
            "        print(f'  features: AMD + voicemail fallback')\n",
        ),
        _md("### Live Twilio call with AMD *(T4)*\n"),
        _code(
            "live_twilio_amd",
            f"from getpatter import Patter, Twilio, OpenAIRealtime\n"
            f"with _setup.cell('live_twilio_amd', tier=4, required={_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid=env.twilio_sid, auth_token=env.twilio_token),\n"
            "            phone_number=env.twilio_number,\n"
            "            webhook_url=env.public_webhook_url,\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='You are a Twilio telephony demo agent. Greet the caller and hang up.',\n"
            "            engine=OpenAIRealtime(api_key=env.openai_key),\n"
            "        )\n"
            "        try:\n"
            "            await p.call(\n"
            "                env.target_number,\n"
            "                agent=agent,\n"
            "                machine_detection=True,\n"
            "                voicemail_message='You reached Patter demo. Goodbye.',\n"
            "                first_message='Hello from Patter Twilio demo.',\n"
            "                ring_timeout=env.max_call_seconds,\n"
            "            )\n"
            "            print('✓ Twilio AMD call completed')\n"
            "        finally:\n"
            "            _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Tests Twilio call flow including AMD detection. Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "await cell('live_preflight', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER'], env }, () => {\n"
            "  console.log(`  carrier:  Twilio ${env.twilioNumber}  →  ${env.targetNumber}`);\n"
            "  console.log(`  webhook:  ${env.publicWebhookUrl || '(ngrok auto-launch)'}`);\n"
            "  console.log('  features: AMD + voicemail fallback');\n"
            "});\n",
        ),
        _md("### Live Twilio call with AMD *(T4)*\n"),
        _code(
            "live_twilio_amd",
            'import { Patter, Twilio, OpenAIRealtime } from "getpatter";\n'
            "await cell('live_twilio_amd', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, async () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: env.twilioSid, authToken: env.twilioToken }),\n"
            "    phoneNumber: env.twilioNumber,\n"
            "    webhookUrl: env.publicWebhookUrl,\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'Twilio demo agent. Greet and hang up.',\n"
            "    engine: new OpenAIRealtime({ apiKey: env.openaiKey }),\n"
            "  });\n"
            "  try {\n"
            "    await p.call(env.targetNumber, {\n"
            "      agent, machineDetection: true,\n"
            "      voicemailMessage: 'Patter demo voicemail.',\n"
            "      firstMessage: 'Hello from Patter Twilio.',\n"
            "      ringTimeout: env.maxCallSeconds,\n"
            "    });\n"
            "    console.log('✓ Twilio AMD call completed');\n"
            "  } finally {\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
