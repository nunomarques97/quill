"""Fakes for product tests: a Win32 layer and a hook installer that record instead of acting."""

from __future__ import annotations

import threading
from collections.abc import Callable

from quill.inject import VK_BACK, Injector, Target
from quill.win32 import INTEGRITY_MEDIUM, VK_RETURN, VK_SHIFT, WH_KEYBOARD_LL, WH_MOUSE_LL, KeyEvent, MouseEvent

TARGET = Target(hwnd=100, pid=7)
OTHER_HWND = 200
OTHER_PID = 8
TARGET_CHILD = 101  # an edit control inside TARGET
QUILL_HWND = 300
QUILL_PID = 9


class FakeWin32:
    """Records SendInput calls and reconstructs the text a window would receive.

    Windows, processes, key states and the clipboard are plain attributes that
    tests change, also from the ``after_send`` hook that runs after each call.
    """

    def __init__(self) -> None:
        self.foreground = TARGET.hwnd
        self.windows = {TARGET.hwnd: TARGET.pid, OTHER_HWND: OTHER_PID}
        self.integrity: dict[int, int] = {TARGET.pid: INTEGRITY_MEDIUM, OTHER_PID: INTEGRITY_MEDIUM}
        self.own: int | None = INTEGRITY_MEDIUM
        self.hung: set[int] = set()
        self.keys_down: set[int] = set()
        self.calls: list[list[KeyEvent]] = []
        self.sequence = 5
        self.short_by = 0
        # Hook run after each SendInput call (index of the call).
        self.after_send: Callable[[int], None] = lambda index: None
        # Clipboard: list of (format, data); None data means a handle format.
        self.clip: list[tuple[int, bytes | None]] = []
        self.clip_open = False
        self.open_failures = 0
        self.refuse_set: set[int] = set()
        self.clipboard_writes = 0
        self.sequence_reads_while_closed = 0
        # Pointer and mouse input.
        self.windows[QUILL_HWND] = QUILL_PID
        self.own_pid = QUILL_PID
        self.cursor: tuple[int, int] | None = (640, 360)
        self.under_pointer = TARGET_CHILD
        self.roots: dict[int, int] = {TARGET_CHILD: TARGET.hwnd}
        self.swapped = False
        self.mouse_calls: list[list[MouseEvent]] = []
        self.mouse_short_by = 0
        # Hook run after each mouse SendInput call (index of the call).
        self.after_mouse: Callable[[int], None] = lambda index: None
        # Window descriptions for the active-window profiles.
        self.images: dict[int, str] = {}
        self.classes: dict[int, str] = {}
        self.titles: dict[int, str] = {}

    # input
    def send_input(self, events: list[KeyEvent]) -> int:
        self.calls.append(list(events))
        self.after_send(len(self.calls) - 1)
        return max(0, len(events) - self.short_by)

    def send_mouse(self, events: list[MouseEvent]) -> int:
        self.mouse_calls.append(list(events))
        self.after_mouse(len(self.mouse_calls) - 1)
        return max(0, len(events) - self.mouse_short_by)

    def last_error(self) -> int:
        return 5

    # pointer
    def cursor_pos(self) -> tuple[int, int] | None:
        return self.cursor

    def window_from_point(self, x: int, y: int) -> int:
        return self.under_pointer

    def root_window(self, hwnd: int) -> int:
        return self.roots.get(hwnd, hwnd if hwnd in self.windows else 0)

    def buttons_swapped(self) -> bool:
        return self.swapped

    def own_process_id(self) -> int:
        return self.own_pid

    def key_down(self, vk: int) -> bool:
        return vk in self.keys_down

    # windows
    def foreground_window(self) -> int:
        return self.foreground

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self.windows

    def is_hung(self, hwnd: int) -> bool:
        return hwnd in self.hung

    def window_process_id(self, hwnd: int) -> int:
        return self.windows.get(hwnd, 0)

    def own_integrity(self) -> int | None:
        return self.own

    def process_integrity(self, pid: int) -> int | None:
        return self.integrity.get(pid)

    def process_image(self, pid: int) -> str:
        if pid not in self.images:
            raise OSError("fake: process not readable")
        return self.images[pid]

    def window_class(self, hwnd: int) -> str:
        return self.classes.get(hwnd, "")

    def window_text(self, hwnd: int) -> str:
        return self.titles.get(hwnd, "")

    # clipboard
    def clipboard_sequence(self) -> int:
        if not self.clip_open:
            self.sequence_reads_while_closed += 1
        return self.sequence

    def open_clipboard(self) -> bool:
        if self.open_failures:
            self.open_failures -= 1
            return False
        assert not self.clip_open
        self.clip_open = True
        return True

    def close_clipboard(self) -> None:
        assert self.clip_open
        self.clip_open = False

    def clipboard_formats(self) -> list[int]:
        assert self.clip_open
        return [fmt for fmt, _ in self.clip]

    def clipboard_format_name(self, fmt: int) -> str:
        return f"Registered{fmt}"

    def clipboard_bytes(self, fmt: int) -> bytes | None:
        assert self.clip_open
        return dict(self.clip)[fmt]

    def empty_clipboard(self) -> bool:
        assert self.clip_open
        self.clip = []
        self.sequence += 1
        self.clipboard_writes += 1
        return True

    def set_clipboard_bytes(self, fmt: int, data: bytes) -> bool:
        assert self.clip_open
        if fmt in self.refuse_set:
            return False
        self.clip.append((fmt, data))
        self.sequence += 1
        self.clipboard_writes += 1
        return True

    # reconstruction
    @property
    def events(self) -> list[KeyEvent]:
        return [event for call in self.calls for event in call]

    def backspaces(self) -> int:
        return sum(1 for event in self.events if event.vk == VK_BACK and not event.is_keyup)

    def enter_presses(self) -> list[bool]:
        """For each Enter key-down sent: whether Shift was down at the time."""
        presses: list[bool] = []
        shift = False
        for event in self.events:
            if event.vk == VK_SHIFT:
                shift = not event.is_keyup
            elif event.vk == VK_RETURN and not event.is_keyup:
                presses.append(shift)
        return presses

    def received_text(self) -> str:
        """Text a window gets from the recorded events (Shift+Enter as a newline, Backspace removes one)."""
        units: list[int] = []
        shift = False
        for event in self.events:
            if event.is_unicode:
                if not event.is_keyup:
                    units.append(event.scan)
            elif event.vk == VK_SHIFT:
                shift = not event.is_keyup
            elif event.vk == VK_RETURN and not event.is_keyup:
                if not shift:
                    raise AssertionError("bare Enter sent")
                units.append(0x0A)
            elif event.vk == VK_BACK and not event.is_keyup:
                if not units:
                    raise AssertionError("Backspace with nothing typed")
                units.pop()
        return b"".join(unit.to_bytes(2, "little") for unit in units).decode("utf-16-le")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def injector(api: FakeWin32, clock: FakeClock | None = None) -> Injector:
    clock = clock or FakeClock()
    return Injector(api, sleep=clock.sleep, clock=clock)


