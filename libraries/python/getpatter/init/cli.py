"""``getpatter init`` — greenfield setup wizard (Python reference impl).

Invoked as ``getpatter init [options]`` via the main CLI dispatcher
(see :mod:`getpatter.cli`). Mirrors the ``eval`` registration shape:
:func:`build_init_parser` attaches the subcommand, :func:`dispatch_init`
runs it, and the actual work happens in an ``async`` ``_run`` coroutine.

PARITY CONTRACT (TypeScript port in ``libraries/typescript/src/init/`` must
match this file byte-for-byte in *behaviour*):

* Prompt strings, the trailing default hint in brackets, and the numbered
  menu rendering are identical (snake_case → camelCase only where a flag /
  field name is shown).
* Flag names map snake_case (Python ``--skip-keys``) ↔ kebab-case is shared
  (both SDKs use ``--skip-keys``); the parsed attribute differs by language
  idiom but the CLI surface is identical.
* Default selections: mode=realtime, realtime engine=OpenAIRealtime2,
  pipeline stt=Deepgram / llm=Cerebras / tts=ElevenLabs, carrier=twilio,
  phone placeholder ``+15550001234``.
* Scaffold codegen rules (import line, ``Patter(...)`` call shape, agent
  call shape, ``.env`` key set, requirements/package manifest, .gitignore,
  README) are defined here as pure string builders so the TS port can
  replicate them exactly.

Security: API keys are optional, written to ``.env`` with mode ``0o600``,
listed in ``.gitignore``, and NEVER echoed to stdout or logged.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import shutil
import subprocess  # noqa: S404 — used with arg arrays only, never shell=True
import sys
from pathlib import Path

from getpatter import __version__

logger = logging.getLogger("getpatter.init")

# Placeholder phone number — NANP 555 fiction range (see security.md). Never a
# real number. Used when the user does not supply one via --phone / prompt.
DEFAULT_PHONE = "+15550001234"

# Valid IDE targets for the skills shell-out (`npx skills add ... -a <ide>`).
IDE_CHOICES = ("claude-code", "cursor", "copilot", "codex")


# ---------------------------------------------------------------------------
# Provider matrix — the single source of truth the scaffold builders read.
# Each entry: key -> (class_name, env_vars tuple, pip_extra or "").
# class_name is the flat alias importable from ``getpatter`` in BOTH SDKs.
# ---------------------------------------------------------------------------

# Realtime engines (mode=realtime — pick ONE). Order = menu order.
REALTIME_ENGINES: dict[str, dict] = {
    "OpenAIRealtime2": {
        "class": "OpenAIRealtime2",
        "label": "OpenAI Realtime 2 (gpt-realtime-2) — default",
        "env": ("OPENAI_API_KEY",),
        "extra": "",
        "ctor": "OpenAIRealtime2()",
    },
    "OpenAIRealtime": {
        "class": "OpenAIRealtime",
        "label": "OpenAI Realtime (legacy gpt-realtime-mini)",
        "env": ("OPENAI_API_KEY",),
        "extra": "",
        "ctor": "OpenAIRealtime()",
    },
    "ElevenLabsConvAI": {
        "class": "ElevenLabsConvAI",
        "label": "ElevenLabs ConvAI (needs ELEVENLABS_AGENT_ID)",
        "env": ("ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID"),
        "extra": "",
        # agent_id is REQUIRED — read from ELEVENLABS_AGENT_ID env at construct.
        "ctor": "ElevenLabsConvAI()",
    },
}
DEFAULT_REALTIME_ENGINE = "OpenAIRealtime2"

# Pipeline STT providers.
PIPELINE_STT: dict[str, dict] = {
    "deepgram": {
        "class": "DeepgramSTT",
        "label": "Deepgram — default",
        "env": ("DEEPGRAM_API_KEY",),
        "extra": "",
    },
    "assemblyai": {
        "class": "AssemblyAISTT",
        "label": "AssemblyAI",
        "env": ("ASSEMBLYAI_API_KEY",),
        "extra": "assemblyai",
    },
    "cartesia": {
        "class": "CartesiaSTT",
        "label": "Cartesia",
        "env": ("CARTESIA_API_KEY",),
        "extra": "cartesia",
    },
    "soniox": {
        "class": "SonioxSTT",
        "label": "Soniox",
        "env": ("SONIOX_API_KEY",),
        "extra": "soniox",
    },
    "speechmatics": {
        "class": "SpeechmaticsSTT",
        "label": "Speechmatics",
        "env": ("SPEECHMATICS_API_KEY",),
        "extra": "speechmatics",
    },
    "whisper": {
        "class": "WhisperSTT",
        "label": "OpenAI Whisper",
        "env": ("OPENAI_API_KEY",),
        "extra": "",
    },
    "openai": {
        "class": "OpenAITranscribeSTT",
        "label": "OpenAI Transcribe",
        "env": ("OPENAI_API_KEY",),
        "extra": "",
    },
    "fish_audio": {
        "class": "FishAudioSTT",
        "label": "Fish Audio (batch — higher latency)",
        "env": ("FISH_AUDIO_API_KEY",),
        "extra": "fish_audio",
    },
    "xai": {
        "class": "XaiSTT",
        "label": "xAI Grok",
        "env": ("XAI_API_KEY",),
        "extra": "xai",
    },
    "telnyx": {
        "class": "TelnyxSTT",
        "label": "Telnyx (on-network — telnyx/google/deepgram/azure)",
        "env": ("TELNYX_API_KEY",),
        "extra": "telnyx-ai",
    },
}
DEFAULT_STT = "deepgram"

# Pipeline LLM providers.
PIPELINE_LLM: dict[str, dict] = {
    "cerebras": {
        "class": "CerebrasLLM",
        "label": "Cerebras (gpt-oss-120b) — default",
        "env": ("CEREBRAS_API_KEY",),
        "extra": "cerebras",
    },
    "openai": {
        "class": "OpenAILLM",
        "label": "OpenAI",
        "env": ("OPENAI_API_KEY",),
        "extra": "",
    },
    "anthropic": {
        "class": "AnthropicLLM",
        "label": "Anthropic Claude",
        "env": ("ANTHROPIC_API_KEY",),
        "extra": "anthropic",
    },
    "groq": {
        "class": "GroqLLM",
        "label": "Groq",
        "env": ("GROQ_API_KEY",),
        "extra": "groq",
    },
    "google": {
        "class": "GoogleLLM",
        "label": "Google Gemini",
        "env": ("GOOGLE_API_KEY",),
        "extra": "google",
    },
}
DEFAULT_LLM = "cerebras"

# Pipeline TTS providers.
PIPELINE_TTS: dict[str, dict] = {
    "elevenlabs": {
        "class": "ElevenLabsTTS",
        "label": "ElevenLabs — default",
        "env": ("ELEVENLABS_API_KEY",),
        "extra": "",
    },
    "openai": {
        "class": "OpenAITTS",
        "label": "OpenAI",
        "env": ("OPENAI_API_KEY",),
        "extra": "",
    },
    "cartesia": {
        "class": "CartesiaTTS",
        "label": "Cartesia",
        "env": ("CARTESIA_API_KEY",),
        "extra": "cartesia",
    },
    "rime": {
        "class": "RimeTTS",
        "label": "Rime",
        "env": ("RIME_API_KEY",),
        "extra": "rime",
    },
    "lmnt": {
        "class": "LMNTTTS",
        "label": "LMNT",
        "env": ("LMNT_API_KEY",),
        "extra": "lmnt",
    },
    "inworld": {
        "class": "InworldTTS",
        "label": "Inworld",
        "env": ("INWORLD_API_KEY",),
        "extra": "inworld",
    },
    "fish_audio": {
        "class": "FishAudioTTS",
        "label": "Fish Audio (s2.1-pro)",
        "env": ("FISH_AUDIO_API_KEY",),
        "extra": "fish_audio",
    },
    "xai": {
        "class": "XaiTTS",
        "label": "xAI Grok",
        "env": ("XAI_API_KEY",),
        "extra": "xai",
    },
    "telnyx": {
        "class": "TelnyxTTS",
        "label": "Telnyx (NaturalHD — on-network)",
        "env": ("TELNYX_API_KEY",),
        "extra": "telnyx-ai",
    },
}
DEFAULT_TTS = "elevenlabs"

# Telephony carriers.
CARRIERS: dict[str, dict] = {
    "twilio": {
        "class": "Twilio",
        "label": "Twilio — default",
        "env": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
        "phone_env": "TWILIO_PHONE_NUMBER",
    },
    "telnyx": {
        "class": "Telnyx",
        "label": "Telnyx",
        "env": ("TELNYX_API_KEY", "TELNYX_CONNECTION_ID"),
        "phone_env": "TELNYX_PHONE_NUMBER",
    },
    "plivo": {
        "class": "Plivo",
        "label": "Plivo",
        "env": ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"),
        "phone_env": "PLIVO_PHONE_NUMBER",
    },
}
DEFAULT_CARRIER = "twilio"


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def build_init_parser(
    subparsers: argparse._SubParsersAction,
) -> argparse.ArgumentParser:
    """Attach the ``init`` subcommand to a parent CLI (mirror of ``eval``)."""
    p = subparsers.add_parser(
        "init",
        help="Scaffold a new Patter voice-agent project (setup wizard)",
    )
    p.add_argument("--name", default=None, help="Project name → target directory")
    p.add_argument(
        "--runtime",
        choices=("python", "typescript"),
        default=None,
        help="SDK runtime (default: python for this CLI)",
    )
    p.add_argument(
        "--mode",
        choices=("realtime", "pipeline"),
        default=None,
        help="Voice mode: realtime (one engine) or pipeline (STT+LLM+TTS)",
    )
    p.add_argument(
        "--engine",
        choices=tuple(REALTIME_ENGINES),
        default=None,
        help="Realtime engine (realtime mode only)",
    )
    p.add_argument(
        "--stt",
        choices=tuple(PIPELINE_STT),
        default=None,
        help="STT provider (pipeline mode only)",
    )
    p.add_argument(
        "--llm",
        choices=tuple(PIPELINE_LLM),
        default=None,
        help="LLM provider (pipeline mode only)",
    )
    p.add_argument(
        "--tts",
        choices=tuple(PIPELINE_TTS),
        default=None,
        help="TTS provider (pipeline mode only)",
    )
    p.add_argument(
        "--carrier", choices=tuple(CARRIERS), default=None, help="Telephony carrier"
    )
    p.add_argument(
        "--phone",
        default=None,
        help=f"Phone number in E.164 (placeholder {DEFAULT_PHONE})",
    )
    p.add_argument(
        "--skip-keys",
        action="store_true",
        help="Do not prompt for API keys; write placeholders to .env",
    )
    p.add_argument(
        "--ide",
        default=None,
        help=(f"Comma-separated IDE skills targets ({', '.join(IDE_CHOICES)})"),
    )
    p.add_argument(
        "--no-skills", action="store_true", help="Skip the IDE skills install step"
    )
    p.add_argument(
        "--no-git", action="store_true", help="Skip 'git init' in the new project"
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Allow scaffolding into a non-empty directory",
    )
    p.add_argument(
        "--yes", "-y", action="store_true", help="Non-interactive: accept all defaults"
    )
    return p


def dispatch_init(args: argparse.Namespace) -> int:
    """Entry for ``getpatter init ...``. Returns a process exit code."""
    try:
        return asyncio.run(_run(args))
    except InitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------------------
# Prompt helpers (stdlib input() only — no third-party prompt libs)
# ---------------------------------------------------------------------------


def _ask_text(prompt: str, default: str) -> str:
    """Free-text prompt with a default shown in brackets."""
    raw = input(f"{prompt} [{default}]: ").strip()
    return raw or default


def _ask_choice(prompt: str, options: list[tuple[str, str]], default_key: str) -> str:
    """Numbered menu. ``options`` is [(key, label)]. Returns the chosen key.

    Empty input picks the default. Accepts either the 1-based number or the
    literal key. Re-prompts on invalid input.
    """
    keys = [k for k, _ in options]
    default_idx = keys.index(default_key) + 1
    print(prompt)
    for i, (key, label) in enumerate(options, start=1):
        marker = " (default)" if key == default_key else ""
        print(f"  {i}) {label}{marker}")
    while True:
        raw = input(f"Choose [1-{len(options)}, default {default_idx}]: ").strip()
        if not raw:
            return default_key
        if raw in keys:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return keys[int(raw) - 1]
        print(
            f"  Invalid choice {raw!r} — enter a number 1-{len(options)} "
            f"or a name ({', '.join(keys)})."
        )


def _ask_yes_no(prompt: str, default: bool) -> bool:
    """Yes/no prompt. Empty input picks the default."""
    hint = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _ask_multi(prompt: str, options: list[tuple[str, str]]) -> list[str]:
    """Multi-select numbered menu. Comma-separated numbers/keys. Empty = none."""
    keys = [k for k, _ in options]
    print(prompt)
    for i, (key, label) in enumerate(options, start=1):
        print(f"  {i}) {label}")
    raw = input("Select (comma-separated, blank for none): ").strip()
    if not raw:
        return []
    chosen: list[str] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok in keys and tok not in chosen:
            chosen.append(tok)
        elif tok.isdigit() and 1 <= int(tok) <= len(options):
            k = keys[int(tok) - 1]
            if k not in chosen:
                chosen.append(k)
    return chosen


# ---------------------------------------------------------------------------
# Selection resolution — flags + prompts → a plan dict
# ---------------------------------------------------------------------------


class InitError(ValueError):
    """User-facing error raised when wizard input is invalid (bad name, …)."""


# A project label (final path segment) may only contain these characters.
# This is the safe set we interpolate into generated files; it forbids quotes,
# shell metacharacters, and path separators that could break package.json or
# escape the target directory.
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_name(name: str) -> None:
    """Reject project names that are unsafe to use as a path or in codegen.

    Raises :class:`InitError` with a clear message. Guards against path
    traversal (``..``), path-separator injection in the *label* segment, and
    characters outside the safe set that would corrupt generated files
    (notably double-quotes breaking ``package.json``). The full ``name`` may
    still be a path (``/tmp/foo/my-agent``); only its final segment becomes the
    project label, so that is what we validate.
    """
    if not name or not name.strip():
        raise InitError("Project name must not be empty.")
    # Path traversal anywhere in the supplied name is rejected outright.
    parts = re.split(r"[\\/]", name)
    if any(p == ".." for p in parts):
        raise InitError(f"Project name {name!r} must not contain '..' path segments.")
    label = Path(name).name
    if not label:
        raise InitError(f"Project name {name!r} has no final path segment.")
    if not _SAFE_LABEL_RE.match(label):
        raise InitError(
            f"Project name {label!r} contains invalid characters. "
            "Use only letters, digits, '.', '_' and '-'."
        )


def _npm_package_name(label: str) -> str:
    """Coerce a project label into a valid npm package name.

    npm names allow only ``[a-z0-9._-]`` and are capped at 214 chars. The label
    has already passed :func:`_validate_name`, so this only lower-cases, maps
    spaces (defensive — validation forbids them) to dashes, drops any residual
    out-of-set char, and truncates. Never produces quotes or shell metachars.
    """
    lowered = label.lower().replace(" ", "-")
    cleaned = re.sub(r"[^a-z0-9._-]", "", lowered) or "patter-agent"
    return cleaned[:214]


def _resolve_plan(args: argparse.Namespace, interactive: bool) -> dict:
    """Turn flags (+ prompts when interactive) into a concrete scaffold plan."""
    # 1. Project name → target dir
    if args.name is not None:
        name = args.name
    elif interactive:
        name = _ask_text("Project name", "my-patter-agent")
    else:
        name = "my-patter-agent"

    # Validate before anything is written or any path is resolved — a bad name
    # (path traversal, quote injection) must fail fast with a clear message.
    _validate_name(name)

    # 2. Runtime (this is the Python CLI → prefill python)
    if args.runtime is not None:
        runtime = args.runtime
    elif interactive:
        runtime = _ask_choice(
            "SDK runtime?",
            [
                ("python", "Python (getpatter)"),
                ("typescript", "TypeScript (getpatter)"),
            ],
            "python",
        )
    else:
        runtime = "python"

    # 3. Voice mode
    if args.mode is not None:
        mode = args.mode
    elif interactive:
        mode = _ask_choice(
            "Voice mode?",
            [
                ("realtime", "Realtime — one engine owns the turn (lowest latency)"),
                ("pipeline", "Pipeline — compose STT + LLM + TTS (full control)"),
            ],
            "realtime",
        )
    else:
        mode = "realtime"

    # 4. Engine / providers (filtered by mode)
    engine = None
    stt = llm = tts = None
    if mode == "realtime":
        if args.engine is not None:
            engine = args.engine
        elif interactive:
            engine = _ask_choice(
                "Realtime engine?",
                [(k, v["label"]) for k, v in REALTIME_ENGINES.items()],
                DEFAULT_REALTIME_ENGINE,
            )
        else:
            engine = DEFAULT_REALTIME_ENGINE
    else:
        stt = (
            args.stt
            if args.stt is not None
            else (
                _ask_choice(
                    "STT provider?",
                    [(k, v["label"]) for k, v in PIPELINE_STT.items()],
                    DEFAULT_STT,
                )
                if interactive
                else DEFAULT_STT
            )
        )
        llm = (
            args.llm
            if args.llm is not None
            else (
                _ask_choice(
                    "LLM provider?",
                    [(k, v["label"]) for k, v in PIPELINE_LLM.items()],
                    DEFAULT_LLM,
                )
                if interactive
                else DEFAULT_LLM
            )
        )
        tts = (
            args.tts
            if args.tts is not None
            else (
                _ask_choice(
                    "TTS provider?",
                    [(k, v["label"]) for k, v in PIPELINE_TTS.items()],
                    DEFAULT_TTS,
                )
                if interactive
                else DEFAULT_TTS
            )
        )

    # 5. Carrier
    if args.carrier is not None:
        carrier = args.carrier
    elif interactive:
        carrier = _ask_choice(
            "Telephony carrier?",
            [(k, v["label"]) for k, v in CARRIERS.items()],
            DEFAULT_CARRIER,
        )
    else:
        carrier = DEFAULT_CARRIER

    # Phone number
    if args.phone is not None:
        phone = args.phone
    elif interactive:
        phone = _ask_text("Phone number (E.164)", DEFAULT_PHONE)
    else:
        phone = DEFAULT_PHONE

    return {
        "name": name,
        "runtime": runtime,
        "mode": mode,
        "engine": engine,
        "stt": stt,
        "llm": llm,
        "tts": tts,
        "carrier": carrier,
        "phone": phone,
    }


def _collect_env_keys(plan: dict) -> list[str]:
    """Ordered, de-duplicated list of env var names the chosen stack reads.

    Carrier creds first, then the engine/provider keys, then the phone env.
    """
    keys: list[str] = []

    def _add(names) -> None:
        for n in names:
            if n not in keys:
                keys.append(n)

    _add(CARRIERS[plan["carrier"]]["env"])
    if plan["mode"] == "realtime":
        _add(REALTIME_ENGINES[plan["engine"]]["env"])
    else:
        _add(PIPELINE_STT[plan["stt"]]["env"])
        _add(PIPELINE_LLM[plan["llm"]]["env"])
        _add(PIPELINE_TTS[plan["tts"]]["env"])
    _add((CARRIERS[plan["carrier"]]["phone_env"],))
    return keys


def _collect_extras(plan: dict) -> list[str]:
    """Sorted, de-duplicated pip extras for the chosen pipeline providers.

    Realtime defaults (OpenAI*) need no extras. Carriers need no extras.
    """
    extras: set[str] = set()
    if plan["mode"] == "pipeline":
        for table, key in (
            (PIPELINE_STT, plan["stt"]),
            (PIPELINE_LLM, plan["llm"]),
            (PIPELINE_TTS, plan["tts"]),
        ):
            extra = table[key]["extra"]
            if extra:
                extras.add(extra)
    else:
        extra = REALTIME_ENGINES[plan["engine"]]["extra"]
        if extra:
            extras.add(extra)
    return sorted(extras)


# ---------------------------------------------------------------------------
# Scaffold string builders — pure functions, the parity-critical codegen.
# ---------------------------------------------------------------------------


def build_main_py(plan: dict) -> str:
    """Generate ``main.py`` for the chosen Python stack."""
    carrier_cls = CARRIERS[plan["carrier"]]["class"]
    phone = plan["phone"]
    if plan["mode"] == "realtime":
        engine_cls = REALTIME_ENGINES[plan["engine"]]["class"]
        imports = f"from getpatter import Patter, {carrier_cls}, {engine_cls}"
        agent_block = (
            f"    agent = phone.agent(\n"
            f"        engine={engine_cls}(),\n"
            f"        system_prompt=(\n"
            f'            "You are the receptionist for Acme Corp. Help callers with "\n'
            f'            "hours, support questions, and simple account changes. "\n'
            f'            "Keep replies short."\n'
            f"        ),\n"
            f'        first_message="Hi! Thanks for calling Acme Corp. How can I help?",\n'
            f"    )"
        )
    else:
        stt_cls = PIPELINE_STT[plan["stt"]]["class"]
        llm_cls = PIPELINE_LLM[plan["llm"]]["class"]
        tts_cls = PIPELINE_TTS[plan["tts"]]["class"]
        imports = (
            f"from getpatter import Patter, {carrier_cls}, "
            f"{stt_cls}, {llm_cls}, {tts_cls}"
        )
        agent_block = (
            f"    agent = phone.agent(\n"
            f"        stt={stt_cls}(),\n"
            f"        llm={llm_cls}(),\n"
            f"        tts={tts_cls}(),\n"
            f"        system_prompt=(\n"
            f'            "You are the receptionist for Acme Corp. Help callers with "\n'
            f'            "hours, support questions, and simple account changes. "\n'
            f'            "Keep replies short."\n'
            f"        ),\n"
            f'        first_message="Hi! Thanks for calling Acme Corp. How can I help?",\n'
            f"    )"
        )
    return (
        '"""Patter voice agent — generated by `getpatter init`.\n'
        "\n"
        "Answers inbound calls and talks to the caller. Set the API keys in\n"
        ".env (already scaffolded), then run:  python main.py\n"
        '"""\n'
        "\n"
        "import asyncio\n"
        "import os\n"
        "\n"
        f"{imports}\n"
        "\n"
        f'PHONE_NUMBER = os.environ.get("{CARRIERS[plan["carrier"]]["phone_env"]}", "{phone}")\n'
        "\n"
        "\n"
        "async def main() -> None:\n"
        "    phone = Patter(\n"
        f"        carrier={carrier_cls}(),   # reads creds from env\n"
        "        phone_number=PHONE_NUMBER,\n"
        "    )\n"
        "\n"
        f"{agent_block}\n"
        "\n"
        '    print(f"Ready on {PHONE_NUMBER}. Waiting for calls... (Ctrl+C to stop)")\n'
        "    await phone.serve(agent, tunnel=True)\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    asyncio.run(main())\n"
    )


