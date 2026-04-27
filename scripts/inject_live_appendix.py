"""Phase 4 driver: inject §3 (Live Appendix) cells into all 24 notebooks."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
PY_DIR = ROOT / "examples" / "notebooks" / "python"
TS_DIR = ROOT / "examples" / "notebooks" / "typescript"
SCRIPTS = Path(__file__).parent

sys.path.insert(0, str(SCRIPTS))

TOPICS = [
    ("01", "01_quickstart"),
    ("02", "02_realtime"),
    ("03", "03_pipeline_stt"),
    ("04", "04_pipeline_tts"),
    ("05", "05_pipeline_llm"),
    ("06", "06_telephony_twilio"),
    ("07", "07_telephony_telnyx"),
    ("08", "08_tools"),
    ("09", "09_guardrails_hooks"),
    ("10", "10_advanced"),
    ("11", "11_metrics_dashboard"),
    ("12", "12_security"),
]

SECTION_MARKER = "## §3"


def main() -> None:
    from inject_section import inject_section

    for num, nb_stem in TOPICS:
        mod_name = f"appendix_cells_{num}"
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError:
            print(f"  skip {mod_name} (not found)")
            continue

        py_nb = PY_DIR / f"{nb_stem}.ipynb"
        ts_nb = TS_DIR / f"{nb_stem}.ipynb"

        cells_py = mod.section_cells_python()
        cells_ts = mod.section_cells_typescript()

        inject_section(py_nb, SECTION_MARKER, cells_py)
        print(f"  injected py §3 → {nb_stem}.ipynb ({len(cells_py)} cells)")

        inject_section(ts_nb, SECTION_MARKER, cells_ts)
        print(f"  injected ts §3 → {nb_stem}.ipynb ({len(cells_ts)} cells)")


if __name__ == "__main__":
    main()
