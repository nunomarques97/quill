"""Offline fakes: a local fake provider server, a fake Ollama and fake engines.

Everything listens on 127.0.0.1 with an ephemeral port. Nothing plays sound,
opens a microphone or leaves the machine.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bench.engines.base import Engine, EngineError, HttpResponse, Hints, pcm16_wav, urllib_send

FAKE_KEY = "fk-test-9f8e7d6c5b4a39281706f5e4d3c2b1a0"


class _Recorder(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        request = {
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        }
        self.server.requests.append(request)
        status, headers, payload = self.server.respond(request)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = _handle


class FakeServer:
    """HTTP server on 127.0.0.1; ``respond(request) -> (status, headers, body)``."""

    def __init__(self, respond) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        self.httpd.requests = []
        self.httpd.respond = respond
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def requests(self) -> list[dict]:
        return self.httpd.requests

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def send(self, request: urllib.request.Request, timeout: float) -> HttpResponse:
        """A Transport sender that delivers the checked HTTPS request to this local server.

        Only the scheme and host change; path, query, headers and body are kept,
        so the fake sees exactly what the provider would receive.
        """
        parts = urllib.parse.urlsplit(request.full_url)
        local = self.base_url + parts.path + (f"?{parts.query}" if parts.query else "")
        headers = dict(request.header_items())
        headers["X-Original-Host"] = parts.hostname or ""
        forwarded = urllib.request.Request(local, data=request.data, headers=headers, method=request.get_method())
        return urllib_send(forwarded, timeout)


def json_reply(data: object, status: int = 200, headers: dict | None = None) -> tuple[int, dict, bytes]:
    return status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(data).encode("utf-8")


class FakeOllama(FakeServer):
    """Answers /api/version, /api/tags, /api/ps and /api/chat like Ollama."""

    def __init__(self, installed=("qwen3:8b",), loaded=(), chat=None, chat_status: int = 200) -> None:
        self.installed = list(installed)
        self.loaded = list(loaded)
        self.chat = chat or (lambda payload: payload["messages"][-1]["content"])
        self.chat_status = chat_status
        super().__init__(self._respond)

    def _respond(self, request: dict):
        path = request["path"]
        if path == "/api/version":
            return json_reply({"version": "0.0-fake"})
        if path == "/api/tags":
            return json_reply({"models": [{"name": name} for name in self.installed]})
        if path == "/api/ps":
            return json_reply(
                {"models": [{"name": name, "size": mib * 2**20, "size_vram": mib * 2**20} for name, mib in self.loaded]}
            )
        if path == "/api/chat":
            if self.chat_status != 200:
                return json_reply({"error": "model failed to load"}, status=self.chat_status)
            payload = json.loads(request["body"])
            return json_reply(
                {"message": {"content": self.chat(payload), "role": "assistant"}, "load_duration": 1_000_000}
            )
        return json_reply({"error": "not found"}, status=404)

    def chat_payloads(self) -> list[dict]:
        return [json.loads(r["body"]) for r in self.requests if r["path"] == "/api/chat"]


def judge_yes(payload: dict) -> str:
    """Fake judge/cleanup: JSON verdict when a schema is requested, else echo."""
    if "format" in payload:
        return json.dumps({"same": "yes", "reason": "same action"})
    return payload["messages"][-1]["content"]


class FakeEngine(Engine):
    """Deterministic engine: returns ``texts`` in order of the audio it sees."""

    def __init__(
        self,
        engine_id: str,
        texts: dict[bytes, str],
        *,
        cacheable: bool = False,
        fail_on: int | None = None,
        error: str = "fake failure",
        hints_reason: str | None = None,
    ) -> None:
        self.id = engine_id
        self.cacheable = cacheable
        self.texts = texts
        self.fail_on = fail_on
        self.error = error
        self.hints_reason = hints_reason
        self.calls = 0
        self.loaded = self.closed = 0

    def hints_unsupported(self) -> str | None:
        return self.hints_reason

    def params(self, hints: Hints | None) -> dict:
        return {"hints": hints.joined() if hints else None}

    def load(self) -> None:
        self.loaded += 1

    def close(self) -> None:
        self.closed += 1

    def transcribe(self, wav: bytes, hints: Hints | None) -> str:
        self.calls += 1
        if self.fail_on is not None and self.calls >= self.fail_on:
            raise EngineError(self.error)
        return self.texts.get(wav, "composite text")


def tone_wav(seed: int, seconds: float = 1.0) -> bytes:
    """A short deterministic non-silent PCM16 WAV (never played)."""
    from array import array

    count = int(seconds * 16_000)
    samples = array("h", (((i * (seed + 3)) % 200) - 100 for i in range(count)))
    return pcm16_wav(samples.tobytes())
