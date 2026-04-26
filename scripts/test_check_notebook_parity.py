from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_notebook_parity import diff_pair


def _write(path: Path, sections: list[str]) -> None:
    cells = [
        {"cell_type": "markdown", "metadata": {}, "source": [s + "\n"]}
        for s in sections
    ]
    path.write_text(
        json.dumps(
            {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
        )
    )


def test_diff_pair_returns_empty_when_aligned(tmp_path):
    py = tmp_path / "01.py.ipynb"
    ts = tmp_path / "01.ts.ipynb"
    _write(py, ["# 01 — Quickstart", "## §1", "## §2"])
    _write(ts, ["# 01 — Quickstart", "## §1", "## §2"])
    assert diff_pair(py, ts) == []


def test_diff_pair_detects_extra_section(tmp_path):
    py = tmp_path / "01.py.ipynb"
    ts = tmp_path / "01.ts.ipynb"
    _write(py, ["# Title", "## §1", "## §2", "## §3"])
    _write(ts, ["# Title", "## §1", "## §2"])
    diffs = diff_pair(py, ts)
    assert any("§3" in d for d in diffs)


def test_diff_pair_detects_renamed_section(tmp_path):
    py = tmp_path / "01.py.ipynb"
    ts = tmp_path / "01.ts.ipynb"
    _write(py, ["# Title", "## §1", "## §2"])
    _write(ts, ["# Title", "## §1: Quickstart", "## §2"])
    diffs = diff_pair(py, ts)
    assert any("§1" in d for d in diffs)
