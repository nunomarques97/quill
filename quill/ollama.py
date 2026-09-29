"""Minimal client of the local Ollama server, for the app.

Ollama is shared with other projects. This client can only list the
installed models (``GET /api/tags``) and chat (``POST /api/chat``). It never
pulls, deletes, loads or unloads a model and never sends ``keep_alive``, so
other projects' models are left exactly as they are. The address must be
loopback (``quill.config`` already refuses any other); no proxy is used.
Errors name the call and the failure type, never the text sent.
"""

from __future__ import annotations

import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field

ALLOWED_CALLS = frozenset({("GET", "/api/tags"), ("POST", "/api/chat")})
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class OllamaError(OSError):
    """Ollama is unreachable or answered with an error. Carries no dictated text."""


@dataclass(frozen=True)
class ChatReply:
    content: str = field(repr=False)


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OllamaClient:
    def __init__(self, base_url: str, timeout_s: float = 10.0) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "http" or not _loopback(parts.hostname or "") or parts.path not in ("", "/"):
            raise ValueError("Ollama must be a local http://127.0.0.1:<port> address")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, payload: dict | None = None, timeout_s: float | None = None) -> dict:
        if (method, path) not in ALLOWED_CALLS:
            raise OllamaError(f"refused Ollama call {method} {path}")
        if payload is not None and "keep_alive" in payload:
            raise OllamaError("refused Ollama call with keep_alive")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout_s or self.timeout_s) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise OllamaError(f"Ollama {path} returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OllamaError(f"Ollama unreachable: {type(exc).__name__}") from None
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OllamaError(f"Ollama {path} returned invalid JSON") from None
        if not isinstance(parsed, dict):
            raise OllamaError(f"Ollama {path} returned an unexpected body")
        return parsed

    def installed(self, timeout_s: float | None = None) -> list[str]:
        models = self._call("GET", "/api/tags", timeout_s=timeout_s).get("models")
        return [m["name"] for m in models or [] if isinstance(m, dict) and isinstance(m.get("name"), str)]

    def chat(self, model: str, system: str, user: str, max_tokens: int | None = None,
             history: Sequence[tuple[str, str]] = ()) -> ChatReply:
        """One deterministic chat turn with thinking disabled; ``max_tokens`` caps the reply.

        ``history`` holds earlier (user message, assistant reply) turns of the
        same conversation, sent before ``user``.
        """
        options: dict = {"temperature": 0, "seed": 0}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        messages = [{"content": system, "role": "system"}]
        for asked, answered in history:
            messages += [{"content": asked, "role": "user"}, {"content": answered, "role": "assistant"}]
        payload = {
            "model": model,
            "messages": messages + [{"content": user, "role": "user"}],
            "stream": False,
            "think": False,
            "options": options,
        }
        data = self._call("POST", "/api/chat", payload)
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise OllamaError("Ollama /api/chat returned no message")
        return ChatReply(THINK_BLOCK.sub("", content).strip())
