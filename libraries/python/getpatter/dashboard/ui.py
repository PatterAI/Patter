"""Dashboard HTML — loaded from the bundled ``ui.html`` asset.

The actual UI is a Vite + React SPA living at the repo root in
``dashboard-app/``. ``npm run build && npm run sync`` from that directory
emits a single self-contained HTML file (JS + CSS inlined by
``vite-plugin-singlefile``) and copies it next to this module as
``ui.html``. The file is shipped inside the wheel via the
``[tool.setuptools.package-data]`` entry in ``libraries/python/pyproject.toml``.

Keeping the asset on disk means the SDK serves the real UI without having
to embed a multi-hundred-KB string literal in source — and the same file
ships in both SDKs (Python + TypeScript) from a single source of truth.
"""

from __future__ import annotations

from importlib import resources

_FALLBACK_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Patter dashboard</title></head>
<body style="font-family:ui-sans-serif,system-ui;padding:2rem;color:#1a1a1a">
<h1>Dashboard asset missing</h1>
<p>The bundled <code>ui.html</code> was not found alongside this module.
Run <code>cd dashboard-app &amp;&amp; npm run build &amp;&amp; npm run sync</code>
from the repo root to regenerate it.</p>
</body></html>"""


def _load_dashboard_html() -> str:
    try:
        return (
            resources.files("getpatter.dashboard")
            .joinpath("ui.html")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        return _FALLBACK_HTML


DASHBOARD_HTML: str = _load_dashboard_html()
