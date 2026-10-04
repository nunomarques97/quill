"""The last Claude Code reply of a project folder, located and read read-only and bounded.

Claude Code keeps each session as a JSONL file in
``<Claude config>/projects/<encoded folder>/<session>.jsonl``, where the Claude
config is ``CLAUDE_CONFIG_DIR`` or ``~/.claude`` and the folder is encoded as
Claude Code does it (every character other than an ASCII letter or digit
becomes ``-``). ``last_reply(folder)`` finds the session of a project folder
and returns the visible text of its last assistant reply, or none with a
reason code. The sources, in order:

1. a fresh pointer for that folder, left in the ignored ``local/claude-pointers``
   by Quill's ``Stop`` hook (``quill.notify``) for attended sessions only: it
   names the session file of the last reply the user saw;
2. otherwise the most recently modified ``*.jsonl`` directly inside the
   folder's session directory (automated sessions in the same folder can win
   here: that is the limit of the fallback).

Pointers and session files older than the max age give none.

Safety: only a regular file whose resolved path lies inside
``<Claude config>/projects`` is opened. Symlinks, junctions and other reparse
points (on the file, the ``projects`` directory and the folder's directory),
``..``, UNC and device paths, and pointer paths outside that directory are
refused. The directory listing, the bytes read (the tail of the file only),
each line and the time spent are capped. Nothing is written or locked in the
Claude config folder.

Parsing: the first partial line of the tail is dropped and malformed or
oversized lines are skipped. The reply is the text blocks (``type`` ``text``)
of the main-chain assistant entries after the last real user message (a user
entry that is not only ``tool_result`` blocks, nor a meta entry Claude Code
adds itself). Sidechain entries, ``tool_use``, ``tool_result`` and
``thinking`` blocks are ignored. At most ``max_chars`` characters are kept,
from the end.

The text never appears in a ``repr`` or a log line; logs hold reason codes,
counts and milliseconds only.

``reply_context(folder, settings)`` is the lookup of a mouse 5 hold into
Claude Code: the reply read with the ``[claude_code]`` caps and turned into a
``quill.reply_terms.ReplyContext``, with a log line of counts only.
``projects_status()`` tells ``python -m quill --check`` whether the projects
folder is readable. The command line reports each known project folder
(``[project_context] folders`` and the shortcut targets) by number, with
reason codes and counts only:

    .venv\\Scripts\\python -m quill.claude_reply --dry-run
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import ntpath
import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("quill.claude_reply")

# The pointers the Stop hook leaves: one small JSON file per project folder, in the ignored local folder.
POINTERS_DIR = Path(__file__).resolve().parent.parent / "local" / "claude-pointers"
POINTER_PREFIX = "pointer-"
POINTER_SUFFIX = ".json"
POINTER_TEMP_SUFFIX = ".tmp"
MAX_POINTER_RECORD = 8 * 1024  # bytes: a folder, a session id, a session file path and a time
MAX_POINTERS = 64  # pointer files kept: the oldest goes first
MAX_PATH = 4096  # characters of a path worth looking at
MAX_SESSION_ID = 128

PROJECTS = "projects"
SESSION_SUFFIX = ".jsonl"
MAX_ENTRIES = 512  # directory entries looked at in the folder's session directory
MAX_TAIL_BYTES = 1024 * 1024  # bytes read from the end of a session file
MAX_LINE_BYTES = 256 * 1024  # a longer line is skipped unparsed
MAX_TIME_S = 0.25  # the whole lookup
MAX_FUTURE_S = 60.0  # a pointer dated further in the future is stale
DEFAULT_MAX_CHARS = 4000
DEFAULT_MAX_AGE_S = 12 * 3600.0
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

SOURCE_POINTER = "pointer"
SOURCE_NEWEST = "newest"

# Reason codes of a lookup.
OK = "ok"
BAD_FOLDER = "bad_folder"  # not a local absolute folder path (UNC, device, relative, '..', drive root)
BAD_CONFIG = "bad_config"  # the Claude config folder is a UNC, device or relative path
NO_PROJECTS = "no_projects_dir"
NO_SESSIONS = "no_session_dir"
NO_FILE = "no_session_file"
LINKED = "linked"  # a symlink, junction or other reparse point
NOT_REGULAR = "not_regular"
BAD_PATH = "bad_path"  # a pointer path that is UNC, device, relative, has '..' or is not a .jsonl file
OUTSIDE = "outside"  # a path outside <Claude config>/projects
STALE = "stale"
CHANGED = "changed"  # the file opened is not the file checked
UNREADABLE = "unreadable"
TIMEOUT = "timeout"
NO_TEXT = "no_text"
FAILED = "failed"

# Reason codes of the pointer step (``ReplyLookup.pointer``).
NO_POINTER = "no_pointer"
POINTER_INVALID = "pointer_invalid"
POINTER_OTHER = "pointer_other_folder"
POINTER_STALE = "pointer_stale"
POINTER_USED = "pointer_used"

_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,%d}" % MAX_SESSION_ID)
_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class ReplyLookup:
    """The outcome of one lookup: ``text`` is the reply, or None with ``reason`` saying why."""

    reason: str
    source: str | None = None
    pointer: str = NO_POINTER
    text: str | None = field(default=None, repr=False)
    ms: int = 0

    @property
    def found(self) -> bool:
        return self.text is not None

    @property
    def chars(self) -> int:
        return 0 if self.text is None else len(self.text)


# ---------------------------------------------------------------- paths


def claude_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The user's Claude Code config folder: ``CLAUDE_CONFIG_DIR`` or ``~/.claude``."""
    environ = os.environ if environ is None else environ
    folder = environ.get("CLAUDE_CONFIG_DIR")
    return Path(folder) if folder else Path.home() / ".claude"


