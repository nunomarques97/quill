"""Click-to-focus: when a dictation hold counts, click where the pointer is.

On the trigger's ``confirm`` signal (the hold passed ``min_hold_ms``, so a
short tap never clicks) the dictation and send triggers (``send_polished``,
``send_raw`` and the older ``send_claude``) send exactly one primary-button
down/up at the current pointer position: two mouse INPUT records, tagged with Quill's marker, with no MOUSEEVENTF_MOVE or
MOUSEEVENTF_ABSOLUTE, so the pointer never moves. Then the target window is
captured, once the window under the pointer has become the foreground window.

No click is sent, and the foreground window is captured as it is, when:

- the action is ``command``: a click would undo the selection to rewrite;
- ``click_to_focus`` is off in the configuration;
- the trigger is a key that passes through to Windows as a modifier (right
  Ctrl, right Shift or right Alt): it is down while the hold is confirmed,
  so a click would become a shortcut (for example Ctrl+click).

No click is sent and no target is returned (so nothing is typed) when the
pointer is over one of Quill's own windows or over no window, when a mouse
button is already down (a drag would be broken), when another Ctrl, Shift,
Alt or Windows key is down (the click would become a shortcut, for example
Ctrl+click; side-specific key states are read so the trigger's own key never
counts), or when SendInput fails. After a click, a target that does not
become the foreground in time is not captured either: typing into the window
that had the focus before would put the text somewhere the user did not point.

Results carry reason codes only; the pointer position is never logged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from quill.config import BUTTONS, KEYS
from quill.inject import Target
from quill.triggers import PASS_THROUGH_KEYS
from quill.win32 import (
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    VK_LBUTTON,
    VK_LCONTROL,
    VK_LMENU,
    VK_LSHIFT,
    VK_LWIN,
    VK_MBUTTON,
    VK_RBUTTON,
    VK_RCONTROL,
    VK_RMENU,
    VK_RSHIFT,
    VK_RWIN,
    MouseEvent,
)

log = logging.getLogger("quill.focus")

CLICK_ACTIONS = ("dictation", "send_claude", "send_polished", "send_raw")
# Side-specific codes: a trigger on right Ctrl must not count as a held Ctrl.
HELD_MODIFIERS = (VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN)
# Trigger input names that pass through to Windows as modifiers.
MODIFIER_TRIGGERS = frozenset(name for name, vk in KEYS.items() if vk in PASS_THROUGH_KEYS)
# The buttons a click would conflict with. The bound trigger button itself is
# down (and suppressed) while this runs, so a middle-button trigger is not
# counted as a held button.
HELD_BUTTONS = (VK_LBUTTON, VK_RBUTTON, VK_MBUTTON)

# Reason codes.
CLICKED = "clicked"
COMMAND_NO_CLICK = "command_no_click"
DISABLED = "disabled"
MODIFIER_TRIGGER_NO_CLICK = "modifier_trigger_no_click"
OWN_WINDOW = "own_window"
NO_WINDOW = "no_window"
NO_POINTER = "no_pointer"
MODIFIER_HELD = "modifier_held"
BUTTON_HELD = "button_held"
SENDINPUT_FAILED = "sendinput_failed"
FOCUS_NOT_MOVED = "focus_not_moved"

SKIPPED_WITH_TARGET = (COMMAND_NO_CLICK, DISABLED, MODIFIER_TRIGGER_NO_CLICK)


@dataclass(frozen=True)
class FocusResult:
    """Outcome of the confirm step: the captured target (None: do not type) and why."""

    target: Target | None
    clicked: bool
    reason: str


def click_events(swapped: bool) -> list[MouseEvent]:
    """One primary-button down and up. With swapped buttons Windows maps the
    physical right button to the primary one, also for injected input."""
    if swapped:
        return [MouseEvent(MOUSEEVENTF_RIGHTDOWN), MouseEvent(MOUSEEVENTF_RIGHTUP)]
    return [MouseEvent(MOUSEEVENTF_LEFTDOWN), MouseEvent(MOUSEEVENTF_LEFTUP)]


class ClickToFocus:
    """Runs the confirm step through a Win32 layer (real ``User32`` or a fake)."""

    def __init__(self, api: object, enabled: bool, *, settle_s: float = 0.25, poll_s: float = 0.01,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.api = api
        self.enabled = enabled
        self.settle_s = settle_s
        self.poll_s = poll_s
        self.sleep = sleep
        self.clock = clock

    def on_confirm(self, action: str, trigger: str = "") -> FocusResult:
        """``trigger`` is the name of the bound input that is held (``Signal.trigger``)."""
        if action not in CLICK_ACTIONS:
            return self._done(self._foreground(), False, COMMAND_NO_CLICK)
        if not self.enabled:
            return self._done(self._foreground(), False, DISABLED)
        if trigger in MODIFIER_TRIGGERS:
            return self._done(self._foreground(), False, MODIFIER_TRIGGER_NO_CLICK)
        position = self.api.cursor_pos()
        if position is None:
            return self._done(None, False, NO_POINTER)
        under = self.api.window_from_point(*position)
        root = self.api.root_window(under) if under else 0
        if not root:
            return self._done(None, False, NO_WINDOW)
        if self.api.window_process_id(root) == self.api.own_process_id():
            return self._done(None, False, OWN_WINDOW)
        if any(self.api.key_down(vk) for vk in HELD_MODIFIERS):
            return self._done(None, False, MODIFIER_HELD)
        own = BUTTONS.get(trigger)
        if any(self.api.key_down(vk) for vk in HELD_BUTTONS if vk != own):
            return self._done(None, False, BUTTON_HELD)
        events = click_events(self.api.buttons_swapped())
        sent = self.api.send_mouse(events)
        if sent != len(events):
            log.warning("click-to-focus: SendInput inserted %d of %d events (error %d)",
                        sent, len(events), self.api.last_error())
            return self._done(None, False, SENDINPUT_FAILED)
        if not self._wait_foreground(root):
            return self._done(None, True, FOCUS_NOT_MOVED)
        return self._done(Target(root, self.api.window_process_id(root)), True, CLICKED)

    def _wait_foreground(self, hwnd: int) -> bool:
        """The click is processed by the target's thread later: wait until it took the foreground."""
        deadline = self.clock() + self.settle_s
        while self.api.foreground_window() != hwnd:
            if self.clock() >= deadline:
                return False
            self.sleep(self.poll_s)
        return True

    def _foreground(self) -> Target | None:
        hwnd = self.api.foreground_window()
        if not hwnd:
            return None
        return Target(hwnd, self.api.window_process_id(hwnd))

    @staticmethod
    def _done(target: Target | None, clicked: bool, reason: str) -> FocusResult:
        log.info("click-to-focus: %s", reason)
        return FocusResult(target, clicked, reason)
