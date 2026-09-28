"""Load the real recordings in place: script, manifest, audio checks, placeholders.

Recordings are only opened for reading; nothing is copied. Spoken text and the
resolved project names are kept out of reprs and error messages.

Two sets share this loader. The commands set (ids pt-NN) is read in place from
the reference project. The dictation set (ids dt-NN by default) has a committed
script whose phrases carry markup: ``{hum}`` marks a filler and ``[abre o]`` a
self-repetition. The markup gives the verbatim reference (what is spoken), the
clean reference (what the final text should say) and the spans cleanup must
remove.
"""

from __future__ import annotations

import json
import re
import tomllib
import wave
from array import array
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bench.normalize import normalize_words
from bench.settings import Settings

SAMPLE_RATE = 16_000
MAX_ZERO_RUN_S = 0.5
DURATION_TOLERANCE_S = 0.05

DISCARDED_MARKER = ".invalida-"
PLACEHOLDER = re.compile(r"<projeto-\d+>")

# Spoken fillers the dictation script may mark (normalized form).
FILLERS = frozenset({"hum", "pronto", "tipo", "é pá"})
MARKUP = re.compile(r"\{([^{}\[\]]*)\}|\[([^{}\[\]]*)\]")
CONTENT, FILLER, REPETITION = "content", "filler", "repetition"


def take_id_pattern(prefix: str = "pt") -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}-\d{{2}}$")


def valid_file_pattern(prefix: str = "pt") -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}-\d{{2}}\.wav$")


VALID_FILE = valid_file_pattern()
TAKE_ID = take_id_pattern()

# Recording-script table columns (the script is written in Portuguese).
SCRIPT_COLUMNS = {
    "id": "id",
    "caso": "case",
    "frase": "text",
    "intenção": "intent",
    "projeto": "project",
    "ativação": "wake",
    "estilo": "style",
}


class DatasetError(Exception):
    """Structural problem that makes the whole dataset unusable."""


@dataclass(frozen=True)
class ScriptRow:
    id: str
    case: str
    intent: str
    project: str
    text: str = field(repr=False)
    style: str = ""


@dataclass(frozen=True)
class Segment:
    """A piece of a script phrase: content, a filler or a self-repetition."""

    kind: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class Take:
    id: str
    duration_s: float
    origin: str
    case: str
    intent: str
    path: Path = field(repr=False)
    # Verbatim reference: everything spoken, fillers and repetitions included.
    reference: str = field(repr=False)
    project_names: tuple[str, ...] = field(repr=False)
    style: str = ""
    # Clean reference (fillers and repetitions removed); empty for a set
    # without markup, where the verbatim reference is already clean.
    clean_reference: str = field(default="", repr=False)
    segments: tuple[Segment, ...] = field(default=(), repr=False)

    @property
    def clean(self) -> str:
        return self.clean_reference or self.reference


@dataclass(frozen=True)
class InvalidTake:
    id: str
    reason: str


@dataclass(frozen=True)
class Dataset:
    takes: tuple[Take, ...]
    invalid: tuple[InvalidTake, ...]
    discarded: int
    placeholders_resolved: bool
    origins: dict[str, int]
    names: frozenset[str] = field(repr=False)
    # Script rows with no recording yet (dictation set only).
    pending: tuple[str, ...] = ()
    script_rows: int = 0

    def reference_texts(self) -> list[str]:
        texts = [take.reference for take in self.takes]
        texts += [take.clean_reference for take in self.takes if take.clean_reference]
        return texts


# ---------------------------------------------------------------- script


def parse_script(text: str, id_prefix: str = "pt", markup: bool = False) -> list[ScriptRow]:
    """Parse the Markdown table of the recording script.

    With ``markup`` every phrase must carry valid filler/repetition markup.
    """
    take_id_re = take_id_pattern(id_prefix)
    header: list[str] | None = None
    rows: list[ScriptRow] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if header is None:
            if cells and cells[0].casefold() == "id" and "frase" in [c.casefold() for c in cells]:
                header = [c.casefold() for c in cells]
            continue
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        if len(cells) != len(header):
            raise DatasetError(f"recording script: row with {len(cells)} columns, expected {len(header)}")
        record = {SCRIPT_COLUMNS.get(name, name): value for name, value in zip(header, cells)}
        take_id = record.get("id", "")
        if not take_id_re.match(take_id):
            raise DatasetError("recording script: row with an invalid id")
        if take_id in seen:
            raise DatasetError(f"recording script: duplicate id {take_id}")
        seen.add(take_id)
        if markup:
            parse_markup(record.get("text", ""), take_id)
        rows.append(
            ScriptRow(
                id=take_id,
                case=record.get("case", ""),
                intent=record.get("intent", ""),
                project=record.get("project", ""),
                text=record.get("text", ""),
                style=record.get("style", ""),
            )
        )
    if header is None:
        raise DatasetError("recording script: no table with 'id' and 'frase' columns")
    return rows


