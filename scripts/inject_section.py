"""Idempotently inject a list of cells after a section marker in a .ipynb."""

from __future__ import annotations

import json
import secrets
from pathlib import Path


def _ensure_ids(cells: list[dict]) -> list[dict]:
    """Add a random 8-char hex id to any cell missing one (nbformat ≥5.1 req)."""
    result = []
    for cell in cells:
        if "id" not in cell:
            cell = {**cell, "id": secrets.token_hex(4)}
        result.append(cell)
    return result


def _next_section_idx(cells: list[dict], after: int, current_marker: str) -> int:
    """Return the index of the next top-level section cell that does NOT share
    the same section prefix as ``current_marker``.

    E.g. if current_marker is '## §2', cells starting '## §2' are skipped
    (they are part of the injected content) and the search continues until a
    different top-level section is found.  Returns len(cells) if none found.
    """
    for i in range(after + 1, len(cells)):
        c = cells[i]
        if not (c["cell_type"] == "markdown" and c["source"]):
            continue
        first = c["source"][0]
        if first.startswith("## ") and not first.startswith(current_marker):
            return i
    return len(cells)


def inject_section(nb_path: Path, marker: str, cells: list[dict]) -> None:
    """Replace everything between ``marker`` and the next top-level '## ' heading.

    Idempotent: re-running the injector replaces the entire old section
    (including stale markdown headings) with the new cells.
    """
    cells = _ensure_ids(cells)
    nb = json.loads(nb_path.read_text())

    inject_idx = -1
    for i, c in enumerate(nb["cells"]):
        if c["cell_type"] == "markdown" and any(marker in line for line in c["source"]):
            inject_idx = i
            break
    if inject_idx == -1:
        raise ValueError(f"marker {marker!r} not found in {nb_path}")

    end_idx = _next_section_idx(nb["cells"], inject_idx, marker)

    nb["cells"] = nb["cells"][: inject_idx + 1] + cells + nb["cells"][end_idx:]
    nb_path.write_text(json.dumps(nb, indent=1) + "\n")