def default_pointers_dir() -> Path:
    """The folder of the Stop hook's pointers."""
    return POINTERS_DIR


def _unc_or_device(path: str) -> bool:
    """``\\\\server\\share\\...``, ``\\\\?\\...`` or ``\\\\.\\...`` (either slash)."""
    return len(path) >= 2 and path[0] in "\\/" and path[1] in "\\/"


def local_path(value: object) -> str | None:
    """``value`` normalised when it is an absolute path on a drive letter; None for a non-string,
    an empty, oversized or NUL-containing text, a UNC or device path, a relative path, one with
    ``..`` or one naming an alternate data stream (a ``:`` after the drive)."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH or "\0" in value or _unc_or_device(value):
        return None
    drive, rest = ntpath.splitdrive(value)
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isascii() or not drive[0].isalpha():
        return None
    if not rest.startswith(("\\", "/")) or ":" in rest or ".." in re.split(r"[\\/]", rest):
        return None
    return ntpath.normpath(value)


def folder_identity(folder: object) -> str | None:
    """The case-insensitive, normalised identity of a project folder; None when it is not a
    local absolute folder path or is a drive root."""
    path = local_path(folder)
    if path is None or ntpath.dirname(path) == path:
        return None
    return ntpath.normcase(path).casefold()


def pointer_name(identity: str) -> str:
    """The pointer file name of a folder identity."""
    digest = hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()
    return POINTER_PREFIX + digest[:32] + POINTER_SUFFIX


def encode_folder(folder: str) -> str:
    """The session directory name Claude Code gives a folder."""
    return _NOT_ALNUM.sub("-", folder)


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _directory(path: str, lstat: Callable[[str], os.stat_result], missing: str) -> str | None:
    """None when ``path`` is a plain directory; else the reason code."""
    try:
        info = lstat(path)
    except FileNotFoundError:
        return missing
    except (OSError, ValueError):
        return UNREADABLE
    if _linked(info):
        return LINKED
    if not stat.S_ISDIR(info.st_mode):
        return missing
    return None


def _same(path: str, root: str) -> bool:
    return ntpath.normcase(path).casefold() == ntpath.normcase(root).casefold()


def _inside(path: str, root: str) -> bool:
    """Whether resolved ``path`` lies strictly inside resolved ``root``."""
    try:
        real, base = os.path.realpath(path), os.path.realpath(root)
        common = ntpath.commonpath([ntpath.normcase(real), ntpath.normcase(base)])
    except (OSError, ValueError):
        return False
    return _same(common, base) and not _same(real, base)


# ---------------------------------------------------------------- pointer


def _read_pointer(pointers_dir: Path, identity: str, now: float, max_age_s: float,
                  lstat: Callable[[str], os.stat_result]) -> tuple[str | None, str]:
    """(session file path text, pointer reason) of the folder's pointer."""
    path = os.path.join(os.fspath(pointers_dir), pointer_name(identity))
    try:
        info = lstat(path)
        if _linked(info) or not stat.S_ISREG(info.st_mode):
            return None, POINTER_INVALID
        with open(path, "rb") as handle:
            data = handle.read(MAX_POINTER_RECORD + 1)
    except FileNotFoundError:
        return None, NO_POINTER
    except (OSError, ValueError):
        return None, POINTER_INVALID
    if len(data) > MAX_POINTER_RECORD:
        return None, POINTER_INVALID
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, POINTER_INVALID
    if not isinstance(record, dict):
        return None, POINTER_INVALID
    if folder_identity(record.get("folder")) != identity:
        return None, POINTER_OTHER
    at = record.get("time")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None, POINTER_INVALID
    if now - at > max_age_s or at - now > MAX_FUTURE_S:
        return None, POINTER_STALE
    session = record.get("transcript_path")
    if not isinstance(session, str) or not isinstance(record.get("session_id"), str):
        return None, POINTER_INVALID
    return session, POINTER_USED


