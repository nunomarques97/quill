"""Claude Code attention alerts: the hook-side notifier and the app-side listener.

Claude Code runs the notifier as a hook (installed only by
``python -m quill.claude_hooks --install``), in exec form without a shell:

    <repo>\\.venv\\Scripts\\pythonw.exe <repo>\\quill\\notify.py stop
    <repo>\\.venv\\Scripts\\pythonw.exe <repo>\\quill\\notify.py permission

``stop`` is the ``Stop`` hook (Claude Code finished its reply and waits for
the user); ``permission`` is the ``Notification`` hook with the matcher
``permission_prompt``. The notifier reads the hook's JSON from stdin (at most
``MAX_INPUT`` bytes, for at most ``READ_TIMEOUT_S``), keeps only the event
name, ``stop_hook_active``, ``notification_type`` and ``cwd``, and never logs,
prints or stores anything else (the message and the assistant's text are
dropped unread). It ignores ``stop_hook_active`` true, any other notification
type, malformed, oversized or late input, and the sessions the filter rejects.
It then opens one named Windows event in the signed-in user's session
(``Local\\``): no network, no port. When Quill is not running the event does
not exist and nothing else happens. Otherwise the project (``project_name``:
the folder name of the Git root holding ``cwd``, else of ``cwd`` itself) goes
into one small record file in ``local/alerts`` (written under a temporary
name, then renamed, so a half-written record is never read), and the event is
set. A failed write still sets the event: Quill then shows the alert without a
name. It prints nothing, never blocks Claude Code and always exits 0; the
project name is never logged.

The headless filter (``[claude_alert] filter``) reads the environment
variable Claude Code gives every hook, ``CLAUDE_CODE_SESSION_ATTENDED``:
``1`` for a session someone is using (the terminal interface, the VS Code
panel), ``0`` for ``claude -p``, the Agent SDK and any Claude Code started by
another Claude Code (for example automated runs). This was read from the
installed Claude Code; its limits are in docs/USAR.md.

``AlertListener`` runs inside Quill: it creates the two events and calls
``on_alert(kind, project)`` from its own thread each time one is set. Each
wake reads and deletes every pending record and delivers each distinct
(kind, project) once. A wake that finds no record of its kind delivers
``on_alert(kind, None)`` (the alert without a name), unless a named alert of
that kind was delivered in the last ``NAMED_GRACE_S``: that wake is the set of
a record an earlier wake already read.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import ntpath
import os
import sys
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

if __package__ in (None, ""):
    # Run as a script by the hook: find the quill package from any working directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quill.sound import DONE, PERMISSION  # noqa: E402

log = logging.getLogger("quill.notify")

EVENT_NAMES = {DONE: "Local\\Quill.ClaudeCode.Done", PERMISSION: "Local\\Quill.ClaudeCode.Permission"}
# Hook argument -> (hook_event_name expected in the input, alert kind).
HOOKS = {"stop": ("Stop", DONE), "permission": ("Notification", PERMISSION)}
PERMISSION_TYPE = "permission_prompt"
MAX_INPUT = 4 * 1024 * 1024
READ_TIMEOUT_S = 0.5
POLL_S = 0.25

ATTENDED_VARIABLE = "CLAUDE_CODE_SESSION_ATTENDED"
FILTER_ATTENDED = "attended"  # only sessions Claude Code marks as attended (the variable is 1)
FILTER_UNLESS_HEADLESS = "unless-headless"  # every session except those marked unattended (0)
FILTER_ALL = "all"  # every session, headless ones too
FILTERS = (FILTER_ATTENDED, FILTER_UNLESS_HEADLESS, FILTER_ALL)
DEFAULT_FILTER = FILTER_ATTENDED

# The project records: one small JSON file per named alert, in the ignored local folder.
ALERTS_DIR = Path(__file__).resolve().parent.parent / "local" / "alerts"
RECORD_PREFIX = "alert-"
RECORD_SUFFIX = ".json"
TEMP_SUFFIX = ".tmp"
MAX_RECORD = 1024  # bytes: a record holds a kind, a name and a time
MAX_PENDING = 32  # records and temporary files waiting: beyond this the hook writes none
MAX_READ = 256  # folder entries one wake looks at
MAX_AGE_S = 60.0  # older records (and records dated in the future) are stale
OPEN_TRIES = 5  # opens of one record refused by a passing handle (a scanner on a fresh file)
OPEN_RETRY_S = 0.02  # seconds between those opens: at most 0.08 s per record
NAMED_GRACE_S = 2.0  # a wake without a record this soon after a named alert of its kind rings nothing
MAX_NAME = 48  # characters of a project name
MAX_CWD = 4096  # characters of a cwd worth looking at
MAX_DEPTH = 32  # folders the Git root search walks up


# ---------------------------------------------------------------- hook side


def read_input(read: Callable[[int], bytes], limit: int = MAX_INPUT, timeout_s: float = READ_TIMEOUT_S) -> bytes | None:
    """Everything on stdin up to end of file; None when larger than ``limit``, late or unreadable.

    ``read(n)`` returns up to ``n`` bytes (b"" at end of file). It runs on a
    daemon thread, so a stdin that never closes cannot hold the hook.
    """
    box: dict[str, object] = {}

    def reader() -> None:
        chunks: list[bytes] = []
        size = 0
        try:
            while size <= limit:
                chunk = read(limit + 1 - size)
                if not chunk:
                    box["data"] = b"".join(chunks)
                    return
                chunks.append(chunk)
                size += len(chunk)
            box["oversized"] = True
        except (OSError, ValueError):
            box["error"] = True

    thread = threading.Thread(target=reader, name="quill-notify-stdin", daemon=True)
    thread.start()
    thread.join(timeout_s)
    data = box.get("data")
    return data if isinstance(data, bytes) else None


def parse_event(data: bytes | None, hook: str) -> str | None:
    """The alert kind for the hook's input, or None when it must not ring."""
    return parse_input(data, hook)[0]


