"""§3 Live Appendix cells — 11 Metrics & Dashboard."""

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
            "Places a real call and inspects the `MetricsStore` after it ends. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "with _setup.cell('live_preflight', tier=4, required=['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env=env) as ok:\n"
            "    if ok:\n"
            "        print(f'  carrier:  Twilio {env.twilio_number}  →  {env.target_number}')\n"
            "        print(f'  metrics:  will inspect MetricsStore after call ends')\n"
            "        print(f'  webhook:  {env.public_webhook_url or \"(ngrok auto-launch)\"}')\n",
        ),
        _md("### Live call + metrics inspection *(T4)*\n"),
        _code(
            "live_metrics_call",
            f"from getpatter import Patter, Twilio, OpenAIRealtime, MetricsStore\n"
            f"with _setup.cell('live_metrics_call', tier=4, required={_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        store = MetricsStore(max_calls=50)\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid=env.twilio_sid, auth_token=env.twilio_token),\n"
            "            phone_number=env.twilio_number,\n"
            "            webhook_url=env.public_webhook_url,\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='You are a metrics demo agent. Greet and hang up.',\n"
            "            engine=OpenAIRealtime(api_key=env.openai_key),\n"
            "        )\n"
            "        try:\n"
            "            await p.call(env.target_number, agent=agent,\n"
            "                         first_message='Hello from Patter metrics demo.',\n"
            "                         ring_timeout=env.max_call_seconds)\n"
            "            agg = store.get_aggregates()\n"
            "            print(f'Calls in store:   {agg[\"total_calls\"]}')\n"
            "            print(f'Avg duration:     {agg[\"avg_duration\"]}s')\n"
            "            print(f'Total cost:       ${agg[\"total_cost\"]:.4f}')\n"
            "            print('✓ Metrics call completed')\n"
            "        finally:\n"
            "            _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Places a real call and inspects the `MetricsStore` after it ends. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "await cell('live_preflight', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, () => {\n"
            "  console.log(`  carrier:  Twilio ${env.twilioNumber}  →  ${env.targetNumber}`);\n"
            "  console.log('  metrics:  will inspect MetricsStore after call ends');\n"
            "  console.log(`  webhook:  ${env.publicWebhookUrl || '(ngrok auto-launch)'}`);\n"
            "});\n",
        ),
        _md("### Live call + metrics inspection *(T4)*\n"),
        _code(
            "live_metrics_call",
            'import { Patter, Twilio, OpenAIRealtime, MetricsStore } from "getpatter";\n'
            "await cell('live_metrics_call', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, async () => {\n"
            "  const store = new MetricsStore({ maxCalls: 50 });\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: env.twilioSid, authToken: env.twilioToken }),\n"
            "    phoneNumber: env.twilioNumber,\n"
            "    webhookUrl: env.publicWebhookUrl,\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'Metrics demo. Greet and hang up.',\n"
            "    engine: new OpenAIRealtime({ apiKey: env.openaiKey }),\n"
            "  });\n"
            "  try {\n"
            "    await p.call(env.targetNumber, { agent, firstMessage: 'Metrics demo.', ringTimeout: env.maxCallSeconds });\n"
            "    const agg = store.getAggregates();\n"
            "    console.log(`Calls in store: ${agg.totalCalls}`);\n"
            "    console.log(`Avg duration:   ${agg.avgDuration}s`);\n"
            "    console.log('✓ Metrics call completed');\n"
            "  } finally {\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
