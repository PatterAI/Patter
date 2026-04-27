"""§2 Feature Tour cells — 11 Metrics & Dashboard."""

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
            "Exercises `CallMetricsAccumulator`, `MetricsStore`, and the CSV/JSON export helpers.\n"
        ),
        _md("### CallMetricsAccumulator\n"),
        _code(
            "ft_metrics_accumulator",
            "import time\n"
            "from getpatter import CallMetricsAccumulator\n"
            "with _setup.cell('metrics_accumulator', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        acc = CallMetricsAccumulator(\n"
            "            call_id='call-demo-001',\n"
            "            provider_mode='openai_realtime',\n"
            "            telephony_provider='twilio',\n"
            "            stt_provider='deepgram',\n"
            "            tts_provider='elevenlabs',\n"
            "        )\n"
            "        # Simulate a single turn.\n"
            "        acc.start_turn()\n"
            "        acc.record_stt_complete('What is the weather today?', audio_seconds=2.1)\n"
            "        acc.record_llm_first_token()\n"
            "        acc.record_llm_first_sentence()\n"
            "        acc.record_tts_first_byte()\n"
            "        tm = acc.record_turn_complete('The weather today is sunny and warm.')\n"
            "\n"
            "        print(f'turn_index:      {tm.turn_index}')\n"
            "        print(f'agent_text:      {tm.agent_text!r}')\n"
            "        print(f'tts_characters:  {tm.tts_characters}')\n"
            "        print(f'stt_audio_secs:  {tm.stt_audio_seconds:.1f}s')\n"
            "        print(f'latency.total:   {tm.latency.total_ms:.0f}ms')\n"
            "        assert tm.tts_characters == len('The weather today is sunny and warm.')\n"
            "        assert tm.stt_audio_seconds == 2.1\n",
        ),
        _md("### MetricsStore\n"),
        _code(
            "ft_metrics_store",
            "from getpatter import MetricsStore\n"
            "with _setup.cell('metrics_store', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        store = MetricsStore(max_calls=100)\n"
            "\n"
            "        # Record two completed calls.\n"
            "        for i in range(1, 3):\n"
            "            store.record_call_start({\n"
            "                'call_id': f'call-{i:03d}',\n"
            "                'from': '+15555550100',\n"
            "                'to': '+15555550200',\n"
            "                'direction': 'inbound',\n"
            "                'carrier': 'twilio',\n"
            "                'provider': 'openai_realtime',\n"
            "            })\n"
            "            store.record_call_end({\n"
            "                'call_id': f'call-{i:03d}',\n"
            "                'duration': 30 * i,\n"
            "                'cost_total': 0.05 * i,\n"
            "            })\n"
            "\n"
            "        calls = store.get_calls()\n"
            "        agg   = store.get_aggregates()\n"
            "        print(f'Total calls:   {agg[\"total_calls\"]}')\n"
            "        print(f'Avg duration:  {agg[\"avg_duration\"]}s')\n"
            "        print(f'Total cost:    ${agg[\"total_cost\"]:.4f}')\n"
            "        print(f'Active calls:  {agg[\"active_calls\"]}')\n"
            "        assert agg['total_calls'] == 2\n",
        ),
        _md("### Export\n"),
        _code(
            "ft_export",
            "from getpatter import MetricsStore, calls_to_csv, calls_to_json\n"
            "with _setup.cell('export', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        store = MetricsStore()\n"
            "        store.record_call_start({'call_id': 'c001', 'from': '+15555550100', 'to': '+15555550200', 'direction': 'inbound', 'carrier': 'twilio', 'provider': 'openai_realtime'})\n"
            "        store.record_call_end({'call_id': 'c001', 'duration': 45, 'cost_total': 0.08})\n"
            "\n"
            "        calls = store.get_calls()\n"
            "        csv_output  = calls_to_csv(calls)\n"
            "        json_output = calls_to_json(calls)\n"
            "\n"
            "        csv_lines = csv_output.strip().splitlines()\n"
            "        print(f'CSV header:  {csv_lines[0]}')\n"
            "        print(f'CSV row:     {csv_lines[1]}')\n"
            "        print(f'JSON output: {json_output[:80]}...')\n"
            "        assert 'call_id' in csv_lines[0]\n"
            "        assert 'c001' in csv_lines[1]\n"
            "        assert 'c001' in json_output\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises `CallMetricsAccumulator`, `MetricsStore`, and export helpers.\n"
        ),
        _md("### CallMetricsAccumulator\n"),
        _code(
            "ft_metrics_accumulator",
            'import { CallMetricsAccumulator } from "getpatter";\n'
            "await cell('metrics_accumulator', { tier: 1, env }, () => {\n"
            "  const acc = new CallMetricsAccumulator({\n"
            "    callId: 'call-demo-001',\n"
            "    providerMode: 'openai_realtime',\n"
            "    telephonyProvider: 'twilio',\n"
            "  });\n"
            "  acc.startTurn();\n"
            "  acc.recordSttComplete('What is the weather?', 2.1);\n"
            "  acc.recordLlmFirstToken();\n"
            "  acc.recordTtsFirstByte();\n"
            "  const tm = acc.recordTurnComplete('The weather is sunny.');\n"
            "  console.log(`turn_index: ${tm.turnIndex}  tts_chars: ${tm.ttsCharacters}`);\n"
            "  console.log(`latency.total_ms: ${tm.latency.totalMs?.toFixed(0)}ms`);\n"
            "});\n",
        ),
        _md("### MetricsStore\n"),
        _code(
            "ft_metrics_store",
            'import { MetricsStore } from "getpatter";\n'
            "await cell('metrics_store', { tier: 1, env }, () => {\n"
            "  const store = new MetricsStore({ maxCalls: 100 });\n"
            "  store.recordCallStart({ callId: 'c001', from: '+15555550100', to: '+15555550200', direction: 'inbound', carrier: 'twilio', provider: 'openai_realtime' });\n"
            "  store.recordCallEnd({ callId: 'c001', duration: 45, costTotal: 0.08 });\n"
            "  const agg = store.getAggregates();\n"
            "  console.log(`total calls: ${agg.totalCalls}  avg_duration: ${agg.avgDuration}s`);\n"
            "});\n",
        ),
        _md("### Export\n"),
        _code(
            "ft_export",
            'import { MetricsStore, callsToCsv, callsToJson } from "getpatter";\n'
            "await cell('export', { tier: 1, env }, () => {\n"
            "  const store = new MetricsStore();\n"
            "  store.recordCallStart({ callId: 'c001', from: '+15555550100', to: '+15555550200', direction: 'inbound', carrier: 'twilio', provider: 'openai_realtime' });\n"
            "  store.recordCallEnd({ callId: 'c001', duration: 45, costTotal: 0.08 });\n"
            "  const calls = store.getCalls();\n"
            "  const csv  = callsToCsv(calls);\n"
            "  const json = callsToJson(calls);\n"
            "  console.log(`CSV header: ${csv.split('\\n')[0]}`);\n"
            "  console.log(`JSON: ${json.slice(0, 60)}...`);\n"
            "});\n",
        ),
    ]
