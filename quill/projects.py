"""The project of the window a dictation goes to, and that project's local folder.

``ProjectDetector.detect`` looks at the target window (``WindowInfo``):

- VS Code (``Code.exe``): the project is read from the title
  (``vscode_project``). A project hub title "<project> | <editor> - Visual
  Studio Code [<view>]" (or "<project> | Visual Studio Code" with no editor
  open, or "<project> | - Visual Studio Code") gives "<project>"; VS
  Code's standard title gives its folder part ("file - folder - Visual
  Studio Code", "file - folder - profile - Visual Studio Code [view]", or
  "folder - Visual Studio Code"). A dirty marker, the focused-view "[...]"
  and the editor state suffix VS Code appends after it with its screen
  reader optimization (" - Modified", ``quill.profiles.split_vscode_title``)
  are ignored; a title with only a file name, an empty title, or an
  overlong or unprintable candidate gives no project.
- Claude Code in a terminal (the ``claude-code`` profile and a terminal
  window): a known project name (``[project_context] folders`` or a
  shortcut name of ``[voice_commands] shortcut_dirs``) in the title wins.
  Otherwise the processes that descend from the window's process are listed
  (``Processes``, a fake in tests), and when exactly one Claude Code session
  (an outermost ``claude.exe``) runs under it, its current directory is read
  read-only (same user, no higher integrity than Quill, query-limited and
  VM-read access only, bounded length) and mapped to its nearest Git root
  (``git_root``: a bounded walk up; UNC, device and network paths are never
  probed; a drive root is never a project). Two or more sessions, access
  denied, an elevated, other-user or vanished process, or any read failure
  give no project.

A project name leads to its folder through ``ProjectFolders``: the
configured ``[project_context] folders`` first, then the targets of the
shortcuts (``.lnk``) in the shortcut folders: a folder, or a ``Code.exe``
argument that is a local folder or a ``.code-workspace`` file (its first
folder, resolved relative to the workspace file; JSON with comments and
trailing commas is tolerated; the file is size-bounded). Names compare
ignoring case, accents and punctuation (``quill.shortcuts.name_key``), also
against the folder's own name. Two different folders for one name, a
network, UNC or device path, a drive root or a missing folder give no folder.
The shortcut map is cached and read again only when a shortcut folder or a
workspace file it read changes (their size and modification time), so a
press does bounded work.

Nothing here writes, launches or changes anything. Window titles, project
names, paths and directories are never logged: logs hold reason codes and
counts only.
"""

from __future__ import annotations

import json
import logging
import ntpath
import os
import re
import sys
import threading
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from quill.command import is_terminal
from quill.notify import clean_name
from quill.profiles import split_vscode_title
from quill.shortcuts import LinkTarget, Listing, list_shortcuts, name_key, opens_vscode, read_link

log = logging.getLogger("quill.projects")

APP_NAME = "Visual Studio Code"
VSCODE_PROCESS = "code.exe"
CLAUDE_PROCESS = "claude.exe"
HUB_SEPARATOR = " | "
WORKSPACE_SUFFIX = ".code-workspace"

MAX_PROJECT_CHARS = 60  # characters of a project name read from a title
MAX_TITLE_CHARS = 1024  # a longer title is not parsed
MAX_PATH_CHARS = 4096  # characters of a path worth looking at
MAX_DEPTH = 32  # folders the Git root search walks up
MAX_TREE_DEPTH = 16  # process generations below the window's process
MAX_DESCENDANTS = 512  # processes below the window's process
MAX_WORKSPACE_BYTES = 64 * 1024
MAX_ARGUMENTS = 16  # arguments of a VS Code shortcut looked at

# GetDriveTypeW: drives whose folders may be probed (removable, fixed, RAM disk); never remote.
LOCAL_DRIVE_TYPES = frozenset({2, 3, 6})
DRIVE_FIXED = 3

