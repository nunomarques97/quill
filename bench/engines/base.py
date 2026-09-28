"""Engine interface, vocabulary hints, the HTTPS transport and the response cache.

Every engine turns the bytes of one 16 kHz mono PCM16 WAV file into text.
Cloud engines talk HTTPS through ``Transport``, which only reaches the three
allowed provider hosts, never follows redirects, never uses a proxy, retries
HTTP 429/5xx within a bounded wait and redacts key values from every message
it produces.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bench.envfile import redact

ALLOWED_HOSTS = frozenset({"api.groq.com", "generativelanguage.googleapis.com", "api.deepgram.com"})
USER_AGENT = "quill-bench/0.1"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
PROMPT_MAX_CHARS = 600


class EngineError(Exception):
    """An engine call failed. The message is short, redacted and has no spoken text."""


class EngineUnavailable(EngineError):
    """The engine cannot run at all (missing key, package or model)."""


# ---------------------------------------------------------------- hints


@dataclass(frozen=True)
class Hints:
    """Vocabulary passed to an engine: runtime project names, then generic terms."""

    names: tuple[str, ...] = field(repr=False)
    terms: tuple[str, ...] = ()

    def vocabulary(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for word in (*self.names, *self.terms):
            word = " ".join(word.split())
            if word and word.casefold() not in seen:
                seen.add(word.casefold())
                result.append(word)
        return result

    def joined(self, max_chars: int = PROMPT_MAX_CHARS, separator: str = ", ") -> str:
        """Vocabulary joined up to ``max_chars``; names come first so they survive."""
        out = ""
        for word in self.vocabulary():
            candidate = word if not out else out + separator + word
            if len(candidate) > max_chars:
                break
            out = candidate
        return out


def build_hints(names: Iterable[str], terms: Iterable[str]) -> Hints:
    return Hints(names=tuple(sorted(set(names))), terms=tuple(terms))


# ---------------------------------------------------------------- engine


class Engine(ABC):
    """One speech-to-text engine. Subclasses set ``id`` and ``cacheable``."""

    id: str = "engine"
    #: Cloud engines cache responses so a rerun never resends audio.
    cacheable: bool = False

    def hints_unsupported(self) -> str | None:
        """None when vocabulary hints are supported, else the documented reason."""
        return None

    @abstractmethod
    def params(self, hints: Hints | None) -> dict:
        """Everything that changes the output (the cache key). Never a key value."""

    def load(self) -> None:
        """Load models or check prerequisites. Raises EngineUnavailable."""

    def close(self) -> None:
        """Release models and memory owned by this engine."""

    def wait_turn(self) -> None:
        """Block until a request may start without rate pacing (called before timing)."""

    @property
    def last_attempts(self) -> int:
        """HTTP attempts of the last call; above 1 a latency sample is not clean."""
        return 1

    @abstractmethod
    def transcribe(self, wav: bytes, hints: Hints | None) -> str:
        """Text for one WAV file. Raises EngineError."""


# ---------------------------------------------------------------- audio


def pcm16_wav(pcm: bytes, sample_rate: int = 16_000) -> bytes:
    """A mono PCM16 WAV file around raw samples."""
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def wav_pcm(wav: bytes) -> tuple[bytes, int]:
    """Raw PCM16 samples and sample rate of a mono PCM16 WAV file."""
    import io
    import wave

    with wave.open(io.BytesIO(wav), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise EngineError("audio is not mono PCM16")
        return handle.readframes(handle.getnframes()), handle.getframerate()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- transport


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)


Sender = Callable[[urllib.request.Request, float], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a redirect could carry the key to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def urllib_send(request: urllib.request.Request, timeout: float) -> HttpResponse:
    """Send with the stdlib: verified TLS, no proxy, no redirects."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return HttpResponse(response.status, _lower(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except OSError:
            body = b""
        return HttpResponse(exc.code, _lower(exc.headers or {}), body)


def _lower(headers) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in headers.items()}


