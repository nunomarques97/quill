"""Manual-edit detection: mirror the text Quill just typed from the user's keys.

After a dictation is typed, ``EditTracker`` keeps an in-memory copy of that
span and the caret, which starts at its end, and replays the user's keys on
it for ``window_s`` seconds, in the same target window:

- characters are inserted at the caret; Backspace and Delete remove one
  character; Left and Right move the caret;
- Home and End move to a line break inside the span;
- Ctrl+Backspace removes the word before the caret when that word is
  letters or digits preceded by a space inside the span (editors differ on
  punctuation, so anything else is not guessed);
- Enter ends the tracking and keeps the edits made before it.

When in doubt it abandons instead of guessing: a mouse click, a paste, an
undo, a focus change, input injected by another program, an unknown key or
shortcut, a selection (Shift with a navigation key), or a key that would act
outside the known span (the caret leaving it, Backspace at its start, Delete
at its end) stops the tracking and nothing is learned. At the end of the
window (or on Enter, or when the next dictation starts) the edited span is
compared with the typed one and ``quill.corrections`` derives the
replacements; only those are kept.

Keystrokes are never logged or stored: the mirror lives in memory only,
its fields are hidden from ``repr`` and logs name reasons and counts.

``KeyTranslator`` turns hook events (``quill.triggers.InputEvent``) into
edit keys. Characters come from the keyboard layout of the foreground window
(``Win32Layout``: ToUnicodeEx with the flag that leaves the keyboard state
untouched, so typing in the target is never disturbed). Dead keys (the
Portuguese accents ´ ` ~ ^ ¨) are composed here.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from quill.corrections import Dictation, Learner
from quill.triggers import BUTTON, InputEvent
from quill.win32 import QUILL_EXTRA_INFO

log = logging.getLogger("quill.edits")

# Edit keys.
CHAR = "char"
BACKSPACE = "backspace"
DELETE = "delete"
LEFT = "left"
RIGHT = "right"
HOME = "home"
END = "end"
CTRL_BACKSPACE = "ctrl_backspace"
ENTER = "enter"
# Keys and events that abandon the tracking.
CLICK = "click"
PASTE = "paste"
UNDO = "undo"
FOCUS = "focus"
INJECTED = "injected"
UNKNOWN = "unknown_key"
OUTSIDE = "outside_span"
TOO_LONG = "too_long"
ABANDON = frozenset({CLICK, PASTE, UNDO, FOCUS, INJECTED, UNKNOWN})

DEFAULT_WINDOW_S = 30.0
# The mirror may grow to this many characters beyond the typed text.
MAX_GROWTH = 400

VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_CAPITAL = 0x14
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_LBUTTON, VK_RBUTTON, VK_MBUTTON = 0x01, 0x02, 0x04
SHIFT_KEYS = frozenset({0x10, 0xA0, 0xA1})
CTRL_KEYS = frozenset({0x11, 0xA2, 0xA3})
LEFT_ALT_KEYS = frozenset({0x12, 0xA4})
RIGHT_ALT = 0xA5
WIN_KEYS = frozenset({0x5B, 0x5C})
MODIFIER_KEYS = SHIFT_KEYS | CTRL_KEYS | LEFT_ALT_KEYS | WIN_KEYS | {RIGHT_ALT}
NAVIGATION = {VK_LEFT: LEFT, VK_RIGHT: RIGHT, VK_HOME: HOME, VK_END: END}
CTRL_SHORTCUTS = {ord("V"): PASTE, ord("Z"): UNDO, ord("Y"): UNDO}

# Spacing accent of a dead key -> combining mark.
DEAD_KEYS = {"´": "́", "`": "̀", "~": "̃", "^": "̂", "¨": "̈", "'": "́", '"': "̈"}


@dataclass(frozen=True)
class EditKey:
    kind: str
    text: str = field(default="", repr=False)


@dataclass(frozen=True)
class EditOutcome:
    """A finished tracking with changes: the dictation and its edited text (never logged)."""

    dictation: Dictation
    edited: str = field(repr=False)


def _word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


class EditTracker:
    """Mirror of the last typed span; thread-safe (hook worker and session thread)."""

    def __init__(self, window_s: float = DEFAULT_WINDOW_S) -> None:
        self.window_s = window_s
        self.last_reason = ""
        self._lock = threading.Lock()
        self._dictation: Dictation | None = None
        self._text: list[str] = []
        self._caret = 0
        self._started = 0.0

    @property
    def tracking(self) -> bool:
        return self._dictation is not None

    def start(self, dictation: Dictation, now: float) -> EditOutcome | None:
        """Follow a newly typed text; the previous one is finished first (and returned)."""
        with self._lock:
            previous = self._finish("next dictation")
            self._dictation = dictation
            self._text = list(dictation.text)
            self._caret = len(self._text)
            self._started = now
            self.last_reason = ""
            return previous

    def finish(self, reason: str = "finished") -> EditOutcome | None:
        with self._lock:
            return self._finish(reason)

    def poll(self, now: float) -> EditOutcome | None:
        """Finish when the window is over."""
        with self._lock:
            if self._dictation is not None and now - self._started >= self.window_s:
                return self._finish("window over")
            return None

    def abandon(self, reason: str) -> None:
        with self._lock:
            self._abandon(reason)

    def key(self, key: EditKey, foreground: int, now: float) -> EditOutcome | None:
        """Replay one key; returns the outcome when this key ends the tracking with changes."""
        with self._lock:
            dictation = self._dictation
            if dictation is None:
                return None
            if now - self._started >= self.window_s:
                return self._finish("window over")  # this key came too late: not mirrored
            if foreground != dictation.target:
                self._abandon(FOCUS)
                return None
            if key.kind == ENTER:
                return self._finish("enter")
            if key.kind in ABANDON:
                self._abandon(key.kind)
                return None
            problem = self._apply(key)
            if problem:
                self._abandon(problem)
            elif len(self._text) > len(dictation.text) + MAX_GROWTH:
                self._abandon(TOO_LONG)
            return None

    # -- internals (lock held)

    def _apply(self, key: EditKey) -> str:
        text, caret = self._text, self._caret
        if key.kind == CHAR:
            if not key.text:
                return UNKNOWN
            text[caret:caret] = list(key.text)
            self._caret += len(key.text)
        elif key.kind == BACKSPACE:
            if caret == 0:
                return OUTSIDE
            del text[caret - 1]
            self._caret -= 1
        elif key.kind == DELETE:
            if caret == len(text):
                return OUTSIDE
            del text[caret]
        elif key.kind == LEFT:
            if caret == 0:
                return OUTSIDE
            self._caret -= 1
        elif key.kind == RIGHT:
            if caret == len(text):
                return OUTSIDE
            self._caret += 1
        elif key.kind == HOME:
            before = "".join(text[:caret]).rfind("\n")
            if before < 0:
                return OUTSIDE  # the line starts before the span: position unknown
            self._caret = before + 1
        elif key.kind == END:
            after = "".join(text[caret:]).find("\n")
            if after < 0:
                return OUTSIDE  # the line may go on after the span
            self._caret = caret + after
        elif key.kind == CTRL_BACKSPACE:
            end = caret
            while end > 0 and text[end - 1] == " ":
                end -= 1
            start = end
            while start > 0 and _word_char(text[start - 1]):
                start -= 1
            if start == end or start == 0 or text[start - 1] != " ":
                return OUTSIDE  # punctuation, or a word that may begin before the span
            del text[start:caret]
            self._caret = start
        else:
            return UNKNOWN
        return ""

    def _abandon(self, reason: str) -> None:
        if self._dictation is not None:
            log.info("manual-edit tracking abandoned: %s", reason)
        self._dictation = None
        self._text = []
        self.last_reason = reason

    def _finish(self, reason: str) -> EditOutcome | None:
        dictation = self._dictation
        if dictation is None:
            return None
        edited = "".join(self._text)
        self._dictation = None
        self._text = []
        self.last_reason = reason
        if edited == dictation.text:
            return None
        log.info("manual-edit tracking finished (%s) with changes", reason)
        return EditOutcome(dictation, edited)


# ---------------------------------------------------------------- key translation


class Layout(Protocol):
    def to_unicode(self, vk: int, shift: bool, altgr: bool, caps: bool) -> tuple[str, bool]:
        """(text, is a dead key) of a key press in the foreground window's layout."""

    def caps_lock(self) -> bool: ...


