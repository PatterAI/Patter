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

from getpatter import Agent, ConsultConfig, OpenAICompatibleConsult
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


def test_consult_config_allow_loopback_defaults_false():
    # Opt-in flag is off by default → backward-compatible strict behaviour.
    assert ConsultConfig(url="https://orchestrator.example.com").allow_loopback is False


def test_build_consult_tool_rejects_loopback_by_default():
    # Without the opt-in flag, a loopback URL is rejected by the real guard.
    with pytest.raises(ValueError):
        build_consult_tool(ConsultConfig(url="http://127.0.0.1:8642/consult"))
    with pytest.raises(ValueError):
        build_consult_tool(ConsultConfig(url="http://localhost:8642/consult"))


def test_build_consult_tool_allows_loopback_ip_when_opted_in():
    # allow_loopback=True exercises the REAL validator (no mock) and permits a
    # developer-configured loopback IP — the tool builds successfully.
    tool = build_consult_tool(
        ConsultConfig(url="http://127.0.0.1:8642/consult", allow_loopback=True)
    )
    assert tool["name"] == "consult_agent"
    assert callable(tool["handler"])


def test_build_consult_tool_allows_localhost_when_opted_in():
    tool = build_consult_tool(
        ConsultConfig(url="http://localhost:8642/consult", allow_loopback=True)
    )
    assert callable(tool["handler"])


def test_build_consult_tool_allows_rfc1918_private_when_opted_in():
    # A private back-office host (RFC1918) is reachable when opted in.
    tool = build_consult_tool(
        ConsultConfig(url="http://10.0.0.5:8642/consult", allow_loopback=True)
    )
    assert callable(tool["handler"])


def test_build_consult_tool_still_rejects_bad_scheme_when_opted_in():
    # The non-HTTP(S) scheme rejection is ALWAYS enforced, even with the flag.
    with pytest.raises(ValueError):
        build_consult_tool(
            ConsultConfig(url="ftp://127.0.0.1/consult", allow_loopback=True)
        )


def test_webhook_tool_path_stays_strict_by_default():
    # The shared validator is used by the generic webhook-tool executor path,
    # which must stay strict regardless of the consult opt-in. The default
    # (no allow_loopback) still rejects loopback.
    from getpatter.tools.tool_executor import _validate_webhook_url

    with pytest.raises(ValueError):
        _validate_webhook_url("http://127.0.0.1:8642/webhook")
    with pytest.raises(ValueError):
        _validate_webhook_url("http://localhost/webhook")


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
    # (Signature mirrors the real ``_validate_webhook_url`` — it now accepts a
    # keyword-only ``allow_loopback`` that ``build_consult_tool`` forwards.)
    monkeypatch.setattr(
        consult_mod, "_validate_webhook_url", lambda _url, *, allow_loopback=False: None
    )


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


# --------------------------------------------------------------------------
# OpenClaw / OpenAI-compatible consult target (native, no adapter)
# --------------------------------------------------------------------------


def test_openclaw_preset_builds_namespaced_model_and_defaults():
    c = ConsultConfig.openclaw("receptionist")
    assert c.url is None
    oc = c.openai_compatible
    assert isinstance(oc, OpenAICompatibleConsult)
    assert oc.model == "openclaw/receptionist"
    assert oc.base_url == "http://127.0.0.1:18789/v1"
    assert oc.api_key_env == "OPENCLAW_API_KEY"
    assert oc.session_header == "x-openclaw-session-key"
    # Loopback default → SSRF guard auto-relaxed for the co-located gateway.
    assert c.allow_loopback is True
    # Phone-safe default timeout, not regressed to a higher value.
    assert c.timeout_s == 30.0
    # Consult-biased ("substantive") description + a default reassurance filler.
    assert "NEVER" in c.description and "account-specific" in c.description
    assert isinstance(c.reassurance, str) and c.reassurance


@pytest.mark.parametrize(
    "agent, expected",
    [
        ("receptionist", "openclaw/receptionist"),
        ("openclaw/roofing-ca", "openclaw/roofing-ca"),
        ("openclaw:home-fl", "openclaw:home-fl"),
        ("agent:desk-1", "agent:desk-1"),
    ],
)
def test_openclaw_preset_agent_target_passthrough(agent, expected):
    assert ConsultConfig.openclaw(agent).openai_compatible.model == expected


@pytest.mark.parametrize("bad", ["", "has space", "a b", "drop;table", "x\n"])
def test_openclaw_preset_rejects_unsafe_agent(bad):
    with pytest.raises(ValueError):
        ConsultConfig.openclaw(bad)


