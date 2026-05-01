"""§2 Feature Tour cells — 04 Pipeline TTS."""

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
            "Exercises TTS provider construction and (T3) live synthesis.\n"
        ),
        _md("### TTS provider construction\n"),
        _code(
            "ft_tts_providers",
            "from getpatter import ElevenLabsTTS, OpenAITTS, CartesiaTTS, RimeTTS, LMNTTTS\n"
            "with _setup.cell('tts_providers', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        el = ElevenLabsTTS(api_key='el-test', voice_id='21m00Tcm4TlvDq8ikWAM')\n"
            "        ot = OpenAITTS(api_key='sk-test', voice='alloy', model='tts-1')\n"
            "        ca = CartesiaTTS(api_key='ca-test')\n"
            "        ri = RimeTTS(api_key='ri-test')\n"
            "        lm = LMNTTTS(api_key='lm-test')\n"
            "        for name, provider in [('ElevenLabs', el), ('OpenAI', ot), ('Cartesia', ca), ('Rime', ri), ('LMNT', lm)]:\n"
            "            print(f'{name}: {type(provider).__name__}')\n"
            "        assert el.voice_id == '21m00Tcm4TlvDq8ikWAM'\n"
            "        assert ot.voice == 'alloy'\n",
        ),
        _md("### TTS text preparation\n"),
        _code(
            "ft_tts_text_prep",
            "from getpatter import filter_for_tts\n"
            "with _setup.cell('tts_text_prep', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        samples = [\n"
            "            '**Bold** and *italic* text.',\n"
            "            'Visit https://example.com for more. 🎉',\n"
            "            'Call us at +1 (800) 555-0100.',\n"
            "            'Code: `x = 1 + 2`',\n"
            "        ]\n"
            "        for raw in samples:\n"
            "            clean = filter_for_tts(raw)\n"
            "            print(f'  in:  {raw}')\n"
            "            print(f'  out: {clean}')\n"
            "            print()\n",
        ),
        _md(
            "### Live: ElevenLabs TTS synthesis  *(T3 — requires `ELEVENLABS_API_KEY`)*\n\n"
            "Synthesises a short phrase and reports the audio byte count.\n"
        ),
        _code(
            "ft_elevenlabs_tts_live",
            "import httpx\n"
            "with _setup.cell('elevenlabs_tts_live', tier=3, required=['elevenlabs_key'], env=env) as ok:\n"
            "    if ok:\n"
            "        voice_id = '21m00Tcm4TlvDq8ikWAM'  # Rachel (default)\n"
            "        resp = httpx.post(\n"
            "            f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}',\n"
            "            headers={\n"
            "                'xi-api-key': env.elevenlabs_key,\n"
            "                'Content-Type': 'application/json',\n"
            "            },\n"
            "            json={'text': 'Hello from Patter.', 'model_id': 'eleven_monolingual_v1'},\n"
            "            timeout=30,\n"
            "        )\n"
            "        resp.raise_for_status()\n"
            "        print(f'ElevenLabs audio: {len(resp.content)} bytes  content-type: {resp.headers[\"content-type\"]}')\n"
            "        assert len(resp.content) > 0\n",
        ),
        _md("### Live: OpenAI TTS synthesis  *(T3 — requires `OPENAI_API_KEY`)*\n"),
        _code(
            "ft_openai_tts_live",
            "import httpx\n"
            "with _setup.cell('openai_tts_live', tier=3, required=['openai_key'], env=env) as ok:\n"
            "    if ok:\n"
            "        resp = httpx.post(\n"
            "            'https://api.openai.com/v1/audio/speech',\n"
            "            headers={\n"
            "                'Authorization': f'Bearer {env.openai_key}',\n"
            "                'Content-Type': 'application/json',\n"
            "            },\n"
            "            json={'model': 'tts-1', 'voice': 'alloy', 'input': 'Hello from Patter.'},\n"
            "            timeout=30,\n"
            "        )\n"
            "        resp.raise_for_status()\n"
            "        print(f'OpenAI TTS audio: {len(resp.content)} bytes')\n"
            "        assert len(resp.content) > 0\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises TTS provider construction and (T3) live synthesis.\n"
        ),
        _md("### TTS provider construction\n"),
        _code(
            "ft_tts_providers",
            'import { ElevenLabsTTS, OpenAITTS, CartesiaTTS, RimeTTS, LMNTTTS } from "getpatter";\n'
            "await cell('tts_providers', { tier: 1, env }, () => {\n"
            "  const el = new ElevenLabsTTS({ apiKey: 'el-test', voiceId: '21m00Tcm4TlvDq8ikWAM' });\n"
            "  const ot = new OpenAITTS({ apiKey: 'sk-test', voice: 'alloy', model: 'tts-1' });\n"
            "  const ca = new CartesiaTTS({ apiKey: 'ca-test' });\n"
            "  const ri = new RimeTTS({ apiKey: 'ri-test' });\n"
            "  const lm = new LMNTTTS({ apiKey: 'lm-test' });\n"
            "  for (const [name, p] of [['ElevenLabs', el], ['OpenAI', ot], ['Cartesia', ca], ['Rime', ri], ['LMNT', lm]]) {\n"
            "    console.log(`${name}: ${(p as any).constructor.name}`);\n"
            "  }\n"
            "});\n",
        ),
        _md("### TTS text preparation\n"),
        _code(
            "ft_tts_text_prep",
            'import { filterForTts } from "getpatter";\n'
            "await cell('tts_text_prep', { tier: 1, env }, () => {\n"
            "  const samples = ['**Bold** text.', 'Visit https://example.com 🎉', 'Code: `x = 1`'];\n"
            "  for (const raw of samples) {\n"
            "    console.log(`in:  ${raw}`);\n"
            "    console.log(`out: ${filterForTts(raw)}`);\n"
            "  }\n"
            "});\n",
        ),
        _md(
            "### Live: ElevenLabs TTS synthesis  *(T3 — requires `ELEVENLABS_API_KEY`)*\n"
        ),
        _code(
            "ft_elevenlabs_tts_live",
            "await cell('elevenlabs_tts_live', { tier: 3, required: ['elevenLabsKey'], env }, async () => {\n"
            "  const voiceId = '21m00Tcm4TlvDq8ikWAM';\n"
            "  const resp = await fetch(`https://api.elevenlabs.io/v1/text-to-speech/${voiceId}`, {\n"
            "    method: 'POST',\n"
            "    headers: { 'xi-api-key': env.elevenLabsKey, 'Content-Type': 'application/json' },\n"
            "    body: JSON.stringify({ text: 'Hello from Patter.', model_id: 'eleven_monolingual_v1' }),\n"
            "  });\n"
            "  const buf = await resp.arrayBuffer();\n"
            "  console.log(`ElevenLabs audio: ${buf.byteLength} bytes`);\n"
            "});\n",
        ),
        _md("### Live: OpenAI TTS synthesis  *(T3 — requires `OPENAI_API_KEY`)*\n"),
        _code(
            "ft_openai_tts_live",
            "await cell('openai_tts_live', { tier: 3, required: ['openaiKey'], env }, async () => {\n"
            "  const resp = await fetch('https://api.openai.com/v1/audio/speech', {\n"
            "    method: 'POST',\n"
            "    headers: { Authorization: `Bearer ${env.openaiKey}`, 'Content-Type': 'application/json' },\n"
            "    body: JSON.stringify({ model: 'tts-1', voice: 'alloy', input: 'Hello from Patter.' }),\n"
            "  });\n"
            "  const buf = await resp.arrayBuffer();\n"
            "  console.log(`OpenAI TTS audio: ${buf.byteLength} bytes`);\n"
            "});\n",
        ),
    ]
