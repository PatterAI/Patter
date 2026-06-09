"""Coarse, anonymous deploy-shape detection for telemetry.

Every probe here is presence-only: it reads whether a marker env var or file
exists and returns a closed enum / boolean — it NEVER returns the value of an env
var (which could carry an account id, region, or hostname). This mirrors the
.NET-CLI / Astro pattern. Keep byte-for-byte parity of the *emitted enums* with
``environment.ts`` (the detection mechanism may differ per language).
"""

from __future__ import annotations

import os
import sys


# --- AI coding agent ---------------------------------------------------------
# Presence of a tool's marker env var → which agent drove this run. Lets the
# maintainers see how much of the install base is set up by an AI agent / skill.
def invoked_by_agent() -> str:
    env = os.environ
    if "CLAUDECODE" in env or "CLAUDE_CODE" in env or "CLAUDE_CODE_ENTRYPOINT" in env:
        return "claude"
    if "CURSOR_TRACE_ID" in env or "CURSOR_AGENT" in env:
        return "cursor"
    if "GITHUB_COPILOT_AGENT" in env or "COPILOT_AGENT_ID" in env:
        return "copilot"
    if "GEMINI_CLI" in env or "GEMINI_AGENT" in env:
        return "gemini"
    if "WINDSURF" in env or "WINDSURF_AGENT" in env:
        return "windsurf"
    if "AIDER" in env or "OPENAI_AGENT" in env:
        return "other"
    return "none"


# --- Container ---------------------------------------------------------------
def in_container() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as fh:
            blob = fh.read()
        return "docker" in blob or "containerd" in blob or "kubepods" in blob
    except OSError:
        return False


# --- Serverless --------------------------------------------------------------
def serverless() -> str:
    env = os.environ
    if env.get("AWS_LAMBDA_FUNCTION_NAME"):
        return "lambda"
    if env.get("K_SERVICE"):  # Google Cloud Run / Knative
        return "cloud_run"
    if env.get("VERCEL"):
        return "vercel"
    if env.get("AZURE_FUNCTIONS_ENVIRONMENT") or env.get("FUNCTIONS_WORKER_RUNTIME"):
        return "azure_functions"
    return "none"


# --- Cloud platform ----------------------------------------------------------
def cloud() -> str:
    env = os.environ
    if (
        env.get("AWS_REGION")
        or env.get("AWS_EXECUTION_ENV")
        or env.get("AWS_LAMBDA_FUNCTION_NAME")
    ):
        return "aws"
    if (
        env.get("K_SERVICE")
        or env.get("GOOGLE_CLOUD_PROJECT")
        or env.get("GCP_PROJECT")
    ):
        return "gcp"
    if env.get("WEBSITE_INSTANCE_ID") or env.get("AZURE_FUNCTIONS_ENVIRONMENT"):
        return "azure"
    if env.get("FLY_APP_NAME"):
        return "fly"
    return "none"


# --- Package manager ---------------------------------------------------------
# Python install/run manager is only weakly observable at runtime; presence-only,
# best-effort, default "pip". (TypeScript reads npm_config_user_agent.)
def package_manager() -> str:
    env = os.environ
    if env.get("POETRY_ACTIVE"):
        return "poetry"
    if env.get("PIPENV_ACTIVE"):
        return "pipenv"
    if env.get("UV") or "uv" in (os.getenv("VIRTUAL_ENV_PROMPT") or "").lower():
        return "uv"
    if env.get("CONDA_DEFAULT_ENV"):
        return "conda"
    # `pip` is the sane catch-all for a CPython process.
    return "pip" if sys.executable else "none"
