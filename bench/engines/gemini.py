"""Gemini API audio transcription through generateContent with inline WAV data.

The model id is configurable (``[engines.gemini].model`` in local/bench.toml)
because free-tier models change. The key goes in the ``x-goog-api-key``
header, never in the URL. Vocabulary hints are added to the instruction.
"""

from __future__ import annotations

import base64
import json
import re

from bench.engines.base import CloudEngine, EngineError, Hints, Transport, parse_json

HOST = "generativelanguage.googleapis.com"
DEFAULT_MODEL = "gemini-2.5-flash"
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,63}$")
# Free tier allows only a few requests per minute on Flash models.
MIN_INTERVAL_S = 6.5

INSTRUCTION = (
    "Transcribe this audio verbatim. The speaker uses European Portuguese with some English "
    "technical terms and names. Write exactly what is said, in European Portuguese spelling, "
    "keeping English terms in English. Do not translate, summarize, correct or answer the "
    "content. Output only the transcription text."
)
HINTS_INSTRUCTION = "Vocabulary that may occur (spell these exactly like this): "


class GeminiEngine(CloudEngine):
    def __init__(
        self,
        api_key: str,
        transport: Transport,
        model: str = DEFAULT_MODEL,
        thinking_budget: int | None = 0,
    ) -> None:
        if not MODEL_ID.match(model):
            raise ValueError("invalid Gemini model id")
        super().__init__(transport)
        self.model = model
        self.thinking_budget = thinking_budget
        self.id = f"gemini-{model}"
        self._api_key = api_key

    def __repr__(self) -> str:
        return f"GeminiEngine({self.model!r})"

    def _instruction(self, hints: Hints | None) -> str:
        if hints and hints.vocabulary():
            return INSTRUCTION + "\n" + HINTS_INSTRUCTION + hints.joined(max_chars=2000) + "."
        return INSTRUCTION

    def params(self, hints: Hints | None) -> dict:
        return {
            "model": self.model,
            "instruction": self._instruction(hints),
            "temperature": 0,
            "thinking_budget": self.thinking_budget,
        }

    def transcribe(self, wav: bytes, hints: Hints | None) -> str:
        generation: dict = {"temperature": 0}
        if self.thinking_budget is not None:
            generation["thinkingConfig"] = {"thinkingBudget": self.thinking_budget}
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": self._instruction(hints)},
                        {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode("ascii")}},
                    ],
                }
            ],
            "generationConfig": generation,
        }
        url = f"https://{HOST}/v1beta/models/{self.model}:generateContent"
        response = self.transport.request(
            "POST",
            url,
            {"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
            json.dumps(payload).encode("utf-8"),
        )
        data = parse_json(response, HOST)
        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise EngineError(f"no candidates in the response from {HOST}")
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") if isinstance(candidate.get("content"), dict) else None
        texts = [p.get("text") for p in parts or [] if isinstance(p, dict) and isinstance(p.get("text"), str) and not p.get("thought")]
        if not texts:
            reason = candidate.get("finishReason")
            known = isinstance(reason, str) and re.fullmatch(r"[A-Z_]{1,40}", reason)
            raise EngineError(f"no text in the response from {HOST}" + (f" (finishReason {reason})" if known else ""))
        return " ".join("".join(texts).split())
