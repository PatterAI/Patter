"""Phase 2: inject the canonical §1 cells into every scaffolded notebook."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from inject_section import inject_section
from quickstart_cells import quickstart_cells_python, quickstart_cells_typescript

REPO = HERE.parent


def main() -> None:
    py_cells = quickstart_cells_python()
    ts_cells = quickstart_cells_typescript()

    for nb in sorted((REPO / "examples/notebooks/python").glob("[0-9][0-9]_*.ipynb")):
        print(f"injecting py §1 into {nb.name}")
        inject_section(nb, marker="§1: Quickstart", cells=py_cells)

    for nb in sorted(
        (REPO / "examples/notebooks/typescript").glob("[0-9][0-9]_*.ipynb")
    ):
        print(f"injecting ts §1 into {nb.name}")
        inject_section(nb, marker="§1: Quickstart", cells=ts_cells)


if __name__ == "__main__":
    main()
