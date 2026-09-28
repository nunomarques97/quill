"""Local faster-whisper (large-v3 and large-v3-turbo) on CUDA float16.

The loader and decoder live in ``quill.whisper`` and are shared with the app:
faster-whisper is imported lazily, only when the engine loads, and models are
read from the ignored ``models/`` folder with ``local_files_only``, so the
benchmark never downloads a model (``bench.run --preflight`` prints the
download command instead).
"""

from __future__ import annotations

import importlib  # noqa: F401  (tests patch importlib.util through this module)
import importlib.util  # noqa: F401
import os  # noqa: F401
from pathlib import Path

from bench.engines.base import Engine, EngineError, EngineUnavailable, Hints, wav_pcm
from quill import whisper
from quill.whisper import (  # noqa: F401
    HINTS_PREFIX,
    MODEL_FILES,
    MODELS,
    MODELS_DIR,
    Decode,
    Whisper,
    WhisperError,
    WhisperUnavailable,
    model_dir,
    model_present,
    register_cuda_dlls,
    shim_requests,
)

_dll_handles = whisper._dll_handles


class LocalWhisperEngine(Engine):
    cacheable = False

    def __init__(
        self,
        model: str,
        models_dir: Path = MODELS_DIR,
        device: str = "cuda",
        compute_type: str = "float16",
        beam_size: int = 5,
        language: str = "pt",
    ) -> None:
        if model not in MODELS:
            raise ValueError(f"unknown faster-whisper model {model!r}")
        self.model = model
        self.models_dir = Path(models_dir)
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.language = language
        self.id = f"faster-whisper-{model}"
        self.whisper = Whisper(model, self.models_dir, device, compute_type, language)

    def __repr__(self) -> str:
        return f"LocalWhisperEngine({self.model!r})"

    def _hint_options(self, hints: Hints | None) -> dict:
        if not hints or not hints.vocabulary():
            return {"initial_prompt": None, "hotwords": None}
        return {"initial_prompt": HINTS_PREFIX + hints.joined() + ".", "hotwords": hints.joined(separator=" ")}

    def params(self, hints: Hints | None) -> dict:
        return {
            "model": self.model,
            "device": self.device,
            "compute_type": self.compute_type,
            "beam_size": self.beam_size,
            "language": self.language,
            **self._hint_options(hints),
        }

    def load(self) -> None:
        try:
            self.whisper.load()
        except WhisperUnavailable as exc:
            raise EngineUnavailable(f"{exc} (see bench.run --preflight)") from None

    def close(self) -> None:
        self.whisper.close()

    def transcribe(self, wav: bytes, hints: Hints | None) -> str:
        self.load()
        pcm, rate = wav_pcm(wav)
        if rate != 16_000:
            raise EngineError(f"{self.id}: audio must be 16 kHz")
        try:
            return self.whisper.transcribe(pcm, Decode(beam_size=self.beam_size, **self._hint_options(hints))).text
        except WhisperError as exc:
            raise EngineError(str(exc)) from None
