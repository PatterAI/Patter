from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quickstart_cells import quickstart_cells_python, quickstart_cells_typescript


def test_python_returns_named_cells():
    cells = quickstart_cells_python()
    tags = [
        c["metadata"].get("tags", [None])[0]
        for c in cells
        if c["cell_type"] == "code"
    ]
    assert "qs_version_check" in tags
    assert "qs_local_mode" in tags
    assert "qs_cloud_mode" in tags
    assert "qs_agent_engines" in tags


def test_python_cells_use_setup_cell_with_ok_pattern():
    cells = quickstart_cells_python()
    code = "".join("".join(c["source"]) for c in cells if c["cell_type"] == "code")
    assert "_setup.cell" in code
    assert "as ok:" in code
    assert "if ok:" in code


def test_python_imports_getpatter_not_patter():
    cells = quickstart_cells_python()
    code = "".join("".join(c["source"]) for c in cells if c["cell_type"] == "code")
    assert "from getpatter" in code
    assert "from patter import" not in code


def test_typescript_mirror_has_same_cell_count():
    py = [c for c in quickstart_cells_python() if c["cell_type"] == "code"]
    ts = [c for c in quickstart_cells_typescript() if c["cell_type"] == "code"]
    assert len(py) == len(ts)


def test_typescript_uses_camelcase_setup_calls():
    cells = quickstart_cells_typescript()
    code = "".join("".join(c["source"]) for c in cells if c["cell_type"] == "code")
    assert "cell(" in code
    assert "from \"getpatter\"" in code
