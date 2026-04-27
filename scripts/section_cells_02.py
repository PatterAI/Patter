"""§2 Feature Tour cells — 02 OpenAI Realtime."""

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


def section_cells_python() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises OpenAI Realtime configuration and related SDK primitives.\n"
        ),
        _md("### OpenAI Realtime agent: full config\n"),
        _code(
            "ft_realtime_agent_config",
            "from getpatter import Patter, Twilio, OpenAIRealtime\n"
            "with _setup.cell('realtime_agent_config', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid='ACtest00000000000000000000000000', auth_token='test'),\n"
            "            phone_number='+15555550100',\n"
            "            webhook_url='https://example.com/webhook',\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='You are a concise assistant.',\n"
            "            engine=OpenAIRealtime(\n"
            "                api_key='sk-test',\n"
            "                model='gpt-4o-realtime-preview',\n"
            "            ),\n"
            "            voice='alloy',\n"
            "            first_message='Hello! How can I help you today?',\n"
            "            barge_in_threshold_ms=500,\n"
            "        )\n"
            "        print(f'provider:              {agent.provider}')\n"
            "        print(f'voice:                 {agent.voice}')\n"
            "        print(f'first_message:         {agent.first_message}')\n"
            "        print(f'barge_in_threshold_ms: {agent.barge_in_threshold_ms}')\n"
            "        assert agent.provider == 'openai_realtime'\n"
            "        assert agent.voice == 'alloy'\n",
        ),
        _md("### SentenceChunker\n"),
        _code(
            "ft_realtime_chunker",
            "from getpatter import SentenceChunker\n"
            "with _setup.cell('realtime_chunker', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        sc = SentenceChunker()\n"
            "        full_response = (\n"
            "            'The weather today is sunny. Temperature is 72°F. '\n"
            "            'Humidity is low. Great day for a walk!'\n"
            "        )\n"
            "        chunks: list[str] = []\n"
            "        for char in full_response:\n"
            "            result = sc.push(char)\n"
            "            chunks.extend(result)\n"
            "        chunks.extend(sc.flush())\n"
            "        print(f'sentences: {len(chunks)}')\n"
            "        for i, chunk in enumerate(chunks):\n"
            "            print(f'  [{i}] {chunk.strip()!r}')\n"
            "        assert len(chunks) >= 2\n",
        ),
        _md(
            "### Live: OpenAI Realtime models  *(T3 — requires `OPENAI_API_KEY`)*\n\n"
            "Connects to OpenAI Realtime WebSocket and lists supported models.\n"
        ),
        _code(
            "ft_openai_realtime_live",
            "import httpx\n"
            "with _setup.cell('openai_realtime_live', tier=3, required=['openai_key'], env=env) as ok:\n"
            "    if ok:\n"
            "        resp = httpx.get(\n"
            "            'https://api.openai.com/v1/models',\n"
            "            headers={'Authorization': f'Bearer {env.openai_key}'},\n"
            "            timeout=10,\n"
            "        )\n"
            "        resp.raise_for_status()\n"
            "        models = [m['id'] for m in resp.json()['data'] if 'realtime' in m['id']]\n"
            "        print(f'OpenAI realtime models: {models[:5]}')\n"
            "        assert len(models) > 0, 'no realtime models found'\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises OpenAI Realtime configuration and related SDK primitives.\n"
        ),
        _md("### OpenAI Realtime agent: full config\n"),
        _code(
            "ft_realtime_agent_config",
            'import { Patter, Twilio, OpenAIRealtime } from "getpatter";\n'
            "await cell('realtime_agent_config', { tier: 1, env }, () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: 'ACtest00000000000000000000000000', authToken: 'test' }),\n"
            "    phoneNumber: '+15555550100',\n"
            "    webhookUrl: 'https://example.com/webhook',\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'You are a concise assistant.',\n"
            "    engine: new OpenAIRealtime({ apiKey: 'sk-test', model: 'gpt-4o-realtime-preview' }),\n"
            "    voice: 'alloy',\n"
            "    firstMessage: 'Hello! How can I help?',\n"
            "    bargeInThresholdMs: 500,\n"
            "  });\n"
            "  console.log(`provider: ${agent.provider}  voice: ${agent.voice}`);\n"
            "  console.log(`firstMessage: ${agent.firstMessage}`);\n"
            "  if (agent.provider !== 'openai_realtime') throw new Error('wrong provider');\n"
            "});\n",
        ),
        _md("### SentenceChunker\n"),
        _code(
            "ft_realtime_chunker",
            'import { SentenceChunker } from "getpatter";\n'
            "await cell('realtime_chunker', { tier: 1, env }, () => {\n"
            "  const sc = new SentenceChunker();\n"
            "  const text = 'The weather is sunny. Temperature is 72F. Great day!';\n"
            "  const chunks: string[] = [];\n"
            "  for (const char of text) { chunks.push(...sc.push(char)); }\n"
            "  chunks.push(...sc.flush());\n"
            "  console.log(`sentences: ${chunks.length}`);\n"
            "  chunks.forEach((c, i) => console.log(`  [${i}] ${JSON.stringify(c.trim())}`));\n"
            "});\n",
        ),
        _md("### Live: OpenAI Realtime models  *(T3 — requires `OPENAI_API_KEY`)*\n"),
        _code(
            "ft_openai_realtime_live",
            "await cell('openai_realtime_live', { tier: 3, required: ['openaiKey'], env }, async () => {\n"
            "  const resp = await fetch('https://api.openai.com/v1/models', {\n"
            "    headers: { Authorization: `Bearer ${env.openaiKey}` },\n"
            "  });\n"
            "  const data = await resp.json() as { data: Array<{ id: string }> };\n"
            "  const models = data.data.filter(m => m.id.includes('realtime')).map(m => m.id);\n"
            "  console.log(`OpenAI realtime models: ${models.slice(0, 5)}`);\n"
            "});\n",
        ),
    ]
