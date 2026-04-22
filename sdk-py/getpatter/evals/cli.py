"""Command-line entry point for Patter evals.

Invoked as ``patter eval run suite.yaml`` via the main CLI dispatcher
(see :mod:`getpatter.cli`).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Awaitable, Callable

from getpatter.evals.llm_judge import LLMJudge
from getpatter.evals.runner import EvalRunner, load_suite

logger = logging.getLogger("getpatter.evals.cli")


def build_eval_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``eval`` subcommand tree to a parent CLI."""
    eval_parser = subparsers.add_parser("eval", help="Run evaluation suites")
    eval_sub = eval_parser.add_subparsers(dest="eval_command")

    run = eval_sub.add_parser("run", help="Run an eval suite")
    run.add_argument("suite", type=Path, help="Path to a YAML or JSON suite file")
    run.add_argument(
        "--agent",
        type=str,
        default="",
        help="Dotted import path to an async agent factory: module:callable",
    )
    run.add_argument(
        "--judge-model", default="gpt-4o-mini", help="Model the LLM judge should use"
    )
    run.add_argument(
        "--pass-threshold", type=float, default=0.7, help="Score threshold for pass"
    )
    run.add_argument(
        "--output", type=Path, default=None, help="Write JSON report to this file"
    )
    return eval_parser


def dispatch_eval(args: argparse.Namespace) -> int:
    """Entry for ``patter eval ...``. Returns a process exit code."""
    if args.eval_command != "run":
        print("Usage: patter eval run <suite>")
        return 2
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite)

    judge = LLMJudge(
        model=args.judge_model,
        pass_threshold=args.pass_threshold,
    )
    runner = EvalRunner(judge=judge)

    factory = _load_agent_factory(args.agent)
    results = await runner.run(suite, factory)
    report = runner.report(suite, results)

    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"Wrote report to {args.output}")
    else:
        print(report)

    total = len(results)
    passed = sum(1 for r in results if r.judge.passed)
    print(f"\n{passed}/{total} cases passed", file=sys.stderr)
    return 0 if passed == total else 1


def _load_agent_factory(
    dotted: str,
) -> Callable[[], Callable[[str], Awaitable[str]]]:
    """Resolve a ``module:factory`` string to a callable.

    Falls back to an echo-style mock agent when no factory is provided — lets
    developers sanity-check their suite shape before wiring the real agent.
    """
    if not dotted:
        async def _echo(text: str) -> str:
            return f"echo: {text}"

        def _factory() -> Callable[[str], Awaitable[str]]:
            return _echo

        return _factory

    if ":" not in dotted:
        raise ValueError("--agent must be of the form 'module.path:attr_name'")
    module_path, attr = dotted.split(":", 1)
    mod = importlib.import_module(module_path)
    obj = getattr(mod, attr)
    if not callable(obj):
        raise TypeError(f"--agent target {dotted!r} is not callable")
    return obj  # type: ignore[return-value]


def main() -> None:
    """Standalone entry for ``python -m getpatter.evals.cli``."""
    parser = argparse.ArgumentParser(prog="patter-eval")
    subparsers = parser.add_subparsers(dest="command")
    build_eval_parser(subparsers)
    args = parser.parse_args()
    if args.command != "eval":
        parser.print_help()
        sys.exit(2)
    sys.exit(dispatch_eval(args))


if __name__ == "__main__":
    main()
