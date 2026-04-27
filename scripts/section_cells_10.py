"""§2 Feature Tour cells — 10 Advanced."""

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
            "Exercises scheduler, IVR/DTMF, background audio, and the EventBus.\n"
        ),
        _md("### Scheduler\n"),
        _code(
            "ft_scheduler",
            "from datetime import datetime, timedelta, timezone\n"
            "from getpatter import schedule_once, schedule_interval, schedule_cron, ScheduleHandle\n"
            "with _setup.cell('scheduler', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        fired: list[str] = []\n"
            "\n"
            "        h1 = schedule_once(\n"
            "            datetime.now(tz=timezone.utc) + timedelta(hours=1),\n"
            "            lambda: fired.append('once'),\n"
            "        )\n"
            "        h2 = schedule_interval(300, lambda: fired.append('interval'))\n"
            "        h3 = schedule_cron('0 9 * * 1-5', lambda: fired.append('cron'))\n"
            "\n"
            "        print(f'once     job_id: {h1.job_id[:20]}...')\n"
            "        print(f'interval job_id: {h2.job_id[:20]}...')\n"
            "        print(f'cron     job_id: {h3.job_id[:20]}...')\n"
            "\n"
            "        h1.cancel()\n"
            "        h2.cancel()\n"
            "        h3.cancel()\n"
            "        print('All handles cancelled — no callbacks should fire')\n"
            "        assert isinstance(h1, ScheduleHandle)\n",
        ),
        _md("### IVR / DTMF\n"),
        _code(
            "ft_ivr_dtmf",
            "from getpatter import DtmfEvent, format_dtmf\n"
            "with _setup.cell('ivr_dtmf', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        # Simulate caller pressing 1-800-555-0100 on their keypad.\n"
            "        sequence = [\n"
            "            DtmfEvent.ONE, DtmfEvent.EIGHT, DtmfEvent.ZERO, DtmfEvent.ZERO,\n"
            "            DtmfEvent.FIVE, DtmfEvent.FIVE, DtmfEvent.FIVE,\n"
            "            DtmfEvent.ZERO, DtmfEvent.ONE, DtmfEvent.ZERO, DtmfEvent.ZERO,\n"
            "        ]\n"
            "        formatted = format_dtmf(sequence)\n"
            "        print(f'DTMF digits:    {[e.value for e in sequence]}')\n"
            "        print(f'format_dtmf:    {formatted!r}')\n"
            "        print(f'Available events: {[e.name for e in DtmfEvent]}')\n"
            "        assert formatted == '1 8 0 0 5 5 5 0 1 0 0'\n",
        ),
        _md("### Background audio\n"),
        _code(
            "ft_background_audio",
            "from getpatter import BackgroundAudioPlayer, BuiltinAudioClip\n"
            "with _setup.cell('background_audio', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        print('Built-in audio clips:')\n"
            "        for clip in BuiltinAudioClip:\n"
            "            print(f'  {clip.name:20s} → {clip.value}')\n"
            "\n"
            "        player = BackgroundAudioPlayer(\n"
            "            source=BuiltinAudioClip.HOLD_MUSIC,\n"
            "            volume=0.15,\n"
            "            loop=True,\n"
            "        )\n"
            "        print(f'\\nPlayer constructed: source={player.source}  volume={player.volume}  loop={player.loop}')\n"
            "        assert player.volume == 0.15\n"
            "        assert player.loop is True\n",
        ),
        _md("### EventBus\n"),
        _code(
            "ft_event_bus",
            "from getpatter import EventBus\n"
            "with _setup.cell('event_bus', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        bus = EventBus()\n"
            "        received: list[dict] = []\n"
            "\n"
            "        unsubscribe = bus.on('call_ended', lambda payload: received.append(payload))\n"
            "        bus.emit('call_ended', {'call_id': 'c001', 'duration': 45})\n"
            "        bus.emit('call_ended', {'call_id': 'c002', 'duration': 120})\n"
            "\n"
            "        print(f'Received {len(received)} events:')\n"
            "        for evt in received:\n"
            '            print(f\'  call_id={evt["call_id"]}  duration={evt["duration"]}s\')\n'
            "\n"
            "        unsubscribe()  # stop listening\n"
            "        bus.emit('call_ended', {'call_id': 'c003'})\n"
            "        print(f'After unsubscribe: still {len(received)} events (c003 not received)')\n"
            "        assert len(received) == 2\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises scheduler, IVR/DTMF, background audio, and the EventBus.\n"
        ),
        _md("### Scheduler\n"),
        _code(
            "ft_scheduler",
            'import { scheduleOnce, scheduleInterval, scheduleCron } from "getpatter";\n'
            "await cell('scheduler', { tier: 1, env }, () => {\n"
            "  const h1 = scheduleOnce(new Date(Date.now() + 3_600_000), () => {});\n"
            "  const h2 = scheduleInterval(300, () => {});\n"
            "  const h3 = scheduleCron('0 9 * * 1-5', () => {});\n"
            "  console.log(`once:     ${h1.jobId.slice(0, 20)}...`);\n"
            "  console.log(`interval: ${h2.jobId.slice(0, 20)}...`);\n"
            "  console.log(`cron:     ${h3.jobId.slice(0, 20)}...`);\n"
            "  h1.cancel(); h2.cancel(); h3.cancel();\n"
            "  console.log('All handles cancelled');\n"
            "});\n",
        ),
        _md("### IVR / DTMF\n"),
        _code(
            "ft_ivr_dtmf",
            'import { DtmfEvent, formatDtmf } from "getpatter";\n'
            "await cell('ivr_dtmf', { tier: 1, env }, () => {\n"
            "  const seq = [DtmfEvent.ONE, DtmfEvent.EIGHT, DtmfEvent.ZERO, DtmfEvent.ZERO];\n"
            "  console.log(`digits:   ${seq.map(e => e.value || e)}`);\n"
            "  console.log(`formatted: ${formatDtmf(seq)}`);\n"
            "});\n",
        ),
        _md("### Background audio\n"),
        _code(
            "ft_background_audio",
            'import { BackgroundAudioPlayer, BuiltinAudioClip } from "getpatter";\n'
            "await cell('background_audio', { tier: 1, env }, () => {\n"
            "  console.log('Built-in clips:', Object.values(BuiltinAudioClip));\n"
            "  const player = new BackgroundAudioPlayer(BuiltinAudioClip.HOLD_MUSIC, { volume: 0.15, loop: true });\n"
            "  console.log(`Player: volume=${player.volume}  loop=${player.loop}`);\n"
            "});\n",
        ),
        _md("### EventBus\n"),
        _code(
            "ft_event_bus",
            'import { EventBus } from "getpatter";\n'
            "await cell('event_bus', { tier: 1, env }, () => {\n"
            "  const bus = new EventBus();\n"
            "  const received: any[] = [];\n"
            "  const unsub = bus.on('call_ended', (p: any) => received.push(p));\n"
            "  bus.emit('call_ended', { callId: 'c001', duration: 45 });\n"
            "  bus.emit('call_ended', { callId: 'c002', duration: 120 });\n"
            "  console.log(`Received ${received.length} events`);\n"
            "  unsub();\n"
            "  bus.emit('call_ended', { callId: 'c003' });\n"
            "  console.log(`After unsub: ${received.length} (c003 not received)`);\n"
            "  if (received.length !== 2) throw new Error('expected 2 events');\n"
            "});\n",
        ),
    ]
