"""§2 Feature Tour cells — 01 Quickstart."""

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
            "These cells require **T2** (local server) or **T3** (real API keys). "
            "Cells skip gracefully if prerequisites are missing.\n"
        ),
        _md("### Agent object inspection\n"),
        _code(
            "ft_agent_inspection",
            "from getpatter import Patter, Twilio, OpenAIRealtime, ElevenLabsConvAI, DeepgramSTT, ElevenLabsTTS\n"
            "with _setup.cell('agent_inspection', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        p = Patter(\n"
            "            carrier=Twilio(account_sid='ACtest00000000000000000000000000', auth_token='test'),\n"
            "            phone_number='+15555550100',\n"
            "            webhook_url='https://example.com/webhook',\n"
            "        )\n"
            "        rt = p.agent(\n"
            "            system_prompt='You are a helpful assistant.',\n"
            "            engine=OpenAIRealtime(api_key='sk-test'),\n"
            "            voice='alloy',\n"
            "        )\n"
            "        pl = p.agent(\n"
            "            system_prompt='Pipeline agent.',\n"
            "            stt=DeepgramSTT(api_key='dg-test'),\n"
            "            tts=ElevenLabsTTS(api_key='el-test'),\n"
            "        )\n"
            "        print(f'realtime: provider={rt.provider}  voice={rt.voice}  model={rt.model}')\n"
            "        print(f'pipeline: provider={pl.provider}  stt={pl.stt}  tts={pl.tts}')\n"
            "        assert rt.system_prompt == 'You are a helpful assistant.'\n"
            "        assert pl.provider in ('openai_realtime', 'pipeline')\n",
        ),
        _md("### Pricing: calculate call costs\n"),
        _code(
            "ft_pricing",
            "from getpatter import DEFAULT_PRICING, calculate_stt_cost, calculate_tts_cost, calculate_telephony_cost\n"
            "with _setup.cell('pricing', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        stt = calculate_stt_cost('deepgram', 30, DEFAULT_PRICING)   # 30s audio\n"
            "        tts = calculate_tts_cost('elevenlabs', 200, DEFAULT_PRICING) # 200 chars\n"
            "        tel = calculate_telephony_cost('twilio', 60, DEFAULT_PRICING) # 60s call\n"
            "        print(f'STT (Deepgram, 30s):        ${stt:.6f}')\n"
            "        print(f'TTS (ElevenLabs, 200 chars): ${tts:.6f}')\n"
            "        print(f'Telephony (Twilio, 60s):     ${tel:.6f}')\n"
            "        print(f'Total estimate:              ${stt + tts + tel:.6f}')\n"
            "        assert stt > 0\n"
            "        assert tts > 0\n"
            "        assert tel > 0\n",
        ),
        _md("### Text transforms\n"),
        _code(
            "ft_text_transforms",
            "from getpatter import filter_markdown, filter_emoji, filter_for_tts\n"
            "with _setup.cell('text_transforms', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        raw = '**Important**: Please call us at +1-800-555-0100. 😊 See https://example.com'\n"
            "        step1 = filter_markdown(raw)\n"
            "        step2 = filter_emoji(raw)\n"
            "        step3 = filter_for_tts(raw)\n"
            "        print(f'original:          {raw}')\n"
            "        print(f'filter_markdown:   {step1}')\n"
            "        print(f'filter_emoji:      {step2}')\n"
            "        print(f'filter_for_tts:    {step3}')\n"
            "        assert '**' not in step1\n"
            "        assert '😊' not in step2\n"
            "        assert '**' not in step3\n",
        ),
        _md("### SentenceChunker\n"),
        _code(
            "ft_sentence_chunker",
            "from getpatter import SentenceChunker\n"
            "with _setup.cell('sentence_chunker', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        sc = SentenceChunker()\n"
            "        tokens = ['Hello', ' world', '!', ' How', ' are', ' you', ' today', '?', ' I', \"'m\", ' fine', '.']\n"
            "        chunks: list[str] = []\n"
            "        for tok in tokens:\n"
            "            result = sc.push(tok)\n"
            "            chunks.extend(result)\n"
            "        remainder = sc.flush()\n"
            "        chunks.extend(remainder)\n"
            "        print(f'input tokens:  {tokens}')\n"
            "        print(f'output chunks: {chunks}')\n"
            "        full = ' '.join(chunks).replace('  ', ' ')\n"
            "        print(f'reassembled:   {full}')\n"
            "        assert len(chunks) >= 1\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "These cells require **T2** (local server) or **T3** (real API keys). "
            "Cells skip gracefully if prerequisites are missing.\n"
        ),
        _md("### Agent object inspection\n"),
        _code(
            "ft_agent_inspection",
            'import { Patter, Twilio, OpenAIRealtime, ElevenLabsConvAI, DeepgramSTT, ElevenLabsTTS } from "getpatter";\n'
            "await cell('agent_inspection', { tier: 1, env }, () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: 'ACtest00000000000000000000000000', authToken: 'test' }),\n"
            "    phoneNumber: '+15555550100',\n"
            "    webhookUrl: 'https://example.com/webhook',\n"
            "  });\n"
            "  const rt = p.agent({ systemPrompt: 'You are helpful.', engine: new OpenAIRealtime({ apiKey: 'sk-test' }), voice: 'alloy' });\n"
            "  const pl = p.agent({ systemPrompt: 'Pipeline.', stt: new DeepgramSTT({ apiKey: 'dg-test' }), tts: new ElevenLabsTTS({ apiKey: 'el-test' }) });\n"
            "  console.log(`realtime: provider=${rt.provider}  voice=${rt.voice}`);\n"
            "  console.log(`pipeline: provider=${pl.provider}`);\n"
            "});\n",
        ),
        _md("### Pricing: calculate call costs\n"),
        _code(
            "ft_pricing",
            'import { DEFAULT_PRICING, calculateSttCost, calculateTtsCost, calculateTelephonyCost } from "getpatter";\n'
            "await cell('pricing', { tier: 1, env }, () => {\n"
            "  const stt = calculateSttCost('deepgram', 30, DEFAULT_PRICING);\n"
            "  const tts = calculateTtsCost('elevenlabs', 200, DEFAULT_PRICING);\n"
            "  const tel = calculateTelephonyCost('twilio', 60, DEFAULT_PRICING);\n"
            "  console.log(`STT (Deepgram, 30s):         $${stt.toFixed(6)}`);\n"
            "  console.log(`TTS (ElevenLabs, 200 chars): $${tts.toFixed(6)}`);\n"
            "  console.log(`Telephony (Twilio, 60s):     $${tel.toFixed(6)}`);\n"
            "});\n",
        ),
        _md("### Text transforms\n"),
        _code(
            "ft_text_transforms",
            'import { filterMarkdown, filterEmoji, filterForTts } from "getpatter";\n'
            "await cell('text_transforms', { tier: 1, env }, () => {\n"
            "  const raw = '**Important**: Hello 😊 world';\n"
            "  console.log(`filter_markdown: ${filterMarkdown(raw)}`);\n"
            "  console.log(`filter_emoji:    ${filterEmoji(raw)}`);\n"
            "  console.log(`filter_for_tts:  ${filterForTts(raw)}`);\n"
            "});\n",
        ),
        _md("### SentenceChunker\n"),
        _code(
            "ft_sentence_chunker",
            'import { SentenceChunker } from "getpatter";\n'
            "await cell('sentence_chunker', { tier: 1, env }, () => {\n"
            "  const sc = new SentenceChunker();\n"
            "  const tokens = ['Hello', ' world', '!', ' How', ' are', ' you', '?'];\n"
            "  const chunks: string[] = [];\n"
            "  for (const tok of tokens) { chunks.push(...sc.push(tok)); }\n"
            "  chunks.push(...sc.flush());\n"
            "  console.log(`chunks: ${JSON.stringify(chunks)}`);\n"
            "});\n",
        ),
    ]
