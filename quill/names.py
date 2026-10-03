"""Record the project names of the Claude Code alerts in your own voice (manual command).

Usage (from the repository folder):
    py -3.12 -m quill.names --dry-run          counts only: names found, recorded, missing, skipped
    py -3.12 -m quill.names --list-devices     MME inputs, the [audio] microphone marked; nothing opened
    py -3.12 -m quill.names                    record every name without a clip (resumes where it stopped)
    py -3.12 -m quill.names --include-skipped  ... and ask the skipped names again
    py -3.12 -m quill.names --redo NAME        record one name again (its clip is replaced)
    py -3.12 -m quill.names --add NAME         add a name the list misses, and record it

The names are the ones an alert can say, found the way an alert finds them:

- the projects Claude Code has opened: the project-path keys of its
  ``.claude.json`` (in ``CLAUDE_CONFIG_DIR`` when set, else in the home
  folder; nothing else of that file is kept, printed or logged), each passed
  through ``quill.notify.project_name`` and ``quill.speech.spoken``; paths that
  no longer exist, are not on a local fixed drive or lie in the temporary
  folder are dropped;
- ``[project_context] folders`` and the ``[voice_commands]`` shortcuts: their
  names and the project names of their folders;
- the vocabulary names, and the names added with ``--add``.

Names that ``quill.clips.clip_key`` makes equal are one name. Recording uses
the ``[audio]`` microphone through ``quill.audio`` (the MME capture of the app):
Enter starts and stops a take, ``s`` skips a name (it is not asked again),
``q`` quits, ``r`` after "Guardado" records that name again. A take with exact
zeros, behind the clock, near-silent, without a name or too long is rejected
and asked again. Each take is trimmed and normalised (``quill.clips``) and
saved under the ignored ``local/names``. The microphone opens only in record
mode. The prompts are in European Portuguese; outside them nothing printed
or logged holds a name.
"""

from __future__ import annotations

import argparse
import json
import logging
import ntpath
import os
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from quill.audio import AudioError, Capture, WinMM, input_devices, name_matches, select_device, take_problem
from quill.clips import ClipsError, ClipStore, Manifest, clip_key, process_take
from quill.config import Config, ConfigError, load_config
from quill.notify import clean_name, project_name
from quill.projects import DRIVE_FIXED, ProjectFolders, drive_type, local_folder
from quill.speech import spoken

log = logging.getLogger("quill.names")

CLAUDE_CONFIG_NAME = ".claude.json"
MAX_CLAUDE_CONFIG_BYTES = 64 * 1024 * 1024
MAX_CLAUDE_PROJECTS = 1000

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_INTERRUPTED = 130


class NamesError(Exception):
    """A source of names cannot be read; the message is a reason code, never a name or a path."""


# ---------------------------------------------------------------- sources


def claude_config_path(environ: dict[str, str] | None = None) -> Path:
    """The user's Claude Code config file (``.claude.json``)."""
    environ = os.environ if environ is None else environ
    folder = environ.get("CLAUDE_CONFIG_DIR")
    return (Path(folder) if folder else Path.home()) / CLAUDE_CONFIG_NAME


def claude_project_paths(path: Path | None = None) -> list[str]:
    """The project-path keys of ``.claude.json`` (empty when it does not exist); nothing else is kept."""
    path = claude_config_path() if path is None else path
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CLAUDE_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise NamesError(f"claude_config_unreadable:{type(exc).__name__}") from None
    if len(raw) > MAX_CLAUDE_CONFIG_BYTES:
        raise NamesError("claude_config_too_large")
    try:
        projects = json.loads(raw.decode("utf-8")).get("projects", {})
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        raise NamesError("claude_config_invalid") from None
    if not isinstance(projects, dict):
        raise NamesError("claude_config_no_projects")
    return [key for key in projects if isinstance(key, str)][:MAX_CLAUDE_PROJECTS]