def build_requirements_txt(plan: dict) -> str:
    """Generate ``requirements.txt`` pinning getpatter with the right extras."""
    extras = _collect_extras(plan)
    spec = "getpatter"
    if extras:
        spec += "[" + ",".join(extras) + "]"
    spec += f"=={__version__}"
    return spec + "\n"


def build_env(plan: dict, *, with_values: dict[str, str] | None = None) -> str:
    """Generate ``.env`` body. ``with_values`` maps env name → entered secret.

    When a value is absent the line is left as ``NAME=`` (placeholder) so the
    user fills it in. Secrets are NEVER logged — only written to disk (0600).
    """
    with_values = with_values or {}
    lines = [
        "# Patter environment — generated by `getpatter init`.",
        "# Fill in the blanks below. Keep this file out of version control.",
        "",
    ]
    for key in _collect_env_keys(plan):
        lines.append(f"{key}={with_values.get(key, '')}")
    return "\n".join(lines) + "\n"


def build_env_example(plan: dict) -> str:
    """Generate ``.env.example`` with empty placeholders (safe to commit)."""
    return build_env(plan, with_values=None)


def build_gitignore(plan: dict) -> str:
    """Generate ``.gitignore`` — always ignores .env and runtime junk."""
    common = [".env", ".env.*", "!.env.example"]
    if plan["runtime"] == "python":
        rest = ["__pycache__/", "*.pyc", ".venv/", "venv/", ".pytest_cache/"]
    else:
        rest = ["node_modules/", "dist/", "*.log"]
    return "\n".join(common + rest) + "\n"