def _pointed_file(value: str, projects: str) -> tuple[str | None, str]:
    """(normalised session file path, reason) of a pointer's path: it must be
    ``<projects>/<directory>/<name>.jsonl``."""
    path = local_path(value)
    if path is None or not path.casefold().endswith(SESSION_SUFFIX):
        return None, BAD_PATH
    if not _same(ntpath.dirname(ntpath.dirname(path)), projects):
        return None, OUTSIDE
    return path, OK


# ---------------------------------------------------------------- session files


def _newest(directory: str, lstat: Callable[[str], os.stat_result],
            expired: Callable[[], bool]) -> tuple[str | None, str]:
    """(path, reason) of the most recently modified ``*.jsonl`` among the first ``MAX_ENTRIES`` entries."""
    try:
        with os.scandir(directory) as entries:
            names = [entry.name for entry in itertools.islice(entries, MAX_ENTRIES)]
    except OSError:
        return None, UNREADABLE
    best: tuple[float, str] | None = None
    for name in names:
        if expired():
            return None, TIMEOUT
        if not name.casefold().endswith(SESSION_SUFFIX):
            continue
        path = os.path.join(directory, name)
        try:
            modified = lstat(path).st_mtime
        except (OSError, ValueError):
            continue
        if best is None or (modified, name) > best:
            best = (modified, name)
    if best is None:
        return None, NO_FILE
    return os.path.join(directory, best[1]), OK


