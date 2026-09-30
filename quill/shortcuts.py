"""Windows shortcuts (.lnk) of the project hub: list, read, match and open them.

The voice command "abre VS Code no <projeto>" (``quill.voice``) opens one of
the shortcuts found in the folders of ``[voice_commands] shortcut_dirs``:

- ``list_shortcuts`` lists each folder non-recursively; only ``*.lnk`` files
  count, at most ``MAX_SHORTCUTS`` in all. A missing or unreadable folder is
  logged by reason code and skipped; it never stops Quill.
- ``read_link`` reads a shortcut's target read-only, from the file bytes
  (the MS-SHLLINK format: the link info's local path, or the environment
  variable block), without launching or resolving anything through the
  shell. A file it cannot read has no target.
- ``match`` compares the spoken project name, and the personal-vocabulary
  names it stands for (a name, one of its variants, or a close spelling), with
  the shortcut names, ignoring case, accents, spaces, hyphens and other
  punctuation, within a bounded edit distance. An exact name beats a longer
  sibling ("alfa" opens alfa, "alfa public" opens alfa-public). A shortcut is
  chosen only when one candidate is inside its bound and clearly ahead of the
  runner-up; otherwise nothing is chosen and up to ``MAX_OPTIONS`` closest
  names are returned. The same name in two folders is one candidate when both
  point to the same target, and ambiguous when the targets differ.
- ``launch`` opens the chosen shortcut: a shortcut that starts VS Code
  (``Code.exe``) is opened with the shell, as a double click would; a
  shortcut to a folder starts VS Code on that folder with an argument list,
  never a shell string; any other target is refused.

Spoken text never reaches the shell, a command line or a file path: it is
only compared with the names of the enumerated shortcuts. Only the matched,
enumerated ``.lnk`` path and the target read from it are used. Logs carry
reason codes and counts only, never names or paths.
"""

from __future__ import annotations

import logging
import os
import struct
import subprocess
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from quill.vocabulary import Matcher, Vocabulary, distance
from quill.vocabulary import fold as vocabulary_fold

log = logging.getLogger("quill.shortcuts")

MAX_SHORTCUTS = 300
MAX_LINK_BYTES = 64 * 1024
MAX_OPTIONS = 3
# Edit distance allowed between the spoken name and a shortcut name, by the name's length.
ONE_EDIT_CHARS = 4
TWO_EDITS_CHARS = 9
# A name that is not exact must lead the runner-up by this many edits.
FUZZY_LEAD = 2
# A part of a name this long makes the name a likely option (never a match by itself).
MIN_PART_CHARS = 4

# Reason codes of a match and a launch.
MATCHED = "matched"
NO_SHORTCUTS = "no_shortcuts"
NO_MATCH = "no_match"
AMBIGUOUS = "ambiguous"
OPENED = "opened"
REFUSED = "refused_target"
UNREADABLE = "unreadable_shortcut"
VSCODE_MISSING = "vscode_missing"
LAUNCH_FAILED = "launch_failed"
# Folder listing reason codes (logged).
FOLDER_MISSING = "missing"
FOLDER_NOT_A_FOLDER = "not_a_folder"
FOLDER_UNREADABLE = "unreadable"

# MS-SHLLINK constants.
HEADER_SIZE = 0x4C
LINK_CLSID = bytes.fromhex("0114020000000000c000000000000046")
HAS_ID_LIST = 0x1
HAS_LINK_INFO = 0x2
HAS_NAME = 0x4
HAS_RELATIVE_PATH = 0x8
HAS_WORKING_DIR = 0x10
HAS_ARGUMENTS = 0x20
HAS_ICON_LOCATION = 0x40
IS_UNICODE = 0x80
HAS_EXP_STRING = 0x200
VOLUME_ID_AND_LOCAL_BASE_PATH = 0x1
ENVIRONMENT_BLOCK = 0xA0000001
ENVIRONMENT_BLOCK_SIZE = 0x314
FILE_ATTRIBUTE_DIRECTORY = 0x10

VSCODE_EXE = "code.exe"
VSCODE_DIR = "Microsoft VS Code"


class LinkError(ValueError):
    """The shortcut file cannot be read as a shell link."""


# ---------------------------------------------------------------- reading a shortcut


@dataclass(frozen=True)
class LinkTarget:
    """What a shortcut points to, read from its bytes (never shown or logged)."""

    path: str = field(repr=False)
    arguments: str = field(default="", repr=False)
    directory: bool = False  # the target's attributes say it is a folder

    @property
    def identity(self) -> tuple[str, str]:
        """Two shortcuts with the same identity open the same thing."""
        return (os.path.normcase(os.path.normpath(self.path)), self.arguments.strip())


