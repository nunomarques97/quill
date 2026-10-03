"""The own-voice clips of the alert project names: one short recording per name.

``python -m quill.names`` records them; the alerts look them up by
``clip_key`` (the name as ``quill.speech.spoken`` says it, casefolded, with
accents folded and spaces collapsed, so ``x-public``, ``X Public`` and
``x_public`` share one clip).

The clips live only in the ignored ``local/names`` folder (``ClipStore``
refuses any folder outside ``local/``), with ``manifest.json``:

    {"version": 1,
     "clips": {"<key>": {"file": "clip-<hash>.wav", "display": "...", "duration_s": 0.8,
                         "recorded_at": "2026-01-01T10:00:00"}},
     "skipped": ["<key>", ...],
     "added": ["<name>", ...]}

A clip file is named from a hash of its key, never from the spoken name, and
is a mono PCM16 WAV at 16 kHz written under a temporary name and then
renamed, like the manifest. A manifest entry whose file is not a plain
``clip-<hash>.wav`` basename of a regular file directly inside the folder is
ignored, so an edited manifest can never point outside it.

``trim_and_normalise`` prepares a take: the silence before and after the
name is cut by 20 ms frame level (keeping a short pad), and the speech is
brought to one fixed loudness with its peak kept below full scale, so every
name plays at the same level.

At alert time ``ClipStore.clips_for`` reads the clip of each spoken name
(``quill.speech.Speaker`` calls it on its worker thread, never under the
session lock). A clip is used only when it is a RIFF PCM16 mono WAV of at
most ``MAX_CLIP_BYTES`` and ``MAX_CLIP_S``; a missing, unreadable, corrupt,
oversized or wrongly formatted clip, or an unreadable manifest, gives no clip
(the name keeps the Windows voice) and logs only a reason code
(``REASONS``), never a name or a path.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import stat
import unicodedata
import wave
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from struct import error as struct_error

from quill.audio import SAMPLE_RATE, SAMPLE_WIDTH
from quill.config import LOCAL_DIR
from quill.speech import MAX_NAME_CHARS, spoken

log = logging.getLogger("quill.clips")

CLIPS_DIR = LOCAL_DIR / "names"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
CLIP_FILE = re.compile(r"^clip-[0-9a-f]{24}\.wav$")
PART_SUFFIX = ".part"
MAX_KEY_CHARS = 4 * MAX_NAME_CHARS  # casefolding and accent folding may lengthen a name

# Trimming and loudness (full scale 1.0).
FRAME_S = 0.02
PAD_S = 0.1  # kept before the first and after the last voiced frame
VOICED_FLOOR = 0.006  # a frame below this RMS is silence, however quiet the take
VOICED_RATIO = 0.1  # ... and so is a frame below this share of the loudest frame
MIN_CLIP_S = 0.2  # voiced span
MAX_CLIP_S = 4.0  # whole trimmed clip, pads included
TARGET_RMS = 0.1  # about -20 dBFS over the voiced frames
MAX_PEAK = 0.89  # about -1 dBFS

# A clip played at alert time: any common rate, at most MAX_CLIP_S, and a file no larger than that at 48 kHz.
PLAY_RATES = (8000, 48000)
PCM_TAGS = (1, 0xFFFE)  # WAVE_FORMAT_PCM, and WAVE_FORMAT_EXTENSIBLE (whose PCM subformat ``wave`` checks)
MAX_CLIP_BYTES = 1024 + int(MAX_CLIP_S * PLAY_RATES[1]) * SAMPLE_WIDTH
# Why a recorded clip was not used (logged; the name keeps the Windows voice).
MANIFEST = "manifest"
MISSING = "missing"
UNREADABLE = "unreadable"
OVERSIZED = "oversized"
CORRUPT = "corrupt"
FORMAT = "format"
REASONS = (MANIFEST, MISSING, UNREADABLE, OVERSIZED, CORRUPT, FORMAT)

TOO_QUIET = "não se ouviu nenhum nome; verifica o microfone e fala mais perto"
TOO_SHORT = "o nome ficou demasiado curto (menos de 0,2 s)"
TOO_LONG = f"o nome ficou demasiado longo (mais de {MAX_CLIP_S:.0f} s); diz só o nome"


class ClipsError(ValueError):
    """The clips folder or manifest cannot be used; the message never holds a name."""


class ClipUnusable(Exception):
    """A recorded clip cannot be played; ``reason`` is one of ``REASONS``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Clip:
    """The samples of a validated clip: mono PCM16 at ``rate``."""

    pcm: bytes = field(repr=False)
    rate: int


def clip_key(name: str) -> str:
    """The lookup key of a name: spoken form, casefolded, accents folded, spaces collapsed."""
    text = spoken(name).casefold() if isinstance(name, str) else ""
    folded = "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))
    return " ".join(folded.split())


def clip_file_name(key: str) -> str:
    return "clip-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24] + ".wav"


# ---------------------------------------------------------------- takes