def test_openclaw_preset_public_base_url_keeps_strict_ssrf():
    c = ConsultConfig.openclaw("receptionist", base_url="https://gw.example.com/v1")
    assert c.allow_loopback is False


def test_openclaw_preset_allow_loopback_override():
    assert ConsultConfig.openclaw("r", allow_loopback=False).allow_loopback is False


def test_consult_config_requires_exactly_one_target():
    with pytest.raises(ValueError, match="exactly one"):
        ConsultConfig()
    with pytest.raises(ValueError, match="exactly one"):
        ConsultConfig(
            url="https://x.example.com",
            openai_compatible=OpenAICompatibleConsult(
                base_url="https://gw.example.com/v1", model="openclaw/r"
            ),
        )


def test_openai_compatible_validation():
    with pytest.raises(ValueError):
        OpenAICompatibleConsult(base_url="ftp://gw/v1", model="m")
    with pytest.raises(ValueError):
        OpenAICompatibleConsult(base_url="https://gw/v1", model="")


def test_openclaw_reassurance_attached_to_tool():
    tool = build_consult_tool(ConsultConfig.openclaw("receptionist"))
    assert tool["reassurance"] == "Let me check on that for you, one moment."


def test_url_path_has_no_reassurance_by_default():
    tool = build_consult_tool(ConsultConfig(url="https://x.example.com/consult"))
    assert "reassurance" not in tool


def _openai_body(content: str) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]}
    ).encode()


@pytest.mark.integration
async def test_openclaw_handler_posts_chat_completions_and_speaks_content():
    # Real local server; allow_loopback is auto-True for the loopback gateway, so
    # the real SSRF validator (not a monkeypatch) permits it — fully authentic.
    with _CapturingServer(body=_openai_body("You're set for Thursday at 10am.")) as srv:
        cfg = ConsultConfig.openclaw(
            "receptionist",
            base_url=f"http://127.0.0.1:{srv.port}/v1",
            api_key="op-secret",
        )
        tool = build_consult_tool(cfg)
        result = await tool["handler"](
            {"request": "Reschedule my roof inspection to Thursday"},
            {"call_id": "CAxyz", "caller": "+15555550100", "callee": "+15555550199"},
        )
    assert result == "You're set for Thursday at 10am."
    # Hit the OpenAI-compatible path, not the generic /consult webhook.
    assert srv.last_path == "/v1/chat/completions"
    assert srv.last_json["model"] == "openclaw/receptionist"
    assert srv.last_json["stream"] is False
    assert srv.last_json["user"] == "CAxyz"
    assert [m["role"] for m in srv.last_json["messages"]] == ["system", "user"]
    assert "Reschedule my roof inspection" in srv.last_json["messages"][1]["content"]
    assert "+15555550100" in srv.last_json["messages"][0]["content"]
    # Operator bearer + explicit deprecation-proof session header.
    assert srv.last_headers.get("Authorization") == "Bearer op-secret"
    assert srv.last_headers.get("x-openclaw-session-key") == "CAxyz"


@pytest.mark.integration
async def test_openclaw_handler_reads_api_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENCLAW_API_KEY", "env-op-key")
    with _CapturingServer(body=_openai_body("ok")) as srv:
        cfg = ConsultConfig.openclaw(
            "receptionist", base_url=f"http://127.0.0.1:{srv.port}/v1"
        )
        await build_consult_tool(cfg)["handler"]({"request": "hi"}, {"call_id": "c1"})
    assert srv.last_headers.get("Authorization") == "Bearer env-op-key"


@pytest.mark.integration
async def test_openclaw_handler_graceful_on_404():
    with _CapturingServer(status=404, body=b"not found") as srv:
        cfg = ConsultConfig.openclaw(
            "receptionist", base_url=f"http://127.0.0.1:{srv.port}/v1"
        )
        result = await build_consult_tool(cfg)["handler"](
            {"request": "hi"}, {"call_id": "c1"}
        )
    assert "wasn't able to reach" in result.lower()


@pytest.mark.integration
async def test_openclaw_handler_graceful_on_missing_choices():
    with _CapturingServer(body=b'{"choices": []}') as srv:
        cfg = ConsultConfig.openclaw(
            "receptionist", base_url=f"http://127.0.0.1:{srv.port}/v1"
        )
        result = await build_consult_tool(cfg)["handler"](
            {"request": "hi"}, {"call_id": "c1"}
        )
    assert "wasn't able to reach" in result.lower()
