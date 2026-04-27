from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inject_section import inject_section


def _scaffold(path: Path) -> None:
    cells = [
        {"cell_type": "markdown", "metadata": {}, "source": ["# Title\n"]},
        {"cell_type": "markdown", "metadata": {}, "source": ["## §1: Quickstart\n"]},
        {"cell_type": "markdown", "metadata": {}, "source": ["## §2: Feature Tour\n"]},
    ]
    path.write_text(
        json.dumps({"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
    )


def test_inject_section_inserts_after_marker(tmp_path):
    nb_path = tmp_path / "01.ipynb"
    _scaffold(nb_path)
    cell = {
        "cell_type": "code",
        "metadata": {"tags": ["t1"]},
        "source": ["print('hi')\n"],
        "execution_count": None,
        "outputs": [],
    }
    inject_section(nb_path, marker="§1: Quickstart", cells=[cell])
    nb = json.loads(nb_path.read_text())
    sources = [c["source"] for c in nb["cells"]]
    assert sources == [
        ["# Title\n"],
        ["## §1: Quickstart\n"],
        ["print('hi')\n"],
        ["## §2: Feature Tour\n"],
    ]


def test_inject_section_idempotent(tmp_path):
    nb_path = tmp_path / "01.ipynb"
    _scaffold(nb_path)
    cell = {
        "cell_type": "code",
        "metadata": {"tags": ["t1"]},
        "source": ["x"],
        "execution_count": None,
        "outputs": [],
    }
    inject_section(nb_path, marker="§1: Quickstart", cells=[cell])
    inject_section(nb_path, marker="§1: Quickstart", cells=[cell])
    nb = json.loads(nb_path.read_text())
    tag_count = sum(
        1 for c in nb["cells"] if "t1" in c.get("metadata", {}).get("tags", [])
    )
    assert tag_count == 1


def test_inject_section_raises_on_missing_marker(tmp_path):
    nb_path = tmp_path / "01.ipynb"
    _scaffold(nb_path)
    import pytest
    with pytest.raises(ValueError, match="marker"):
        inject_section(nb_path, marker="DOES_NOT_EXIST", cells=[])
