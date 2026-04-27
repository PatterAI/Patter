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