def build_readme(plan: dict) -> str:
    """Generate ``README.md`` explaining how to run the project."""
    if plan["runtime"] == "python":
        run_block = (
            "```sh\n"
            "pip install -r requirements.txt\n"
            "# edit .env with your API keys\n"
            "python main.py\n"
            "```"
        )
        entry = "main.py"
    else:
        run_block = "```sh\nnpm install\n# edit .env with your API keys\nnpm start\n```"
        entry = "src/index.ts"
    if plan["mode"] == "realtime":
        stack = f"Realtime engine: `{REALTIME_ENGINES[plan['engine']]['class']}`"
    else:
        stack = (
            f"Pipeline: STT `{PIPELINE_STT[plan['stt']]['class']}` · "
            f"LLM `{PIPELINE_LLM[plan['llm']]['class']}` · "
            f"TTS `{PIPELINE_TTS[plan['tts']]['class']}`"
        )
    tunnel_ref = "`tunnel=True`" if plan["runtime"] == "python" else "`tunnel: true`"
    return (
        f"# {_project_label(plan)}\n"
        "\n"
        "A Patter voice agent, scaffolded by `getpatter init`.\n"
        "\n"
        f"- Carrier: `{CARRIERS[plan['carrier']]['class']}`\n"
        f"- {stack}\n"
        f"- Entry point: `{entry}`\n"
        "\n"
        "## Run\n"
        "\n"
        f"{run_block}\n"
        "\n"
        "The agent answers inbound calls and starts a Cloudflare Quick Tunnel\n"
        f"({tunnel_ref}) so the carrier can reach your local server during dev.\n"
    )