class FakeHooks:
    """Hook installer that records hooks and lets tests call the callbacks directly.

    ``lparam`` values are keys into ``structs`` instead of memory addresses.
    ``run_loop`` blocks until ``post_quit``, like a message loop. ``tick`` is
    the fake GetTickCount; an event's ``time`` field defaults to it (age 0),
    and a test passes an older ``time`` to model a callback that runs late.
    """

    def __init__(self, fail_kind: int | None = None) -> None:
        self.fail_kind = fail_kind
        self.hooks: dict[int, Callable[[int, int, int], int]] = {}
        self.handles: dict[int, int] = {}
        self.unhooked: list[int] = []
        self.next_calls: list[tuple[int, int, int]] = []
        self.structs: dict[int, tuple[int, int, int, int]] = {}
        self.tick = 0
        self.loop_thread = 0
        self.quit = threading.Event()
        self._next = 1

    def make_callback(self, function: Callable[[int, int, int], int]) -> Callable[[int, int, int], int]:
        return function

    def ensure_queue(self) -> None:
        pass

    def current_thread_id(self) -> int:
        return threading.get_ident()

    def set_hook(self, kind: int, callback: Callable[[int, int, int], int]) -> int:
        if kind == self.fail_kind:
            raise OSError(f"fake SetWindowsHookExW({kind}) failed")
        self.hooks[kind] = callback
        handle = 1000 + kind
        self.handles[kind] = handle
        return handle

    def unhook(self, handle: int) -> None:
        self.unhooked.append(handle)

    def call_next(self, code: int, wparam: int, lparam: int) -> int:
        self.next_calls.append((code, wparam, lparam))
        return 0

    def run_loop(self) -> None:
        self.loop_thread = threading.get_ident()
        self.quit.wait()

    def post_quit(self, thread_id: int) -> bool:
        self.quit.set()
        return True

    def tick_count(self) -> int:
        return self.tick

    def keyboard_fields(self, lparam: int) -> tuple[int, int, int, int]:
        return self.structs[lparam]

    def mouse_fields(self, lparam: int) -> tuple[int, int, int, int]:
        return self.structs[lparam]

    # test helpers: return what the callback returned (1 = swallowed)
    def _call(self, kind: int, wparam: int, fields: tuple[int, int, int], time: int | None, code: int) -> int:
        lparam = self._next
        self._next += 1
        self.structs[lparam] = (*fields, self.tick if time is None else time)
        return self.hooks[kind](code, wparam, lparam)

    def key(self, wparam: int, vk: int, flags: int = 0, extra: int = 0, code: int = 0, time: int | None = None) -> int:
        return self._call(WH_KEYBOARD_LL, wparam, (vk, flags, extra), time, code)

    def mouse(self, wparam: int, mouse_data: int = 0, flags: int = 0, extra: int = 0, code: int = 0,
              time: int | None = None) -> int:
        return self._call(WH_MOUSE_LL, wparam, (mouse_data, flags, extra), time, code)


