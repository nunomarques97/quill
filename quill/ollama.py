"""Minimal client of the local Ollama server, for the app.

Ollama is shared with other projects. This client can only list the
installed models (``GET /api/tags``) and the models in memory
(``GET /api/ps``), chat (``POST /api/chat``) and warm Quill's own model with
an empty chat (``warm``: the same call with no message, which loads the model
and generates nothing). It never pulls, deletes or unloads a model. The only
``keep_alive`` it may send is a positive duration ("30m", "2h": how long the
server keeps Quill's own model after Quill's request), set on the client by
``[autorewrite] keep_alive``; zero, negative, numeric and unload values are
refused before any request. Other projects' models are left to the server.
The address must be loopback (``quill.config`` already refuses any other); no
proxy is used. Errors name the call and the failure type, never the text sent.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

ALLOWED_CALLS = frozenset({("GET", "/api/tags"), ("GET", "/api/ps"), ("POST", "/api/chat")})
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
KEEP_ALIVE = re.compile(r"([1-9][0-9]{0,5})([smh])")
KEEP_ALIVE_UNIT_S = {"s": 1, "m": 60, "h": 3600}
# A warm-up waits for a whole cold load (about 2 to 10 s here): a client that gives up aborts it.
WARM_TIMEOUT_S = 60.0
CHECK_TIMEOUT_S = 0.5  # the read-only "is it in memory" check before a model call

log = logging.getLogger(__name__)


class OllamaError(OSError):
    """Ollama is unreachable or answered with an error. Carries no dictated text."""


def keep_alive_seconds(value: object) -> int | None:
    """The seconds of a positive ``keep_alive`` duration ("90s", "30m", "2h"); None for anything else.

    Zero, a negative or bare number, "-1", "0" and any other form (each of
    which could unload a model or keep it forever) are not durations here.
    """
    match = KEEP_ALIVE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    return int(match.group(1)) * KEEP_ALIVE_UNIT_S[match.group(2)]


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
    """``keep_alive`` (None: not sent, the server's default) goes with every chat and warm-up of this client."""

    def __init__(self, base_url: str, timeout_s: float = 10.0, keep_alive: str | None = None) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "http" or not _loopback(parts.hostname or "") or parts.path not in ("", "/"):
            raise ValueError("Ollama must be a local http://127.0.0.1:<port> address")
        if keep_alive is not None and keep_alive_seconds(keep_alive) is None:
            raise ValueError("Ollama keep_alive must be a positive duration such as 30m")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.keep_alive = keep_alive
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, payload: dict | None = None, timeout_s: float | None = None) -> dict:
        if (method, path) not in ALLOWED_CALLS:
            raise OllamaError(f"refused Ollama call {method} {path}")
        if payload is not None and "keep_alive" in payload:
            # Only Quill's own chat may carry it, and only as a positive duration: never an unload.
            if (method, path) != ("POST", "/api/chat") or keep_alive_seconds(payload["keep_alive"]) is None:
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

    def loaded(self, timeout_s: float | None = None) -> list[str]:
        """The models the server holds in memory now (``GET /api/ps``, read-only)."""
        models = self._call("GET", "/api/ps", timeout_s=timeout_s).get("models")
        return [m["name"] for m in models or [] if isinstance(m, dict) and isinstance(m.get("name"), str)]

    def chat(self, model: str, system: str, user: str, max_tokens: int | None = None,
             history: Sequence[tuple[str, str]] = (), timeout_s: float | None = None) -> ChatReply:
        """One deterministic chat turn with thinking disabled; ``max_tokens`` caps the reply.

        ``history`` holds earlier (user message, assistant reply) turns of the
        same conversation, sent before ``user``. ``timeout_s`` replaces the
        client's timeout for this call only.
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
            **self._keep_alive(),
        }
        data = self._call("POST", "/api/chat", payload, timeout_s=timeout_s)
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise OllamaError("Ollama /api/chat returned no message")
        return ChatReply(THINK_BLOCK.sub("", content).strip())

    def warm(self, model: str, timeout_s: float | None = None) -> None:
        """Load ``model`` if the server does not hold it: a chat with no message, which generates nothing.

        The server answers once the model is in memory; a client that gives up
        earlier makes the server abort the load, so ``timeout_s`` should cover a
        cold load.
        """
        payload = {"model": model, "messages": [], "stream": False, **self._keep_alive()}
        data = self._call("POST", "/api/chat", payload, timeout_s=timeout_s)
        if data.get("error"):
            raise OllamaError("Ollama /api/chat warm-up returned an error")

    def _keep_alive(self) -> dict:
        return {} if self.keep_alive is None else {"keep_alive": self.keep_alive}


def _daemon(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="quill-model-warm-up", daemon=True).start()


class ModelWarmer:
    """Loads Quill's own model in the background, so a correction does not pay a cold load.

    ``warm(reason)`` starts one warm-up (``OllamaClient.warm``) on its own
    thread and returns at once; while one is in flight another is not
    started. With ``unless_other`` the warm-up first reads which models are in
    memory and leaves the server alone when it holds another project's model
    (at Quill's start nothing needs the model yet). ``before_call(wait_s)``
    runs just before a model call: when the model is not in memory it starts
    a warm-up, then it waits at most ``wait_s`` seconds for the warm-up in
    flight and returns the seconds waited. Every failure is only logged: the
    model call that follows decides what is typed. ``spawn(job)`` runs
    ``job`` on another thread (tests: a recorder).
    """

    def __init__(self, client: object, model: str, *, timeout_s: float = WARM_TIMEOUT_S,
                 check_s: float = CHECK_TIMEOUT_S, clock: Callable[[], float] = time.perf_counter,
                 spawn: Callable[[Callable[[], None]], None] | None = None) -> None:
        self.client = client
        self.model = model
        self.timeout_s = timeout_s
        self.check_s = check_s
        self.clock = clock
        self.spawn = spawn or _daemon
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    @property
    def busy(self) -> bool:
        """Whether a warm-up is in flight."""
        return not self._idle.is_set()

    def warm(self, reason: str, *, unless_other: bool = False) -> bool:
        """Start a warm-up unless one is in flight; whether one was started. Never blocks on the server."""
        with self._lock:
            if not self._idle.is_set():
                return False
            self._idle.clear()
        try:
            self.spawn(lambda: self._run(reason, unless_other))
        except Exception as exc:  # noqa: BLE001 - no thread: the model call loads the model as before
            log.warning("model warm-up (%s) not started (%s)", reason, type(exc).__name__)
            self._idle.set()
            return False
        return True

    def _run(self, reason: str, unless_other: bool) -> None:
        started = self.clock()
        try:
            if unless_other:
                loaded = self.client.loaded(timeout_s=self.check_s)
                if self.model in loaded:
                    log.info("model warm-up (%s): already in memory", reason)
                    return
                if loaded:
                    log.info("model warm-up (%s) skipped: Ollama holds another model", reason)
                    return
            self.client.warm(self.model, timeout_s=self.timeout_s)
            log.info("model warm-up (%s) done in %.1f s", reason, self.clock() - started)
        except Exception as exc:  # noqa: BLE001 - Ollama down or slow: only logged
            log.warning("model warm-up (%s) failed after %.1f s (%s)", reason, self.clock() - started,
                        type(exc).__name__)
        finally:
            self._idle.set()

    def before_call(self, wait_s: float) -> float:
        """Wait (at most ``wait_s``) until the model is in memory or the warm-up gives up; the seconds waited."""
        started = self.clock()
        if self._idle.is_set():
            try:
                if self.model in self.client.loaded(timeout_s=self.check_s):
                    return 0.0
            except Exception:  # noqa: BLE001 - unknown state: the model call goes ahead
                return 0.0
            self.warm("before a model call")
        left = wait_s - (self.clock() - started)  # the check counts in the wait
        if left > 0:
            self._idle.wait(left)
        return max(0.0, self.clock() - started)