def _frames(samples: array, size: int) -> list[float]:
    levels = []
    for start in range(0, len(samples), size):
        chunk = samples[start : start + size]
        levels.append((sum(s * s for s in chunk) / len(chunk)) ** 0.5 / 32768.0)
    return levels


def process_take(pcm: bytes) -> tuple[bytes | None, str | None]:
    """(trimmed and normalised PCM16, None), or (None, the Portuguese reason it is rejected)."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % SAMPLE_WIDTH])
    size = int(FRAME_S * SAMPLE_RATE)
    levels = _frames(samples, size)
    if not levels or max(levels) < VOICED_FLOOR:
        return None, TOO_QUIET
    threshold = max(VOICED_FLOOR, VOICED_RATIO * max(levels))
    voiced = [index for index, level in enumerate(levels) if level >= threshold]
    first, last = voiced[0], voiced[-1]
    if (last + 1 - first) * FRAME_S < MIN_CLIP_S:
        return None, TOO_SHORT
    pad = int(PAD_S * SAMPLE_RATE)
    start = max(0, first * size - pad)
    end = min(len(samples), (last + 1) * size + pad)
    if (end - start) / SAMPLE_RATE > MAX_CLIP_S:
        return None, TOO_LONG
    energy = count = 0
    for index in voiced:
        chunk = samples[index * size : (index + 1) * size]
        energy += sum(s * s for s in chunk)
        count += len(chunk)
    rms = (energy / count) ** 0.5 / 32768.0
    peak = max(abs(s) for s in samples[start:end]) / 32768.0
    gain = min(TARGET_RMS / rms, MAX_PEAK / peak)
    out = array("h", (max(-32768, min(32767, round(s * gain))) for s in samples[start:end]))
    return out.tobytes(), None


def trim_and_normalise(pcm: bytes) -> bytes | None:
    """The take trimmed and normalised, or None when it is rejected (see ``process_take``)."""
    return process_take(pcm)[0]


def wav_bytes_duration(pcm: bytes) -> float:
    return round(len(pcm) / SAMPLE_WIDTH / SAMPLE_RATE, 3)


# ---------------------------------------------------------------- store


def _inside(path: Path, root: Path) -> bool:
    return root.resolve() in path.resolve().parents


def _write_atomic(path: Path, write: Callable[[Path], None]) -> None:
    temporary = path.with_name(path.name + PART_SUFFIX)
    try:
        write(temporary)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


@dataclass
class Manifest:
    clips: dict[str, dict] = field(default_factory=dict, repr=False)
    skipped: list[str] = field(default_factory=list, repr=False)
    added: list[str] = field(default_factory=list, repr=False)

    def to_json(self) -> dict:
        return {"version": MANIFEST_VERSION, "clips": self.clips, "skipped": self.skipped, "added": self.added}


def _valid_entry(key: object, entry: object) -> bool:
    return (isinstance(key, str) and key != "" and " ".join(key.casefold().split()) == key and len(key) <= MAX_KEY_CHARS
            and isinstance(entry, dict)
            and isinstance(entry.get("file"), str) and CLIP_FILE.match(entry["file"]) is not None)


class ClipStore:
    """The clips folder (inside ``local/``) and its manifest."""

    def __init__(self, folder: Path = CLIPS_DIR, *, local_root: Path = LOCAL_DIR) -> None:
        folder = Path(folder)
        if not _inside(folder, Path(local_root)):
            raise ClipsError("the name clips must stay under the ignored local/ folder")
        self.folder = folder.resolve()
        self.manifest_path = self.folder / MANIFEST_NAME

    def load(self) -> Manifest:
        """The manifest (empty when there is none); ClipsError when it is unreadable or malformed.
        Entries with an unsafe or unexpected file are dropped."""
        try:
            with self.manifest_path.open("rb") as handle:
                raw = handle.read(MAX_MANIFEST_BYTES + 1)
        except FileNotFoundError:
            return Manifest()
        except OSError as exc:
            raise ClipsError(f"name clips manifest not readable ({type(exc).__name__})") from None
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ClipsError("name clips manifest is too large")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ClipsError("name clips manifest is not valid JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("clips", {}), dict):
            raise ClipsError("name clips manifest has no 'clips' table")
        skipped, added = data.get("skipped", []), data.get("added", [])
        if not isinstance(skipped, list) or not isinstance(added, list):
            raise ClipsError("name clips manifest 'skipped' and 'added' must be lists")
        clips = {key: entry for key, entry in data.get("clips", {}).items() if _valid_entry(key, entry)}
        return Manifest(clips=clips,
                        skipped=list(dict.fromkeys(key for key in skipped if isinstance(key, str) and key)),
                        added=list(dict.fromkeys(name for name in added if isinstance(name, str) and clip_key(name))))

    def save(self, manifest: Manifest) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        text = json.dumps(manifest.to_json(), ensure_ascii=False, indent=2) + "\n"
        _write_atomic(self.manifest_path, lambda path: path.write_text(text, encoding="utf-8"))

    def clip_path(self, manifest: Manifest, key: str) -> Path | None:
        """The clip file of ``key`` when its entry is valid and the file is a regular file in the folder."""
        entry = manifest.clips.get(key)
        if not _valid_entry(key, entry):
            return None
        path = self.folder / entry["file"]
        try:
            status = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(status.st_mode) or path.resolve().parent != self.folder:
            return None
        return path

    def has_clip(self, manifest: Manifest, key: str) -> bool:
        return self.clip_path(manifest, key) is not None

    def save_clip(self, display: str, pcm: bytes, now: datetime) -> float:
        """Write the clip of ``display`` (replacing an earlier one) and its manifest entry; its duration."""
        key = clip_key(display)
        if not key:
            raise ClipsError("a name clip needs a name")
        manifest = self.load()
        self.folder.mkdir(parents=True, exist_ok=True)
        name = clip_file_name(key)

        def write(path: Path) -> None:
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(SAMPLE_WIDTH)
                handle.setframerate(SAMPLE_RATE)
                handle.writeframes(pcm)

        _write_atomic(self.folder / name, write)
        duration = wav_bytes_duration(pcm)
        manifest.clips[key] = {"file": name, "display": display, "duration_s": duration,
                               "recorded_at": now.isoformat(timespec="seconds")}
        manifest.skipped = [item for item in manifest.skipped if item != key]
        self.save(manifest)
        return duration

    def skip(self, key: str) -> None:
        manifest = self.load()
        if key and key not in manifest.skipped:
            manifest.skipped.append(key)
            self.save(manifest)

    def add(self, name: str) -> None:
        manifest = self.load()
        key = clip_key(name)
        if key and all(clip_key(item) != key for item in manifest.added):
            manifest.added.append(name)
            self.save(manifest)

    def read_clip(self, manifest: Manifest, key: str) -> Clip:
        """The validated clip of ``key``; ClipUnusable with the reason when it cannot be played."""
        path = self.clip_path(manifest, key)
        if path is None:
            raise ClipUnusable(MISSING)
        try:
            with path.open("rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ClipUnusable(MISSING)
                raw = handle.read(MAX_CLIP_BYTES + 1)
        except FileNotFoundError:
            raise ClipUnusable(MISSING) from None
        except OSError:
            raise ClipUnusable(UNREADABLE) from None
        return parse_clip(raw)

    def clips_for(self, names: Sequence[str]) -> list[Clip | None]:
        """The clip of each name (by ``clip_key``), or None where the name keeps the Windows voice.
        A name without a manifest entry has no clip; any other problem is logged as a reason code only."""
        try:
            manifest = self.load()
        except ClipsError:
            log.warning("own-voice clips not used (%s)", MANIFEST)
            return [None] * len(names)
        found: list[Clip | None] = []
        for name in names:
            key = clip_key(name)
            clip = None
            if key in manifest.clips:
                try:
                    clip = self.read_clip(manifest, key)
                except ClipUnusable as problem:
                    log.warning("own-voice clip not used (%s)", problem.reason)
            found.append(clip)
        return found

    def count(self) -> int:
        """How many names have a clip file (an unreadable manifest counts none)."""
        try:
            manifest = self.load()
        except ClipsError:
            return 0
        return sum(1 for key in manifest.clips if self.has_clip(manifest, key))


def _format_tag(raw: bytes) -> int | None:
    """The format tag of the WAV's ``fmt `` chunk, or None when there is none."""
    position = 12
    while position + 8 <= len(raw):
        chunk, size = raw[position : position + 4], int.from_bytes(raw[position + 4 : position + 8], "little")
        if chunk == b"fmt ":
            return int.from_bytes(raw[position + 8 : position + 10], "little") if size >= 2 else None
        position += 8 + size + (size & 1)
    return None