# Where a project came from (reason codes; logged).
VSCODE_TITLE = "vscode_title"
TERMINAL_TITLE = "terminal_title"
SESSION = "claude_session"
# Why no project was found (reason codes; logged).
NO_WINDOW = "no_window"
OTHER_WINDOW = "other_window"
NO_NAME = "no_name"
NO_FOLDER = "no_folder"
NO_READER = "no_process_reader"
NO_SESSION = "no_session"
SESSIONS = "several_sessions"
TOO_MANY = "too_many_processes"
LIST_FAILED = "list_failed"
UNVERIFIED = "unverified_process"
OTHER_USER = "other_user"
ELEVATED = "elevated"
UNREADABLE = "unreadable"
VANISHED = "vanished"
NO_GIT_ROOT = "no_git_root"
FAILED = "failed"

_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_FILE_NAME = re.compile(r"\.\w{1,5}$")
_DIRTY = "●* "


# ---------------------------------------------------------------- titles


def _candidate(text: str) -> str | None:
    candidate = " ".join(_BRACKETS.sub("", text).split()).strip(_DIRTY)
    if not candidate or len(candidate) > MAX_PROJECT_CHARS or not candidate.isprintable():
        return None
    return candidate


def vscode_project(title: object) -> str | None:
    """The project named by a VS Code window title, or None; see the module docstring."""
    if not isinstance(title, str) or len(title) > MAX_TITLE_CHARS:
        return None
    split = split_vscode_title(title)  # the focused-view marker and the editor state suffix cut off
    if split is None:
        return None
    head = split.head.lstrip(_DIRTY)
    if not head.endswith((" - ", HUB_SEPARATOR)):
        return None  # "Visual Studio Code" alone, or a name that only ends with it
    head = head[:-3].strip()
    if not head:
        return None
    if HUB_SEPARATOR in head + " ":  # "<project> | - Visual Studio Code": no editor open
        return _candidate((head + " ").split(HUB_SEPARATOR, 1)[0])
    parts = [part.strip() for part in head.split(" - ")]
    if len(parts) == 1:
        candidate = _candidate(parts[0])
        return None if candidate is None or _FILE_NAME.search(candidate) else candidate
    # "file - folder" or "file - folder - profile": the folder is second from the end with a profile.
    return _candidate(parts[1] if len(parts) == 2 else parts[-2])


