"""Tests for the built-in ``consult`` escalation tool.

Authentic: the orchestrator endpoint is a REAL local HTTP server (stdlib
``http.server`` in a background thread), and the consult handler performs a real
``httpx`` POST against it. Only the SSRF guard is relaxed (monkeypatched) for
the loopback-bound test server — the guard itself is verified separately in
``test_build_consult_tool_rejects_ssrf`` / ``test_consult_config_*``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from getpatter import Agent, ConsultConfig
from getpatter.stream_handler import _inject_consult_tool
from getpatter.tools import consult as consult_mod
from getpatter.tools.consult import build_consult_tool


# --------------------------------------------------------------------------
# ConsultConfig validation (no server)
# --------------------------------------------------------------------------


def test_consult_config_rejects_bad_scheme():
    with pytest.raises(ValueError):
        ConsultConfig(url="ftp://orchestrator.example.com/consult")


def test_consult_config_rejects_missing_host():
    with pytest.raises(ValueError):
        ConsultConfig(url="https:///no-host")


def test_consult_config_rejects_empty_tool_name():
    with pytest.raises(ValueError):
        ConsultConfig(url="https://orchestrator.example.com", tool_name="")


def test_consult_config_defaults():
    c = ConsultConfig(url="https://orchestrator.example.com/consult")
    assert c.tool_name == "consult_agent"
    assert c.timeout_s == 30.0
    assert c.headers is None
    assert "deeper reasoning" in c.description


def test_build_consult_tool_rejects_ssrf():
    # The SSRF guard runs at build time — a link-local metadata address is
    # rejected even though the scheme is valid.
    with pytest.raises(ValueError):
        build_consult_tool(ConsultConfig(url="http://169.254.169.254/consult"))


def test_build_consult_tool_shape():
    tool = build_consult_tool(
        ConsultConfig(url="https://orchestrator.example.com/consult")
    )
    assert tool["name"] == "consult_agent"
    assert callable(tool["handler"])
    assert tool["parameters"]["required"] == ["request"]
    assert "request" in tool["parameters"]["properties"]


# --------------------------------------------------------------------------
# Injection into the (frozen) Agent
# --------------------------------------------------------------------------


def test_inject_consult_tool_merges_into_agent():
    agent = Agent(
        system_prompt="hi",
        consult=ConsultConfig(url="https://orchestrator.example.com/consult"),
    )
    merged = _inject_consult_tool(agent)
    assert merged is not agent  # frozen → new instance
    names = [t["name"] for t in (merged.tools or [])]
    assert "consult_agent" in names


def test_inject_consult_tool_is_idempotent():
    agent = Agent(
        system_prompt="hi",
        consult=ConsultConfig(url="https://orchestrator.example.com/consult"),
    )
    once = _inject_consult_tool(agent)
    twice = _inject_consult_tool(once)
    assert [t["name"] for t in (twice.tools or [])].count("consult_agent") == 1


def test_inject_consult_tool_noop_without_consult():
    agent = Agent(system_prompt="hi")
    assert _inject_consult_tool(agent) is agent


def test_inject_consult_tool_preserves_user_tools():
    agent = Agent(
        system_prompt="hi",
        tools=[{"name": "lookup", "description": "", "parameters": {}}],
        consult=ConsultConfig(url="https://orchestrator.example.com/consult"),
    )
    names = [t["name"] for t in (_inject_consult_tool(agent).tools or [])]
    assert names == ["lookup", "consult_agent"]


# --------------------------------------------------------------------------
# Handler behaviour against a REAL local orchestrator server
# --------------------------------------------------------------------------


class _CapturingServer:
    """A real local HTTP server that records the last request and replies
    with a configurable status + body."""

    def __init__(self, status: int = 200, body: bytes = b'{"reply": "ok"}') -> None:
        self.status = status
        self.body = body
        self.last_path: str | None = None
        self.last_headers: dict[str, str] = {}
        self.last_json: dict | None = None
        captor = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # silence
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                captor.last_path = self.path
                captor.last_headers = {k: v for k, v in self.headers.items()}
                try:
                    captor.last_json = json.loads(raw)
                except ValueError:
                    captor.last_json = None
                self.send_response(captor.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(captor.body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/consult"

    def __enter__(self) -> "_CapturingServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def allow_loopback(monkeypatch):
    # Relax ONLY the SSRF guard so the consult handler can reach the
    # loopback-bound test server. The guard itself is tested separately.
    monkeypatch.setattr(consult_mod, "_validate_webhook_url", lambda _url: None)


@pytest.mark.integration
async def test_consult_handler_posts_payload_and_returns_reply(allow_loopback):
    with _CapturingServer(body=b'{"reply": "The order ships Tuesday."}') as srv:
        tool = build_consult_tool(
            ConsultConfig(url=srv.url, headers={"Authorization": "Bearer secret-xyz"})
        )
        result = await tool["handler"](
            {"request": "When does my order ship?"},
            {"call_id": "CAtest", "caller": "+15555550100", "callee": "+15555550199"},
        )
    assert result == "The order ships Tuesday."
    # The orchestrator received the request text + call correlation.
    assert srv.last_json == {
        "request": "When does my order ship?",
        "call_id": "CAtest",
        "caller": "+15555550100",
        "callee": "+15555550199",
    }
    # Custom auth header was forwarded.
    assert srv.last_headers.get("Authorization") == "Bearer secret-xyz"


@pytest.mark.integration
async def test_consult_handler_returns_raw_text_when_not_json(allow_loopback):
    with _CapturingServer(body=b"plain text answer") as srv:
        tool = build_consult_tool(ConsultConfig(url=srv.url))
        result = await tool["handler"]({"request": "hi"}, {"call_id": "x"})
    assert result == "plain text answer"


@pytest.mark.integration
async def test_consult_handler_graceful_on_server_error(allow_loopback):
    with _CapturingServer(status=500, body=b"boom") as srv:
        tool = build_consult_tool(ConsultConfig(url=srv.url))
        result = await tool["handler"]({"request": "hi"}, {"call_id": "x"})
    # No exception bubbles to the call; the agent gets a spoken fallback.
    assert "wasn't able to reach" in result.lower()
