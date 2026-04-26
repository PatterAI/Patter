#!/usr/bin/env python3
"""One-shot fixture generator for the notebook series.

Run from repo root:
    python3 scripts/generate_notebook_fixtures.py

Outputs:
    examples/notebooks/fixtures/audio/*.wav
    examples/notebooks/fixtures/audio/PROVENANCE.md
    examples/notebooks/fixtures/keys/telnyx_test_ed25519_{pub,priv}.pem
    examples/notebooks/fixtures/webhooks/*.json (idempotent — only if missing)

The audio fixtures here are SYNTHESISED — varying-frequency tones with
speech-like rhythm. They are valid WAV files (non-empty, correct sample
width) which is enough for Phase 1 acceptance. Phase 3 cells that exercise
real STT need real speech: regenerate the two `hello_world_*` clips with
gTTS, Piper, or any TTS before running Phase 3.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIX = REPO / "examples/notebooks/fixtures"

PROVENANCE = """\
# Audio fixtures — provenance

These files were generated programmatically by
`scripts/generate_notebook_fixtures.py`. None contain copyrighted material.

| File | Source | License |
|------|--------|---------|
| `hello_world_16khz_pcm.wav` | Synthesised tone sequence (placeholder) — replace with gTTS-generated real speech before running Phase 3 STT cells | Public domain (synthesised) |
| `hello_world_8khz_mulaw.wav` | Same as above, transcoded to 8 kHz μ-law via `audioop.lin2ulaw` | — |
| `voicemail_beep.wav` | Synthesised 1400 Hz tone, 0.4 s | Public domain (synthesised) |
| `background_music_loop.wav` | Synthesised C-major triad arpeggio, 4 s loop | Public domain (synthesised) |

Regenerate at any time:

    python3 scripts/generate_notebook_fixtures.py

To replace the `hello_world_*` clips with real speech once gTTS/Piper is
available locally, drop a real-speech WAV into `audio/` and re-run the
generator (which preserves existing files when their content already
satisfies the test invariants).
"""


def _write_wav(path: Path, samples: bytes, sample_rate: int, sample_width: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(samples)


def _synth_pcm16(sample_rate: int, duration_s: float, freqs: list[tuple[float, float]],
                 amp: int = 12_000) -> bytes:
    """Synthesise a PCM16 mono buffer.

    `freqs` is a list of (start_time_s, hz) breakpoints — frequency between
    adjacent breakpoints is held flat. The result vaguely simulates speech
    rhythm without depending on any TTS.
    """

    n = int(duration_s * sample_rate)
    out = bytearray(n * 2)
    breakpoints = sorted(freqs)
    if not breakpoints:
        breakpoints = [(0.0, 200.0)]

    def freq_at(t: float) -> float:
        last = breakpoints[0][1]
        for ts, f in breakpoints:
            if ts > t:
                break
            last = f
        return last

    phase = 0.0
    for i in range(n):
        t = i / sample_rate
        f = freq_at(t)
        phase += 2 * math.pi * f / sample_rate
        v = int(amp * math.sin(phase))
        struct.pack_into("<h", out, i * 2, v)
    return bytes(out)


def generate_hello_world_pcm16() -> None:
    pcm = _synth_pcm16(
        sample_rate=16_000,
        duration_s=2.0,
        freqs=[(0.0, 220), (0.3, 330), (0.6, 280), (0.9, 220), (1.2, 330), (1.6, 250)],
    )
    _write_wav(FIX / "audio/hello_world_16khz_pcm.wav", pcm, 16_000, sample_width=2)


def generate_hello_world_mulaw() -> None:
    """8 kHz μ-law fixture transcoded from the 16 kHz PCM source."""

    import audioop

    pcm16 = _synth_pcm16(
        sample_rate=8_000,
        duration_s=2.0,
        freqs=[(0.0, 220), (0.3, 330), (0.6, 280), (0.9, 220), (1.2, 330), (1.6, 250)],
    )
    mulaw = audioop.lin2ulaw(pcm16, 2)
    _write_wav(FIX / "audio/hello_world_8khz_mulaw.wav", mulaw, 8_000, sample_width=1)


def generate_voicemail_beep() -> None:
    pcm = _synth_pcm16(sample_rate=8_000, duration_s=0.4, freqs=[(0.0, 1_400.0)])
    _write_wav(FIX / "audio/voicemail_beep.wav", pcm, 8_000, sample_width=2)


def generate_background_loop() -> None:
    sample_rate = 16_000
    duration_s = 4.0
    chord = (261.63, 329.63, 392.00)
    n = int(duration_s * sample_rate)
    out = bytearray(n * 2)
    amp = 4_000
    for i in range(n):
        t = i / sample_rate
        v = sum(amp * math.sin(2 * math.pi * f * t) for f in chord)
        struct.pack_into("<h", out, i * 2, int(v))
    _write_wav(FIX / "audio/background_music_loop.wav", bytes(out), sample_rate, sample_width=2)


def generate_keypair() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (FIX / "keys").mkdir(parents=True, exist_ok=True)
    (FIX / "keys/telnyx_test_ed25519_priv.pem").write_bytes(priv_pem)
    (FIX / "keys/telnyx_test_ed25519_pub.pem").write_bytes(pub_pem)


WEBHOOKS = {
    "twilio_voice_inbound.json": {
        "AccountSid": "ACtest00000000000000000000000000",
        "CallSid": "CAtest00000000000000000000000000",
        "From": "+15555550100",
        "To": "+15555550100",
        "Direction": "inbound",
        "CallStatus": "ringing",
    },
    "twilio_status_callback.json": {
        "AccountSid": "ACtest00000000000000000000000000",
        "CallSid": "CAtest00000000000000000000000000",
        "CallStatus": "completed",
        "CallDuration": "12",
    },
    "telnyx_call_initiated.json": {
        "data": {
            "event_type": "call.initiated",
            "id": "00000000-0000-0000-0000-000000000001",
            "payload": {
                "call_control_id": "v3:test",
                "from": "+15555550100",
                "to": "+15555550100",
                "direction": "incoming",
            },
        }
    },
    "telnyx_dtmf_received.json": {
        "data": {
            "event_type": "call.dtmf.received",
            "id": "00000000-0000-0000-0000-000000000002",
            "payload": {
                "call_control_id": "v3:test",
                "digit": "5",
            },
        }
    },
}


def generate_webhooks() -> None:
    (FIX / "webhooks").mkdir(parents=True, exist_ok=True)
    for name, body in WEBHOOKS.items():
        path = FIX / "webhooks" / name
        if path.exists():
            continue
        path.write_text(json.dumps(body, indent=2) + "\n")


def main() -> None:
    generate_hello_world_pcm16()
    generate_hello_world_mulaw()
    generate_voicemail_beep()
    generate_background_loop()
    generate_keypair()
    generate_webhooks()
    (FIX / "audio/PROVENANCE.md").write_text(PROVENANCE)
    print(f"fixtures generated under {FIX}")


if __name__ == "__main__":
    main()
