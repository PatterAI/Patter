"""``getpatter init`` — greenfield setup wizard for the Patter SDK.

Mirrors the ``getpatter.evals.cli`` registration pattern: this package exposes
:func:`build_init_parser` (attaches the ``init`` subcommand to the main CLI's
subparsers) and :func:`dispatch_init` (runs the wizard, returns an exit code).
The wizard scaffolds a runnable inbound voice agent project — choosing a voice
mode (``realtime`` or ``pipeline``), engine/providers, telephony carrier — and
writes ``main.py`` (Python) or ``src/index.ts`` (TypeScript), ``.env`` (chmod
0600), ``.env.example``, dependency manifest, ``.gitignore``, and ``README.md``.

The TypeScript SDK ships a byte-for-byte behavioural mirror in
``libraries/typescript/src/init/`` — keep the prompt strings, flag names,
default values, and scaffold codegen rules in sync (see ``cli.py`` module
docstring for the parity contract).
"""

from __future__ import annotations

from getpatter.init.cli import build_init_parser, dispatch_init

__all__ = ["build_init_parser", "dispatch_init"]