def build_index_ts(plan: dict) -> str:
    """Generate ``src/index.ts`` — kept here so the Python reference documents
    the exact TS scaffold the TS port must emit (camelCase, ``new``, options).
    The Python CLI only writes this when ``runtime=typescript`` is forced.
    """
    carrier_cls = CARRIERS[plan["carrier"]]["class"]
    phone = plan["phone"]
    phone_env = CARRIERS[plan["carrier"]]["phone_env"]
    if plan["mode"] == "realtime":
        engine_cls = REALTIME_ENGINES[plan["engine"]]["class"]
        imports = f'import {{ Patter, {carrier_cls}, {engine_cls} }} from "getpatter";'
        agent_block = (
            "  const agent = phone.agent({\n"
            f"    engine: new {engine_cls}(),\n"
            "    systemPrompt:\n"
            '      "You are the receptionist for Acme Corp. Help callers with " +\n'
            '      "hours, support questions, and simple account changes. " +\n'
            '      "Keep replies short.",\n'
            '    firstMessage: "Hi! Thanks for calling Acme Corp. How can I help?",\n'
            "  });"
        )
    else:
        stt_cls = PIPELINE_STT[plan["stt"]]["class"]
        llm_cls = PIPELINE_LLM[plan["llm"]]["class"]
        tts_cls = PIPELINE_TTS[plan["tts"]]["class"]
        imports = (
            f"import {{ Patter, {carrier_cls}, {stt_cls}, {llm_cls}, "
            f'{tts_cls} }} from "getpatter";'
        )
        agent_block = (
            "  const agent = phone.agent({\n"
            f"    stt: new {stt_cls}(),\n"
            f"    llm: new {llm_cls}(),\n"
            f"    tts: new {tts_cls}(),\n"
            "    systemPrompt:\n"
            '      "You are the receptionist for Acme Corp. Help callers with " +\n'
            '      "hours, support questions, and simple account changes. " +\n'
            '      "Keep replies short.",\n'
            '    firstMessage: "Hi! Thanks for calling Acme Corp. How can I help?",\n'
            "  });"
        )
    return (
        "/**\n"
        " * Patter voice agent — generated by `getpatter init`.\n"
        " *\n"
        " * Answers inbound calls and talks to the caller. Set the API keys in\n"
        " * .env (already scaffolded), then run:  npm start\n"
        " */\n"
        f"{imports}\n"
        "\n"
        f'const PHONE_NUMBER = process.env.{phone_env} ?? "{phone}";\n'
        "\n"
        "async function main(): Promise<void> {\n"
        "  const phone = new Patter({\n"
        f"    carrier: new {carrier_cls}(),   // reads creds from env\n"
        "    phoneNumber: PHONE_NUMBER,\n"
        "  });\n"
        "\n"
        f"{agent_block}\n"
        "\n"
        "  console.log(`Ready on ${PHONE_NUMBER}. Waiting for calls... "
        "(Ctrl+C to stop)`);\n"
        "  await phone.serve({ agent, tunnel: true });\n"
        "}\n"
        "\n"
        "main().catch((err) => {\n"
        "  console.error(err);\n"
        "  process.exit(1);\n"
        "});\n"
    )


