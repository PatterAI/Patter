"""§2 Feature Tour cells — 05 Pipeline LLM."""

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
            "Exercises LLM provider construction, fallback routing, ChatContext, and (T3) live completions.\n"
        ),
        _md("### LLM provider construction\n"),
        _code(
            "ft_llm_providers",
            "from getpatter import OpenAILLM, AnthropicLLM, GroqLLM, CerebrasLLM, GoogleLLM\n"
            "with _setup.cell('llm_providers', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        oai  = OpenAILLM(api_key='sk-test',  model='gpt-4o-mini')\n"
            "        ant  = AnthropicLLM(api_key='sk-ant-test', model='claude-haiku-4-5-20251001')\n"
            "        grq  = GroqLLM(api_key='gr-test',   model='llama3-8b-8192')\n"
            "        cer  = CerebrasLLM(api_key='ce-test', model='llama-4-scout-17b-16e-instruct')\n"
            "        goo  = GoogleLLM(api_key='go-test',  model='gemini-2.0-flash')\n"
            "        for name, p in [('OpenAI', oai), ('Anthropic', ant), ('Groq', grq), ('Cerebras', cer), ('Google', goo)]:\n"
            "            print(f'{name:10s}: model={p.model}')\n"
            "        assert oai.model == 'gpt-4o-mini'\n"
            "        assert ant.model == 'claude-haiku-4-5-20251001'\n",
        ),
        _md("### FallbackLLMProvider\n"),
        _code(
            "ft_fallback_provider",
            "from getpatter import FallbackLLMProvider, OpenAILLM, AnthropicLLM, AllProvidersFailedError\n"
            "with _setup.cell('fallback_provider', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        primary   = OpenAILLM(api_key='sk-test',    model='gpt-4o-mini')\n"
            "        secondary = AnthropicLLM(api_key='sk-ant-test', model='claude-haiku-4-5-20251001')\n"
            "        fallback  = FallbackLLMProvider([primary, secondary])\n"
            "        print(f'FallbackLLMProvider with {len(fallback.providers)} providers:')\n"
            "        for i, p in enumerate(fallback.providers):\n"
            "            print(f'  [{i}] {type(p).__name__} model={p.model}')\n"
            "        print(f'AllProvidersFailedError: {AllProvidersFailedError.__name__}')\n",
        ),
        _md("### ChatContext\n"),
        _code(
            "ft_chat_context",
            "from getpatter import ChatContext\n"
            "with _setup.cell('chat_context', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        ctx = ChatContext(system_prompt='You are a helpful assistant.')\n"
            "        ctx.add_user('What is the capital of France?')\n"
            "        ctx.add_assistant('The capital of France is Paris.')\n"
            "        ctx.add_user('And Germany?')\n"
            "        msgs = ctx.get_messages()\n"
            "        print(f'Total messages: {len(msgs)}')\n"
            "        for m in msgs:\n"
            "            print(f'  {m.role:9s}: {m.content[:50]}')\n"
            "        oai_fmt = ctx.to_openai()\n"
            "        print(f'OpenAI format ({len(oai_fmt)} msgs): roles={[m[\"role\"] for m in oai_fmt]}')\n"
            "        ant_fmt = ctx.to_anthropic()\n"
            "        print(f'Anthropic format: {ant_fmt[:1]}')\n"
            "        assert ctx.length() == 4\n",
        ),
        _md("### Live: OpenAI chat completion  *(T3 — requires `OPENAI_API_KEY`)*\n"),
        _code(
            "ft_openai_chat_live",
            "import httpx\n"
            "with _setup.cell('openai_chat_live', tier=3, required=['openai_key'], env=env) as ok:\n"
            "    if ok:\n"
            "        resp = httpx.post(\n"
            "            'https://api.openai.com/v1/chat/completions',\n"
            "            headers={'Authorization': f'Bearer {env.openai_key}', 'Content-Type': 'application/json'},\n"
            "            json={\n"
            "                'model': 'gpt-4o-mini',\n"
            "                'messages': [{'role': 'user', 'content': 'Reply with exactly: pong'}],\n"
            "                'max_tokens': 10,\n"
            "            },\n"
            "            timeout=30,\n"
            "        )\n"
            "        resp.raise_for_status()\n"
            "        reply = resp.json()['choices'][0]['message']['content']\n"
            "        print(f'OpenAI reply: {reply!r}')\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises LLM provider construction, fallback routing, ChatContext, and (T3) live completions.\n"
        ),
        _md("### LLM provider construction\n"),
        _code(
            "ft_llm_providers",
            'import { OpenAILLM, AnthropicLLM, GroqLLM, CerebrasLLM, GoogleLLM } from "getpatter";\n'
            "await cell('llm_providers', { tier: 1, env }, () => {\n"
            "  const providers = [\n"
            "    ['OpenAI',    new OpenAILLM({ apiKey: 'sk-test', model: 'gpt-4o-mini' })],\n"
            "    ['Anthropic', new AnthropicLLM({ apiKey: 'sk-ant-test', model: 'claude-haiku-4-5-20251001' })],\n"
            "    ['Groq',      new GroqLLM({ apiKey: 'gr-test', model: 'llama3-8b-8192' })],\n"
            "    ['Cerebras',  new CerebrasLLM({ apiKey: 'ce-test', model: 'llama-4-scout-17b-16e-instruct' })],\n"
            "    ['Google',    new GoogleLLM({ apiKey: 'go-test', model: 'gemini-2.0-flash' })],\n"
            "  ] as const;\n"
            "  for (const [name, p] of providers) console.log(`${name}: model=${(p as any).model}`);\n"
            "});\n",
        ),
        _md("### FallbackLLMProvider\n"),
        _code(
            "ft_fallback_provider",
            'import { FallbackLLMProvider, OpenAILLM, AnthropicLLM } from "getpatter";\n'
            "await cell('fallback_provider', { tier: 1, env }, () => {\n"
            "  const fb = new FallbackLLMProvider([\n"
            "    new OpenAILLM({ apiKey: 'sk-test', model: 'gpt-4o-mini' }),\n"
            "    new AnthropicLLM({ apiKey: 'sk-ant-test', model: 'claude-haiku-4-5-20251001' }),\n"
            "  ]);\n"
            "  console.log(`FallbackLLMProvider with ${fb.providers.length} providers`);\n"
            "});\n",
        ),
        _md("### ChatContext\n"),
        _code(
            "ft_chat_context",
            'import { ChatContext } from "getpatter";\n'
            "await cell('chat_context', { tier: 1, env }, () => {\n"
            "  const ctx = new ChatContext('You are a helpful assistant.');\n"
            "  ctx.addUser('What is the capital of France?');\n"
            "  ctx.addAssistant('The capital of France is Paris.');\n"
            "  ctx.addUser('And Germany?');\n"
            "  const msgs = ctx.getMessages();\n"
            "  console.log(`Total messages: ${msgs.length}`);\n"
            "  msgs.forEach(m => console.log(`  ${m.role}: ${m.content.slice(0, 50)}`));\n"
            "});\n",
        ),
        _md("### Live: OpenAI chat completion  *(T3 — requires `OPENAI_API_KEY`)*\n"),
        _code(
            "ft_openai_chat_live",
            "await cell('openai_chat_live', { tier: 3, required: ['openaiKey'], env }, async () => {\n"
            "  const resp = await fetch('https://api.openai.com/v1/chat/completions', {\n"
            "    method: 'POST',\n"
            "    headers: { Authorization: `Bearer ${env.openaiKey}`, 'Content-Type': 'application/json' },\n"
            "    body: JSON.stringify({ model: 'gpt-4o-mini', messages: [{ role: 'user', content: 'Reply: pong' }], max_tokens: 10 }),\n"
            "  });\n"
            "  const data = await resp.json() as any;\n"
            "  console.log(`reply: ${data.choices[0].message.content}`);\n"
            "});\n",
        ),
    ]
