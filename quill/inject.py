"""Type text at the cursor of the window that was focused at key press.

Text is typed as Unicode keyboard events (SendInput with KEYEVENTF_UNICODE),
so the clipboard is never used and the keyboard layout does not matter. The
injector:

- never sends a bare Enter: a line break becomes Shift+Enter or a space,
  according to the per-target ``newline`` option (``space`` by default,
  because many terminals send Shift+Enter as Enter);
- drops control characters (and bidirectional overrides), so dictated text
  can never act as a shortcut, an escape sequence or a signal, nor reorder
  what is displayed;
- types in bursts; before each one it waits for Ctrl, Alt and Windows keys
  to be released and then checks that the target still exists, belongs to
  the same process, responds and is the foreground window;
- refuses a target running at a higher integrity level (elevated), where
  Windows would silently discard the input, and reports a short SendInput
  count;
- reports every failure with a reason code. Results and logs never contain
  the text itself.

Enter is only ever pressed by ``press_enter``, a separate call used by the
send-to-Claude trigger after a successful injection; ``inject`` never calls it.
``erase`` presses Backspace a given number of times with the same checks; the
undo key of the automatic rewrite uses it to remove the rewrite it typed.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from quill.win32 import (
    INTEGRITY_HIGH,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    SCAN_RETURN,
    SCAN_SHIFT,
    SHORTCUT_MODIFIERS,
    VK_RETURN,
    VK_SHIFT,
    KeyEvent,
)

VK_BACK = 0x08
SCAN_BACK = 0x0E

# Modifiers that must be up for press_enter: Shift+Enter is a line break and
# Ctrl/Alt/Windows+Enter are shortcuts, never the plain Enter that sends.
ENTER_MODIFIERS = (VK_SHIFT, *SHORTCUT_MODIFIERS)

log = logging.getLogger("quill.inject")

NEWLINE_SPACE = "space"
NEWLINE_SHIFT_ENTER = "shift_enter"
NEWLINE_POLICIES = (NEWLINE_SPACE, NEWLINE_SHIFT_ENTER)

# Reason codes of an InjectResult.
OK = "ok"
NO_TARGET = "no_target"
TARGET_GONE = "target_gone"
FOREGROUND_CHANGED = "foreground_changed"
TARGET_ELEVATED = "target_elevated"
TARGET_ACCESS_DENIED = "target_access_denied"
TARGET_NOT_RESPONDING = "target_not_responding"
MODIFIER_HELD = "modifier_held"
SENDINPUT_FAILED = "sendinput_failed"

NEWLINE = "\n"
_LINE_BREAKS = re.compile(r"\r\n|[\r\n\v\f\u0085  ]")
_SPACED_BREAKS = re.compile(r" *\n[\n ]*")


def _is_dropped(code: int) -> bool:
    """Control characters (C0, DEL, C1), bidirectional overrides and lone surrogates are never typed."""
    return (code < 0x20 or 0x7F <= code <= 0x9F or 0xD800 <= code <= 0xDFFF
            or 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069)


def normalize_text(text: str, newline: str = NEWLINE_SPACE) -> str:
    """Text as it will be typed: line breaks unified, tabs as spaces, controls dropped.

    With the ``space`` policy every run of line breaks, with the spaces around
    it, becomes one space. With ``shift_enter`` each line break stays ``\\n``
    and is typed as Shift+Enter.
    """
    if newline not in NEWLINE_POLICIES:
        raise ValueError(f"unknown newline policy: {newline!r}")
    text = _LINE_BREAKS.sub(NEWLINE, text).replace("\t", " ")
    text = "".join(ch for ch in text if ch == NEWLINE or not _is_dropped(ord(ch)))
    if newline == NEWLINE_SPACE:
        text = _SPACED_BREAKS.sub(" ", text)
    return text


def utf16_units(ch: str) -> list[int]:
    """UTF-16 code units of one code point (a surrogate pair above U+FFFF)."""
    code = ord(ch)
    if code <= 0xFFFF:
        return [code]
    code -= 0x10000
    return [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]


def events_for(ch: str) -> list[KeyEvent]:
    """Keyboard events that type one normalized character."""
    if ch == NEWLINE:
        return [
            KeyEvent(VK_SHIFT, SCAN_SHIFT, 0),
            KeyEvent(VK_RETURN, SCAN_RETURN, 0),
            KeyEvent(VK_RETURN, SCAN_RETURN, KEYEVENTF_KEYUP),
            KeyEvent(VK_SHIFT, SCAN_SHIFT, KEYEVENTF_KEYUP),
        ]
    events: list[KeyEvent] = []
    for unit in utf16_units(ch):
        events.append(KeyEvent(0, unit, KEYEVENTF_UNICODE))
        events.append(KeyEvent(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def bursts(text: str, chunk_chars: int) -> Iterator[tuple[int, list[KeyEvent]]]:
    """Split normalized text into bursts of at most ``chunk_chars`` characters.

    Yields (characters in the burst, events). A character is never split, so
    a surrogate pair always travels in one SendInput call.
    """
    if chunk_chars < 1:
        raise ValueError("chunk_chars must be at least 1")
    for start in range(0, len(text), chunk_chars):
        piece = text[start:start + chunk_chars]
        yield len(piece), [event for ch in piece for event in events_for(ch)]


@dataclass(frozen=True)
class Target:
    """The window that was in the foreground when the dictation key went down."""

    hwnd: int
    pid: int


@dataclass(frozen=True)
class InjectOptions:
    newline: str = NEWLINE_SPACE
    chunk_chars: int = 32
    burst_pause_s: float = 0.004
    modifier_wait_s: float = 1.0

    def __post_init__(self) -> None:
        if self.newline not in NEWLINE_POLICIES:
            raise ValueError(f"unknown newline policy: {self.newline!r}")
        if self.chunk_chars < 1:
            raise ValueError("chunk_chars must be at least 1")


@dataclass(frozen=True)
class InjectResult:
    """Outcome of one injection. ``typed`` counts characters fully sent."""

    reason: str
    typed: int
    total: int
    detail: str = ""
    clipboard_changed: bool = False

    @property
    def ok(self) -> bool:
        return self.reason == OK


@dataclass
class Injector:
    """Types text into a captured target through a Win32 layer (real or fake)."""

    api: object
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _own_integrity: int | None = field(default=None, init=False, repr=False)

    def capture_target(self) -> Target | None:
        """The current foreground window; call it when the dictation key goes down."""
        hwnd = self.api.foreground_window()
        if not hwnd:
            return None
        return Target(hwnd, self.api.window_process_id(hwnd))

    # ------------------------------------------------------------ checks

    def _integrity_problem(self, target: Target) -> tuple[str, str] | None:
        if self._own_integrity is None:
            self._own_integrity = self.api.own_integrity()
        own = self._own_integrity
        theirs = self.api.process_integrity(target.pid)
        if own is None:
            return TARGET_ACCESS_DENIED, "own process integrity cannot be read"
        if theirs is None:
            # An elevated Quill can type anywhere a readable level would allow; otherwise refuse.
            if own >= INTEGRITY_HIGH:
                return None
            return TARGET_ACCESS_DENIED, "target process integrity cannot be read"
        if theirs > own:
            return TARGET_ELEVATED, f"target integrity {theirs:#x} is above own {own:#x}"
        return None

    def _target_problem(self, target: Target) -> tuple[str, str] | None:
        if not self.api.is_window(target.hwnd):
            return TARGET_GONE, "target window no longer exists"
        if self.api.window_process_id(target.hwnd) != target.pid:
            return TARGET_GONE, "target window handle now belongs to another process"
        if self.api.foreground_window() != target.hwnd:
            return FOREGROUND_CHANGED, "foreground window differs from the target captured at key press"
        if self.api.is_hung(target.hwnd):
            return TARGET_NOT_RESPONDING, "target window is not responding"
        return None

    def _preflight(self, target: Target | None) -> tuple[str, str] | None:
        """Problems that refuse a target before anything is sent."""
        if target is None or not target.hwnd:
            return NO_TARGET, "no foreground window at key press"
        return self._target_problem(target) or self._integrity_problem(target)

    def check(self, target: Target | None) -> tuple[str, str] | None:
        """(reason, detail) when ``target`` would be refused now; None when it may receive input."""
        return self._preflight(target)

    def _wait_modifiers(self, wait_s: float) -> bool:
        deadline = self.clock() + wait_s
        while any(self.api.key_down(vk) for vk in SHORTCUT_MODIFIERS):
            if self.clock() >= deadline:
                return False
            self.sleep(0.01)
        return True

    # ------------------------------------------------------------ inject

    def inject(self, text: str, target: Target | None, options: InjectOptions | None = None) -> InjectResult:
        """Type ``text`` into ``target``; never presses a bare Enter."""
        options = options or InjectOptions()
        normalized = normalize_text(text, options.newline)
        total = len(normalized)
        problem = self._preflight(target)
        if problem is not None:
            return self._fail(problem[0], 0, total, problem[1])
        if not normalized:
            return InjectResult(OK, 0, 0)

        sequence = self.api.clipboard_sequence()
        typed = 0
        for index, (count, events) in enumerate(bursts(normalized, options.chunk_chars)):
            if index:
                self.sleep(options.burst_pause_s)
            if not self._wait_modifiers(options.modifier_wait_s):
                return self._fail(MODIFIER_HELD, typed, total, "Ctrl, Alt or Windows key held", sequence)
            # Checked after the modifier wait, right before sending: the wait can take a while.
            problem = self._target_problem(target)
            if problem is not None:
                return self._fail(problem[0], typed, total, problem[1], sequence)
            sent = self.api.send_input(events)
            if sent != len(events):
                error = self.api.last_error()
                return self._fail(
                    SENDINPUT_FAILED, typed, total,
                    f"SendInput inserted {sent} of {len(events)} events (error {error})", sequence,
                )
            typed += count
        changed = self.api.clipboard_sequence() != sequence
        if changed:
            log.warning("clipboard changed while typing (not by Quill)")
        log.info("typed %d characters", typed)
        return InjectResult(OK, typed, total, clipboard_changed=changed)

    def erase(self, count: int, target: Target | None, options: InjectOptions | None = None) -> InjectResult:
        """Press Backspace ``count`` times in ``target``, in bursts checked like ``inject``.

        ``typed`` counts the Backspaces fully sent, so a caller knows how much
        was removed when it stops half way.
        """
        if count < 0:
            raise ValueError("count must not be negative")
        options = options or InjectOptions()
        problem = self._preflight(target)
        if problem is not None:
            return self._fail(problem[0], 0, count, problem[1])
        press = [KeyEvent(VK_BACK, SCAN_BACK, 0), KeyEvent(VK_BACK, SCAN_BACK, KEYEVENTF_KEYUP)]
        erased = 0
        while erased < count:
            if erased:
                self.sleep(options.burst_pause_s)
            if not self._wait_modifiers(options.modifier_wait_s):
                return self._fail(MODIFIER_HELD, erased, count, "Ctrl, Alt or Windows key held")
            problem = self._target_problem(target)
            if problem is not None:
                return self._fail(problem[0], erased, count, problem[1])
            burst = min(options.chunk_chars, count - erased)
            events = press * burst
            sent = self.api.send_input(events)
            if sent != len(events):
                detail = f"SendInput inserted {sent} of {len(events)} events (error {self.api.last_error()})"
                return self._fail(SENDINPUT_FAILED, erased + sent // 2, count, detail)
            erased += burst
        log.info("erased %d characters", erased)
        return InjectResult(OK, erased, count)

    def press_enter(self, target: Target | None) -> InjectResult:
        """Press one plain Enter in ``target`` (the send-to-Claude triggers only).

        Refused, without waiting, when the target is gone, reused, hung,
        elevated or no longer the foreground window, or when Shift, Ctrl, Alt
        or a Windows key is held.
        """
        problem = self._preflight(target)
        if problem is None and any(self.api.key_down(vk) for vk in ENTER_MODIFIERS):
            problem = MODIFIER_HELD, "Shift, Ctrl, Alt or Windows key held"
        if problem is not None:
            return self._fail(problem[0], 0, 1, problem[1])
        events = [KeyEvent(VK_RETURN, SCAN_RETURN, 0), KeyEvent(VK_RETURN, SCAN_RETURN, KEYEVENTF_KEYUP)]
        sent = self.api.send_input(events)
        if sent != len(events):
            detail = f"SendInput inserted {sent} of {len(events)} events (error {self.api.last_error()})"
            return self._fail(SENDINPUT_FAILED, 0, 1, detail)
        log.info("pressed Enter")
        return InjectResult(OK, 1, 1)

    def _fail(self, reason: str, typed: int, total: int, detail: str, sequence: int | None = None) -> InjectResult:
        changed = sequence is not None and self.api.clipboard_sequence() != sequence
        log.warning("injection aborted: %s (%d of %d characters typed): %s", reason, typed, total, detail)
        return InjectResult(reason, typed, total, detail, changed)
