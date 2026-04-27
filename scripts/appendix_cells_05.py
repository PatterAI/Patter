"""§3 Live Appendix cells — 05 Pipeline LLM."""

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
            "Places a call using the Pipeline engine with OpenAI LLM + tool call. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "with _setup.cell('live_preflight', tier=4, required=['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env=env) as ok:\n"
            "    if ok:\n"
            "        print(f'  carrier:  Twilio {env.twilio_number}  →  {env.target_number}')\n"
            "        print(f'  LLM:      OpenAI  (gpt-4o-mini)')\n"
            "        print(f'  webhook:  {env.public_webhook_url or \"(ngrok auto-launch)\"}')\n",
        ),
        _md("### Live LLM call with tool *(T4)*\n"),
        _code(
            "live_llm_call",
            f"from getpatter import Patter, Twilio, OpenAIRealtime, tool\n"
            f"with _setup.cell('live_llm_call', tier=4, required={_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        @tool\n"
            "        def get_time() -> str:\n"
            '            """Return the current UTC time."""\n'
            "            import datetime\n"
            "            return datetime.datetime.utcnow().strftime('%H:%M UTC')\n"
            "\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid=env.twilio_sid, auth_token=env.twilio_token),\n"
            "            phone_number=env.twilio_number,\n"
            "            webhook_url=env.public_webhook_url,\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='You are a helpful demo assistant. If asked for the time, use your tool.',\n"
            "            engine=OpenAIRealtime(api_key=env.openai_key),\n"
            "            tools=[get_time],\n"
            "        )\n"
            "        try:\n"
            "            await p.call(env.target_number, agent=agent,\n"
            "                         first_message='Hello! Ask me what time it is.',\n"
            "                         ring_timeout=env.max_call_seconds)\n"
            "            print('✓ LLM call with tool completed')\n"
            "        finally:\n"
            "            _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Places a call using the Pipeline engine with OpenAI LLM + tool call. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "await cell('live_preflight', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, () => {\n"
            "  console.log(`  carrier:  Twilio ${env.twilioNumber}  →  ${env.targetNumber}`);\n"
            "  console.log('  LLM:      OpenAI  (gpt-4o-mini)');\n"
            "  console.log(`  webhook:  ${env.publicWebhookUrl || '(ngrok auto-launch)'}`);\n"
            "});\n",
        ),
        _md("### Live LLM call with tool *(T4)*\n"),
        _code(
            "live_llm_call",
            'import { Patter, Twilio, OpenAIRealtime, tool } from "getpatter";\n'
            "await cell('live_llm_call', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, async () => {\n"
            "  const getTime = tool({\n"
            "    name: 'get_time',\n"
            "    description: 'Return the current UTC time.',\n"
            "    parameters: {},\n"
            "    handler: () => new Date().toUTCString(),\n"
            "  });\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: env.twilioSid, authToken: env.twilioToken }),\n"
            "    phoneNumber: env.twilioNumber,\n"
            "    webhookUrl: env.publicWebhookUrl,\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'Demo assistant. Use the get_time tool if asked.',\n"
            "    engine: new OpenAIRealtime({ apiKey: env.openaiKey }),\n"
            "    tools: [getTime],\n"
            "  });\n"
            "  try {\n"
            "    await p.call(env.targetNumber, { agent, firstMessage: 'Hello! Ask me the time.', ringTimeout: env.maxCallSeconds });\n"
            "    console.log('✓ LLM call with tool completed');\n"
            "  } finally {\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