def _cstring(data: bytes, start: int, wide: bool) -> str:
    if not 0 <= start < len(data):
        raise LinkError("string offset out of range")
    if wide:
        end = start
        while end + 1 < len(data) and data[end : end + 2] != b"\0\0":
            end += 2
        if end + 1 >= len(data):
            raise LinkError("unterminated string")
        return data[start:end].decode("utf-16-le")
    end = data.find(b"\0", start)
    if end < 0:
        raise LinkError("unterminated string")
    return data[start:end].decode("mbcs" if os.name == "nt" else "cp1252", errors="strict")


def _link_info_path(info: bytes) -> str:
    if len(info) < 0x1C:
        raise LinkError("short link info")
    header_size, flags = struct.unpack_from("<II", info, 4)
    if not flags & VOLUME_ID_AND_LOCAL_BASE_PATH:
        return ""  # a network share only: not a local target
    base_offset, = struct.unpack_from("<I", info, 16)
    suffix_offset, = struct.unpack_from("<I", info, 24)
    if header_size >= 0x24 and len(info) >= 0x24:
        base_wide, suffix_wide = struct.unpack_from("<II", info, 28)
        return _cstring(info, base_wide, True) + _cstring(info, suffix_wide, True)
    return _cstring(info, base_offset, False) + _cstring(info, suffix_offset, False)


def parse_link(data: bytes) -> LinkTarget:
    """The target of a shell link from its bytes; ``LinkError`` when they are not one."""
    if len(data) < HEADER_SIZE or struct.unpack_from("<I", data, 0)[0] != HEADER_SIZE or data[4:20] != LINK_CLSID:
        raise LinkError("not a shell link")
    flags, attributes = struct.unpack_from("<II", data, 20)
    at = HEADER_SIZE
    try:
        if flags & HAS_ID_LIST:
            size, = struct.unpack_from("<H", data, at)
            at += 2 + size
        path = ""
        if flags & HAS_LINK_INFO:
            size, = struct.unpack_from("<I", data, at)
            if size < 4 or at + size > len(data):
                raise LinkError("link info out of range")
            path = _link_info_path(data[at : at + size])
            at += size
        wide = bool(flags & IS_UNICODE)
        strings: dict[int, str] = {}
        for flag in (HAS_NAME, HAS_RELATIVE_PATH, HAS_WORKING_DIR, HAS_ARGUMENTS, HAS_ICON_LOCATION):
            if flags & flag:
                count, = struct.unpack_from("<H", data, at)
                at += 2
                size = count * 2 if wide else count
                if at + size > len(data):
                    raise LinkError("string data out of range")
                raw = data[at : at + size]
                strings[flag] = raw.decode("utf-16-le") if wide else raw.decode("cp1252")
                at += size
        if not path and flags & HAS_EXP_STRING:
            path = _environment_path(data, at)
    except (struct.error, UnicodeDecodeError) as exc:
        raise LinkError(type(exc).__name__) from None
    if not path:
        raise LinkError("no local target")
    return LinkTarget(path, strings.get(HAS_ARGUMENTS, ""), bool(attributes & FILE_ATTRIBUTE_DIRECTORY))


def _environment_path(data: bytes, at: int) -> str:
    """The target of the environment variable block (``%VAR%`` expanded), or ""."""
    while at + 8 <= len(data):
        size, signature = struct.unpack_from("<II", data, at)
        if size < 4:
            break
        if signature == ENVIRONMENT_BLOCK and size >= ENVIRONMENT_BLOCK_SIZE and at + size <= len(data):
            wide = data[at + 8 + 260 : at + 8 + 260 + 520]
            text = wide.decode("utf-16-le").split("\0", 1)[0]
            if not text:
                text = data[at + 8 : at + 8 + 260].split(b"\0", 1)[0].decode("cp1252")
            return os.path.expandvars(text)
        at += size
    return ""


