"""Built-in tunnel support via cloudflared.

Spawns a Cloudflare Quick Tunnel that exposes a local port to the internet.
Zero account required — uses Cloudflare's free trycloudflare.com service.

Requires the ``cloudflared`` binary to be installed and on PATH.
Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import re
import shutil
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("patter")

_TUNNEL_URL_RE = re.compile(r"https://([a-zA-Z0-9._-]+\.trycloudflare\.com)")
_STARTUP_TIMEOUT = 30  # seconds


@dataclass(frozen=True)
class TunnelHandle:
    """Handle to a running cloudflared tunnel."""

    hostname: str
    """Public hostname (no protocol), e.g. 'random-name.trycloudflare.com'."""

    stop: Callable[[], None]
    """Stop the tunnel process."""


async def start_tunnel(port: int, timeout: float = _STARTUP_TIMEOUT) -> TunnelHandle:
    """Start a cloudflared quick tunnel pointing to the given local port.

    Args:
        port: Local port to tunnel to.
        timeout: How long to wait for the tunnel URL (default 30s).

    Returns:
        A ``TunnelHandle`` with the public hostname and a stop function.

    Raises:
        FileNotFoundError: If cloudflared binary is not installed.
        TimeoutError: If the tunnel doesn't produce a URL within ``timeout``.
    """
    binary = shutil.which("cloudflared")
    if binary is None:
        raise FileNotFoundError(
            "Built-in tunnel requires the 'cloudflared' binary. Install it:\n\n"
            "  brew install cloudflared          # macOS\n"
            "  sudo apt install cloudflared      # Debian/Ubuntu\n"
            "  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/\n\n"
            "Or provide your own webhook_url instead of using tunnel=True."
        )

    logger.info("Starting tunnel to localhost:%d ...", port)

    proc = await asyncio.create_subprocess_exec(
        binary,
        "tunnel",
        "--url",
        f"http://localhost:{port}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    hostname: str | None = None

    async def _read_stream(stream: asyncio.StreamReader) -> str | None:
        """Read lines from a stream looking for the tunnel URL."""
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace")
            match = _TUNNEL_URL_RE.search(line)
            if match:
                return match.group(1)
        return None

    try:
        # cloudflared prints the URL to stderr
        tasks = []
        if proc.stderr:
            tasks.append(asyncio.create_task(_read_stream(proc.stderr)))
        if proc.stdout:
            tasks.append(asyncio.create_task(_read_stream(proc.stdout)))

        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done:
            result = task.result()
            if result:
                hostname = result
                break

        # Cancel remaining tasks
        for task in pending:
            task.cancel()

    except Exception:
        proc.terminate()
        raise

    if hostname is None:
        proc.terminate()
        raise TimeoutError(
            f"Tunnel failed to start within {timeout}s. "
            "Check your internet connection or provide webhook_url manually."
        )

    def _stop() -> None:
        logger.info("Stopping tunnel...")
        try:
            proc.terminate()
        except ProcessLookupError:
            pass  # Already exited

    # Safety net: kill tunnel if process exits without calling stop
    atexit.register(_stop)

    logger.info("Tunnel ready: https://%s", hostname)

    return TunnelHandle(hostname=hostname, stop=_stop)
