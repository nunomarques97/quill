"""Claude Code attention alerts: the hook-side notifier and the app-side listener.

Claude Code runs the notifier as a hook (installed only by
``python -m quill.claude_hooks --install``), in exec form without a shell:

    <repo>\\.venv\\Scripts\\pythonw.exe <repo>\\quill\\notify.py stop
    <repo>\\.venv\\Scripts\\pythonw.exe <repo>\\quill\\notify.py permission

``stop`` is the ``Stop`` hook (Claude Code finished its reply and waits for
the user); ``permission`` is the ``Notification`` hook with the matcher
``permission_prompt``. The notifier reads the hook's JSON from stdin (at most
``MAX_INPUT`` bytes, for at most ``READ_TIMEOUT_S``), keeps only the event
name, ``stop_hook_active`` and ``notification_type``, and never logs, prints
or stores anything else (the message and the assistant's text are dropped
unread). It ignores ``stop_hook_active`` true, any other notification type,
malformed, oversized or late input, and the sessions the filter rejects. It
then sets one named Windows event in the signed-in user's session
(``Local\\``): no network, no port. When Quill is not running the event does
not exist and nothing happens. It prints nothing, never blocks Claude Code
and always exits 0.

The headless filter (``[claude_alert] filter``) reads the environment
variable Claude Code gives every hook, ``CLAUDE_CODE_SESSION_ATTENDED``:
``1`` for a session someone is using (the terminal interface, the VS Code
panel), ``0`` for ``claude -p``, the Agent SDK and any Claude Code started by
another Claude Code (for example automated runs). This was read from the
installed Claude Code; its limits are in docs/USAR.md.

``AlertListener`` runs inside Quill: it creates the two events and calls
``on_alert(kind)`` from its own thread each time one is set (several sets
before it wakes count once).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
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
    if hook not in HOOKS or data is None:
        return None
    expected, kind = HOOKS[hook]
    try:
        event = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(event, dict) or event.get("hook_event_name") != expected:
        return None
    if kind == DONE and event.get("stop_hook_active", False) is not False:
        # Claude Code is continuing because a Stop hook asked it to: not the end of the reply.
        return None
    if kind == PERMISSION and event.get("notification_type") != PERMISSION_TYPE:
        return None
    return kind


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

    def set_event(self, name: str) -> bool:
        """Set an existing named event; False when it does not exist (Quill is not running)."""
        handle = self._k.OpenEventW(self.EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(self._k.SetEvent(handle))
        finally:
            self._k.CloseHandle(handle)

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
           settings: Callable[[], tuple[bool, str]] = alert_settings) -> str:
    """One hook run; returns a reason code (for tests only: the hook prints nothing)."""
    if hook not in HOOKS:
        return "unknown_hook"
    kind = parse_event(read_input(read), hook)
    if kind is None:
        return "ignored"
    enabled, mode = settings()
    if not enabled:
        return "disabled"
    if not session_allowed(environ, mode):
        return "filtered"
    if events is None:
        events = Events()
    return "signalled" if events.set_event(EVENT_NAMES[kind]) else "not_running"


def _stdin_reader() -> Callable[[int], bytes]:
    def read(size: int) -> bytes:
        return os.read(0, min(size, 65536))
    return read


def main(argv: list[str] | None = None) -> int:
    """The hook entry point: always 0, nothing on stdout or stderr."""
    args = sys.argv[1:] if argv is None else argv
    try:
        notify(args[0] if len(args) == 1 else "", _stdin_reader(), os.environ)
    except BaseException:  # noqa: BLE001 - never report to (or block) Claude Code
        pass
    return 0


# ---------------------------------------------------------------- app side


class AlertListener:
    """Inside Quill: turns the named events into ``on_alert(kind)`` calls on its own thread.

    ``start`` creates the events (a failure leaves the listener off and
    raises ``OSError``); ``stop`` ends the thread and closes them; ``start``
    may be called again.
    """

    def __init__(self, on_alert: Callable[[str], None], events: object | None = None, poll_s: float = POLL_S) -> None:
        self.on_alert = on_alert
        self.events = events
        self.poll_s = poll_s
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
                self.on_alert(kinds[index])
            except Exception as exc:  # noqa: BLE001 - one failed alert never stops the listener
                log.error("alert failed (%s)", type(exc).__name__)

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


if __name__ == "__main__":
    os._exit(main())  # a stdin reader still blocked cannot delay the exit
