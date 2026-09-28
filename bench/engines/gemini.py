"""Gemini API audio transcription with inline WAV data.

The model id is configurable (``[engines.gemini].model`` in local/bench.toml)
because free-tier models change. The key goes in the ``x-goog-api-key``
header, never in the URL.

General models use generateContent and get the vocabulary hints in the
instruction. Dedicated speech-to-text models (``*-transcribe``) reject that
configuration, so they use the Interactions API with ``transcription_config``
(language and ``custom_vocabulary``) and ``store: false``, which keeps the
request out of server-side interaction storage.
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
# Interactions API settings for dedicated transcription models.
LANGUAGE_CODES = ("pt-PT",)
# The API accepts up to 1,000 terms; its guide reports the best results up to 100.
VOCABULARY_MAX_TERMS = 100


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
        self.transcription_model = model.endswith("-transcribe")
        self.id = f"gemini-{model}"
        self._api_key = api_key

    def __repr__(self) -> str:
        return f"GeminiEngine({self.model!r})"

    def _instruction(self, hints: Hints | None) -> str:
        if hints and hints.vocabulary():
            return INSTRUCTION + "\n" + HINTS_INSTRUCTION + hints.joined(max_chars=2000) + "."
        return INSTRUCTION

    def _vocabulary(self, hints: Hints | None) -> list[str]:
        return hints.vocabulary()[:VOCABULARY_MAX_TERMS] if hints else []

    def params(self, hints: Hints | None) -> dict:
        if self.transcription_model:
            return {
                "model": self.model,
                "api": "interactions",
                "language_codes": list(LANGUAGE_CODES),
                "custom_vocabulary": self._vocabulary(hints),
                "store": False,
            }
        return {
            "model": self.model,
            "instruction": self._instruction(hints),
            "temperature": 0,
            "thinking_budget": self.thinking_budget,
        }

    def transcribe(self, wav: bytes, hints: Hints | None) -> str:
        if self.transcription_model:
            return self._transcribe_interaction(wav, hints)
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

    def _transcribe_interaction(self, wav: bytes, hints: Hints | None) -> str:
        config: dict = {"language_codes": list(LANGUAGE_CODES)}
        vocabulary = self._vocabulary(hints)
        if vocabulary:
            config["custom_vocabulary"] = vocabulary
        payload = {
            "model": self.model,
            "input": [{"type": "audio", "data": base64.b64encode(wav).decode("ascii"), "mime_type": "audio/wav"}],
            "generation_config": {"transcription_config": config},
            "store": False,
        }
        response = self.transport.request(
            "POST",
            f"https://{HOST}/v1beta/interactions",
            {"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
            json.dumps(payload).encode("utf-8"),
        )
        data = parse_json(response, HOST)
        steps = data.get("steps") if isinstance(data, dict) else None
        texts = [
            item.get("text")
            for step in steps or []
            if isinstance(step, dict) and step.get("type") == "model_output" and isinstance(step.get("content"), list)
            for item in step["content"]
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        if not texts:
            status = data.get("status") if isinstance(data, dict) else None
            known = isinstance(status, str) and re.fullmatch(r"[a-z_]{1,40}", status)
            raise EngineError(f"no text in the response from {HOST}" + (f" (status {status})" if known else ""))
        return " ".join("".join(texts).split())
