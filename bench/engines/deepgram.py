"""Deepgram Nova-3 pre-recorded transcription.

Language: Deepgram's models and languages overview lists ``pt``, ``pt-BR`` and
``pt-PT`` for Nova-3; the benchmark uses ``pt-PT`` (European Portuguese) by
default. Vocabulary hints use ``keyterm``, which the keyterm prompting page
documents for monolingual and multilingual Nova-3 (at most 100 key terms).
Both pages were checked on 2026-09-28; ``[engines.deepgram]`` in
local/bench.toml can change the language or disable keyterm.
"""

from __future__ import annotations

import urllib.parse

from bench.engines.base import CloudEngine, EngineError, Hints, Transport, parse_json

HOST = "api.deepgram.com"
URL = f"https://{HOST}/v1/listen"
MODEL = "nova-3"
DEFAULT_LANGUAGE = "pt-PT"
MAX_KEYTERMS = 100


class DeepgramEngine(CloudEngine):
    id = "deepgram-nova-3"

    def __init__(self, api_key: str, transport: Transport, language: str = DEFAULT_LANGUAGE, keyterm: bool = True) -> None:
        super().__init__(transport)
        self.language = language
        self.keyterm = keyterm
        self._api_key = api_key

    def __repr__(self) -> str:
        return f"DeepgramEngine({self.language!r})"

    def hints_unsupported(self) -> str | None:
        if not self.keyterm:
            return "keyterm prompting disabled in [engines.deepgram] settings"
        return None

    def _keyterms(self, hints: Hints | None) -> list[str]:
        return hints.vocabulary()[:MAX_KEYTERMS] if hints else []

    def params(self, hints: Hints | None) -> dict:
        return {"model": MODEL, "language": self.language, "punctuate": True, "keyterm": self._keyterms(hints)}

    def transcribe(self, wav: bytes, hints: Hints | None) -> str:
        if hints is not None and self.hints_unsupported():
            raise EngineError(self.hints_unsupported())
        query = [("model", MODEL), ("language", self.language), ("punctuate", "true")]
        query += [("keyterm", term) for term in self._keyterms(hints)]
        url = URL + "?" + urllib.parse.urlencode(query)
        response = self.transport.request(
            "POST", url, {"Authorization": f"Token {self._api_key}", "Content-Type": "audio/wav"}, wav
        )
        data = parse_json(response, HOST)
        try:
            text = data["results"]["channels"][0]["alternatives"][0]["transcript"]
        except (KeyError, IndexError, TypeError):
            raise EngineError(f"no transcript in the response from {HOST}") from None
        if not isinstance(text, str):
            raise EngineError(f"no transcript in the response from {HOST}")
        return text.strip()
