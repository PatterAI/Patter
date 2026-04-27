from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scaffold_notebook import build_notebook, KERNELS


def test_python_scaffold_has_three_sections():
    nb = build_notebook(
        topic_id="01", title="Quickstart", language="python", brief="Hello world."
    )
    headings = [c["source"][0] for c in nb["cells"] if c["cell_type"] == "markdown"]
    assert any("§1: Quickstart" in h for h in headings)
    assert any("§2: Feature Tour" in h for h in headings)
    assert any("§3: Live Appendix" in h for h in headings)


def test_python_scaffold_imports_setup():
    nb = build_notebook(topic_id="01", title="Quickstart", language="python", brief="x")
    code_cells = [c["source"] for c in nb["cells"] if c["cell_type"] == "code"]
    assert any("import _setup" in "".join(c) for c in code_cells)


def test_typescript_uses_deno_kernel():
    nb = build_notebook(
        topic_id="01", title="Quickstart", language="typescript", brief="x"
    )
    assert nb["metadata"]["kernelspec"]["name"] == KERNELS["typescript"]["name"]
    assert "deno" in nb["metadata"]["kernelspec"]["display_name"].lower()


def test_python_uses_python_kernel():
    nb = build_notebook(topic_id="01", title="Quickstart", language="python", brief="x")
    assert nb["metadata"]["kernelspec"]["language"] == "python"


def test_outputs_empty():
    nb = build_notebook(topic_id="01", title="Quickstart", language="python", brief="x")
    for c in nb["cells"]:
        if c["cell_type"] == "code":
            assert c["outputs"] == []
            assert c["execution_count"] is None
