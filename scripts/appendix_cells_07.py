"""§3 Live Appendix cells — 07 Telephony Telnyx."""

from __future__ import annotations

_REQUIRED = ["TELNYX_API_KEY", "TELNYX_CONNECTION_ID", "TELNYX_PHONE_NUMBER", "TARGET_PHONE_NUMBER", "OPENAI_API_KEY"]


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
            "Places a real call through the Telnyx carrier. "
            "Requires `ENABLE_LIVE_CALLS=1` and Telnyx credentials.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "with _setup.cell('live_preflight', tier=4, required=['TELNYX_API_KEY', 'TELNYX_CONNECTION_ID', 'TELNYX_PHONE_NUMBER', 'TARGET_PHONE_NUMBER'], env=env) as ok:\n"
            "    if ok:\n"
            "        print(f'  carrier:       Telnyx {env.telnyx_number}  →  {env.target_number}')\n"
            "        print(f'  connection_id: {env.telnyx_connection_id}')\n"
            "        print(f'  webhook:       {env.public_webhook_url or \"(ngrok auto-launch)\"}')\n",
        ),
        _md("### Live Telnyx outbound call *(T4)*\n"),
        _code(
            "live_telnyx_call",
            f"from getpatter import Patter, Telnyx, OpenAIRealtime\n"
            f"with _setup.cell('live_telnyx_call', tier=4, required={_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(\n"
            "            carrier=Telnyx(api_key=env.telnyx_key, public_key=env.telnyx_public_key),\n"
            "            phone_number=env.telnyx_number,\n"
            "            webhook_url=env.public_webhook_url,\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='You are a Telnyx demo agent. Greet the caller and hang up.',\n"
            "            engine=OpenAIRealtime(api_key=env.openai_key),\n"
            "        )\n"
            "        try:\n"
            "            await p.call(\n"
            "                env.target_number,\n"
            "                agent=agent,\n"
            "                first_message='Hello from Patter via Telnyx.',\n"
            "                ring_timeout=env.max_call_seconds,\n"
            "            )\n"
            "            print('✓ Telnyx call completed')\n"
            "        finally:\n"
            "            _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Places a real call through the Telnyx carrier. "
            "Requires `ENABLE_LIVE_CALLS=1` and Telnyx credentials.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "await cell('live_preflight', { tier: 4, required: ['TELNYX_API_KEY', 'TELNYX_CONNECTION_ID', 'TELNYX_PHONE_NUMBER', 'TARGET_PHONE_NUMBER'], env }, () => {\n"
            "  console.log(`  carrier:       Telnyx ${env.telnyxNumber}  →  ${env.targetNumber}`);\n"
            "  console.log(`  connection_id: ${env.telnyxConnectionId}`);\n"
            "  console.log(`  webhook:       ${env.publicWebhookUrl || '(ngrok auto-launch)'}`);\n"
            "});\n",
        ),
        _md("### Live Telnyx outbound call *(T4)*\n"),
        _code(
            "live_telnyx_call",
            'import { Patter, Telnyx, OpenAIRealtime } from "getpatter";\n'
            "await cell('live_telnyx_call', { tier: 4, required: ['TELNYX_API_KEY', 'TELNYX_CONNECTION_ID', 'TELNYX_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, async () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Telnyx({ apiKey: env.telnyxKey, publicKey: env.telnyxPublicKey }),\n"
            "    phoneNumber: env.telnyxNumber,\n"
            "    webhookUrl: env.publicWebhookUrl,\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'Telnyx demo agent. Greet and hang up.',\n"
            "    engine: new OpenAIRealtime({ apiKey: env.openaiKey }),\n"
            "  });\n"
            "  try {\n"
            "    await p.call(env.targetNumber, { agent, firstMessage: 'Hello from Patter via Telnyx.', ringTimeout: env.maxCallSeconds });\n"
            "    console.log('✓ Telnyx call completed');\n"
            "  } finally {\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