class FakeIndicator:
    """Records every indicator call as a tuple; ``states`` lists the shown states."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.lock = threading.Lock()
        self.starts = self.stops = 0
        self.fail = False  # every call raises (a broken overlay)

    def _record(self, *call: object) -> None:
        with self.lock:
            self.calls.append(call)
        if self.fail:
            raise OSError("fake indicator failure")

    def start(self) -> None:
        self.starts += 1
        self._record("start")

    def stop(self) -> None:
        self.stops += 1
        self._record("stop")

    def show(self, state: str, text: str = "", hide_after_s: float | None = None) -> None:
        self._record("show", state, text)

    def set_text(self, text: str) -> None:
        self._record("text", text)

    def set_level(self, level: float) -> None:
        self._record("level", level)

    def hide(self) -> None:
        self._record("hide")

    @property
    def states(self) -> list[str]:
        with self.lock:
            return [call[1] if call[0] == "show" else call[0] for call in self.calls
                    if call[0] in ("show", "hide")]

    @property
    def last(self) -> tuple:
        with self.lock:
            shown = [call for call in self.calls if call[0] in ("show", "hide")]
        return shown[-1] if shown else ()


class FakeCapture:
    """An MME capture that delivers the PCM a test pushes; never opens a device."""

    def __init__(self, owner: "FakeCaptures", on_data: Callable[[bytes], None]) -> None:
        self.owner = owner
        self.on_data = on_data
        self.started = self.stopped = False

    def start(self) -> None:
        if self.owner.fail_start:
            raise OSError("fake: microphone missing")
        self.started = True
        if self.owner.on_start is not None:
            self.owner.on_start(self)

    def push(self, pcm: bytes, chunk: int = 3200) -> None:
        for index in range(0, len(pcm), chunk):
            self.on_data(pcm[index : index + chunk])

    def stop(self) -> None:
        self.stopped = True
        if self.owner.fail_stop:
            raise OSError("fake: microphone unplugged")


class FakeCaptures:
    """The capture factory: one ``FakeCapture`` per press, kept in ``made``."""

    def __init__(self) -> None:
        self.made: list[FakeCapture] = []
        self.fail_start = False
        self.fail_stop = False
        self.on_start: Callable[[FakeCapture], None] | None = None

    def __call__(self, on_data: Callable[[bytes], None]) -> FakeCapture:
        capture = FakeCapture(self, on_data)
        self.made.append(capture)
        return capture

    @property
    def open(self) -> list[FakeCapture]:
        return [c for c in self.made if c.started and not c.stopped]


class FakeKernel:
    """Named mutexes and events of ``quill.app.InstanceLock``, shared by the locks given the same fake."""

    def __init__(self) -> None:
        self.objects: dict[int, str] = {}
        self.events: dict[str, threading.Event] = {}
        self.closed: list[int] = []
        self._next = 1

    def _open(self, name: str) -> int:
        handle = self._next
        self._next += 1
        self.objects[handle] = name
        return handle

    def create_mutex(self, name: str) -> tuple[int, bool]:
        existed = name in self.objects.values()
        return self._open(name), existed

    def create_event(self, name: str) -> int:
        self.events.setdefault(name, threading.Event())
        return self._open(name)

    def set_event(self, name: str) -> bool:
        if name not in self.objects.values():
            return False
        self.events[name].set()
        return True

    def wait(self, handle: int, timeout_s: float) -> bool:
        return self.events[self.objects[handle]].wait(timeout_s)

    def close(self, handle: int) -> None:
        self.closed.append(handle)
        name = self.objects.pop(handle)
        if name in self.events and name not in self.objects.values():
            del self.events[name]


class FakeRegistry:
    """The current user's Run key as a dict."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.writes: list[tuple[str, str]] = []
        self.fail: OSError | None = None

    def read(self, name: str) -> str | None:
        if self.fail:
            raise self.fail
        return self.values.get(name)

    def write(self, name: str, value: str) -> None:
        if self.fail:
            raise self.fail
        self.writes.append((name, value))
        self.values[name] = value

    def delete(self, name: str) -> bool:
        if self.fail:
            raise self.fail
        return self.values.pop(name, None) is not None