def compose(dead: str, text: str) -> str:
    """What Windows types for a dead key followed by ``text``."""
    if text == " ":
        return dead
    mark = DEAD_KEYS.get(dead)
    if mark is not None and len(text) == 1:
        composed = unicodedata.normalize("NFC", text + mark)
        if len(composed) == 1:
            return composed
    return dead + text


class KeyTranslator:
    """Hook events -> edit keys; tracks the modifiers it sees. Not thread-safe: one worker thread.

    ``ignore`` holds the virtual keys of Quill's own triggers and correction
    key: the session handles them, so they neither edit nor abandon.
    """

    def __init__(self, layout: Layout, ignore: frozenset[int] = frozenset()) -> None:
        self.layout = layout
        self.ignore = frozenset(ignore) - MODIFIER_KEYS
        self._down: set[int] = set()
        self._dead = ""

    def reset(self) -> None:
        self._down.clear()
        self._dead = ""

    def translate(self, event: InputEvent) -> EditKey | None:
        """The edit key for ``event``; None when it changes nothing (key up, a modifier, Quill's own input)."""
        if event.extra_info == QUILL_EXTRA_INFO:
            return None  # Quill's own typing, click or copy
        if event.kind == BUTTON:
            if event.down and event.vk in (VK_LBUTTON, VK_RBUTTON, VK_MBUTTON):
                return EditKey(CLICK)
            return None  # the side buttons are triggers; the session handles them
        vk = event.vk
        if event.injected:
            return EditKey(INJECTED) if event.down else None
        if vk in MODIFIER_KEYS:
            (self._down.add if event.down else self._down.discard)(vk)
            return None
        if not event.down or vk in self.ignore:
            return None
        key = self._key(vk)
        if key is not None and key.kind != CHAR:
            self._dead = ""
        return key

    def _key(self, vk: int) -> EditKey | None:
        down = self._down
        shift = bool(down & SHIFT_KEYS)
        altgr = RIGHT_ALT in down or bool(down & CTRL_KEYS and down & LEFT_ALT_KEYS)
        ctrl = bool(down & CTRL_KEYS) and not altgr
        if down & WIN_KEYS or (down & LEFT_ALT_KEYS and not altgr):
            return EditKey(UNKNOWN)
        if ctrl:
            if vk == VK_BACK and not shift:
                return EditKey(CTRL_BACKSPACE)
            return EditKey(CTRL_SHORTCUTS.get(vk, UNKNOWN))
        if vk == VK_INSERT:
            return EditKey(PASTE if shift else UNKNOWN)
        if vk in NAVIGATION:
            return EditKey(UNKNOWN if shift else NAVIGATION[vk])  # Shift selects: not followed
        if vk in (VK_UP, VK_DOWN, VK_TAB, VK_CAPITAL):
            return EditKey(UNKNOWN)
        if vk == VK_BACK:
            return EditKey(BACKSPACE)
        if vk == VK_DELETE:
            return EditKey(DELETE)
        if vk == VK_RETURN:
            return EditKey(ENTER)
        try:
            text, dead = self.layout.to_unicode(vk, shift, altgr, self.layout.caps_lock())
        except OSError:
            return EditKey(UNKNOWN)
        if dead:
            if self._dead or text not in DEAD_KEYS:
                self._dead = ""
                return EditKey(UNKNOWN)
            self._dead = text
            return None  # nothing typed until the next key
        if not text or any(unicodedata.category(ch) in ("Cc", "Cf") for ch in text):
            self._dead = ""
            return EditKey(UNKNOWN)
        if self._dead:
            text, self._dead = compose(self._dead, text), ""
        return EditKey(CHAR, text)


