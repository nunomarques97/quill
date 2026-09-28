"""Local text cleanup with Ollama (qwen3:8b) and the minimal Ollama client.

Ollama is shared with other projects. The client can only read the version,
the installed models (/api/tags), the loaded models (/api/ps) and chat
(/api/chat). It never pulls, deletes or unloads a model, and never sends
``keep_alive``, so other projects' models are left exactly as they are.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

OLLAMA_URL = "http://127.0.0.1:11434"
CLEANUP_MODEL = "qwen3:8b"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
ALLOWED_CALLS = frozenset({("GET", "/api/version"), ("GET", "/api/tags"), ("GET", "/api/ps"), ("POST", "/api/chat")})
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class OllamaError(Exception):
    """Ollama is unreachable or answered with an error. Carries no spoken text."""


@dataclass(frozen=True)
class ChatResult:
    content: str
    load_s: float
    total_s: float


class OllamaClient:
    def __init__(self, base_url: str = OLLAMA_URL, timeout_s: float = 180.0, clock: Callable[[], float] = time.perf_counter) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "http" or (parts.hostname or "") not in LOCAL_HOSTS or parts.path not in ("", "/"):
            raise ValueError("Ollama must be a local http://127.0.0.1:<port> address")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._clock = clock
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        if (method, path) not in ALLOWED_CALLS:
            raise OllamaError(f"refused Ollama call {method} {path}")
        if payload is not None and "keep_alive" in payload:
            raise OllamaError("refused Ollama call with keep_alive")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                parsed = json.loads(exc.read().decode("utf-8"))
                if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
                    detail = ": " + " ".join(parsed["error"].split())[:160]
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise OllamaError(f"Ollama {path} returned HTTP {exc.code}{detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OllamaError(f"Ollama unreachable at {self.base_url}: {type(exc).__name__}") from None
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OllamaError(f"Ollama {path} returned invalid JSON") from None
        if not isinstance(parsed, dict):
            raise OllamaError(f"Ollama {path} returned an unexpected body")
        return parsed

    def version(self) -> str:
        value = self._call("GET", "/api/version").get("version")
        return value if isinstance(value, str) else "unknown"

    def installed(self) -> list[str]:
        models = self._call("GET", "/api/tags").get("models")
        return [m["name"] for m in models or [] if isinstance(m, dict) and isinstance(m.get("name"), str)]

    def loaded(self) -> list[dict]:
        """Loaded models with their VRAM use in MiB."""
        models = self._call("GET", "/api/ps").get("models")
        result = []
        for model in models or []:
            if isinstance(model, dict) and isinstance(model.get("name"), str):
                vram = model.get("size_vram")
                size = model.get("size")
                result.append(
                    {
                        "name": model["name"],
                        "vram_mib": round(vram / 2**20) if isinstance(vram, (int, float)) else None,
                        "size_mib": round(size / 2**20) if isinstance(size, (int, float)) else None,
                    }
                )
        return result

    def chat(
        self, model: str, system: str, user: str, schema: dict | None = None, max_tokens: int | None = None
    ) -> ChatResult:
        """One deterministic chat turn with thinking disabled; ``max_tokens`` caps the reply."""
        options: dict = {"temperature": 0, "seed": 0}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        payload: dict = {
            "model": model,
            "messages": [{"content": system, "role": "system"}, {"content": user, "role": "user"}],
            "stream": False,
            "think": False,
            "options": options,
        }
        if schema is not None:
            payload["format"] = schema
        started = self._clock()
        data = self._call("POST", "/api/chat", payload)
        total = self._clock() - started
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise OllamaError("Ollama /api/chat returned no message")
        load_ns = data.get("load_duration")
        load_s = load_ns / 1e9 if isinstance(load_ns, (int, float)) else 0.0
        return ChatResult(content=THINK_BLOCK.sub("", content).strip(), load_s=load_s, total_s=total)


CLEANUP_SYSTEM = (
    "You clean up dictated text. The text is European Portuguese and may contain English "
    "technical terms and names. Add punctuation and capitalization. Remove filler words "
    "(such as hum, hã, é pá, tipo or pronto when they are only fillers) and accidental "
    "repetitions. Keep every English term and every name exactly as written. Do not "
    "translate, paraphrase, summarize, answer or add anything. Reply with the cleaned text only."
)
VOCABULARY_LINE = "\nWhen a word clearly refers to one of these, spell it exactly like this: "
# Reply cap: a cleaned text is about as long as its input. Without a cap, a
# repetition loop hallucinated by the engine can make the model loop too until
# the request times out.
CLEANUP_TOKENS_PER_WORD = 4
CLEANUP_MIN_TOKENS = 64


@dataclass(frozen=True)
class Cleaner:
    """Cleanup with one fixed prompt; the vocabulary is only used by hints+cleanup."""

    client: OllamaClient
    model: str = CLEANUP_MODEL
    vocabulary: tuple[str, ...] = ()

    def system_prompt(self) -> str:
        if self.vocabulary:
            return CLEANUP_SYSTEM + VOCABULARY_LINE + ", ".join(self.vocabulary) + "."
        return CLEANUP_SYSTEM

    def params(self) -> dict:
        prompt_hash = hashlib.sha256(self.system_prompt().encode("utf-8")).hexdigest()[:16]
        return {"model": self.model, "prompt": prompt_hash, "temperature": 0, "think": False}

    @staticmethod
    def max_tokens(text: str) -> int:
        return max(CLEANUP_MIN_TOKENS, CLEANUP_TOKENS_PER_WORD * len(text.split()))

    def clean(self, text: str) -> ChatResult:
        if not text.strip():
            return ChatResult(content="", load_s=0.0, total_s=0.0)
        return self.client.chat(self.model, self.system_prompt(), text, max_tokens=self.max_tokens(text))
