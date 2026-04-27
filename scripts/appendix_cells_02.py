"""§3 Live Appendix cells — 02 OpenAI Realtime."""

from __future__ import annotations

_TWILIO_REQUIRED = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TARGET_PHONE_NUMBER"]
_REALTIME_REQUIRED = _TWILIO_REQUIRED + ["OPENAI_API_KEY"]


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
            "Places a real call through OpenAI Realtime. Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            f"with _setup.cell('live_preflight', tier=4, required={_TWILIO_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        print(f'  carrier:  Twilio {env.twilio_number}  →  {env.target_number}')\n"
            "        print(f'  webhook:  {env.public_webhook_url or \"(ngrok auto-launch)\"}')\n"
            "        print(f'  engine:   OpenAI Realtime  (gpt-4o-realtime-preview)')\n",
        ),
        _md("### Live OpenAI Realtime call *(T4)*\n"),
        _code(
            "live_realtime_call",
            f"from getpatter import Patter, Twilio, OpenAIRealtime\n"
            f"with _setup.cell('live_realtime_call', tier=4, required={_REALTIME_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid=env.twilio_sid, auth_token=env.twilio_token),\n"
            "            phone_number=env.twilio_number,\n"
            "            webhook_url=env.public_webhook_url,\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='You are a demo assistant. Greet the caller and immediately say goodbye.',\n"
            "            engine=OpenAIRealtime(api_key=env.openai_key, model='gpt-4o-realtime-preview'),\n"
            "        )\n"
            "        try:\n"
            "            await p.call(\n"
            "                env.target_number,\n"
            "                agent=agent,\n"
            "                first_message='Hello! This is a Patter demo call. Goodbye!',\n"
            "                ring_timeout=env.max_call_seconds,\n"
            "            )\n"
            "            print('✓ Realtime call completed')\n"
            "        finally:\n"
            "            _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Places a real call through OpenAI Realtime. Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "await cell('live_preflight', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER'], env }, () => {\n"
            "  console.log(`  carrier:  Twilio ${env.twilioNumber}  →  ${env.targetNumber}`);\n"
            "  console.log(`  webhook:  ${env.publicWebhookUrl || '(ngrok auto-launch)'}`);\n"
            "  console.log('  engine:   OpenAI Realtime  (gpt-4o-realtime-preview)');\n"
            "});\n",
        ),
        _md("### Live OpenAI Realtime call *(T4)*\n"),
        _code(
            "live_realtime_call",
            'import { Patter, Twilio, OpenAIRealtime } from "getpatter";\n'
            "await cell('live_realtime_call', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, async () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: env.twilioSid, authToken: env.twilioToken }),\n"
            "    phoneNumber: env.twilioNumber,\n"
            "    webhookUrl: env.publicWebhookUrl,\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'Greet the caller and immediately say goodbye.',\n"
            "    engine: new OpenAIRealtime({ apiKey: env.openaiKey, model: 'gpt-4o-realtime-preview' }),\n"
            "  });\n"
            "  try {\n"
            "    await p.call(env.targetNumber, { agent, firstMessage: 'Hello! Patter demo. Goodbye!', ringTimeout: env.maxCallSeconds });\n"
            "    console.log('✓ Realtime call completed');\n"
            "  } finally {\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