class Win32Layout:
    """The foreground window's keyboard layout through ToUnicodeEx (keyboard state untouched)."""

    DONT_CHANGE_STATE = 0x4

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Win32Layout needs Windows")
        import ctypes
        from ctypes import wintypes

        from quill.win32 import bind

        self._ctypes = ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        bind(user32, "ToUnicodeEx", ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
             wintypes.LPWSTR, ctypes.c_int, wintypes.UINT, wintypes.HKL)
        bind(user32, "GetKeyboardLayout", wintypes.HKL, wintypes.DWORD)
        bind(user32, "GetForegroundWindow", wintypes.HWND)
        bind(user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, ctypes.c_void_p)
        bind(user32, "MapVirtualKeyExW", wintypes.UINT, wintypes.UINT, wintypes.UINT, wintypes.HKL)
        bind(user32, "GetKeyState", ctypes.c_short, ctypes.c_int)
        self._user32 = user32

    def _layout(self) -> int:
        thread = self._user32.GetWindowThreadProcessId(self._user32.GetForegroundWindow(), None)
        return self._user32.GetKeyboardLayout(thread)

    def caps_lock(self) -> bool:
        return bool(self._user32.GetKeyState(VK_CAPITAL) & 1)

    def to_unicode(self, vk: int, shift: bool, altgr: bool, caps: bool) -> tuple[str, bool]:
        ctypes = self._ctypes
        state = (ctypes.c_ubyte * 256)()
        if shift:
            state[0x10] = state[0xA0] = 0x80
        if altgr:
            state[0x11] = state[0xA2] = state[0x12] = state[0xA5] = 0x80
        if caps:
            state[VK_CAPITAL] = 0x01
        layout = self._layout()
        scan = self._user32.MapVirtualKeyExW(vk, 0, layout)
        buffer = ctypes.create_unicode_buffer(8)
        count = self._user32.ToUnicodeEx(vk, scan, state, buffer, len(buffer), self.DONT_CHANGE_STATE, layout)
        if count < 0:
            return buffer.value[:1], True
        return buffer.value[:count], False


# ---------------------------------------------------------------- product wiring


class ManualEdits:
    """Wires hook events, the tracker and the learner; the app calls these from its threads."""

    def __init__(self, learner: Learner, translator: KeyTranslator, tracker: EditTracker,
                 foreground: Callable[[], int], clock: Callable[[], float] = time.monotonic) -> None:
        self.learner = learner
        self.translator = translator
        self.tracker = tracker
        self.foreground = foreground
        self.clock = clock

    def typed(self, dictation: Dictation) -> None:
        """A dictation was typed: learn from the previous one's edits and follow this one."""
        self._learn(self.tracker.start(dictation, self.clock()))

    def on_input(self, event: InputEvent) -> None:
        key = self.translator.translate(event)
        if key is None or not self.tracker.tracking:
            return
        self._learn(self.tracker.key(key, self.foreground(), self.clock()))

    def poll(self) -> None:
        self._learn(self.tracker.poll(self.clock()))

    def stop(self) -> None:
        """Before the next dictation or at shutdown: keep what was edited so far."""
        self._learn(self.tracker.finish("stopped"))

    def _learn(self, outcome: EditOutcome | None) -> None:
        if outcome is not None:
            self.learner.learn(outcome.dictation, outcome.edited, partial=False, source="manual edit")