def read_link(path: Path) -> LinkTarget | None:
    """The target of the shortcut at ``path``, or None when it cannot be read (logged by reason)."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_LINK_BYTES + 1)
    except OSError as exc:
        log.warning("shortcut not readable (%s)", type(exc).__name__)
        return None
    if len(data) > MAX_LINK_BYTES:
        log.warning("shortcut too large")
        return None
    try:
        return parse_link(data)
    except LinkError as exc:
        log.warning("shortcut not understood (%s)", exc)
        return None


# ---------------------------------------------------------------- listing


@dataclass(frozen=True)
class Shortcut:
    """One enumerated .lnk file: its name (the file name without .lnk) and path."""

    name: str = field(repr=False)
    path: Path = field(repr=False)
    folder: int = 0  # index of its folder in shortcut_dirs


@dataclass(frozen=True)
class Listing:
    shortcuts: tuple[Shortcut, ...]
    folders_read: int
    folders_failed: int
    truncated: bool = False


def list_shortcuts(folders: Sequence[Path], limit: int = MAX_SHORTCUTS) -> Listing:
    """The .lnk files directly inside ``folders``, in folder order and by name within a folder."""
    found: list[Shortcut] = []
    read = failed = 0
    truncated = False
    for index, folder in enumerate(folders):
        try:
            with os.scandir(folder) as entries:
                names = sorted((entry.name, Path(entry.path)) for entry in entries
                               if entry.name.lower().endswith(".lnk") and entry.is_file(follow_symlinks=False))
        except FileNotFoundError:
            failed += 1
            log.warning("shortcut folder %d: %s", index, FOLDER_MISSING)
            continue
        except NotADirectoryError:
            failed += 1
            log.warning("shortcut folder %d: %s", index, FOLDER_NOT_A_FOLDER)
            continue
        except OSError as exc:
            failed += 1
            log.warning("shortcut folder %d: %s (%s)", index, FOLDER_UNREADABLE, type(exc).__name__)
            continue
        read += 1
        for name, path in names:
            if len(found) >= limit:
                truncated = True
                break
            found.append(Shortcut(name[: -len(".lnk")], path, index))
        if truncated:
            log.warning("shortcut folders: more than %d shortcuts; the rest are ignored", limit)
            break
    return Listing(tuple(found), read, failed, truncated)


# ---------------------------------------------------------------- matching


def name_key(text: str) -> str:
    """Comparison key of a name: no case or accents, letters and digits only."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(ch for ch in decomposed if ch.isalnum() and not unicodedata.combining(ch))


def name_bound(key: str) -> int:
    if len(key) >= TWO_EDITS_CHARS:
        return 2
    if len(key) >= ONE_EDIT_CHARS:
        return 1
    return 0


def spoken_forms(spoken: str, vocabulary: Vocabulary) -> list[str]:
    """The spoken name and the personal-vocabulary names it stands for."""
    forms = [spoken]
    folded = vocabulary_fold(spoken)
    names = Vocabulary(names=vocabulary.names)
    for entry in names.names:
        if folded and any(vocabulary_fold(text) == folded for text in (entry.text, *entry.variants)):
            forms.append(entry.text)
    matched = Matcher(names).apply(spoken)
    if matched != spoken:
        forms.append(matched)
    return list(dict.fromkeys(forms))


@dataclass(frozen=True)
class Candidate:
    """Shortcuts sharing one name key, and how far the spoken name is from it."""

    key: str
    name: str = field(repr=False)
    shortcuts: tuple[Shortcut, ...] = field(repr=False)
    distance: int = 0
    conflict: bool = False  # same name, different targets


@dataclass(frozen=True)
class Match:
    """The outcome of ``match``: the chosen shortcut, or the closest names."""

    reason: str
    shortcut: Shortcut | None = field(default=None, repr=False)
    options: tuple[str, ...] = field(default=(), repr=False)
    distance: int | None = None
    candidates: int = 0

    @property
    def ok(self) -> bool:
        return self.shortcut is not None


def _groups(shortcuts: Iterable[Shortcut]) -> dict[str, list[Shortcut]]:
    groups: dict[str, list[Shortcut]] = {}
    for shortcut in shortcuts:
        key = name_key(shortcut.name)
        if key:
            groups.setdefault(key, []).append(shortcut)
    return groups


def _conflict(shortcuts: Sequence[Shortcut], reader: Callable[[Path], LinkTarget | None]) -> bool:
    if len(shortcuts) < 2:
        return False
    targets = [reader(shortcut.path) for shortcut in shortcuts]
    if any(target is None for target in targets):
        return True
    return len({target.identity for target in targets}) > 1


def _options(ranked: Sequence[Candidate], forms: Sequence[str]) -> tuple[str, ...]:
    """Up to ``MAX_OPTIONS`` closest names: inside the bound, then names that contain the
    spoken name (or the reverse, "deck" for nimbus-deck), then by edit distance."""
    def rank(candidate: Candidate) -> tuple[int, int, str]:
        if candidate.distance <= name_bound(candidate.key):
            group = 0
        elif any(len(form) >= MIN_PART_CHARS and form in candidate.key
                 or len(candidate.key) >= MIN_PART_CHARS and candidate.key in form for form in forms):
            group = 1
        else:
            group = 2
        return (group, candidate.distance, candidate.name.casefold())
    return tuple(candidate.name for candidate in sorted(ranked, key=rank)[:MAX_OPTIONS])