def _spaced(text: str) -> str:
    """Case, accents and punctuation ignored, words separated by single spaces."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    kept = "".join(ch if ch.isalnum() else " " for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(kept.split())


def names_in_title(title: str, names: Iterable[str]) -> list[str]:
    """The ``names`` said as whole words in ``title``; a name inside a longer hit is dropped."""
    if not isinstance(title, str) or len(title) > MAX_TITLE_CHARS:
        return []
    padded = f" {_spaced(title)} "
    hits = [(name, _spaced(name)) for name in names if _spaced(name) and f" {_spaced(name)} " in padded]
    return [name for name, spaced in hits
            if not any(len(other) > len(spaced) and f" {spaced} " in f" {other} " for _, other in hits)]


# ---------------------------------------------------------------- paths


def _unc_or_device(path: str) -> bool:
    """``\\\\server\\share``, ``\\\\?\\...`` or ``\\\\.\\...`` (either slash): never probed."""
    return len(path) >= 2 and path[0] in "\\/" and path[1] in "\\/"


def drive_type(drive: str) -> int:
    """GetDriveTypeW of ``X:``; outside Windows every drive counts as fixed."""
    if sys.platform != "win32":
        return DRIVE_FIXED
    import ctypes

    return int(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\"))


def _local_absolute(path: str, drives: Callable[[str], int]) -> bool:
    """An absolute path on a local drive letter; UNC, device and network paths are refused unprobed."""
    if not path or "\0" in path or len(path) > MAX_PATH_CHARS or _unc_or_device(path):
        return False
    drive, rest = ntpath.splitdrive(path)
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha() or not rest.startswith(("\\", "/")):
        return False
    try:
        return drives(drive.upper()) in LOCAL_DRIVE_TYPES
    except (OSError, ValueError):
        return False


def local_folder(path: object, *, is_dir: Callable[[Path], bool] = Path.is_dir,
                 drives: Callable[[str], int] = drive_type) -> str | None:
    """``path`` normalised when it is an existing folder on a local drive and not a drive root."""
    if not isinstance(path, (str, os.PathLike)):
        return None
    text = os.fspath(path)
    if not isinstance(text, str) or not _local_absolute(text, drives):
        return None
    folder = ntpath.normpath(text)
    if ntpath.dirname(folder) == folder:
        return None  # a drive root is never a project
    try:
        return folder if is_dir(Path(folder)) else None
    except (OSError, ValueError):
        return None


def git_root(cwd: object, *, exists: Callable[[str], bool] = os.path.lexists,
             drives: Callable[[str], int] = drive_type) -> str | None:
    """The nearest ancestor-or-self of ``cwd`` holding a ``.git`` entry, or None.

    The walk stops after ``MAX_DEPTH`` folders and never checks a drive root.
    A UNC, device, network or relative ``cwd`` is never probed.
    """
    if not isinstance(cwd, str) or not _local_absolute(cwd, drives):
        return None
    folder = ntpath.normpath(cwd)
    for _ in range(MAX_DEPTH):
        parent = ntpath.dirname(folder)
        if parent == folder:
            return None
        try:
            if exists(ntpath.join(folder, ".git")):
                return folder
        except (OSError, ValueError):
            return None
        folder = parent
    return None


def _identity(folder: str) -> str:
    return os.path.normcase(ntpath.normpath(folder))


# ---------------------------------------------------------------- shortcut targets


def split_arguments(text: str) -> list[str]:
    """A Windows command line's arguments, split as ``CommandLineToArgvW`` splits them."""
    args: list[str] = []
    current: list[str] = []
    quoted = started = False
    at = 0
    while at < len(text):
        char = text[at]
        if char == "\\":
            end = at
            while end < len(text) and text[end] == "\\":
                end += 1
            count = end - at
            if end < len(text) and text[end] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    end += 1
            else:
                current.append("\\" * count)
            started, at = True, end
        elif char == '"':
            if quoted and text[at + 1 : at + 2] == '"':
                current.append('"')
                at += 2
            else:
                quoted = not quoted
                at += 1
            started = True
        elif char in " \t" and not quoted:
            if started:
                args.append("".join(current))
                current, started = [], False
            at += 1
        else:
            current.append(char)
            started = True
            at += 1
    if started:
        args.append("".join(current))
    return args


def strip_jsonc(text: str) -> str:
    """JSON with ``//`` and ``/* */`` comments and trailing commas made plain JSON."""
    out: list[str] = []
    at, size = 0, len(text)
    in_string = False
    while at < size:
        char = text[at]
        if in_string:
            out.append(char)
            if char == "\\" and at + 1 < size:
                out.append(text[at + 1])
                at += 2
                continue
            if char == '"':
                in_string = False
            at += 1
        elif char == '"':
            in_string = True
            out.append(char)
            at += 1
        elif text.startswith("//", at):
            end = text.find("\n", at)
            at = size if end < 0 else end
        elif text.startswith("/*", at):
            end = text.find("*/", at + 2)
            at = size if end < 0 else end + 2
            out.append(" ")
        else:
            out.append(char)
            at += 1
    plain = "".join(out)
    # Trailing commas, outside strings.
    result: list[str] = []
    in_string = False
    at = 0
    while at < len(plain):
        char = plain[at]
        if in_string:
            result.append(char)
            if char == "\\" and at + 1 < len(plain):
                result.append(plain[at + 1])
                at += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            result.append(char)
        elif char == ",":
            rest = plain[at + 1 :].lstrip()
            if not rest.startswith(("}", "]")):
                result.append(char)
        else:
            result.append(char)
        at += 1
    return "".join(result)