def parse_input(data: bytes | None, hook: str) -> tuple[str | None, object]:
    """(alert kind, raw ``cwd`` value) of the hook's input; (None, None) when it must not ring.

    Only ``hook_event_name``, ``stop_hook_active``, ``notification_type`` and
    ``cwd`` are looked at.
    """
    if hook not in HOOKS or data is None:
        return None, None
    expected, kind = HOOKS[hook]
    try:
        event = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, None
    if not isinstance(event, dict) or event.get("hook_event_name") != expected:
        return None, None
    if kind == DONE and event.get("stop_hook_active", False) is not False:
        # Claude Code is continuing because a Stop hook asked it to: not the end of the reply.
        return None, None
    if kind == PERMISSION and event.get("notification_type") != PERMISSION_TYPE:
        return None, None
    return kind, event.get("cwd")


def clean_name(value: object) -> str | None:
    """A project name safe to show and to speak, or None when nothing is left.

    Control, format (bidi overrides included), surrogate, private-use and
    unassigned characters are removed, whitespace is collapsed to single
    spaces, and at most ``MAX_NAME`` characters are kept.
    """
    if not isinstance(value, str):
        return None
    kept: list[str] = []
    for char in value[:MAX_CWD]:
        if char.isspace():
            kept.append(" ")
        elif not unicodedata.category(char).startswith("C"):
            kept.append(char)
    name = " ".join("".join(kept).split())[:MAX_NAME].strip()
    return name or None


def _unc_or_device(path: str) -> bool:
    """``\\\\server\\share\\...``, ``\\\\?\\...`` or ``\\\\.\\...`` (either slash): never probed on disk."""
    return len(path) >= 2 and path[0] in "\\/" and path[1] in "\\/"


def project_name(cwd: object, exists: Callable[[str], bool] = os.path.lexists) -> str | None:
    """The project of a Claude Code session, from its ``cwd``, or None.

    It is the folder name of the nearest ancestor-or-self of ``cwd`` holding a
    ``.git`` entry (a folder, or a file in a worktree), else the last folder
    name of ``cwd``; the walk up stops after ``MAX_DEPTH`` folders. A UNC or
    device path, or one without a drive, is never probed: only its last folder
    name counts. A missing, non-string, empty, NUL-containing or oversized
    ``cwd``, or a drive root, gives None.
    """
    if not isinstance(cwd, str) or not cwd or "\0" in cwd or len(cwd) > MAX_CWD:
        return None
    if _unc_or_device(cwd):
        return clean_name(ntpath.basename(cwd.rstrip("\\/")))
    path = ntpath.normpath(cwd)
    own = clean_name(ntpath.basename(path.rstrip("\\/")))
    if own is None or not ntpath.isabs(path) or not ntpath.splitdrive(path)[0]:
        return own
    folder = path
    for _ in range(MAX_DEPTH):
        try:
            if exists(ntpath.join(folder, ".git")):
                return clean_name(ntpath.basename(folder.rstrip("\\/"))) or own
        except (OSError, ValueError):
            return own
        parent = ntpath.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return own