# ---------------------------------------------------------------- markup


def parse_markup(text: str, take_id: str = "phrase") -> tuple[Segment, ...]:
    """Split a phrase into content, filler ``{..}`` and repetition ``[..]`` segments.

    A filler must be one of FILLERS. A repetition must be followed by content
    that starts with the same word (``[abre o] abre o ficheiro``). Errors name
    the id, never the text.
    """
    segments: list[Segment] = []
    position = 0
    for match in MARKUP.finditer(text):
        if match.start() > position:
            segments.append(Segment(CONTENT, text[position : match.start()]))
        if match.group(1) is not None:
            if " ".join(normalize_words(match.group(1))) not in FILLERS:
                raise DatasetError(f"{take_id}: unknown filler in markup")
            segments.append(Segment(FILLER, match.group(1).strip()))
        else:
            if not normalize_words(match.group(2)):
                raise DatasetError(f"{take_id}: empty repetition in markup")
            segments.append(Segment(REPETITION, match.group(2).strip()))
        position = match.end()
    if position < len(text):
        segments.append(Segment(CONTENT, text[position:]))
    for segment in segments:
        if segment.kind == CONTENT and any(char in segment.text for char in "{}[]"):
            raise DatasetError(f"{take_id}: unbalanced markup")
    for index, segment in enumerate(segments):
        if segment.kind != REPETITION:
            continue
        following = segments[index + 1] if index + 1 < len(segments) else None
        next_words = normalize_words(following.text) if following and following.kind == CONTENT else []
        if not next_words or next_words[0] != normalize_words(segment.text)[0]:
            raise DatasetError(f"{take_id}: a repetition must be followed by the words it repeats")
    if not any(normalize_words(s.text) for s in segments if s.kind == CONTENT):
        raise DatasetError(f"{take_id}: phrase has no content words")
    return tuple(segments)


