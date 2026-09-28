"""Local faster-whisper (large-v3 and large-v3-turbo) on CUDA float16.

faster-whisper is imported lazily, only when the engine loads, so the rest of
the benchmark runs on the standard library. Models are read from the ignored
``models/`` folder with ``local_files_only``: the benchmark never downloads a
model (``bench.run --preflight`` prints the download command instead).
"""

from __future__ import annotations

import gc
import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path

from bench.engines.base import Engine, EngineError, EngineUnavailable, Hints, wav_pcm
from bench.settings import REPO_ROOT

MODELS_DIR = REPO_ROOT / "models"
MODELS = {
    "large-v3": "faster-whisper-large-v3",
    "large-v3-turbo": "faster-whisper-large-v3-turbo",
}
MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")
# Whisper imitates the prompt's language and style, so the prompt is Portuguese.
HINTS_PREFIX = "Vocabulário: "

_dll_handles: list = []


def model_dir(model: str, models_dir: Path = MODELS_DIR) -> Path:
    return Path(models_dir) / MODELS[model]


def model_present(model: str, models_dir: Path = MODELS_DIR) -> bool:
    folder = model_dir(model, models_dir)
    return all((folder / name).is_file() for name in MODEL_FILES)


def register_cuda_dlls() -> list[str]:
    """On Windows, let CTranslate2 find cuBLAS/cuDNN shipped in torch or nvidia wheels.

    Locates the packages without importing them; returns the folder names added.
    CTranslate2 loads cuBLAS lazily with the default DLL search order, which
    ignores ``add_dll_directory``, so the folders also go at the front of this
    process's PATH (the environment of Windows itself is not changed).
    """
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return []
    folders: list[Path] = []
    torch_spec = importlib.util.find_spec("torch")
    if torch_spec is not None and torch_spec.origin:
        folders.append(Path(torch_spec.origin).parent / "lib")
    nvidia_spec = importlib.util.find_spec("nvidia")
    if nvidia_spec is not None and nvidia_spec.submodule_search_locations:
        for location in nvidia_spec.submodule_search_locations:
            folders.extend(sorted(Path(location).glob("*/bin")))
    added = []
    for folder in folders:
        if folder.is_dir():
            try:
                _dll_handles.append(os.add_dll_directory(str(folder)))
                added.append(folder.name)
            except OSError:
                continue
            path = os.environ.get("PATH", "")
            if str(folder).lower() not in (part.lower() for part in path.split(os.pathsep)):
                os.environ["PATH"] = str(folder) + (os.pathsep + path if path else "")
    return added


def shim_requests() -> bool:
    """Stand in for ``requests`` when it is absent; True when the shim was registered.

    faster-whisper 1.1.1 imports ``requests`` only to name
    ``requests.exceptions.ConnectionError`` in its model download helper.
    huggingface_hub 1.x no longer depends on ``requests``, and the benchmark
    never downloads (models load from a local folder), so a module exposing
    that one exception type is enough and avoids another install.
    """
    if "requests" in sys.modules or importlib.util.find_spec("requests") is not None:
        return False
    exceptions = types.ModuleType("requests.exceptions")
    exceptions.ConnectionError = type("ConnectionError", (OSError,), {})
    shim = types.ModuleType("requests")
    shim.exceptions = exceptions
    sys.modules["requests"] = shim
    sys.modules["requests.exceptions"] = exceptions
    return True


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
        self._whisper = None
        self._numpy = None

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
        if self._whisper is not None:
            return
        if not model_present(self.model, self.models_dir):
            raise EngineUnavailable(
                f"{self.id}: model files missing in models/{MODELS[self.model]} (see bench.run --preflight)"
            )
        if self.device == "cuda":
            register_cuda_dlls()
        try:
            if importlib.util.find_spec("faster_whisper") is None:
                raise ImportError("faster_whisper")
            shim_requests()
            faster_whisper = importlib.import_module("faster_whisper")
            self._numpy = importlib.import_module("numpy")
        except ImportError:
            raise EngineUnavailable(
                f"{self.id}: faster-whisper is not installed in this interpreter (see bench.run --preflight)"
            ) from None
        try:
            self._whisper = faster_whisper.WhisperModel(
                str(model_dir(self.model, self.models_dir)),
                device=self.device,
                compute_type=self.compute_type,
                local_files_only=True,
            )
        except Exception as exc:  # CTranslate2 raises RuntimeError/ValueError subclasses
            raise EngineUnavailable(f"{self.id}: failed to load on {self.device}: {type(exc).__name__}") from None

    def close(self) -> None:
        self._whisper = None
        gc.collect()

    def transcribe(self, wav: bytes, hints: Hints | None) -> str:
        if self._whisper is None:
            self.load()
        pcm, rate = wav_pcm(wav)
        if rate != 16_000:
            raise EngineError(f"{self.id}: audio must be 16 kHz")
        audio = self._numpy.frombuffer(pcm, dtype=self._numpy.int16).astype(self._numpy.float32) / 32768.0
        try:
            segments, _info = self._whisper.transcribe(
                audio,
                language=self.language,
                beam_size=self.beam_size,
                temperature=0.0,
                vad_filter=False,
                condition_on_previous_text=False,
                **self._hint_options(hints),
            )
            text = " ".join(segment.text.strip() for segment in segments)
        except Exception as exc:
            raise EngineError(f"{self.id}: transcription failed: {type(exc).__name__}") from None
        return " ".join(text.split())
