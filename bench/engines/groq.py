"""Groq speech-to-text (Whisper large-v3 and large-v3-turbo), OpenAI-compatible REST.

Vocabulary hints go in the ``prompt`` field, which Groq documents as optional
context or spelling guidance (at most 224 tokens), so the prompt is kept short.
"""

from __future__ import annotations

import uuid

from bench.engines.base import CloudEngine, EngineError, Hints, Transport, parse_json

HOST = "api.groq.com"
URL = f"https://{HOST}/openai/v1/audio/transcriptions"
MODELS = ("whisper-large-v3", "whisper-large-v3-turbo")
# Free tier: 20 requests per minute for the Whisper models.
MIN_INTERVAL_S = 3.2


def multipart(fields: dict[str, str], file_field: str, file_name: str, file_type: str, data: bytes) -> tuple[bytes, str]:
    """A multipart/form-data body and its content type."""
    boundary = f"quill-{uuid.uuid4().hex}"
    lines: list[bytes] = []
    for name, value in fields.items():
        lines += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            value.encode("utf-8"),
        ]
    lines += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"'.encode(),
        f"Content-Type: {file_type}".encode(),
        b"",
        data,
        f"--{boundary}--".encode(),
        b"",
    ]
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


class GroqEngine(CloudEngine):
    def __init__(self, model: str, api_key: str, transport: Transport, language: str = "pt") -> None:
        if model not in MODELS:
            raise ValueError(f"unknown Groq model {model!r}")
        super().__init__(transport)
        self.model = model
        self.language = language
        self.id = f"groq-{model}"
        self._api_key = api_key

    def __repr__(self) -> str:
        return f"GroqEngine({self.model!r})"

    def _prompt(self, hints: Hints | None) -> str | None:
        return hints.joined() if hints else None

    def params(self, hints: Hints | None) -> dict:
        return {
            "model": self.model,
            "language": self.language,
            "temperature": 0,
            "prompt": self._prompt(hints),
        }

    def transcribe(self, wav: bytes, hints: Hints | None) -> str:
        fields = {"model": self.model, "language": self.language, "temperature": "0", "response_format": "json"}
        prompt = self._prompt(hints)
        if prompt:
            fields["prompt"] = prompt
        body, content_type = multipart(fields, "file", "audio.wav", "audio/wav", wav)
        response = self.transport.request(
            "POST", URL, {"Authorization": f"Bearer {self._api_key}", "Content-Type": content_type}, body
        )
        data = parse_json(response, HOST)
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise EngineError(f"no text in the response from {HOST}")
        return text.strip()