def _project_label(plan: dict) -> str:
    """Display/basename for the project, derived from ``plan["name"]``.

    ``name`` may be a bare label ("my-agent") or a path ("/tmp/foo/my-agent").
    The label is always the final path segment so package.json / README never
    leak the parent directory. Parity rule: TS port uses ``path.basename``.
    """
    base = Path(plan["name"]).name or "patter-agent"
    return base


def build_package_json(plan: dict) -> str:
    """Generate ``package.json`` for the TS scaffold (getpatter pinned)."""
    # Sanitize to a valid npm name, then JSON-encode so a stray quote / unicode
    # in the label can never produce malformed JSON that breaks `npm install`.
    name = json.dumps(_npm_package_name(_project_label(plan)))
    return (
        "{\n"
        f'  "name": {name},\n'
        '  "version": "0.1.0",\n'
        '  "private": true,\n'
        '  "type": "module",\n'
        '  "scripts": {\n'
        '    "start": "tsx src/index.ts"\n'
        "  },\n"
        '  "dependencies": {\n'
        f'    "getpatter": "{__version__}"\n'
        "  },\n"
        '  "devDependencies": {\n'
        '    "tsx": "^4.0.0",\n'
        '    "typescript": "^5.0.0"\n'
        "  }\n"
        "}\n"
    )