def write_record(folder: Path, kind: str, project: str, now: float | None = None) -> bool:
    """One alert record in ``folder``, written under a temporary name and then renamed.

    The folder is never created. False when it is missing, already holds
    ``MAX_PENDING`` records, or the write fails.
    """
    created: Path | None = None
    try:
        with os.scandir(folder) as entries:
            pending = sum(1 for entry in itertools.islice(entries, MAX_READ) if entry.name.startswith(RECORD_PREFIX))
        if pending >= MAX_PENDING:
            return False
        stem = RECORD_PREFIX + uuid.uuid4().hex
        data = json.dumps({"kind": kind, "project": project, "time": time.time() if now is None else now},
                          ensure_ascii=True).encode("ascii")
        if len(data) > MAX_RECORD:
            return False
        temporary = folder / (stem + TEMP_SUFFIX)
        with open(temporary, "xb") as handle:
            created = temporary
            handle.write(data)
        os.replace(temporary, folder / (stem + RECORD_SUFFIX))
        return True
    except (OSError, ValueError):
        if created is not None:
            try:
                os.remove(created)
            except OSError:
                pass
        return False


def session_allowed(environ: Mapping[str, str], mode: str = DEFAULT_FILTER) -> bool:
    """Whether the Claude Code session that ran the hook may ring, under the filter ``mode``."""
    value = environ.get(ATTENDED_VARIABLE)
    if mode == FILTER_ALL:
        return True
    if mode == FILTER_UNLESS_HEADLESS:
        return value != "0"
    return value == "1"


def alert_settings(load: Callable[[], object] | None = None) -> tuple[bool, str]:
    """(enabled, filter) from the Quill settings; an unreadable file keeps the defaults."""
    try:
        if load is None:
            from quill.config import load_config

            load = load_config
        settings = load().claude_alert
        return settings.enabled, settings.filter
    except Exception:  # noqa: BLE001 - a hook never fails: the defaults apply
        return True, DEFAULT_FILTER


class Events:
    """The kernel32 named-event calls of the notifier and the listener (ctypes)."""

    EVENT_MODIFY_STATE = 0x0002
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x102

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateEventW": (wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR),
            "OpenEventW": (wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR),
            "SetEvent": (wintypes.BOOL, wintypes.HANDLE),
            "WaitForMultipleObjects": (wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
                                       wintypes.BOOL, wintypes.DWORD),
            "CloseHandle": (wintypes.BOOL, wintypes.HANDLE),
        }
        for name, (restype, *argtypes) in signatures.items():
            function = getattr(kernel32, name)
            function.restype = restype
            function.argtypes = argtypes
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._k = kernel32

    def open_event(self, name: str) -> int | None:
        """A handle that may set an existing named event; None when it does not exist (Quill is not running)."""
        return self._k.OpenEventW(self.EVENT_MODIFY_STATE, False, name) or None

    def signal(self, handle: int) -> bool:
        return bool(self._k.SetEvent(handle))

    def set_event(self, name: str) -> bool:
        """Set an existing named event; False when it does not exist (Quill is not running)."""
        handle = self.open_event(name)
        if handle is None:
            return False
        try:
            return self.signal(handle)
        finally:
            self.close(handle)

    def create_event(self, name: str) -> int:
        """An auto-reset named event: one wait consumes every set before it."""
        handle = self._k.CreateEventW(None, False, False, name)
        if not handle:
            raise OSError(f"CreateEventW failed (error {self._ctypes.get_last_error()})")
        return handle

    def wait_any(self, handles: list[int], timeout_s: float) -> int | None:
        """The index of a set event (reset by the wait), or None after ``timeout_s``."""
        array = (self._wintypes.HANDLE * len(handles))(*handles)
        result = self._k.WaitForMultipleObjects(len(handles), array, False, max(0, int(timeout_s * 1000)))
        if self.WAIT_OBJECT_0 <= result < self.WAIT_OBJECT_0 + len(handles):
            return result - self.WAIT_OBJECT_0
        if result == self.WAIT_TIMEOUT:
            return None
        raise OSError(f"WaitForMultipleObjects failed (error {self._ctypes.get_last_error()})")

    def close(self, handle: int) -> None:
        self._k.CloseHandle(handle)