def temp_folders(environ: dict[str, str] | None = None) -> list[str]:
    """The temporary folders whose projects are never listed."""
    environ = os.environ if environ is None else environ
    found = [tempfile.gettempdir(), environ.get("TEMP", ""), environ.get("TMP", "")]
    if environ.get("LOCALAPPDATA"):
        found.append(ntpath.join(environ["LOCALAPPDATA"], "Temp"))
    return [folder for folder in dict.fromkeys(found) if folder]


def _identity(path: str) -> str:
    return os.path.normcase(ntpath.normpath(path)).rstrip("\\/")


def _in_any(folder: str, roots: Iterable[str]) -> bool:
    identity = _identity(folder)
    return any(identity == root or identity.startswith(root + "\\")
               for root in (_identity(item) for item in roots) if root)


@dataclass
class Sources:
    """Where the names come from; tests pass fakes for every part."""

    claude_paths: Callable[[], Sequence[str]]
    folders: object  # ``ProjectFolders`` (or a fake with ``shortcut_folders()``)
    configured: Sequence[tuple[str, Path | str]] = field(default=(), repr=False)
    vocabulary_names: Callable[[], Sequence[str]] = lambda: ()
    is_dir: Callable[[Path], bool] = Path.is_dir
    drives: Callable[[str], int] = drive_type
    exists: Callable[[str], bool] = os.path.lexists
    temp_dirs: Sequence[str] = ()


def default_sources(config: Config) -> Sources:
    from quill.vocabulary import load_vocabulary

    return Sources(
        claude_paths=claude_project_paths,
        folders=ProjectFolders(config.project_context.folders, config.voice.shortcut_dirs),
        configured=config.project_context.folders,
        vocabulary_names=lambda: [entry.text for entry in load_vocabulary(config.vocabulary_path).names],
        temp_dirs=temp_folders(),
    )


def claude_names(paths: Iterable[str], sources: Sources) -> list[str]:
    """The name an alert would say for each Claude Code project still on a local fixed drive."""
    def fixed(drive: str) -> int:
        kind = sources.drives(drive)
        return kind if kind == DRIVE_FIXED else 0

    names = []
    for path in paths:
        folder = local_folder(path, is_dir=sources.is_dir, drives=fixed)
        if folder is None or _in_any(folder, sources.temp_dirs):
            continue
        name = project_name(path, sources.exists)
        if name:
            names.append(name)
    return names


def folder_names(sources: Sources) -> list[str]:
    """The [project_context] and shortcut names, then the project names of their folders."""
    entries = [(name, os.fspath(folder)) for name, folder in sources.configured]
    entries += list(sources.folders.shortcut_folders())
    names = [name for name, _ in entries]
    for _, folder in entries:
        name = project_name(folder, sources.exists)
        if name:
            names.append(name)
    return names


@dataclass(frozen=True)
class Name:
    key: str
    display: str = field(repr=False)
    spoken: str = field(repr=False)


def make_name(text: str) -> Name | None:
    display = clean_name(text)
    if display is None:
        return None
    said = spoken(display)
    key = clip_key(display)
    return Name(key, display, said) if key else None


@dataclass
class Discovery:
    names: list[Name] = field(default_factory=list, repr=False)
    counts: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def discover(sources: Sources, added: Sequence[str] = ()) -> Discovery:
    """Every name an alert can say, once per ``clip_key``, in source order."""
    found = Discovery()
    groups: list[tuple[str, Callable[[], list[str]]]] = [
        ("claude_code", lambda: claude_names(sources.claude_paths(), sources)),
        ("folders", lambda: folder_names(sources)),
        ("vocabulary", lambda: list(sources.vocabulary_names())),
        ("added", lambda: list(added)),
    ]
    seen: set[str] = set()
    for label, read in groups:
        try:
            texts = read()
        except NamesError as exc:
            found.problems.append(str(exc))
            texts = []
        except Exception as exc:  # noqa: BLE001 - one source failing keeps the others
            found.problems.append(f"{label}_unreadable:{type(exc).__name__}")
            texts = []
        found.counts[label] = len(texts)
        for text in texts:
            name = make_name(text) if isinstance(text, str) else None
            if name is not None and name.key not in seen:
                seen.add(name.key)
                found.names.append(name)
    for problem in found.problems:
        log.info("names source skipped (%s)", problem)
    return found


