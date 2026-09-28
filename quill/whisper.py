"""Local faster-whisper loader and decoder shared by the app and the benchmark.

faster-whisper and numpy are imported lazily, only when a model loads, so the
rest of Quill runs on the standard library. Models are read from the ignored
``models/`` folder with ``local_files_only``: nothing is ever downloaded. On
Windows the CUDA libraries shipped in the torch and nvidia wheels are
registered before the import. Audio never leaves the PC.

``Whisper.transcribe`` takes 16 kHz mono PCM16 bytes and one ``Decode`` set of
options. Every call uses temperature 0 without fallback, no VAD filter and no
conditioning on its own earlier windows, the settings of the Phase 2 baseline.
"""

from __future__ import annotations

import gc
import importlib
import importlib.util
import os
import sys
import types
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "models"
MODELS = {
    "large-v3": "faster-whisper-large-v3",
    "large-v3-turbo": "faster-whisper-large-v3-turbo",
}
# Sponsor decision 2026-09-29: large-v3-turbo is the default engine, because it
# meets the release-to-final latency target; large-v3 is the precise mode,
# selectable in local/quill.toml ([engine] model).
DEFAULT_MODEL = "large-v3-turbo"
PRECISE_MODEL = "large-v3"
MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")
SAMPLE_RATE = 16_000
# Whisper imitates the prompt's language and style, so the prompt is Portuguese.
HINTS_PREFIX = "Vocabulário: "
PROMPT_MAX_CHARS = 600
# Whisper's decoder context and faster-whisper's prompt limits (tokens).
MAX_LENGTH = 448
PROMPT_PART_MAX = MAX_LENGTH // 2 - 1
SOT_SEQUENCE_TOKENS = 3  # start of transcript, language, task

_dll_handles: list = []


class WhisperUnavailable(RuntimeError):
    """The model cannot run: missing files, missing packages or a failed load."""


class WhisperError(RuntimeError):
    """One transcription failed. The message never contains audio or text."""


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
    huggingface_hub 1.x no longer depends on ``requests``, and Quill never
    downloads (models load from a local folder), so a module exposing that one
    exception type is enough and avoids another install.
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


# ---------------------------------------------------------------- vocabulary


def join_vocabulary(vocabulary: Sequence[str], max_chars: int = PROMPT_MAX_CHARS, separator: str = ", ") -> str:
    """Words joined in order up to ``max_chars``; the first word that does not fit ends the list."""
    out = ""
    for word in vocabulary:
        candidate = word if not out else out + separator + word
        if len(candidate) > max_chars:
            break
        out = candidate
    return out


def hint_options(vocabulary: Sequence[str], max_chars: int = PROMPT_MAX_CHARS) -> tuple[str | None, str | None]:
    """(initial_prompt, hotwords) for a vocabulary list, or (None, None) when it is empty."""
    joined = join_vocabulary(vocabulary, max_chars)
    if not joined:
        return None, None
    return HINTS_PREFIX + joined + ".", join_vocabulary(vocabulary, max_chars, separator=" ")


# ---------------------------------------------------------------- decoding


@dataclass(frozen=True)
class Decode:
    """Options of one transcription call."""

    beam_size: int = 5
    initial_prompt: str | None = field(default=None, repr=False)
    hotwords: str | None = field(default=None, repr=False)
    word_timestamps: bool = False
    without_timestamps: bool = False
    # Upper bound on generated tokens per 30 s window; stops a runaway
    # decode on near-silent audio. None keeps faster-whisper's default.
    max_new_tokens: int | None = None


@dataclass(frozen=True)
class Word:
    """One recognized word; times in seconds from the start of the audio passed."""

    start: float
    end: float
    text: str = field(repr=False)


@dataclass(frozen=True)
class Transcript:
    text: str = field(repr=False)
    words: tuple[Word, ...] = field(default=(), repr=False)