# ---------------------------------------------------------------------------
# Filesystem + side effects
# ---------------------------------------------------------------------------


def _write_scaffold(target: Path, plan: dict, env_values: dict[str, str]) -> list[Path]:
    """Write every scaffold file. Returns the list of written paths.

    ``.env`` is created with mode 0600. Secrets in ``env_values`` are written
    to ``.env`` only — never echoed or logged.
    """
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _w(rel: str, content: str, *, mode: int | None = None) -> None:
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode is not None:
            # Create the file with the target mode atomically via os.open, so a
            # secrets-bearing file (.env) is NEVER world-/group-readable for the
            # window between write and a separate chmod (TOCTOU). O_TRUNC keeps
            # --force re-scaffolds correct; os.fchmod re-asserts the mode even
            # when the file already existed (the mode arg to os.open is masked
            # by umask and only applies on create).
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(str(path), flags, mode)
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        written.append(path)

    if plan["runtime"] == "python":
        _w("main.py", build_main_py(plan))
        _w("requirements.txt", build_requirements_txt(plan))
    else:
        _w("src/index.ts", build_index_ts(plan))
        _w("package.json", build_package_json(plan))

    # .env first (0600), then the committable example.
    _w(".env", build_env(plan, with_values=env_values), mode=0o600)
    _w(".env.example", build_env_example(plan))
    _w(".gitignore", build_gitignore(plan))
    _w("README.md", build_readme(plan))
    return written


