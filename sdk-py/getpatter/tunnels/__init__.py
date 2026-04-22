"""Public tunnel directives for exposing a Patter dev server.

Each class is a small frozen dataclass the server reads to decide how the
local webhook / media-stream URL should be exposed. No programmatic process
management is performed in Phase 1a — users running ``ngrok`` themselves
should use :class:`Static`.

Usage::

    from getpatter.tunnels import Static, CloudflareTunnel, Ngrok

    tunnel = Static(hostname="abc.ngrok.io")    # already running
    tunnel = CloudflareTunnel()                 # server spawns cloudflared
    tunnel = Ngrok()                            # directive only (Phase 1a)
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Ngrok", "CloudflareTunnel", "Static"]


@dataclass(frozen=True)
class Ngrok:
    """Ngrok tunnel directive.

    In Phase 1a this is a marker only — programmatic ``ngrok`` launching via
    the ``ngrok`` Python package is planned as a future addition. Users who
    already run ``ngrok`` themselves should use :class:`Static` with the
    public hostname instead.

    Args:
        hostname: Optional reserved hostname. Currently informational only.
    """

    hostname: str | None = None

    @property
    def kind(self) -> str:
        return "ngrok"


@dataclass(frozen=True)
class CloudflareTunnel:
    """Auto-start a Cloudflare Tunnel via the local ``cloudflared`` binary."""

    @property
    def kind(self) -> str:
        return "cloudflare"


@dataclass(frozen=True)
class Static:
    """Use a pre-existing public hostname (user-managed tunnel).

    Args:
        hostname: Public hostname, e.g. ``"abc.ngrok.io"`` or
            ``"agent.example.com"``. The server will not attempt to spawn a
            tunnel process; the hostname must already route to the local port.
    """

    hostname: str

    def __post_init__(self) -> None:
        if not self.hostname:
            raise ValueError("Static tunnel requires a non-empty hostname")

    @property
    def kind(self) -> str:
        return "static"