def _tidy(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(r"\s+([,.;:?!…])", r"\1", text)
    text = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", text)
    text = re.sub(r"[,;:]\s*([.?!])", r"\1", text)
    return re.sub(r"^[\s,;:.]+", "", text).strip()


def verbatim_text(segments: Sequence[Segment]) -> str:
    return _tidy(" ".join(segment.text for segment in segments))


def clean_text(segments: Sequence[Segment]) -> str:
    return _tidy(" ".join(segment.text for segment in segments if segment.kind == CONTENT))


def display_text(segments: Sequence[Segment]) -> str:
    """What the recorder shows: hesitations end in an ellipsis and are spoken as written."""
    parts = [segment.text if segment.kind == CONTENT else segment.text + "…" for segment in segments]
    return _tidy(" ".join(parts))


def strip_markup(text: str) -> tuple[str, str]:
    """(verbatim, clean) forms of a phrase with markup."""
    segments = parse_markup(text)
    return verbatim_text(segments), clean_text(segments)


# ---------------------------------------------------------------- config


def load_reference_names(path: Path) -> frozenset[str]:
    """Project names from the reference project's local config.toml."""
    if not path.is_file():
        raise DatasetError("reference config not found (check [paths].reference_config)")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        raise DatasetError("reference config is not valid TOML") from None
    projects = data.get("projetos", data.get("projects"))
    if not isinstance(projects, list):
        raise DatasetError("reference config has no project list")
    names: set[str] = set()
    for project in projects:
        if not isinstance(project, dict):
            continue
        name = project.get("nome", project.get("name"))
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    if not names:
        raise DatasetError("reference config has no project names")
    return frozenset(names)


def resolve_placeholders(text: str, mapping: dict[str, str], known: frozenset[str], take_id: str) -> tuple[str, tuple[str, ...]]:
    """Replace <projeto-N> with the recording's name; validate against ``known``."""
    used: list[str] = []

    def replace(match: re.Match[str]) -> str:
        placeholder = match.group(0)
        name = mapping.get(placeholder)
        if not isinstance(name, str) or not name.strip():
            raise DatasetError(f"{take_id}: placeholder {placeholder} has no name in the manifest entry")
        if name not in known:
            raise DatasetError(f"{take_id}: placeholder {placeholder} maps to a name missing from the reference config")
        used.append(name)
        return name

    return PLACEHOLDER.sub(replace, text), tuple(used)


def dictation_projects(settings: Settings, commands: Settings | None = None) -> dict[str, str]:
    """Placeholder -> name shown while recording the dictation set.

    ``[dictation.projects]`` in local/bench.toml wins; otherwise the top-level
    ``projetos`` mapping of the commands manifest is reused. Names are
    validated against the reference config.
    """
    if settings.projects is not None:
        mapping = dict(settings.projects)
    else:
        mapping = {}
        if commands is not None and commands.manifest.is_file():
            try:
                data = json.loads(commands.manifest.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise DatasetError("recordings manifest is not valid JSON") from None
            top = data.get("projetos") if isinstance(data, dict) else None
            if isinstance(top, dict):
                mapping = {k: v for k, v in top.items() if isinstance(k, str) and PLACEHOLDER.fullmatch(k)}
    if not mapping:
        raise DatasetError("no project names for the dictation placeholders: add [dictation.projects] to local/bench.toml")
    known = load_reference_names(settings.reference_config)
    for placeholder, name in mapping.items():
        if not isinstance(name, str) or name not in known:
            raise DatasetError(f"dictation: placeholder {placeholder} maps to a name missing from the reference config")
    return mapping


# ---------------------------------------------------------------- audio


def longest_zero_run(samples: array) -> int:
    longest = current = 0
    for sample in samples:
        if sample == 0:
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0
    return longest


def check_audio(path: Path, expected_duration_s: object) -> tuple[str | None, float]:
    """Return (invalid reason or None, measured duration in seconds)."""
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.getnframes()
            if handle.getcomptype() != "NONE" or channels != 1 or width != 2 or rate != SAMPLE_RATE:
                return "not 16 kHz mono PCM16", frames / rate if rate else 0.0
            data = handle.readframes(frames)
    except (wave.Error, EOFError, OSError):
        return "unreadable WAV", 0.0
    samples = array("h")
    samples.frombytes(data[: len(data) - len(data) % 2])
    duration = len(samples) / SAMPLE_RATE
    if longest_zero_run(samples) >= MAX_ZERO_RUN_S * SAMPLE_RATE:
        return "0.5 s or more of exact digital zeros", duration
    if isinstance(expected_duration_s, bool) or not isinstance(expected_duration_s, (int, float)):
        return "manifest duration missing", duration
    if abs(duration - float(expected_duration_s)) > DURATION_TOLERANCE_S:
        return "duration does not match the manifest", duration
    return None, duration


# ---------------------------------------------------------------- dataset


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        raise DatasetError("recordings manifest not found (check [paths].manifest)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise DatasetError("recordings manifest is not valid JSON") from None
    entries = data.get("gravacoes") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        raise DatasetError("recordings manifest has no 'gravacoes' table")
    return entries


def load_script(settings: Settings) -> list[ScriptRow]:
    if not settings.recording_script.is_file():
        raise DatasetError("recording script not found (check [paths].recording_script)")
    return parse_script(settings.recording_script.read_text(encoding="utf-8"), settings.id_prefix, settings.markup)


def load_dataset(settings: Settings) -> Dataset:
    if settings.allow_pending and not settings.recording_script.is_file():
        raise DatasetError(f"{settings.name}: recording script not found")
    rows = load_script(settings) if settings.allow_pending else None
    if settings.allow_pending and (not settings.recordings_dir.is_dir() or not settings.manifest.is_file()):
        # Nothing recorded yet: every script row is pending.
        return Dataset((), (), 0, True, {}, frozenset(), tuple(row.id for row in rows), len(rows))
    if not settings.recordings_dir.is_dir():
        raise DatasetError("recordings folder not found (check [paths].recordings_dir)")
    if rows is None:
        rows = load_script(settings)
    entries = load_manifest(settings.manifest)
    known = load_reference_names(settings.reference_config)
    valid_file = valid_file_pattern(settings.id_prefix)

    takes: list[Take] = []
    invalid: list[InvalidTake] = []
    pending: list[str] = []
    names: set[str] = set()
    origins: Counter[str] = Counter()
    script_ids = {row.id for row in rows}

    for row in rows:
        entry = entries.get(row.id)
        if not isinstance(entry, dict):
            if settings.allow_pending:
                pending.append(row.id)
            else:
                invalid.append(InvalidTake(row.id, "not in manifest"))
            continue
        mapping = entry.get("projetos")
        if PLACEHOLDER.search(row.text) and not isinstance(mapping, dict):
            raise DatasetError(f"{row.id}: manifest entry has no project mapping")
        reference, used = resolve_placeholders(row.text, mapping or {}, known, row.id)
        if isinstance(mapping, dict):
            for placeholder, name in mapping.items():
                if not isinstance(name, str) or name not in known:
                    raise DatasetError(f"{row.id}: placeholder {placeholder} maps to a name missing from the reference config")
                names.add(name)
        segments: tuple[Segment, ...] = ()
        clean_reference = ""
        if settings.markup:
            segments = parse_markup(reference, row.id)
            reference = verbatim_text(segments)
            clean_reference = clean_text(segments)
        file_name = entry.get("ficheiro")
        if not isinstance(file_name, str) or DISCARDED_MARKER in file_name:
            invalid.append(InvalidTake(row.id, "manifest points to a discarded take"))
            continue
        if file_name != f"{row.id}.wav" or not valid_file.match(file_name):
            invalid.append(InvalidTake(row.id, "manifest file name does not match the id"))
            continue
        path = settings.recordings_dir / file_name
        if not path.is_file():
            invalid.append(InvalidTake(row.id, "audio file missing"))
            continue
        reason, duration = check_audio(path, entry.get("duracao_s"))
        if reason is not None:
            invalid.append(InvalidTake(row.id, reason))
            continue
        origin = entry.get("origem") if isinstance(entry.get("origem"), str) else "unknown"
        origins[origin] += 1
        takes.append(
            Take(
                id=row.id,
                duration_s=round(duration, 3),
                origin=origin,
                case=row.case,
                intent=row.intent,
                path=path,
                reference=reference,
                project_names=used,
                style=row.style,
                clean_reference=clean_reference,
                segments=segments,
            )
        )

    for take_id in sorted(set(entries) - script_ids):
        invalid.append(InvalidTake(str(take_id), "not in recording script"))

    discarded = 0
    for path in sorted(settings.recordings_dir.glob("*.wav")):
        if DISCARDED_MARKER in path.name:
            discarded += 1
        elif not valid_file.match(path.name) or path.name[:-4] not in script_ids:
            invalid.append(InvalidTake(path.name, "unexpected audio file"))

    return Dataset(
        takes=tuple(takes),
        invalid=tuple(invalid),
        discarded=discarded,
        placeholders_resolved=True,
        origins=dict(origins),
        names=frozenset(names),
        pending=tuple(pending),
        script_rows=len(rows),
    )


def load_private_text(settings: Settings) -> tuple[list[str], frozenset[str]]:
    """Every script phrase (raw and resolved) plus the resolved project names.

    Used by the privacy guard; does not open audio. Same validation as
    load_dataset: a missing or unknown name is an error.
    """
    if not settings.recording_script.is_file():
        raise DatasetError("recording script not found (check [paths].recording_script)")
    rows = parse_script(settings.recording_script.read_text(encoding="utf-8"))
    entries = load_manifest(settings.manifest)
    known = load_reference_names(settings.reference_config)
    texts: list[str] = []
    names: set[str] = set()
    for row in rows:
        texts.append(row.text)
        entry = entries.get(row.id)
        mapping = entry.get("projetos") if isinstance(entry, dict) else None
        if isinstance(mapping, dict):
            for placeholder, name in mapping.items():
                if not isinstance(name, str) or name not in known:
                    raise DatasetError(f"{row.id}: placeholder {placeholder} maps to a name missing from the reference config")
                names.add(name)
            resolved, _ = resolve_placeholders(row.text, mapping, known, row.id)
            texts.append(resolved)
    if not names:
        raise DatasetError("no project names resolved from the manifest")
    return texts, frozenset(names)


def load_dictation_private_text(settings: Settings, commands: Settings | None = None) -> tuple[list[str], frozenset[str]]:
    """Verbatim and clean forms of every dictation phrase, raw and resolved.

    Resolved forms use the placeholder mapping shown while recording plus any
    mapping stored in the dictation manifest. Does not open audio.
    """
    rows = load_script(settings)
    mappings = [dictation_projects(settings, commands)]
    if settings.manifest.is_file():
        known = load_reference_names(settings.reference_config)
        for entry in load_manifest(settings.manifest).values():
            mapping = entry.get("projetos") if isinstance(entry, dict) else None
            if isinstance(mapping, dict):
                for placeholder, name in mapping.items():
                    if not isinstance(name, str) or name not in known:
                        raise DatasetError(f"dictation: placeholder {placeholder} maps to a name missing from the reference config")
                if mapping not in mappings:
                    mappings.append(mapping)
    known = load_reference_names(settings.reference_config)
    texts: list[str] = []
    names: set[str] = set()
    for row in rows:
        texts.extend(strip_markup(row.text))
        for mapping in mappings:
            if not PLACEHOLDER.search(row.text):
                break
            resolved, used = resolve_placeholders(row.text, mapping, known, row.id)
            names.update(used)
            texts.extend(strip_markup(resolved))
    for mapping in mappings:
        names.update(mapping.values())
    return texts, frozenset(names)
