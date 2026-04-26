# Patter Notebook Tutorial Series

24 Jupyter notebooks (12 topics × Python + TypeScript) that walk through every public Patter feature and every supported provider. Three layers in every notebook:

- **Quickstart (T1+T2)** — offline. No API keys required. Runs in <30s.
- **Feature Tour (T1+T2+T3)** — real provider integrations. Per-key gated; missing keys auto-skip.
- **Live Appendix (T4)** — real PSTN calls, opt-in via `ENABLE_LIVE_CALLS=1`.

## Quickstart

```bash
cp examples/notebooks/.env.example examples/notebooks/.env
cd examples/notebooks/python
pip install -e ".[dev]"
jupyter lab 01_quickstart.ipynb
```

For TypeScript, install the Deno Jupyter kernel:

```bash
deno jupyter --install
cd examples/notebooks/typescript
npm install
jupyter lab 01_quickstart.ipynb
```

See `RELEASES.md` for the per-release run log.