@dataclass(frozen=True)
class Counts:
    found: int
    recorded: int
    missing: int
    skipped: int


def count(store: ClipStore, manifest: Manifest, names: Sequence[Name]) -> Counts:
    recorded = sum(1 for name in names if store.has_clip(manifest, name.key))
    skipped = sum(1 for name in names if not store.has_clip(manifest, name.key) and name.key in manifest.skipped)
    return Counts(len(names), recorded, len(names) - recorded - skipped, skipped)


# ---------------------------------------------------------------- recording

# quill.audio.take_problem reason (English, for the logs and the benchmark) -> what the Sponsor reads.
TAKE_REASONS = (
    ("take too short", "take demasiado curto: carrega Enter, diz o nome, espera um instante e carrega Enter"),
    ("exact digital zeros", "o microfone deu silêncio digital; confirma que não está em mute"),
    ("capture fell behind", "a captura atrasou-se; tenta outra vez"),
    ("capture ran faster", "a captura adiantou-se; tenta outra vez"),
    ("level too low", "som demasiado baixo; verifica o microfone e fala mais perto"),
)


def take_reason(problem: str) -> str:
    """The European Portuguese reason of a ``take_problem`` rejection."""
    for marker, text in TAKE_REASONS:
        if marker in problem:
            return text
    return problem



class Console:
    """Terminal prompts; tests replace it with a scripted fake."""

    def __init__(self) -> None:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def ask(self, prompt: str) -> str:
        try:
            return input(prompt).strip().casefold()
        except EOFError:
            return "q"


@dataclass
class Report:
    saved: int = 0
    skipped: int = 0
    rejected: int = 0
    quit: bool = False


@dataclass
class Recorder:
    store: ClipStore
    capture_factory: Callable[[], Capture]
    console: Console
    now: Callable[[], datetime] = datetime.now

    def record_one(self, name: Name, position: str, report: Report) -> str:
        """Record ``name`` until saved or skipped; returns "next", "skip" or "quit"."""
        say, ask = self.console.say, self.console.ask
        while True:
            say()
            say(f"[{position}] Nome: {name.display}")
            if name.spoken != name.display:
                say(f"  Diz: {name.spoken}")
            answer = ask("Enter = gravar · s = saltar · q = sair: ")
            if answer == "q":
                return "quit"
            if answer == "s":
                if not self.store.has_clip(self.store.load(), name.key):
                    self.store.skip(name.key)
                report.skipped += 1
                return "skip"
            capture = self.capture_factory()
            capture.start()
            try:
                ask("  A gravar… diz só o nome e carrega Enter para parar. ")
            finally:
                result = capture.stop()
            problem = take_problem(result)
            pcm, reason = (None, take_reason(problem)) if problem is not None else process_take(result.pcm)
            if pcm is None:
                report.rejected += 1
                say(f"  Take rejeitado ({reason}). Vamos repetir este nome.")
                continue
            duration = self.store.save_clip(name.display, pcm, self.now())
            say(f"  Guardado: {name.display} ({duration:.1f} s).")
            answer = ask("Enter = próximo · r = repetir este · q = sair: ")
            if answer == "r":
                continue
            report.saved += 1
            return "quit" if answer == "q" else "next"

    def run(self, queue: Sequence[Name]) -> Report:
        report = Report()
        for number, name in enumerate(queue, start=1):
            if self.record_one(name, f"{number}/{len(queue)}", report) == "quit":
                report.quit = True
                break
        return report


# ---------------------------------------------------------------- command


