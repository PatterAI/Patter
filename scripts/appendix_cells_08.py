"""§3 Live Appendix cells — 08 Tools."""

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
            "Fires a real tool call mid-call and demonstrates `transfer_call`. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "with _setup.cell('live_preflight', tier=4, required=['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env=env) as ok:\n"
            "    if ok:\n"
            "        print(f'  carrier:  Twilio {env.twilio_number}  →  {env.target_number}')\n"
            "        print(f'  tools:    get_time (custom) + transfer_call (auto-injected)')\n"
            "        print(f'  webhook:  {env.public_webhook_url or \"(ngrok auto-launch)\"}')\n",
        ),
        _md("### Live call with custom tool *(T4)*\n"),
        _code(
            "live_tools_call",
            f"from getpatter import Patter, Twilio, OpenAIRealtime, tool\n"
            f"with _setup.cell('live_tools_call', tier=4, required={_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        @tool\n"
            "        def lookup_order(order_id: str) -> str:\n"
            '            """Look up the status of an order by ID."""\n'
            "            return f'Order {order_id} is shipped — expected delivery: tomorrow.'\n"
            "\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid=env.twilio_sid, auth_token=env.twilio_token),\n"
            "            phone_number=env.twilio_number,\n"
            "            webhook_url=env.public_webhook_url,\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='You are a demo order assistant. If asked, look up order 12345.',\n"
            "            engine=OpenAIRealtime(api_key=env.openai_key),\n"
            "            tools=[lookup_order],\n"
            "        )\n"
            "        try:\n"
            "            await p.call(env.target_number, agent=agent,\n"
            "                         first_message='Hi! Ask me about order 12345.',\n"
            "                         ring_timeout=env.max_call_seconds)\n"
            "            print('✓ Tools call completed')\n"
            "        finally:\n"
            "            _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Fires a real tool call mid-call and demonstrates `transfer_call`. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "await cell('live_preflight', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, () => {\n"
            "  console.log(`  carrier:  Twilio ${env.twilioNumber}  →  ${env.targetNumber}`);\n"
            "  console.log('  tools:    lookup_order (custom) + transfer_call (auto-injected)');\n"
            "  console.log(`  webhook:  ${env.publicWebhookUrl || '(ngrok auto-launch)'}`);\n"
            "});\n",
        ),
        _md("### Live call with custom tool *(T4)*\n"),
        _code(
            "live_tools_call",
            'import { Patter, Twilio, OpenAIRealtime, tool } from "getpatter";\n'
            "await cell('live_tools_call', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, async () => {\n"
            "  const lookupOrder = tool({\n"
            "    name: 'lookup_order',\n"
            "    description: 'Look up order status.',\n"
            "    parameters: { orderId: { type: 'string' } },\n"
            "    handler: ({ orderId }: { orderId: string }) => `Order ${orderId}: shipped.`,\n"
            "  });\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: env.twilioSid, authToken: env.twilioToken }),\n"
            "    phoneNumber: env.twilioNumber,\n"
            "    webhookUrl: env.publicWebhookUrl,\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'Demo order assistant. Look up order 12345 if asked.',\n"
            "    engine: new OpenAIRealtime({ apiKey: env.openaiKey }),\n"
            "    tools: [lookupOrder],\n"
            "  });\n"
            "  try {\n"
            "    await p.call(env.targetNumber, { agent, firstMessage: 'Hi! Ask about order 12345.', ringTimeout: env.maxCallSeconds });\n"
            "    console.log('✓ Tools call completed');\n"
            "  } finally {\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