class Whisper:
    """One faster-whisper model on one device, loaded once and kept warm."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        models_dir: Path = MODELS_DIR,
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "pt",
    ) -> None:
        if model not in MODELS:
            raise ValueError(f"unknown faster-whisper model {model!r}")
        self.model = model
        self.models_dir = Path(models_dir)
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.id = f"faster-whisper-{model}"
        self._whisper = None
        self._numpy = None

    def __repr__(self) -> str:
        return f"Whisper({self.model!r}, {self.device!r})"

    @property
    def loaded(self) -> bool:
        return self._whisper is not None

    def load(self) -> None:
        if self._whisper is not None:
            return
        if not model_present(self.model, self.models_dir):
            raise WhisperUnavailable(f"{self.id}: model files missing in models/{MODELS[self.model]}")
        if self.device == "cuda":
            register_cuda_dlls()
        try:
            if importlib.util.find_spec("faster_whisper") is None:
                raise ImportError("faster_whisper")
            shim_requests()
            faster_whisper = importlib.import_module("faster_whisper")
            self._numpy = importlib.import_module("numpy")
        except ImportError:
            raise WhisperUnavailable(f"{self.id}: faster-whisper is not installed in this interpreter") from None
        try:
            self._whisper = faster_whisper.WhisperModel(
                str(model_dir(self.model, self.models_dir)),
                device=self.device,
                compute_type=self.compute_type,
                local_files_only=True,
            )
        except Exception as exc:  # CTranslate2 raises RuntimeError/ValueError subclasses
            raise WhisperUnavailable(f"{self.id}: failed to load on {self.device}: {type(exc).__name__}") from None

    def close(self) -> None:
        self._whisper = None
        gc.collect()

    def _prompt_length(self, options: Decode) -> int | None:
        """Tokens faster-whisper puts before the first generated token, or None if unknown."""
        tokenizer = getattr(self._whisper, "hf_tokenizer", None)
        if tokenizer is None:
            return None

        def count(text: str | None) -> int:
            if not text:
                return 0
            return min(len(tokenizer.encode(" " + text.strip(), add_special_tokens=False).ids), PROMPT_PART_MAX)

        hot, previous = count(options.hotwords), count(options.initial_prompt)
        return (1 if hot or previous else 0) + hot + previous + SOT_SEQUENCE_TOKENS + (1 if options.without_timestamps else 0)

    def _max_new_tokens(self, options: Decode) -> int | None:
        if options.max_new_tokens is None:
            return None
        prompt = self._prompt_length(options)
        room = None if prompt is None else MAX_LENGTH - prompt
        if room is None or room < 1:
            return None  # faster-whisper refuses a bound it cannot honour
        return max(1, min(options.max_new_tokens, room))

    def transcribe(self, pcm: bytes, options: Decode = Decode()) -> Transcript:
        """Text (and words when asked) of 16 kHz mono PCM16 audio."""
        if self._whisper is None:
            self.load()
        numpy = self._numpy
        audio = numpy.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=numpy.int16).astype(numpy.float32) / 32768.0
        extra = {}
        if options.word_timestamps:
            extra["word_timestamps"] = True
        if options.without_timestamps:
            extra["without_timestamps"] = True
        max_new = self._max_new_tokens(options)
        if max_new is not None:
            extra["max_new_tokens"] = max_new
        try:
            segments, _info = self._whisper.transcribe(
                audio,
                language=self.language,
                beam_size=options.beam_size,
                temperature=0.0,
                vad_filter=False,
                condition_on_previous_text=False,
                initial_prompt=options.initial_prompt,
                hotwords=options.hotwords,
                **extra,
            )
            texts: list[str] = []
            words: list[Word] = []
            for segment in segments:
                texts.append(segment.text.strip())
                for word in segment.words or ():
                    text = word.word.strip()
                    if text:
                        words.append(Word(float(word.start), float(word.end), text))
        except Exception as exc:
            raise WhisperError(f"{self.id}: transcription failed: {type(exc).__name__}") from None
        return Transcript(" ".join(" ".join(texts).split()), tuple(words))
