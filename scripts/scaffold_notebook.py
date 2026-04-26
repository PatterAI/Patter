"""Emit an empty .ipynb with §1 / §2 / §3 markdown headers and the
shared _setup import cell.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

KERNELS = {
    "python": {"name": "python3", "display_name": "Python 3", "language": "python"},
    "typescript": {"name": "deno", "display_name": "Deno", "language": "typescript"},
}


def _md(*lines: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}


def _code(source: str) -> dict:
    return {
        "cell_type": "code",
        "metadata": {},
        "source": source.splitlines(keepends=True),
        "execution_count": None,
        "outputs": [],
    }


def _setup_cell_python() -> dict:
    return _code(
        "%load_ext autoreload\n"
        "%autoreload 2\n"
        "\n"
        "import _setup\n"
        "env = _setup.load()\n"
        "print(f'getpatter version target: {env.patter_version}')\n"
    )


def _setup_cell_typescript() -> dict:
    return _code(
        'import { load } from "./_setup.ts";\n'
        "const env = load();\n"
        "console.log(`getpatter version target: ${env.patterVersion}`);\n"
    )


def build_notebook(*, topic_id: str, title: str, language: str, brief: str) -> dict:
    setup_cell = (
        _setup_cell_python() if language == "python" else _setup_cell_typescript()
    )
    cells = [
        _md(f"# {topic_id} — {title}\n", "\n", brief, "\n"),
        _md(
            "## Prerequisites\n",
            "\n",
            "| Tier | Cells | Required env |\n",
            "|------|-------|--------------|\n",
            "| T1+T2 (§1) | always | _none_ |\n",
            "| T3 (§2) | per-cell | provider keys auto-detected |\n",
            "| T4 (§3) | gated | `ENABLE_LIVE_CALLS=1` + carrier creds |\n",
        ),
        setup_cell,
        _md("## §1: Quickstart\n\nRuns end-to-end with zero API keys.\n"),
        _md(
            "## §2: Feature Tour\n\nOne cell per feature/provider. "
            "Missing keys auto-skip.\n"
        ),
        _md(
            "## §3: Live Appendix\n\nReal PSTN calls. Off by default — "
            "set `ENABLE_LIVE_CALLS=1`.\n"
        ),
    ]
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": KERNELS[language],
            "language_info": {"name": language},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--topic-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--language", choices=["python", "typescript"], required=True)
    p.add_argument("--brief", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()

    nb = build_notebook(
        topic_id=args.topic_id,
        title=args.title,
        language=args.language,
        brief=args.brief,
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(nb, indent=1) + "\n")


if __name__ == "__main__":
    main()