def _maybe_git_init(target: Path) -> None:
    """Run ``git init`` in the target dir via subprocess (arg array, no shell)."""
    git = shutil.which("git")
    if not git:
        print("  git not found on PATH — skipping 'git init'.")
        return
    try:
        subprocess.run([git, "init", "--quiet"], cwd=str(target), check=True)  # noqa: S603
        print("  Initialised a git repository.")
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"  'git init' failed ({exc}) — skipping.")


def _maybe_install_skills(target: Path, ides: list[str]) -> None:
    """Shell out to ``npx skills add ...`` per IDE. Skips if npx is missing."""
    if not ides:
        return
    npx = shutil.which("npx")
    if not npx:
        print("  npx (Node) not found — skipping IDE skills install.")
        return
    ref = f"patterai/skills#v{__version__}"
    for ide in ides:
        print(f"  Installing skills for {ide}...")
        try:
            subprocess.run(  # noqa: S603
                [npx, "skills", "add", ref, "-a", ide],
                cwd=str(target),
                check=True,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"  skills add for {ide} failed ({exc}) — skipping.")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    interactive = not args.yes

    plan = _resolve_plan(args, interactive)

    target = Path(plan["name"]).expanduser().resolve()

    # 9. Non-empty dir guard (--force overrides).
    if target.exists() and any(target.iterdir()) and not args.force:
        print(
            f"Target directory {target} is not empty. "
            "Re-run with --force to scaffold into it anyway."
        )
        return 1

    env_keys = _collect_env_keys(plan)

    # 6. API keys → .env (0600). Skippable; secrets never echoed/logged.
    env_values: dict[str, str] = {}
    if not args.skip_keys and interactive:
        print(
            "\nAPI keys (input is hidden — press Enter to leave blank, "
            "fill them in later):"
        )
        for key in env_keys:
            # getpass() does NOT echo characters, so the raw secret never lands
            # on a shared terminal, a shoulder-surf, or a terminal recording.
            # The value is written to .env (0600) only — never logged or printed.
            val = getpass.getpass(f"  {key}: ").strip()
            if val:
                env_values[key] = val
    # In --skip-keys (or non-interactive) mode every value stays a placeholder.

    written = _write_scaffold(target, plan, env_values)

    # 7. IDE skills (multi-select). --no-skills / non-interactive default skips.
    ides: list[str] = []
    if not args.no_skills:
        if args.ide is not None:
            ides = [s.strip() for s in args.ide.split(",") if s.strip() in IDE_CHOICES]
        elif interactive:
            ides = _ask_multi(
                "Install Patter skills for which IDEs?",
                [(k, k) for k in IDE_CHOICES],
            )
    _maybe_install_skills(target, ides)

    # 8. git init (optional). --no-git / non-interactive default skips.
    do_git = False
    if not args.no_git:
        do_git = (
            _ask_yes_no("Initialise a git repository?", True) if interactive else False
        )
    if do_git:
        _maybe_git_init(target)

    # Summary — paths only, never secret values.
    print(f"\nScaffolded {plan['runtime']} project at {target}")
    for path in written:
        print(f"  + {path.relative_to(target)}")
    if plan["runtime"] == "python":
        print(
            "\nNext:  cd "
            f"{target}  &&  pip install -r requirements.txt  &&  python main.py"
        )
    else:
        print(f"\nNext:  cd {target}  &&  npm install  &&  npm start")
    return 0


def main() -> None:
    """Standalone entry for ``python -m getpatter.init.cli``."""
    parser = argparse.ArgumentParser(prog="patter-init")
    subparsers = parser.add_subparsers(dest="command")
    build_init_parser(subparsers)
    args = parser.parse_args()
    if args.command != "init":
        parser.print_help()
        sys.exit(2)
    sys.exit(dispatch_init(args))


if __name__ == "__main__":
    main()
