"""§3 Live Appendix cells — 09 Guardrails & Hooks."""

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
            "Places a call with an active guardrail so a blocked phrase triggers a redirect. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "with _setup.cell('live_preflight', tier=4, required=['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env=env) as ok:\n"
            "    if ok:\n"
            "        print(f'  carrier:  Twilio {env.twilio_number}  →  {env.target_number}')\n"
            "        print(f'  guardrail: blocks any mention of \"competitor\"')\n"
            "        print(f'  webhook:  {env.public_webhook_url or \"(ngrok auto-launch)\"}')\n",
        ),
        _md("### Live call with guardrail *(T4)*\n"),
        _code(
            "live_guardrail_call",
            f"from getpatter import Patter, Twilio, OpenAIRealtime, guardrail\n"
            f"with _setup.cell('live_guardrail_call', tier=4, required={_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        @guardrail\n"
            "        def no_competitor(text: str) -> str | None:\n"
            '            """Block competitor mentions."""\n'
            "            if 'competitor' in text.lower():\n"
            "                return 'I cannot discuss other companies. How can I help you today?'\n"
            "            return None\n"
            "\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid=env.twilio_sid, auth_token=env.twilio_token),\n"
            "            phone_number=env.twilio_number,\n"
            "            webhook_url=env.public_webhook_url,\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='You are a helpful demo agent.',\n"
            "            engine=OpenAIRealtime(api_key=env.openai_key),\n"
            "            guardrails=[no_competitor],\n"
            "        )\n"
            "        try:\n"
            "            await p.call(env.target_number, agent=agent,\n"
            "                         first_message='Hello! Try mentioning a competitor to see the guardrail.',\n"
            "                         ring_timeout=env.max_call_seconds)\n"
            "            print('✓ Guardrail call completed')\n"
            "        finally:\n"
            "            _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Places a call with an active guardrail so a blocked phrase triggers a redirect. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "await cell('live_preflight', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, () => {\n"
            "  console.log(`  carrier:  Twilio ${env.twilioNumber}  →  ${env.targetNumber}`);\n"
            "  console.log('  guardrail: blocks any mention of \"competitor\"');\n"
            "  console.log(`  webhook:  ${env.publicWebhookUrl || '(ngrok auto-launch)'}`);\n"
            "});\n",
        ),
        _md("### Live call with guardrail *(T4)*\n"),
        _code(
            "live_guardrail_call",
            'import { Patter, Twilio, OpenAIRealtime, guardrail } from "getpatter";\n'
            "await cell('live_guardrail_call', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, async () => {\n"
            "  const noCompetitor = guardrail({\n"
            "    name: 'no_competitor',\n"
            "    handler: (text: string) =>\n"
            "      text.toLowerCase().includes('competitor') ? 'I cannot discuss other companies.' : null,\n"
            "  });\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: env.twilioSid, authToken: env.twilioToken }),\n"
            "    phoneNumber: env.twilioNumber,\n"
            "    webhookUrl: env.publicWebhookUrl,\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'Demo agent.',\n"
            "    engine: new OpenAIRealtime({ apiKey: env.openaiKey }),\n"
            "    guardrails: [noCompetitor],\n"
            "  });\n"
            "  try {\n"
            "    await p.call(env.targetNumber, { agent, firstMessage: 'Hello! Mention a competitor to test the guardrail.', ringTimeout: env.maxCallSeconds });\n"
            "    console.log('✓ Guardrail call completed');\n"
            "  } finally {\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
