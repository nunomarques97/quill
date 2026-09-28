"""Fakes for product tests: a Win32 layer that records instead of acting."""

from __future__ import annotations

from collections.abc import Callable

from quill.inject import Injector, Target
from quill.win32 import INTEGRITY_MEDIUM, VK_RETURN, VK_SHIFT, KeyEvent

TARGET = Target(hwnd=100, pid=7)
OTHER_HWND = 200
OTHER_PID = 8


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

    # input
    def send_input(self, events: list[KeyEvent]) -> int:
        self.calls.append(list(events))
        self.after_send(len(self.calls) - 1)
        return max(0, len(events) - self.short_by)

    def last_error(self) -> int:
        return 5

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
        """Text a window gets from the recorded events (Shift+Enter as a newline)."""
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
