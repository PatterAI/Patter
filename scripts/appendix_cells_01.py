"""§3 Live Appendix cells — 01 Quickstart."""

from __future__ import annotations

_TWILIO_REQUIRED = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TARGET_PHONE_NUMBER"]
_OPENAI_REQUIRED = _TWILIO_REQUIRED + ["OPENAI_API_KEY"]


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
            "Places a real PSTN call. Requires `ENABLE_LIVE_CALLS=1` and carrier credentials.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            f"with _setup.cell('live_preflight', tier=4, required={_TWILIO_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        webhook = env.public_webhook_url or '(auto-tunnel via ngrok)'\n"
            "        print(f'✓ T4 pre-flight')\n"
            "        print(f'  carrier:       Twilio {env.twilio_number}')\n"
            "        print(f'  target:        {env.target_number}')\n"
            "        print(f'  webhook:       {webhook}')\n"
            "        print(f'  max_seconds:   {env.max_call_seconds}')\n"
            "        print(f'  max_cost:      ${env.max_cost_usd:.2f}')\n",
        ),
        _md("### Live outbound call *(T4 — places a real 5-second call)*\n"),
        _code(
            "live_outbound_call",
            f"import asyncio\n"
            f"from getpatter import Patter, Twilio, OpenAIRealtime\n"
            f"with _setup.cell('live_outbound_call', tier=4, required={_OPENAI_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid=env.twilio_sid, auth_token=env.twilio_token),\n"
            "            phone_number=env.twilio_number,\n"
            "            webhook_url=env.public_webhook_url,\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='Say: Hello from Patter. Then end the call immediately.',\n"
            "            engine=OpenAIRealtime(api_key=env.openai_key),\n"
            "        )\n"
            "        try:\n"
            "            await p.call(\n"
            "                env.target_number,\n"
            "                agent=agent,\n"
            "                first_message='Hello from Patter.',\n"
            "                ring_timeout=env.max_call_seconds,\n"
            "            )\n"
            "            print(f'✓ Call completed')\n"
            "        finally:\n"
            "            _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Places a real PSTN call. Requires `ENABLE_LIVE_CALLS=1` and carrier credentials.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "await cell('live_preflight', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER'], env }, () => {\n"
            "  const webhook = env.publicWebhookUrl || '(auto-tunnel via ngrok)';\n"
            "  console.log('✓ T4 pre-flight');\n"
            "  console.log(`  carrier:       Twilio ${env.twilioNumber}`);\n"
            "  console.log(`  target:        ${env.targetNumber}`);\n"
            "  console.log(`  webhook:       ${webhook}`);\n"
            "  console.log(`  max_seconds:   ${env.maxCallSeconds}`);\n"
            "  console.log(`  max_cost:      $${env.maxCostUsd.toFixed(2)}`);\n"
            "});\n",
        ),
        _md("### Live outbound call *(T4 — places a real 5-second call)*\n"),
        _code(
            "live_outbound_call",
            'import { Patter, Twilio, OpenAIRealtime } from "getpatter";\n'
            "await cell('live_outbound_call', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, async () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: env.twilioSid, authToken: env.twilioToken }),\n"
            "    phoneNumber: env.twilioNumber,\n"
            "    webhookUrl: env.publicWebhookUrl,\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'Say: Hello from Patter. Then end the call immediately.',\n"
            "    engine: new OpenAIRealtime({ apiKey: env.openaiKey }),\n"
            "  });\n"
            "  try {\n"
            "    await p.call(env.targetNumber, { agent, firstMessage: 'Hello from Patter.', ringTimeout: env.maxCallSeconds });\n"
            "    console.log('✓ Call completed');\n"
            "  } finally {\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