def list_devices(console: Console, api: object | None, config: Config | None) -> int:
    try:
        names = input_devices(api)
    except (AudioError, OSError) as exc:
        console.say(f"error: MME inputs not listed ({type(exc).__name__})")
        return EXIT_FAILED
    console.say(f"MME inputs: {len(names)} (the microphone is not opened)")
    for index, name in enumerate(names):
        mark = "  <- configured" if config is not None and name_matches(name, config.microphone) else ""
        console.say(f"  {index}: {name}{mark}")
    if config is None:
        console.say("quill config not read: no microphone marked (see py -3.12 -m quill.config --check)")
    return EXIT_OK


def summary(console: Console, counts: Counts) -> None:
    console.say(f"names found: {counts.found}, recorded: {counts.recorded}, missing: {counts.missing}, "
                f"skipped: {counts.skipped}")


def dry_run(console: Console, found: Discovery, counts: Counts) -> int:
    sources = ", ".join(f"{label} {number}" for label, number in found.counts.items())
    console.say(f"name sources: {sources} (the microphone is not opened)")
    for problem in found.problems:
        console.say(f"source skipped: {problem}")
    summary(console, counts)
    return EXIT_OK


def main(argv: list[str] | None = None, *, config: Config | None = None, sources: Sources | None = None,
         store: ClipStore | None = None, api: object | None = None, console: Console | None = None,
         capture_factory: Callable[[], Capture] | None = None, now: Callable[[], datetime] = datetime.now) -> int:
    parser = argparse.ArgumentParser(prog="python -m quill.names",
                                     description="Record the project names of the Claude Code alerts in your own voice.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print counts only; the microphone is not opened")
    mode.add_argument("--list-devices", action="store_true", help="list MME inputs without opening them")
    mode.add_argument("--redo", metavar="NAME", help="record one name again (its clip is replaced)")
    mode.add_argument("--add", metavar="NAME", help="add a name the list misses, and record it")
    parser.add_argument("--include-skipped", action="store_true", help="ask the skipped names again")
    args = parser.parse_args(argv)
    console = console or Console()
    if args.list_devices:
        if config is None:
            try:
                config = load_config()
            except ConfigError:
                config = None
        return list_devices(console, api, config)
    try:
        config = config or load_config()
        store = store or ClipStore()
        manifest = store.load()
        added = list(manifest.added)
        new = None
        if args.add is not None:
            new = make_name(args.add)
            if new is None:
                raise ClipsError("--add needs a name with letters or digits")
            added.append(new.display)
        found = discover(sources or default_sources(config), added)
        if args.dry_run:
            return dry_run(console, found, count(store, manifest, found.names))
        if new is not None:
            store.add(new.display)
            queue = [next(name for name in found.names if name.key == new.key)]
        elif args.redo is not None:
            key = clip_key(args.redo)
            queue = [name for name in found.names if name.key == key]
            if not queue and key and key in manifest.clips:
                queue = [name for name in (make_name(manifest.clips[key].get("display", "")), make_name(args.redo))
                         if name is not None and name.key == key][:1]
            if not queue:
                raise ClipsError("--redo: that name is not in the list (add it with --add)")
        else:
            queue = [name for name in found.names if not store.has_clip(manifest, name.key)
                     and (args.include_skipped or name.key not in manifest.skipped)]
        if not queue:
            console.say("Todos os nomes já têm gravação; nada a gravar.")
            summary(console, count(store, manifest, found.names))
            return EXIT_OK
        if capture_factory is None:
            device_api = api if api is not None else WinMM()
            index = select_device(input_devices(device_api), config.microphone)
            capture_factory = lambda: Capture(device_api, index)  # noqa: E731
        console.say(f"Nomes a gravar: {len(queue)}. Diz só o nome, como o queres ouvir no alerta.")
        Recorder(store, capture_factory, console, now).run(queue)
        console.say()
        summary(console, count(store, store.load(), found.names))
    except (ConfigError, ClipsError, AudioError) as exc:
        console.say(f"error: {exc}")
        return EXIT_FAILED
    except OSError as exc:
        console.say(f"error: name clips not written or read ({type(exc).__name__})")
        return EXIT_FAILED
    except KeyboardInterrupt:
        console.say("interrupted; the names saved so far are kept")
        return EXIT_INTERRUPTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