def notify(hook: str, read: Callable[[int], bytes], environ: Mapping[str, str], *, events: object | None = None,
           settings: Callable[[], tuple[bool, str]] = alert_settings, alerts_dir: Path | None = None,
           exists: Callable[[str], bool] = os.path.lexists) -> str:
    """One hook run; returns a reason code (for tests only: the hook prints nothing).

    ``alerts_dir`` receives the project record (None: the alert carries no
    name). The codes: ``signalled`` (with the project's record),
    ``signalled_nameless`` (no project name), ``signalled_no_record`` (the
    record could not be written), ``not_running``, ``signal_failed``,
    ``ignored``, ``disabled``, ``filtered`` and ``unknown_hook``.
    """
    if hook not in HOOKS:
        return "unknown_hook"
    kind, cwd = parse_input(read_input(read), hook)
    if kind is None:
        return "ignored"
    enabled, mode = settings()
    if not enabled:
        return "disabled"
    if not session_allowed(environ, mode):
        return "filtered"
    if events is None:
        events = Events()
    handle = events.open_event(EVENT_NAMES[kind])
    if handle is None:
        return "not_running"  # nothing is written while Quill is not there to read it
    try:
        reason = "signalled_nameless"
        if alerts_dir is not None:
            try:
                project = project_name(cwd, exists)
            except Exception:  # noqa: BLE001 - the alert still rings, without a name
                project = None
            if project is not None:
                reason = "signalled" if write_record(alerts_dir, kind, project) else "signalled_no_record"
        return reason if events.signal(handle) else "signal_failed"
    finally:
        events.close(handle)


def _stdin_reader() -> Callable[[int], bytes]:
    def read(size: int) -> bytes:
        return os.read(0, min(size, 65536))
    return read


def main(argv: list[str] | None = None) -> int:
    """The hook entry point: always 0, nothing on stdout or stderr."""
    args = sys.argv[1:] if argv is None else argv
    try:
        notify(args[0] if len(args) == 1 else "", _stdin_reader(), os.environ, alerts_dir=ALERTS_DIR)
    except BaseException:  # noqa: BLE001 - never report to (or block) Claude Code
        pass
    return 0


# ---------------------------------------------------------------- app side


def parse_record(data: bytes | None, now: float) -> tuple[float, str, str] | None:
    """(time, kind, cleaned project) of one record read at wall time ``now``; None when
    it is oversized, malformed, of an unknown kind, stale or nameless."""
    if data is None or len(data) > MAX_RECORD:
        return None
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("kind") not in EVENT_NAMES:
        return None
    at = record.get("time")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None
    if now - at > MAX_AGE_S or at - now > MAX_AGE_S:
        return None
    project = clean_name(record.get("project"))
    if project is None:
        return None
    return float(at), record["kind"], project


