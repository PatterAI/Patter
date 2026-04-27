"""§2 Feature Tour cells — 09 Guardrails & Hooks."""

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
            "Exercises guardrail construction, PipelineHooks, and PipelineHookExecutor.\n"
        ),
        _md("### `guardrail()` factory\n"),
        _code(
            "ft_guardrail_decorator",
            "from getpatter import guardrail, Guardrail\n"
            "with _setup.cell('guardrail_decorator', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        @guardrail\n"
            "        def no_competitor_mention(text: str) -> str | None:\n"
            '            """Block mentions of competitor names."""\n'
            "            competitors = ['rival-co', 'othercorp']\n"
            "            for c in competitors:\n"
            "                if c.lower() in text.lower():\n"
            "                    return f'I cannot discuss {c}.'\n"
            "            return None  # allow\n"
            "\n"
            "        result_allowed = no_competitor_mention.handler('Hello, how can I help?')\n"
            "        result_blocked = no_competitor_mention.handler('Have you tried rival-co?')\n"
            "        print(f'Allowed text → handler returns: {result_allowed!r}')\n"
            "        print(f'Blocked text → handler returns: {result_blocked!r}')\n"
            "        assert result_allowed is None\n"
            "        assert result_blocked is not None\n"
            "        assert isinstance(no_competitor_mention, Guardrail)\n",
        ),
        _md("### Agent with guardrails\n"),
        _code(
            "ft_guardrail_in_agent",
            "from getpatter import Patter, Twilio, OpenAIRealtime, guardrail\n"
            "with _setup.cell('guardrail_in_agent', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        @guardrail\n"
            "        def no_pricing_talk(text: str) -> str | None:\n"
            '            """Redirect pricing questions to sales."""\n'
            "            if 'price' in text.lower() or 'cost' in text.lower():\n"
            "                return 'For pricing, please contact our sales team.'\n"
            "            return None\n"
            "\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid='ACtest00000000000000000000000000', auth_token='test'),\n"
            "            phone_number='+15555550100',\n"
            "            webhook_url='https://example.com/webhook',\n"
            "        )\n"
            "        agent = p.agent(\n"
            "            system_prompt='You are a support agent.',\n"
            "            engine=OpenAIRealtime(api_key='sk-test'),\n"
            "            guardrails=[no_pricing_talk],\n"
            "        )\n"
            "        print(f'Agent guardrails: {[g.name for g in agent.guardrails]}')\n"
            "        assert len(agent.guardrails) == 1\n"
            "        assert agent.guardrails[0].name == 'no_pricing_talk'\n",
        ),
        _md("### PipelineHooks\n"),
        _code(
            "ft_pipeline_hooks",
            "from getpatter import PipelineHooks, PipelineHookExecutor\n"
            "with _setup.cell('pipeline_hooks', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        events: list[str] = []\n"
            "\n"
            "        async def on_transcript(text: str, is_final: bool) -> None:\n"
            "            events.append(f'transcript:{text[:20]}:final={is_final}')\n"
            "\n"
            "        async def on_llm_response(text: str) -> str:\n"
            "            events.append(f'llm:{text[:20]}')\n"
            "            return text  # pass-through\n"
            "\n"
            "        hooks = PipelineHooks(\n"
            "            on_transcript=on_transcript,\n"
            "            on_llm_response=on_llm_response,\n"
            "        )\n"
            "        print(f'PipelineHooks constructed: on_transcript={hooks.on_transcript is not None}')\n"
            "        print(f'                           on_llm_response={hooks.on_llm_response is not None}')\n"
            "        executor = PipelineHookExecutor(hooks)\n"
            "        print(f'PipelineHookExecutor: {type(executor).__name__}')\n"
            "        assert hooks.on_transcript is on_transcript\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises guardrail construction and PipelineHooks.\n"
        ),
        _md("### `guardrail()` factory\n"),
        _code(
            "ft_guardrail_decorator",
            'import { guardrail } from "getpatter";\n'
            "await cell('guardrail_decorator', { tier: 1, env }, () => {\n"
            "  const noCompetitor = guardrail({\n"
            "    name: 'no_competitor_mention',\n"
            "    handler: (text: string) => {\n"
            "      if (text.toLowerCase().includes('rival-co')) return 'I cannot discuss that.';\n"
            "      return null;\n"
            "    },\n"
            "  });\n"
            "  const allowed = noCompetitor.handler('Hello, how can I help?');\n"
            "  const blocked = noCompetitor.handler('Have you tried rival-co?');\n"
            "  console.log(`allowed: ${allowed}  blocked: ${blocked}`);\n"
            "  if (allowed !== null) throw new Error('expected null for allowed text');\n"
            "  if (blocked === null) throw new Error('expected non-null for blocked text');\n"
            "});\n",
        ),
        _md("### Agent with guardrails\n"),
        _code(
            "ft_guardrail_in_agent",
            'import { Patter, Twilio, OpenAIRealtime, guardrail } from "getpatter";\n'
            "await cell('guardrail_in_agent', { tier: 1, env }, () => {\n"
            "  const noPricing = guardrail({\n"
            "    name: 'no_pricing_talk',\n"
            "    handler: (text: string) =>\n"
            "      text.toLowerCase().includes('price') ? 'Contact sales for pricing.' : null,\n"
            "  });\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: 'ACtest00000000000000000000000000', authToken: 'test' }),\n"
            "    phoneNumber: '+15555550100',\n"
            "    webhookUrl: 'https://example.com/webhook',\n"
            "  });\n"
            "  const agent = p.agent({\n"
            "    systemPrompt: 'You are a support agent.',\n"
            "    engine: new OpenAIRealtime({ apiKey: 'sk-test' }),\n"
            "    guardrails: [noPricing],\n"
            "  });\n"
            "  console.log(`Agent guardrails: ${agent.guardrails?.map((g: any) => g.name)}`);\n"
            "});\n",
        ),
        _md("### PipelineHooks\n"),
        _code(
            "ft_pipeline_hooks",
            'import { PipelineHooks } from "getpatter";\n'
            "await cell('pipeline_hooks', { tier: 1, env }, () => {\n"
            "  const hooks: PipelineHooks = {\n"
            "    onTranscript: async (text: string, isFinal: boolean) => { /* log */ },\n"
            "    onLlmResponse: async (text: string) => text,\n"
            "  };\n"
            "  console.log(`PipelineHooks: onTranscript=${typeof hooks.onTranscript}`);\n"
            "});\n",
        ),
    ]
