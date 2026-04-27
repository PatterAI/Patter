"""Idempotently inject a list of cells after a section marker in a .ipynb."""

from __future__ import annotations

import json
from pathlib import Path


def inject_section(nb_path: Path, marker: str, cells: list[dict]) -> None:
    """Insert ``cells`` immediately after the markdown cell containing ``marker``.

    Idempotent via tag dedup: if any cell with one of the incoming cells'
    tags already exists in the notebook, it is removed first (so re-running
    the injector keeps a single copy).
    """
    nb = json.loads(nb_path.read_text())
    inject_idx = -1
    for i, c in enumerate(nb["cells"]):
        if c["cell_type"] == "markdown" and any(marker in line for line in c["source"]):
            inject_idx = i
            break
    if inject_idx == -1:
        raise ValueError(f"marker {marker!r} not found in {nb_path}")

    incoming_tags = {
        tag
        for cell in cells
        for tag in cell.get("metadata", {}).get("tags", [])
    }

    out: list[dict] = []
    for i, c in enumerate(nb["cells"]):
        if any(tag in incoming_tags for tag in c.get("metadata", {}).get("tags", [])):
            continue
        out.append(c)
        if i == inject_idx:
            out.extend(cells)

    nb["cells"] = out
    nb_path.write_text(json.dumps(nb, indent=1) + "\n")
