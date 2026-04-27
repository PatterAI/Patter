"""Canonical §1 (Quickstart, T1+T2) cell sequence shared by every notebook.

All cells are tier=1 (pure offline, no network). They exercise:
1. SDK version + import sanity
2. Local-mode Patter construction (Twilio carrier)
3. Cloud-mode Patter construction (api_key)
4. Agent engine dispatch (Realtime / ConvAI / Pipeline)
"""

from __future__ import annotations


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


def quickstart_cells_python() -> list[dict]:
    return [
        _md(
            "These cells run with **zero API keys** in <30 seconds. "
            "They exercise the public Patter API offline (no network, no carrier calls).\n"
        ),
        _code(
            "qs_version_check",
            "import sys\n"
            "import getpatter\n"
            "with _setup.cell('version_check', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        print(f'getpatter {getpatter.__version__} on Python {sys.version.split()[0]}')\n"
            "        assert getpatter.__version__ >= env.patter_version, \\\n"
            "            f'installed {getpatter.__version__} < target {env.patter_version}'\n",
        ),
        _md(
            "### Local mode\n"
            "Construct a Patter instance with a Twilio carrier. No API key — runs entirely on your machine.\n"
        ),
        _code(
            "qs_local_mode",
            "from getpatter import Patter, Twilio\n"
            "with _setup.cell('local_mode', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(\n"
            "            carrier=Twilio(\n"
            "                account_sid='ACtest00000000000000000000000000',\n"
            "                auth_token='test',\n"
            "            ),\n"
            "            phone_number='+15555550100',\n"
            "            webhook_url='https://example.com/webhook',\n"
            "        )\n"
            "        # _mode is the documented internal field; auto-detected from carrier+api_key.\n"
            "        assert p._mode == 'local', f'expected local, got {p._mode}'\n"
            "        print(f'mode = {p._mode}')\n",
        ),
        _md(
            "### Cloud mode\n"
            "Same SDK, just an `api_key=` instead of a carrier — Patter cloud handles telephony.\n"
        ),
        _code(
            "qs_cloud_mode",
            "from getpatter import Patter\n"
            "with _setup.cell('cloud_mode', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(api_key='pt_test_xxx')\n"
            "        assert p._mode == 'cloud', f'expected cloud, got {p._mode}'\n"
            "        assert p.api_key == 'pt_test_xxx'\n"
            "        print(f'mode = {p._mode}; api_key = {p.api_key[:8]}...')\n",
        ),
        _md(
            "### Three engine types\n"
            "An agent picks one of *OpenAI Realtime*, *ElevenLabs ConvAI*, or *Pipeline* "
            "(STT + LLM + TTS). The factory derives the mode from `engine=` / `stt=`/`tts=`.\n"
        ),
        _code(
            "qs_agent_engines",
            "from getpatter import Patter, Twilio, OpenAIRealtime, ElevenLabsConvAI\n"
            "with _setup.cell('agent_engines', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid='ACtest00000000000000000000000000',\n"
            "                           auth_token='test'),\n"
            "            phone_number='+15555550100',\n"
            "            webhook_url='https://example.com/webhook',\n"
            "        )\n"
            "        rt = p.agent(system_prompt='hi', engine=OpenAIRealtime(api_key='sk-test'))\n"
            "        cv = p.agent(system_prompt='hi', engine=ElevenLabsConvAI(api_key='el-test', agent_id='a1'))\n"
            "        pl = p.agent(system_prompt='hi')  # default: pipeline / OpenAI Realtime fallback\n"
            "        print(f'realtime agent → {type(rt).__name__}')\n"
            "        print(f'convai agent   → {type(cv).__name__}')\n"
            "        print(f'pipeline agent → {type(pl).__name__}')\n",
        ),
    ]


def quickstart_cells_typescript() -> list[dict]:
    return [
        _md(
            "These cells run with **zero API keys** in <30 seconds. "
            "They exercise the public Patter API offline.\n"
        ),
        _code(
            "qs_version_check",
            'import { cell } from "./_setup.ts";\n'
            'import * as getpatter from "getpatter";\n'
            "await cell('version_check', { tier: 1, env }, () => {\n"
            "  const v = (getpatter as any).version ?? 'unknown';\n"
            "  console.log(`getpatter ${v} on ${typeof Deno !== 'undefined' ? `Deno ${Deno.version.deno}` : `Node ${process.version}`}`);\n"
            "});\n",
        ),
        _md(
            "### Local mode\n"
            "Construct a Patter instance with a Twilio carrier.\n"
        ),
        _code(
            "qs_local_mode",
            'import { Patter, Twilio } from "getpatter";\n'
            "await cell('local_mode', { tier: 1, env }, () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({\n"
            "      accountSid: 'ACtest00000000000000000000000000',\n"
            "      authToken: 'test',\n"
            "    }),\n"
            "    phoneNumber: '+15555550100',\n"
            "    webhookUrl: 'https://example.com/webhook',\n"
            "  });\n"
            "  if ((p as any)._mode !== 'local') throw new Error(`expected local, got ${(p as any)._mode}`);\n"
            "  console.log(`mode = ${(p as any)._mode}`);\n"
            "});\n",
        ),
        _md(
            "### Cloud mode\n"
            "Same SDK, just an `apiKey` — Patter cloud handles telephony.\n"
        ),
        _code(
            "qs_cloud_mode",
            'import { Patter } from "getpatter";\n'
            "await cell('cloud_mode', { tier: 1, env }, () => {\n"
            "  const p = new Patter({ apiKey: 'pt_test_xxx' });\n"
            "  if ((p as any)._mode !== 'cloud') throw new Error(`expected cloud, got ${(p as any)._mode}`);\n"
            "  console.log(`mode = ${(p as any)._mode}; apiKey = ${p.apiKey.slice(0, 8)}...`);\n"
            "});\n",
        ),
        _md(
            "### Three engine types\n"
            "An agent picks one of *OpenAI Realtime*, *ElevenLabs ConvAI*, or *Pipeline*.\n"
        ),
        _code(
            "qs_agent_engines",
            'import { Patter, Twilio, OpenAIRealtime, ElevenLabsConvAI } from "getpatter";\n'
            "await cell('agent_engines', { tier: 1, env }, () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: 'ACtest00000000000000000000000000', authToken: 'test' }),\n"
            "    phoneNumber: '+15555550100',\n"
            "    webhookUrl: 'https://example.com/webhook',\n"
            "  });\n"
            "  const rt = p.agent({ systemPrompt: 'hi', engine: new OpenAIRealtime({ apiKey: 'sk-test' }) });\n"
            "  const cv = p.agent({ systemPrompt: 'hi', engine: new ElevenLabsConvAI({ apiKey: 'el-test', agentId: 'a1' }) });\n"
            "  const pl = p.agent({ systemPrompt: 'hi' });\n"
            "  console.log(`realtime agent → ${rt.constructor.name}`);\n"
            "  console.log(`convai agent   → ${cv.constructor.name}`);\n"
            "  console.log(`pipeline agent → ${pl.constructor.name}`);\n"
            "});\n",
        ),
    ]