def workspace_folder(path: str, *, is_dir: Callable[[Path], bool] = Path.is_dir,
                     drives: Callable[[str], int] = drive_type) -> str | None:
    """The first folder of a ``.code-workspace`` file, resolved relative to the file, or None."""
    if not _local_absolute(path, drives):
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_WORKSPACE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_WORKSPACE_BYTES:
        return None
    try:
        document = json.loads(strip_jsonc(data.decode("utf-8-sig")))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    folders = document.get("folders") if isinstance(document, dict) else None
    if not isinstance(folders, list) or not folders or not isinstance(folders[0], dict):
        return None
    raw = folders[0].get("path")
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_CHARS or "\0" in raw or _unc_or_device(raw):
        return None
    drive, rest = ntpath.splitdrive(raw)
    if drive or rest.startswith(("\\", "/")):
        candidate = raw  # absolute (or drive-relative, which local_folder refuses)
    else:
        candidate = ntpath.join(ntpath.dirname(ntpath.normpath(path)), raw)
    return local_folder(candidate, is_dir=is_dir, drives=drives)


def target_folder(target: LinkTarget, *, is_dir: Callable[[Path], bool] = Path.is_dir,
                  drives: Callable[[str], int] = drive_type) -> tuple[str | None, str | None]:
    """(project folder, workspace file read) of a shortcut target; (None, None) when it names none."""
    if not opens_vscode(target):
        return local_folder(target.path, is_dir=is_dir, drives=drives), None
    for argument in split_arguments(target.arguments)[:MAX_ARGUMENTS]:
        if not argument or argument.startswith("-"):
            continue
        if argument.casefold().endswith(WORKSPACE_SUFFIX):
            return workspace_folder(argument, is_dir=is_dir, drives=drives), argument
        folder = local_folder(argument, is_dir=is_dir, drives=drives)
        if folder is not None:
            return folder, None
    return None, None


# ---------------------------------------------------------------- names to folders


