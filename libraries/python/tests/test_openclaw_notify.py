"""Tests for the OpenClaw post-call notifier (``on_call_end`` → OpenClaw).

Authentic: the OpenClaw gateway is a REAL local HTTP server (stdlib
``http.server`` in a background thread); the notifier performs a real ``httpx``
POST against it. ``allow_loopback`` is auto-enabled for the loopback gateway, so
the real SSRF validator (not a monkeypatch) permits it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from getpatter import openclaw_post_call_notifier


class _Server:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b'{"choices":[{"message":{"content":"logged"}}]}',
    ) -> None:
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

    def __enter__(self) -> "_Server":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.mark.integration
async def test_notifier_posts_call_record_to_openclaw():
    with _Server() as srv:
        notify = openclaw_post_call_notifier(
            "receptionist",
            base_url=f"http://127.0.0.1:{srv.port}/v1",
            api_key="op-secret",
        )
        await notify(
            {
                "call_id": "CAend",
                "caller": "+15555550100",
                "callee": "+15555550199",
                "transcript": [
                    {"role": "user", "text": "Reschedule Tuesday"},
                    {"role": "assistant", "text": "Done, moved to Thursday."},
                ],
                "metrics": SimpleNamespace(duration_seconds=42.0),
            }
        )
    assert srv.last_path == "/v1/chat/completions"
    assert srv.last_json["model"] == "openclaw/receptionist"
    assert srv.last_json["user"] == "CAend"
    assert srv.last_json["stream"] is False
    assert srv.last_headers.get("x-openclaw-session-key") == "CAend"
    assert srv.last_headers.get("Authorization") == "Bearer op-secret"
    record = srv.last_json["messages"][1]["content"]
    assert "+15555550100" in record
    assert "Reschedule Tuesday" in record
    assert "Done, moved to Thursday." in record
    assert "42s" in record  # duration from metrics


@pytest.mark.integration
async def test_notifier_can_omit_transcript():
    with _Server() as srv:
        notify = openclaw_post_call_notifier(
            "receptionist",
            base_url=f"http://127.0.0.1:{srv.port}/v1",
            include_transcript=False,
        )
        await notify(
            {
                "call_id": "c1",
                "caller": "+15555550100",
                "transcript": [{"role": "user", "text": "secret"}],
            }
        )
    record = srv.last_json["messages"][1]["content"]
    assert "secret" not in record
    assert "+15555550100" in record


@pytest.mark.integration
async def test_notifier_is_fire_and_forget_on_error():
    with _Server(status=500, body=b"boom") as srv:
        notify = openclaw_post_call_notifier(
            "receptionist", base_url=f"http://127.0.0.1:{srv.port}/v1"
        )
        # Must NOT raise into call teardown.
        await notify({"call_id": "x", "caller": "+15555550100"})


def test_notifier_rejects_unsafe_agent():
    with pytest.raises(ValueError):
        openclaw_post_call_notifier("bad agent")