def check_url(url: str, allowed: frozenset[str] = ALLOWED_HOSTS) -> str:
    """Return the host when ``url`` is HTTPS on an allowed host, else raise."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in allowed or parts.username or parts.password:
        raise EngineError("refused request: only HTTPS to the allowed provider hosts")
    if parts.port not in (None, 443):
        raise EngineError("refused request: only the default HTTPS port")
    return host


def retry_after_seconds(headers: Mapping[str, str], now: Callable[[], datetime] | None = None) -> float | None:
    """Retry-After as seconds (delta or HTTP date); None when absent or invalid."""
    value = headers.get("retry-after")
    if value is None:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now() if now else datetime.now(timezone.utc)
    return max(0.0, (when - current).total_seconds())


def error_detail(body: bytes, secrets: Iterable[str]) -> str:
    """A short provider error message from a JSON error body, redacted."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    message = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("status")
        elif isinstance(error, str):
            message = error
        message = message or data.get("err_msg") or data.get("message")
    if not isinstance(message, str):
        return ""
    return redact(" ".join(message.split()), secrets)[:160]


class Transport:
    """HTTPS requests to the allowed hosts with pacing, retries and redaction."""

    def __init__(
        self,
        secrets: Iterable[str],
        *,
        send: Sender | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = 120.0,
        max_attempts: int = 5,
        max_wait_s: float = 120.0,
        backoff_s: float = 2.0,
        min_interval_s: float = 0.0,
    ) -> None:
        self._secrets = tuple(s for s in secrets if s)
        self._send = send or urllib_send
        self._sleep = sleep
        self._clock = clock
        self.timeout_s = timeout_s
        self.max_attempts = max(1, max_attempts)
        self.max_wait_s = max_wait_s
        self.backoff_s = backoff_s
        self.min_interval_s = min_interval_s
        self._last_start: float | None = None
        self.last_attempts = 0

    def wait_turn(self) -> None:
        """Sleep until ``min_interval_s`` has passed since the previous request."""
        if self._last_start is None or self.min_interval_s <= 0:
            return
        remaining = self.min_interval_s - (self._clock() - self._last_start)
        if remaining > 0:
            self._sleep(remaining)

    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
        host = check_url(url)
        all_headers = {"User-Agent": USER_AGENT, **headers}
        self.last_attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            self.wait_turn()
            self._last_start = self._clock()
            self.last_attempts = attempt
            request = urllib.request.Request(url, data=body, headers=all_headers, method=method)
            try:
                response = self._send(request, self.timeout_s)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                reason = f"network error contacting {host}: {type(exc).__name__}"
                if attempt == self.max_attempts:
                    raise EngineError(redact(reason, self._secrets)) from None
                self._sleep(self._backoff(attempt))
                continue
            if response.status in RETRY_STATUSES:
                wait = retry_after_seconds(response.headers)
                if wait is None:
                    wait = self._backoff(attempt)
                if wait > self.max_wait_s:
                    raise EngineError(
                        f"HTTP {response.status} from {host}: retry after {wait:.0f} s exceeds "
                        f"{self.max_wait_s:.0f} s (free-tier limit reached)"
                    )
                if attempt == self.max_attempts:
                    raise EngineError(f"HTTP {response.status} from {host} after {attempt} attempts")
                self._sleep(wait)
                continue
            if response.status >= 400:
                detail = error_detail(response.body, self._secrets)
                message = f"HTTP {response.status} from {host}" + (f": {detail}" if detail else "")
                raise EngineError(redact(message, self._secrets))
            return response
        raise EngineError(f"no response from {host}")  # pragma: no cover - loop always returns or raises

    def _backoff(self, attempt: int) -> float:
        return self.backoff_s * (2 ** (attempt - 1))


def parse_json(response: HttpResponse, host: str) -> object:
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EngineError(f"invalid JSON from {host}") from None


class CloudEngine(Engine):
    """Shared plumbing for engines that use ``Transport``."""

    cacheable = True

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def wait_turn(self) -> None:
        self.transport.wait_turn()

    @property
    def last_attempts(self) -> int:
        return self.transport.last_attempts


# ---------------------------------------------------------------- cache


class ResponseCache:
    """JSON records under an ignored folder, keyed by a hash of their inputs.

    Keys hash the audio hash, engine id and parameters; values hold only what
    the benchmark needs (text and timings), never request headers or keys.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @staticmethod
    def key(**parts: object) -> str:
        blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def put(self, key: str, value: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