class AlertListener:
    """Inside Quill: turns the named events and their records into ``on_alert(kind, project)``
    calls on its own thread (``project`` None: an alert without a name).

    ``start`` creates the records folder ``alerts_dir`` and deletes the
    records left in it (a folder that cannot be used only leaves the alerts
    nameless), then creates the events (a failure leaves the listener off and
    raises ``OSError``); ``stop`` ends the thread and closes them; ``start``
    may be called again. ``alerts_dir`` None: every alert is nameless. Only
    reason codes and counts are logged, never a project name.
    """

    def __init__(self, on_alert: Callable[[str, str | None], None], events: object | None = None,
                 poll_s: float = POLL_S, alerts_dir: Path | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.on_alert = on_alert
        self.events = events
        self.poll_s = poll_s
        self.alerts_dir = alerts_dir
        self.clock = clock
        self.wall = wall
        self._records = False
        self._named_at: dict[str, float] = {}
        self._stuck: set[str] = set()  # records that could not be deleted: never delivered twice
        self._handles: list[tuple[str, int]] = []
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("alert listener already started")
        if self.events is None:
            self.events = Events()
        # Before the events exist no hook writes a record: whatever is in the folder is left over.
        self._records = self._prepare()
        self._named_at = {}
        handles: list[tuple[str, int]] = []
        try:
            for kind, name in EVENT_NAMES.items():
                handles.append((kind, self.events.create_event(name)))
        except BaseException:
            for _, handle in handles:
                self.events.close(handle)
            raise
        self._handles = handles
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="quill-alerts", daemon=True)
        self._thread.start()

    def _prepare(self) -> bool:
        """Create the records folder and delete the records left in it; False: nameless alerts only."""
        if self.alerts_dir is None:
            return False
        removed = 0
        try:
            self.alerts_dir.mkdir(parents=True, exist_ok=True)
            with os.scandir(self.alerts_dir) as entries:
                leftovers = [entry.path for entry in entries
                             if _record_file(entry.name, RECORD_SUFFIX) or _record_file(entry.name, TEMP_SUFFIX)]
        except OSError as exc:
            log.error("alert records off (%s)", type(exc).__name__)
            return False
        for path in leftovers:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        self._stuck = set()
        if removed:
            log.info("deleted %d stale alert records", removed)
        return True

    def _run(self) -> None:
        handles = [handle for _, handle in self._handles]
        kinds = [kind for kind, _ in self._handles]
        while not self._stopping.is_set():
            try:
                index = self.events.wait_any(handles, self.poll_s)
            except OSError as exc:
                log.error("alert listener stopped (%s)", type(exc).__name__)
                return
            if index is None or self._stopping.is_set():
                continue
            try:
                self._wake(kinds[index])
            except Exception as exc:  # noqa: BLE001 - one failed wake never stops the listener
                log.error("alert failed (%s)", type(exc).__name__)

    def _wake(self, kind: str) -> None:
        """Deliver every pending record, then the nameless alert of ``kind`` when no record carried it."""
        named = self._read_records()
        now = self.clock()
        for record_kind, project in named:
            self._named_at[record_kind] = now
            self._deliver(record_kind, project)
        if any(record_kind == kind for record_kind, _ in named):
            return
        last = self._named_at.get(kind)
        if last is not None and now - last <= NAMED_GRACE_S:
            return  # the set of a record an earlier wake already delivered
        self._deliver(kind, None)

    def _deliver(self, kind: str, project: str | None) -> None:
        try:
            self.on_alert(kind, project)
        except Exception as exc:  # noqa: BLE001 - one failed alert never stops the others
            log.error("alert failed (%s)", type(exc).__name__)

    def _read_records(self) -> list[tuple[str, str]]:
        """Read and delete every pending record: the distinct (kind, project), oldest first."""
        if not self._records or self.alerts_dir is None:
            return []
        try:
            with os.scandir(self.alerts_dir) as entries:
                pending = list(itertools.islice(entries, MAX_READ))
        except OSError as exc:
            log.error("alert records unreadable (%s)", type(exc).__name__)
            return []
        now = self.wall()
        found: list[tuple[float, str, str, str]] = []
        dropped = 0
        for entry in pending:
            if _record_file(entry.name, TEMP_SUFFIX):
                _drop_stale_temporary(entry, now)
                continue
            if not _record_file(entry.name, RECORD_SUFFIX) or entry.name in self._stuck:
                continue
            record = parse_record(self._take(entry), now)
            if record is None:
                dropped += 1
                continue
            found.append((record[0], entry.name, record[1], record[2]))
        if dropped:
            log.info("dropped %d alert records", dropped)
        found.sort()
        distinct: list[tuple[str, str]] = []
        for _, _, kind, project in found:
            if (kind, project) not in distinct:
                distinct.append((kind, project))
        return distinct

    def _take(self, entry: os.DirEntry) -> bytes | None:
        """The first bytes of a record file (one more than a record may hold); the file is then deleted."""
        try:
            if not entry.is_file(follow_symlinks=False):
                self._stuck.add(entry.name)
                return None
            data: bytes | None = _read_record(entry.path)
        except OSError:
            data = None
        try:
            os.remove(entry.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # A record that stays would ring again at every wake: it is remembered and never read again.
            log.error("alert record not deleted (%s)", type(exc).__name__)
            if len(self._stuck) >= MAX_READ:
                return None
            self._stuck.add(entry.name)
        return data

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout_s)
            if thread.is_alive():
                log.error("alert listener did not stop in time")
        handles, self._handles = self._handles, []
        for _, handle in handles:
            self.events.close(handle)


def _read_record(path: str) -> bytes:
    """The first bytes of a record; an open refused while another handle holds the fresh file is tried again."""
    for _ in range(OPEN_TRIES - 1):
        try:
            with open(path, "rb") as handle:
                return handle.read(MAX_RECORD + 1)
        except PermissionError:
            time.sleep(OPEN_RETRY_S)
    with open(path, "rb") as handle:
        return handle.read(MAX_RECORD + 1)


def _record_file(name: str, suffix: str) -> bool:
    return name.startswith(RECORD_PREFIX) and name.endswith(suffix)


def _drop_stale_temporary(entry: os.DirEntry, now: float) -> None:
    """A temporary file older than ``MAX_AGE_S`` belongs to a hook run that never finished it."""
    try:
        if entry.is_file(follow_symlinks=False) and now - entry.stat(follow_symlinks=False).st_mtime > MAX_AGE_S:
            os.remove(entry.path)
    except OSError:
        pass


if __name__ == "__main__":
    os._exit(main())  # a stdin reader still blocked cannot delay the exit