def parse_clip(raw: bytes) -> Clip:
    """The samples of a WAV file's bytes; ClipUnusable unless it is RIFF PCM16 mono within the limits."""
    if len(raw) > MAX_CLIP_BYTES:
        raise ClipUnusable(OVERSIZED)
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE" or _format_tag(raw) not in PCM_TAGS:
        raise ClipUnusable(FORMAT)
    try:
        with wave.open(io.BytesIO(raw), "rb") as handle:
            channels, width, rate = handle.getnchannels(), handle.getsampwidth(), handle.getframerate()
            frames = handle.getnframes()
            if channels != 1 or width != SAMPLE_WIDTH or not PLAY_RATES[0] <= rate <= PLAY_RATES[1]:
                raise ClipUnusable(FORMAT)
            if frames <= 0:
                raise ClipUnusable(CORRUPT)
            if frames > MAX_CLIP_S * rate:
                raise ClipUnusable(OVERSIZED)
            pcm = handle.readframes(frames)
    except (wave.Error, EOFError, ValueError, struct_error):
        raise ClipUnusable(CORRUPT) from None
    if len(pcm) != frames * SAMPLE_WIDTH:
        raise ClipUnusable(CORRUPT)
    return Clip(pcm, rate)


def default_store() -> ClipStore:
    """The app's clips folder, ``local/names`` (the tests replace this function, so they never read it)."""
    return ClipStore(CLIPS_DIR)
