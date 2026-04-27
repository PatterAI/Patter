"""§2 Feature Tour cells — 03 Pipeline STT."""

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
            "Exercises STT provider construction, audio transcoding, and (T3) live transcription.\n"
        ),
        _md("### STT provider construction\n"),
        _code(
            "ft_stt_providers",
            "from getpatter import DeepgramSTT, WhisperSTT, OpenAITranscribeSTT\n"
            "with _setup.cell('stt_providers', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        dg = DeepgramSTT(api_key='dg-test', model='nova-2', language='en-US')\n"
            "        wh = WhisperSTT(api_key='sk-test', model='whisper-1')\n"
            "        ot = OpenAITranscribeSTT(api_key='sk-test')\n"
            "        print(f'Deepgram: model={dg.model}  lang={dg.language}')\n"
            "        print(f'Whisper:  model={wh.model}')\n"
            "        print(f'OpenAI Transcribe: provider={ot.provider if hasattr(ot, \"provider\") else type(ot).__name__}')\n"
            "        assert dg.model == 'nova-2'\n",
        ),
        _md("### μ-law ↔ PCM-16 transcoding roundtrip\n"),
        _code(
            "ft_mulaw_transcoding",
            "from getpatter import mulaw_to_pcm16, pcm16_to_mulaw\n"
            "with _setup.cell('mulaw_transcoding', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        # 100ms of silence at 8kHz = 800 bytes\n"
            "        import struct\n"
            "        SAMPLES = 800\n"
            "        pcm_original = struct.pack('<' + 'h' * SAMPLES, *([0] * SAMPLES))\n"
            "        mulaw_bytes = pcm16_to_mulaw(pcm_original)\n"
            "        pcm_recovered = mulaw_to_pcm16(mulaw_bytes)\n"
            "        print(f'PCM original:  {len(pcm_original)} bytes ({SAMPLES} samples)')\n"
            "        print(f'μ-law encoded: {len(mulaw_bytes)} bytes (8-bit, 2:1 compression)')\n"
            "        print(f'PCM recovered: {len(pcm_recovered)} bytes')\n"
            "        assert len(mulaw_bytes) == SAMPLES\n"
            "        assert len(pcm_recovered) == len(pcm_original)\n",
        ),
        _md("### 8kHz → 16kHz resampling\n"),
        _code(
            "ft_resampler",
            "from getpatter import resample_8k_to_16k, resample_16k_to_8k\n"
            "with _setup.cell('resampler', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        import struct\n"
            "        # 100ms silence at 8kHz\n"
            "        pcm_8k = struct.pack('<' + 'h' * 800, *([0] * 800))\n"
            "        pcm_16k = resample_8k_to_16k(pcm_8k)\n"
            "        pcm_8k_back = resample_16k_to_8k(pcm_16k)\n"
            "        print(f'8kHz input:   {len(pcm_8k)} bytes ({len(pcm_8k)//2} samples)')\n"
            "        print(f'16kHz output: {len(pcm_16k)} bytes ({len(pcm_16k)//2} samples)')\n"
            "        print(f'8kHz round-trip: {len(pcm_8k_back)} bytes')\n"
            "        assert len(pcm_16k) == len(pcm_8k) * 2\n",
        ),
        _md(
            "### Live: Deepgram transcription  *(T3 — requires `DEEPGRAM_API_KEY`)*\n\n"
            "Transcribes a synthetic fixture WAV using the Deepgram REST API.\n"
        ),
        _code(
            "ft_deepgram_live",
            "import httpx\n"
            "with _setup.cell('deepgram_live', tier=3, required=['deepgram_key'], env=env) as ok:\n"
            "    if ok:\n"
            "        audio = _setup.load_fixture('audio/hello_world_16khz_pcm.wav')\n"
            "        resp = httpx.post(\n"
            "            'https://api.deepgram.com/v1/listen?model=nova-2&language=en-US',\n"
            "            headers={\n"
            "                'Authorization': f'Token {env.deepgram_key}',\n"
            "                'Content-Type': 'audio/wav',\n"
            "            },\n"
            "            content=audio,\n"
            "            timeout=30,\n"
            "        )\n"
            "        resp.raise_for_status()\n"
            "        transcript = resp.json()['results']['channels'][0]['alternatives'][0]['transcript']\n"
            "        print(f'Deepgram transcript: {transcript!r}')\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises STT provider construction, audio transcoding, and (T3) live transcription.\n"
        ),
        _md("### STT provider construction\n"),
        _code(
            "ft_stt_providers",
            'import { DeepgramSTT, WhisperSTT, OpenAITranscribeSTT } from "getpatter";\n'
            "await cell('stt_providers', { tier: 1, env }, () => {\n"
            "  const dg = new DeepgramSTT({ apiKey: 'dg-test', model: 'nova-2', language: 'en-US' });\n"
            "  const wh = new WhisperSTT({ apiKey: 'sk-test', model: 'whisper-1' });\n"
            "  const ot = new OpenAITranscribeSTT({ apiKey: 'sk-test' });\n"
            "  console.log(`Deepgram: model=${dg.model}  lang=${dg.language}`);\n"
            "  console.log(`Whisper:  model=${wh.model}`);\n"
            "  console.log(`OpenAI Transcribe: ${ot.constructor.name}`);\n"
            "});\n",
        ),
        _md("### μ-law ↔ PCM-16 transcoding roundtrip\n"),
        _code(
            "ft_mulaw_transcoding",
            'import { mulawToPcm16, pcm16ToMulaw } from "getpatter";\n'
            "await cell('mulaw_transcoding', { tier: 1, env }, () => {\n"
            "  const pcm = new Uint8Array(1600).fill(0);  // 800 silent 16-bit samples\n"
            "  const mulaw = pcm16ToMulaw(Buffer.from(pcm.buffer));\n"
            "  const recovered = mulawToPcm16(mulaw);\n"
            "  console.log(`PCM: ${pcm.length}B  μ-law: ${mulaw.length}B  recovered: ${recovered.length}B`);\n"
            "});\n",
        ),
        _md("### 8kHz → 16kHz resampling\n"),
        _code(
            "ft_resampler",
            'import { resample8kTo16k, resample16kTo8k } from "getpatter";\n'
            "await cell('resampler', { tier: 1, env }, () => {\n"
            "  const pcm8k = Buffer.alloc(1600, 0);  // 800 silent samples @ 8kHz\n"
            "  const pcm16k = resample8kTo16k(pcm8k);\n"
            "  const back = resample16kTo8k(pcm16k);\n"
            "  console.log(`8kHz: ${pcm8k.length}B  16kHz: ${pcm16k.length}B  roundtrip: ${back.length}B`);\n"
            "});\n",
        ),
        _md("### Live: Deepgram transcription  *(T3 — requires `DEEPGRAM_API_KEY`)*\n"),
        _code(
            "ft_deepgram_live",
            "await cell('deepgram_live', { tier: 3, required: ['deepgramKey'], env }, async () => {\n"
            "  const fs = await import('fs/promises');\n"
            "  const audio = await fs.readFile('./fixtures/audio/hello_world_16khz_pcm.wav');\n"
            "  const resp = await fetch('https://api.deepgram.com/v1/listen?model=nova-2&language=en-US', {\n"
            "    method: 'POST',\n"
            "    headers: { Authorization: `Token ${env.deepgramKey}`, 'Content-Type': 'audio/wav' },\n"
            "    body: audio,\n"
            "  });\n"
            "  const data = await resp.json() as any;\n"
            "  const transcript = data.results.channels[0].alternatives[0].transcript;\n"
            "  console.log(`Deepgram transcript: ${JSON.stringify(transcript)}`);\n"
            "});\n",
        ),
    ]