def match(spoken: str, shortcuts: Sequence[Shortcut], vocabulary: Vocabulary = Vocabulary(), *,
          reader: Callable[[Path], LinkTarget | None] = read_link) -> Match:
    """The one shortcut the spoken name clearly means, or the closest names."""
    groups = _groups(shortcuts)
    if not groups:
        return Match(NO_SHORTCUTS)
    forms = [key for key in (name_key(form) for form in spoken_forms(spoken, vocabulary)) if key]
    if not forms:
        return Match(NO_MATCH)
    ranked: list[Candidate] = []
    for key, members in groups.items():
        cap = max(len(key), max(len(form) for form in forms))
        found = min(distance(form, key, cap) for form in forms)
        ranked.append(Candidate(key, members[0].name, tuple(members), found))
    ranked.sort(key=lambda candidate: (candidate.distance, candidate.name.casefold()))
    options = _options(ranked, forms)
    best = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    if best.distance > name_bound(best.key):
        return Match(NO_MATCH, options=options, distance=best.distance, candidates=len(ranked))
    lead = FUZZY_LEAD if best.distance else 1
    if runner is not None and runner.distance - best.distance < lead:
        return Match(AMBIGUOUS, options=options, distance=best.distance, candidates=len(ranked))
    if _conflict(best.shortcuts, reader):
        return Match(AMBIGUOUS, options=(best.name,), distance=best.distance, candidates=len(ranked))
    return Match(MATCHED, best.shortcuts[0], distance=best.distance, candidates=len(ranked))


# ---------------------------------------------------------------- launching


class ShellLauncher:
    """Opens things for real. Tests use a fake with the same two methods."""

    def open_shortcut(self, path: Path) -> None:
        """Open an enumerated .lnk with the shell, as a double click would."""
        os.startfile(os.fspath(path))  # type: ignore[attr-defined]  # Windows only

    def start(self, argv: Sequence[str]) -> None:
        """Start a program from an argument list; never through a shell."""
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(list(argv), shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)


def vscode_candidates(env: Mapping[str, str]) -> list[Path]:
    """Where VS Code's Code.exe is installed for the user or for the machine."""
    found: list[Path] = []
    for variable, *parts in (("LOCALAPPDATA", "Programs", VSCODE_DIR), ("ProgramFiles", VSCODE_DIR),
                             ("ProgramFiles(x86)", VSCODE_DIR)):
        base = env.get(variable)
        if base:
            found.append(Path(base, *parts, "Code.exe"))
    return found


def find_vscode(env: Mapping[str, str] | None = None, exists: Callable[[Path], bool] = Path.is_file) -> Path | None:
    for path in vscode_candidates(os.environ if env is None else env):
        if exists(path):
            return path
    return None


def opens_vscode(target: LinkTarget) -> bool:
    path = PureWindowsPath(target.path)
    return path.is_absolute() and path.name.casefold() == VSCODE_EXE


def local_folder(target: LinkTarget, is_dir: Callable[[Path], bool] = Path.is_dir) -> bool:
    """A folder on a local drive (never a network share)."""
    path = PureWindowsPath(target.path)
    return bool(path.drive) and len(path.drive) == 2 and path.is_absolute() and is_dir(Path(target.path))


@dataclass(frozen=True)
class Launch:
    reason: str
    how: str = ""  # "shortcut" (shell-opened .lnk) or "folder" (VS Code on a folder)

    @property
    def ok(self) -> bool:
        return self.reason == OPENED


def launch(shortcut: Shortcut, launcher: object, *, reader: Callable[[Path], LinkTarget | None] = read_link,
           vscode: Callable[[], Path | None] = find_vscode,
           is_dir: Callable[[Path], bool] = Path.is_dir) -> Launch:
    """Open ``shortcut`` when its target is VS Code or a local folder; refuse anything else."""
    target = reader(shortcut.path)
    if target is None:
        return Launch(UNREADABLE)
    try:
        if opens_vscode(target):
            launcher.open_shortcut(shortcut.path)
            return Launch(OPENED, "shortcut")
        if local_folder(target, is_dir):
            code = vscode()
            if code is None:
                return Launch(VSCODE_MISSING)
            launcher.start([os.fspath(code), "--new-window", os.path.normpath(target.path)])
            return Launch(OPENED, "folder")
    except OSError as exc:
        log.warning("launch failed (%s)", type(exc).__name__)
        return Launch(LAUNCH_FAILED)
    return Launch(REFUSED)