def _open_checked(path: str, projects: str, now: float, max_age_s: float,
                  lstat: Callable[[str], os.stat_result]) -> tuple[tuple[bytes, bool] | None, str]:
    """((tail bytes, starts mid-file), reason) of a session file that passes every check."""
    reason = _directory(ntpath.dirname(path), lstat, NO_SESSIONS)
    if reason is not None:
        return None, reason
    try:
        info = lstat(path)
    except FileNotFoundError:
        return None, NO_FILE
    except (OSError, ValueError):
        return None, UNREADABLE
    if _linked(info):
        return None, LINKED
    if not stat.S_ISREG(info.st_mode):
        return None, NOT_REGULAR
    if not _inside(path, projects):
        return None, OUTSIDE
    if now - info.st_mtime > max_age_s:
        return None, STALE
    try:
        with open(path, "rb", buffering=0) as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
                return None, CHANGED
            start = max(0, opened.st_size - MAX_TAIL_BYTES)
            handle.seek(start)
            chunks: list[bytes] = []
            size = 0
            while size < MAX_TAIL_BYTES:
                chunk = handle.read(MAX_TAIL_BYTES - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
    except (OSError, ValueError):
        return None, UNREADABLE
    return (b"".join(chunks), start > 0), OK


def _real_user(content: object) -> bool:
    """A user message typed by the user: text, or blocks other than tool results only."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(not (isinstance(block, dict) and block.get("type") == "tool_result") for block in content)
    return False


def _texts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]


def reply_text(data: bytes, partial: bool, max_chars: int,
               expired: Callable[[], bool] = lambda: False) -> tuple[str | None, str]:
    """(visible text of the last assistant reply, reason) in the tail ``data`` of a session file.

    ``partial``: the tail starts mid-file, so its first line is dropped.
    """
    lines = data.split(b"\n")
    if partial:
        lines = lines[1:]
    parts: list[str] = []
    total = 0
    for raw in reversed(lines):
        if expired():
            return None, TIMEOUT
        if len(raw) > MAX_LINE_BYTES or not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain", False) is not False:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        kind = entry.get("type")
        if kind == "user":
            if entry.get("isMeta") is True:
                continue
            if _real_user(message.get("content")):
                break
            continue
        if kind != "assistant" or entry.get("isApiErrorMessage") is True:
            continue
        for text in reversed(_texts(message.get("content"))):
            parts.append(text)
            total += len(text)
        if total >= max_chars:
            break  # everything before is cut anyway
    text = _CONTROL.sub("", "\n\n".join(part.strip() for part in reversed(parts) if part.strip())).strip()
    if not text:
        return None, NO_TEXT
    return text[-max_chars:].strip(), OK


# ---------------------------------------------------------------- lookup


def last_reply(folder: object, *, config_dir: Path | str | None = None, pointers_dir: Path | str | None = None,
               max_chars: int = DEFAULT_MAX_CHARS, max_age_s: float = DEFAULT_MAX_AGE_S,
               lstat: Callable[[str], os.stat_result] = os.lstat, wall: Callable[[], float] = time.time,
               clock: Callable[[], float] = time.monotonic, max_time_s: float = MAX_TIME_S) -> ReplyLookup:
    """The last assistant reply of the Claude Code session of project ``folder``.

    ``config_dir`` and ``pointers_dir`` default to ``claude_config_dir()`` and
    ``default_pointers_dir()``. Every failure gives none with a reason code.
    """
    started = clock()
    max_chars = max(1, int(max_chars))
    try:
        # Outside the catch-all below, so the tests' guard of the real folders is never swallowed.
        config = claude_config_dir() if config_dir is None else config_dir
        pointers = default_pointers_dir() if pointers_dir is None else pointers_dir
    except (OSError, RuntimeError, KeyError, ValueError):
        config = pointers = None
    try:
        if config is None or pointers is None:
            lookup = ReplyLookup(BAD_CONFIG)
        else:
            lookup = _lookup(folder, config, Path(pointers), max_chars, max_age_s, lstat, wall(),
                             lambda: clock() - started > max_time_s)
    except Exception as exc:  # noqa: BLE001 - a failed lookup only leaves the dictation without context
        log.debug("last reply failed (%s)", type(exc).__name__)
        lookup = ReplyLookup(FAILED)
    ms = max(0, int(round((clock() - started) * 1000)))
    lookup = ReplyLookup(lookup.reason, lookup.source, lookup.pointer, lookup.text, ms)
    log.debug("last reply: %s (source %s, %s, %d chars, %d ms)", lookup.reason, lookup.source or "none",
              lookup.pointer, lookup.chars, lookup.ms)
    return lookup


def _lookup(folder: object, config_dir: Path | str, pointers: Path, max_chars: int, max_age_s: float,
            lstat: Callable[[str], os.stat_result], now: float, expired: Callable[[], bool]) -> ReplyLookup:
    identity = folder_identity(folder)
    if identity is None:
        return ReplyLookup(BAD_FOLDER)
    config = local_path(os.fspath(config_dir))
    if config is None:
        return ReplyLookup(BAD_CONFIG)
    projects = ntpath.join(config, PROJECTS)
    reason = _directory(projects, lstat, NO_PROJECTS)
    if reason is not None:
        return ReplyLookup(reason)

    pointed, pointer = _read_pointer(pointers, identity, now, max_age_s, lstat)
    if pointed is not None:
        path, reason = _pointed_file(pointed, projects)
        if path is not None:
            opened, reason = _open_checked(path, projects, now, max_age_s, lstat)
            if opened is not None:
                text, reason = reply_text(opened[0], opened[1], max_chars, expired)
                if reason != TIMEOUT:
                    return ReplyLookup(reason, SOURCE_POINTER, pointer, text)
        if reason == TIMEOUT:
            return ReplyLookup(reason, SOURCE_POINTER, pointer)
        pointer = reason if reason.startswith("pointer") else "pointer_" + reason

    directory = ntpath.join(projects, encode_folder(local_path(folder) or ""))
    reason = _directory(directory, lstat, NO_SESSIONS)
    if reason is not None:
        return ReplyLookup(reason, pointer=pointer)
    path, reason = _newest(directory, lstat, expired)
    if path is None:
        return ReplyLookup(reason, pointer=pointer)
    opened, reason = _open_checked(path, projects, now, max_age_s, lstat)
    if opened is None:
        return ReplyLookup(reason, SOURCE_NEWEST, pointer)
    text, reason = reply_text(opened[0], opened[1], max_chars, expired)
    return ReplyLookup(reason, SOURCE_NEWEST, pointer, text)


# ---------------------------------------------------------------- the context of a mouse 5 hold

NO_TERMS = "no_terms"  # a reply was read but gives no term, option or question


@dataclass(frozen=True)
class ReplyFound:
    """The reply context of one lookup for mouse 5: ``context`` is the derived
    ``quill.reply_terms.ReplyContext`` (None: none, ``reason`` says why) and the
    rest are counts for the log line (``log_fields``)."""

    reason: str
    source: str | None = None
    context: object | None = field(default=None, repr=False)
    terms: int = 0
    options: int = 0
    ms: int = 0

    def log_fields(self) -> str:
        """Reason code, source, counts and milliseconds: never text, a title, a session id or a path."""
        return (f"last reply {self.reason} (source {self.source or 'none'}, {self.terms} terms, "
                f"{self.options} options, {self.ms} ms)")


def reply_context(folder: object, settings: object, *, reader: Callable[..., ReplyLookup] | None = None,
                  clock: Callable[[], float] = time.monotonic) -> ReplyFound:
    """The ``ReplyFound`` of project ``folder`` with the ``[claude_code]`` caps of ``settings``
    (``quill.config.ClaudeCode``): at most ``last_reply_max_chars`` characters read by ``reader``
    (default ``last_reply``), sessions older than ``last_reply_max_age_h`` hours refused, at most
    ``last_reply_max_terms`` terms derived (``quill.reply_terms.derive``). Never raises: any failure
    gives none with ``failed``."""
    from quill.reply_terms import derive  # here: quill.reply_terms imports quill.config, which imports this module

    started = clock()
    try:
        lookup = (reader or last_reply)(folder, max_chars=settings.last_reply_max_chars,
                                        max_age_s=settings.last_reply_max_age_h * 3600.0)
        context = derive(lookup.text, settings.last_reply_max_terms) if lookup.text is not None else None
        if lookup.text is None:
            found = ReplyFound(lookup.reason, lookup.source)
        elif not context:
            found = ReplyFound(NO_TERMS, lookup.source)
        else:
            found = ReplyFound(OK, lookup.source, context, len(context.terms), len(context.options))
    except Exception as exc:  # noqa: BLE001 - a failed lookup only leaves the dictation without context
        log.debug("reply context failed (%s)", type(exc).__name__)
        found = ReplyFound(FAILED)
    ms = max(0, int(round((clock() - started) * 1000)))
    return ReplyFound(found.reason, found.source, found.context, found.terms, found.options, ms)


def projects_status(config_dir: Path | str | None = None, *,
                    lstat: Callable[[str], os.stat_result] = os.lstat) -> tuple[str, int]:
    """(reason, session directories) of ``<Claude config>/projects``: ``ok`` when it is a plain
    readable directory, with the count of its directories (at most ``MAX_ENTRIES`` looked at)."""
    config = claude_config_dir() if config_dir is None else config_dir
    path = local_path(os.fspath(config))
    if path is None:
        return BAD_CONFIG, 0
    projects = ntpath.join(path, PROJECTS)
    reason = _directory(projects, lstat, NO_PROJECTS)
    if reason is not None:
        return reason, 0
    try:
        with os.scandir(projects) as entries:
            count = sum(1 for entry in itertools.islice(entries, MAX_ENTRIES)
                        if entry.is_dir(follow_symlinks=False))
    except OSError:
        return UNREADABLE, 0
    return OK, count


# ---------------------------------------------------------------- dry run


def known_folders(project_context: object, shortcut_dirs: object, *,
                  folders: object | None = None) -> list[str]:
    """The known project folders, once each: ``[project_context] folders``, then the shortcut targets."""
    if folders is None:
        from quill.projects import ProjectFolders

        folders = ProjectFolders(project_context.folders, shortcut_dirs)
    found: dict[str, str] = {}
    entries = [os.fspath(folder) for _, folder in project_context.folders]
    entries += [folder for _, folder in folders.shortcut_folders()]
    for folder in entries:
        identity = folder_identity(folder)
        if identity is not None and identity not in found:
            found[identity] = folder
    return list(found.values())


def dry_run(config: object, *, out: Callable[[str], None] = print, reader: Callable[..., ReplyLookup] | None = None,
            folders: object | None = None) -> int:
    """One line per known project folder: its number, the reason code, source, counts and milliseconds.

    Never prints text, a name, a title, a session id or a path; opens no microphone and touches no window.
    """
    settings = config.claude_code
    known = known_folders(config.project_context, config.voice.shortcut_dirs, folders=folders)
    out(f"last reply context: {'on' if settings.last_reply_context else 'off'} in the settings; "
        f"{len(known)} known project folders")
    found = 0
    for number, folder in enumerate(known, 1):
        result = reply_context(folder, settings, reader=reader)
        found += result.context is not None
        out(f"folder {number}: {result.reason} (source {result.source or 'none'}, {result.terms} terms, "
            f"{result.options} options, {result.ms} ms)")
    out(f"{found} of {len(known)} folders have a reply context")
    return 0


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print,
         reader: Callable[..., ReplyLookup] | None = None, folders: object | None = None) -> int:
    """``python -m quill.claude_reply --dry-run [--config PATH]``. Tests pass a fake reader and folders."""
    import argparse

    from quill.config import LOCAL_CONFIG, ConfigError, load_config

    parser = argparse.ArgumentParser(prog="python -m quill.claude_reply",
                                     description="Report the last Claude Code reply lookup of each known project "
                                                 "folder (reason codes and counts only).")
    parser.add_argument("--dry-run", action="store_true", required=True,
                        help="look up and report counts; prints no text, name or path")
    parser.add_argument("--config", type=Path, default=LOCAL_CONFIG, help="settings file (default local/quill.toml)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        out(str(exc))
        return 2
    return dry_run(config, out=out, reader=reader, folders=folders)


if __name__ == "__main__":
    raise SystemExit(main())