class ProjectFolders:
    """Project names to local folders: ``[project_context] folders``, then the shortcut targets."""

    def __init__(self, configured: Sequence[tuple[str, Path | str]] = (), shortcut_dirs: Sequence[Path] = (), *,
                 reader: Callable[[Path], LinkTarget | None] = read_link,
                 lister: Callable[[Sequence[Path]], Listing] = list_shortcuts,
                 is_dir: Callable[[Path], bool] = Path.is_dir, drives: Callable[[str], int] = drive_type,
                 stat: Callable[[object], os.stat_result] = os.stat) -> None:
        self._configured = tuple((name, os.fspath(folder)) for name, folder in configured)
        self._dirs = tuple(shortcut_dirs)
        self._reader, self._lister, self._stat = reader, lister, stat
        self.is_dir, self.drives = is_dir, drives
        self._lock = threading.Lock()
        self._signature: tuple | None = None
        self._workspaces: tuple[str, ...] = ()
        self._shortcuts: tuple[tuple[str, str], ...] = ()  # (shortcut name, folder)
        self.builds = 0

    def _stamp(self, path: object) -> tuple[int, int] | None:
        try:
            status = self._stat(path)
        except (OSError, ValueError):
            return None
        return (status.st_mtime_ns, status.st_size)

    def _current_signature(self) -> tuple:
        return (tuple(self._stamp(folder) for folder in self._dirs),
                tuple(self._stamp(path) for path in self._workspaces))

    def shortcut_folders(self) -> tuple[tuple[str, str], ...]:
        """(shortcut name, folder) of the shortcuts that name a folder; read again when a source changed."""
        if not self._dirs:
            return ()
        with self._lock:
            if self._signature is not None and self._current_signature() == self._signature:
                return self._shortcuts
            listing = self._lister(self._dirs)
            found: list[tuple[str, str]] = []
            workspaces: list[str] = []
            for shortcut in listing.shortcuts:
                target = self._reader(shortcut.path)
                if target is None:
                    continue
                folder, workspace = target_folder(target, is_dir=self.is_dir, drives=self.drives)
                if workspace is not None:
                    workspaces.append(workspace)
                if folder is not None:
                    found.append((shortcut.name, folder))
            self._shortcuts, self._workspaces = tuple(found), tuple(dict.fromkeys(workspaces))
            self._signature = self._current_signature()
            self.builds += 1
            log.info("project folders: %d of %d shortcuts name a folder (%d workspaces)", len(found),
                     len(listing.shortcuts), len(self._workspaces))
            return self._shortcuts

    def names(self) -> list[str]:
        """Every known project name: the configured ones, then the shortcut names."""
        return list(dict.fromkeys([name for name, _ in self._configured]
                                  + [name for name, _ in self.shortcut_folders()]))

    def _one(self, entries: Iterable[tuple[str, str]]) -> tuple[bool, str | None]:
        """(found any, the one valid folder or None) of the entries of one name."""
        folders = {_identity(folder): folder for _, folder in entries}
        if not folders:
            return False, None
        if len(folders) > 1:
            return True, None
        return True, local_folder(next(iter(folders.values())), is_dir=self.is_dir, drives=self.drives)

    def folder_for(self, name: str) -> str | None:
        """The folder of project ``name``, or None (unknown, ambiguous, network or missing)."""
        key = name_key(name) if isinstance(name, str) else ""
        if not key:
            return None
        shortcuts = self.shortcut_folders()
        for entries in ([entry for entry in self._configured if name_key(entry[0]) == key],
                        [entry for entry in shortcuts if name_key(entry[0]) == key],
                        [entry for entry in (*self._configured, *shortcuts)
                         if name_key(ntpath.basename(ntpath.normpath(entry[1]))) == key]):
            found, folder = self._one(entries)
            if found:
                return folder
        return None

    def name_for(self, folder: str) -> str | None:
        """The configured or shortcut name of ``folder``, or None."""
        identity = _identity(folder)
        for name, known in (*self._configured, *self.shortcut_folders()):
            if _identity(known) == identity:
                return name
        return None


# ---------------------------------------------------------------- Claude Code sessions


def claude_directory(reader: object, root: int) -> tuple[str | None, str]:
    """(current directory, reason) of the one Claude Code session under process ``root``.

    ``reader`` is ``quill.win32.Processes`` or a fake. A process counts as a
    descendant only when it was created after its parent (a reused process ID
    never makes a stranger a child); an outermost ``claude.exe`` is a
    session, and the processes it starts are part of it.
    """
    if not root:
        return None, NO_SESSION
    try:
        entries = reader.processes()
    except Exception as exc:  # noqa: BLE001 - no listing: no project
        log.warning("project: %s (%s)", LIST_FAILED, type(exc).__name__)
        return None, LIST_FAILED
    children: dict[int, list[tuple[int, str]]] = {}
    for pid, parent, image in entries:
        if pid != parent and pid:
            children.setdefault(parent, []).append((pid, str(image)))
    born = reader.created(root)
    if born is None:
        return None, UNVERIFIED
    sessions: list[int] = []
    level, seen = [(root, born)], {root}
    for _ in range(MAX_TREE_DEPTH):
        below: list[tuple[int, int]] = []
        for pid, parent_born in level:
            for child, image in children.get(pid, ()):
                if child in seen:
                    continue
                created = reader.created(child)
                if created is None:
                    return None, UNVERIFIED
                if created < parent_born:
                    continue  # the parent's ID was reused: not a descendant
                seen.add(child)
                if len(seen) > MAX_DESCENDANTS:
                    return None, TOO_MANY
                if ntpath.basename(image).casefold() == CLAUDE_PROCESS:
                    sessions.append(child)
                else:
                    below.append((child, created))
        if not below:
            break
        level = below
    else:
        return None, TOO_MANY
    if not sessions:
        return None, NO_SESSION
    if len(sessions) > 1:
        return None, SESSIONS
    pid = sessions[0]
    if not reader.same_user(pid):
        return None, OTHER_USER
    own, theirs = reader.own_integrity(), reader.process_integrity(pid)
    if own is None or theirs is None or theirs > own:
        return None, ELEVATED
    before = reader.created(pid)
    try:
        directory = reader.current_directory(pid)
    except Exception as exc:  # noqa: BLE001 - access denied, gone, not readable: no project
        log.info("project: %s (%s)", UNREADABLE, type(exc).__name__)
        return None, UNREADABLE
    if before is None or reader.created(pid) != before:
        return None, VANISHED
    if not isinstance(directory, str) or not directory or len(directory) > MAX_PATH_CHARS:
        return None, UNREADABLE
    return directory, SESSION


