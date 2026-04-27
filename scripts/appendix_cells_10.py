"""§3 Live Appendix cells — 10 Advanced."""

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
            "Places a scheduled outbound call using `schedule_once`. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "with _setup.cell('live_preflight', tier=4, required=['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env=env) as ok:\n"
            "    if ok:\n"
            "        print(f'  carrier:  Twilio {env.twilio_number}  →  {env.target_number}')\n"
            "        print(f'  feature:  schedule_once — fires a call 5 seconds from now')\n"
            "        print(f'  webhook:  {env.public_webhook_url or \"(ngrok auto-launch)\"}')\n",
        ),
        _md("### Live scheduled call *(T4)*\n"),
        _code(
            "live_scheduled_call",
            f"import asyncio\n"
            f"from datetime import datetime, timedelta, timezone\n"
            f"from getpatter import Patter, Twilio, OpenAIRealtime, schedule_once\n"
            f"with _setup.cell('live_scheduled_call', tier=4, required={_REQUIRED!r}, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid=env.twilio_sid, auth_token=env.twilio_token),\n"
            "            phone_number=env.twilio_number,\n"
            "            webhook_url=env.public_webhook_url,\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='You are a scheduled-call demo. Greet and hang up.',\n"
            "            engine=OpenAIRealtime(api_key=env.openai_key),\n"
            "        )\n"
            "        fired = []\n"
            "\n"
            "        async def place_call():\n"
            "            fired.append(True)\n"
            "            await p.call(env.target_number, agent=agent,\n"
            "                         first_message='Hello, this is your scheduled Patter call.',\n"
            "                         ring_timeout=env.max_call_seconds)\n"
            "\n"
            "        when = datetime.now(tz=timezone.utc) + timedelta(seconds=5)\n"
            "        handle = schedule_once(when, lambda: asyncio.ensure_future(place_call()))\n"
            "        print(f'Scheduled call at {when.strftime(\"%H:%M:%S UTC\")}  job_id={handle.job_id[:20]}...')\n"
            "        try:\n"
            "            await asyncio.sleep(8)  # wait for the scheduler to fire\n"
            "            print(f'Fired: {len(fired)} call(s)')\n"
            "        finally:\n"
            "            handle.cancel()\n"
            "            _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §3 — Live Appendix\n\n"
            "Places a scheduled outbound call using `scheduleOnce`. "
            "Requires `ENABLE_LIVE_CALLS=1`.\n"
        ),
        _md("### Pre-flight checklist\n"),
        _code(
            "live_preflight",
            "await cell('live_preflight', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, () => {\n"
            "  console.log(`  carrier:  Twilio ${env.twilioNumber}  →  ${env.targetNumber}`);\n"
            "  console.log('  feature:  scheduleOnce — fires a call 5 seconds from now');\n"
            "  console.log(`  webhook:  ${env.publicWebhookUrl || '(ngrok auto-launch)'}`);\n"
            "});\n",
        ),
        _md("### Live scheduled call *(T4)*\n"),
        _code(
            "live_scheduled_call",
            'import { Patter, Twilio, OpenAIRealtime, scheduleOnce } from "getpatter";\n'
            "await cell('live_scheduled_call', { tier: 4, required: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TARGET_PHONE_NUMBER', 'OPENAI_API_KEY'], env }, async () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: env.twilioSid, authToken: env.twilioToken }),\n"
            "    phoneNumber: env.twilioNumber,\n"
            "    webhookUrl: env.publicWebhookUrl,\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'Scheduled demo. Greet and hang up.',\n"
            "    engine: new OpenAIRealtime({ apiKey: env.openaiKey }),\n"
            "  });\n"
            "  let fired = 0;\n"
            "  const when = new Date(Date.now() + 5_000);\n"
            "  const handle = scheduleOnce(when, async () => {\n"
            "    fired++;\n"
            "    await p.call(env.targetNumber, { agent, firstMessage: 'Your scheduled Patter call.', ringTimeout: env.maxCallSeconds });\n"
            "  });\n"
            "  console.log(`Scheduled at ${when.toISOString()}  jobId=${handle.jobId.slice(0, 20)}...`);\n"
            "  try {\n"
            "    await new Promise(r => setTimeout(r, 8_000));\n"
            "    console.log(`Fired: ${fired} call(s)`);\n"
            "  } finally {\n"
            "    handle.cancel();\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