# ---------------------------------------------------------------- detection


@dataclass(frozen=True)
class Project:
    """A detected project: its name, its local folder and where it came from (a reason code)."""

    name: str = field(repr=False)
    folder: Path = field(repr=False)
    source: str


class ProjectDetector:
    """The project and folder of a target window; None when either is unknown."""

    def __init__(self, folders: ProjectFolders, processes: object | None = None, *,
                 exists: Callable[[str], bool] = os.path.lexists) -> None:
        self.folders = folders
        self.processes = processes
        self._exists = exists

    def detect(self, info: object | None, pid: int = 0, *, claude_code: bool = False) -> Project | None:
        """``info`` describes the window (``WindowInfo``), ``pid`` is its process; ``claude_code`` its profile."""
        project, reason = self.detect_reason(info, pid, claude_code=claude_code)
        if reason != FAILED:
            log.info("project: %s", reason)
        return project

    def detect_reason(self, info: object | None, pid: int = 0, *,
                      claude_code: bool = False) -> tuple[Project | None, str]:
        """(project or None, reason code) of the window; never raises."""
        try:
            return self._detect(info, pid, claude_code)
        except Exception as exc:  # noqa: BLE001 - detection never stops a dictation
            log.warning("project: %s (%s)", FAILED, type(exc).__name__)
            return None, FAILED

    def _named(self, name: str, source: str) -> tuple[Project | None, str]:
        folder = self.folders.folder_for(name)
        if folder is None:
            return None, NO_FOLDER
        return Project(name, Path(folder), source), source

    def _detect(self, info: object | None, pid: int, claude_code: bool) -> tuple[Project | None, str]:
        if info is None:
            return None, NO_WINDOW
        title = getattr(info, "title", "")
        title = title if isinstance(title, str) else ""
        if str(getattr(info, "process", "")).casefold() == VSCODE_PROCESS:
            name = vscode_project(title)
            return (None, NO_NAME) if name is None else self._named(name, VSCODE_TITLE)
        if not (claude_code and is_terminal(info)):
            return None, OTHER_WINDOW
        said = names_in_title(title, self.folders.names())
        folders = {_identity(folder): (name, folder) for name in said
                   if (folder := self.folders.folder_for(name)) is not None}
        if len(folders) == 1:
            name, folder = next(iter(folders.values()))
            return Project(name, Path(folder), TERMINAL_TITLE), TERMINAL_TITLE
        if self.processes is None:
            return None, NO_READER
        directory, reason = claude_directory(self.processes, pid)
        if directory is None:
            return None, reason
        root = git_root(directory, exists=self._exists, drives=self.folders.drives)
        folder = None if root is None else local_folder(root, is_dir=self.folders.is_dir,
                                                        drives=self.folders.drives)
        if folder is None:
            return None, NO_GIT_ROOT
        name = self.folders.name_for(folder) or clean_name(ntpath.basename(folder))
        if not name:
            return None, NO_NAME
        return Project(name, Path(folder), SESSION), SESSION
