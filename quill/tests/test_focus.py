"""Click-to-focus with a fake Win32 layer: the exact mouse events, the skips and the captured target."""

from __future__ import annotations

import ctypes
import unittest

from quill import focus, win32
from quill.focus import ClickToFocus, FocusResult
from quill.inject import Target
from quill.tests.fakes import OTHER_HWND, OTHER_PID, QUILL_HWND, TARGET, TARGET_CHILD, FakeClock, FakeWin32
from quill.win32 import (
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    QUILL_EXTRA_INFO,
    VK_CONTROL,
    VK_LBUTTON,
    VK_LCONTROL,
    VK_LMENU,
    VK_LSHIFT,
    VK_LWIN,
    VK_MBUTTON,
    VK_MENU,
    VK_RBUTTON,
    VK_RCONTROL,
    VK_RMENU,
    VK_RSHIFT,
    VK_SHIFT,
    VK_XBUTTON1,
    MouseEvent,
)

LEFT_CLICK = [[MouseEvent(MOUSEEVENTF_LEFTDOWN), MouseEvent(MOUSEEVENTF_LEFTUP)]]


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeWin32()
        # Before the click another window has the focus; the click moves it to the window under the pointer.
        self.api.foreground = OTHER_HWND
        self.api.after_mouse = lambda index: setattr(self.api, "foreground", TARGET.hwnd)
        self.clock = FakeClock()

    def focus(self, enabled: bool = True) -> ClickToFocus:
        return ClickToFocus(self.api, enabled, sleep=self.clock.sleep, clock=self.clock)

    def assert_no_click(self, result: FocusResult, reason: str) -> None:
        self.assertEqual(result.reason, reason)
        self.assertFalse(result.clicked)
        self.assertEqual(self.api.mouse_calls, [])
        self.assertEqual(self.api.calls, [])


class ClickTest(Case):
    def test_dictation_and_send_triggers_click_once_then_capture_the_target(self) -> None:
        for action in ("dictation", "send_claude", "send_polished", "send_raw"):
            with self.subTest(action=action):
                self.setUp()
                result = self.focus().on_confirm(action)
                self.assertEqual(result, FocusResult(TARGET, True, focus.CLICKED))
                self.assertEqual(self.api.mouse_calls, LEFT_CLICK)
                self.assertEqual(self.api.calls, [])  # no keyboard input

    def test_click_carries_no_move_or_absolute_flag(self) -> None:
        self.focus().on_confirm("dictation")
        flags = [event.flags for call in self.api.mouse_calls for event in call]
        self.assertEqual(flags, [MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP])
        self.assertFalse(any(flag & (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE) for flag in flags))

    def test_target_is_captured_after_the_click_takes_effect(self) -> None:
        # The target thread processes the click a little later: the capture waits for it.
        seen_at_click: list[int] = []
        polls = iter([OTHER_HWND, OTHER_HWND, TARGET.hwnd])
        self.api.after_mouse = lambda index: seen_at_click.append(self.api.foreground)
        self.api.foreground_window = lambda: next(polls, TARGET.hwnd)  # type: ignore[method-assign]
        result = self.focus().on_confirm("dictation")
        self.assertEqual(seen_at_click, [OTHER_HWND])
        self.assertEqual(result.target, TARGET)
        self.assertEqual(len(self.clock.sleeps), 2)

    def test_focus_that_never_moves_gives_no_target(self) -> None:
        # For example an elevated window under the pointer drops injected input.
        self.api.after_mouse = lambda index: None
        result = self.focus().on_confirm("dictation")
        self.assertEqual(result, FocusResult(None, True, focus.FOCUS_NOT_MOVED))
        self.assertEqual(len(self.api.mouse_calls), 1)
        self.assertGreaterEqual(self.clock.now, 0.25)

    def test_swapped_buttons_click_the_primary_button(self) -> None:
        self.api.swapped = True
        self.focus().on_confirm("dictation")
        self.assertEqual(self.api.mouse_calls, [[MouseEvent(MOUSEEVENTF_RIGHTDOWN), MouseEvent(MOUSEEVENTF_RIGHTUP)]])

    def test_short_sendinput_gives_no_target(self) -> None:
        self.api.mouse_short_by = 1
        result = self.focus().on_confirm("dictation")
        self.assertEqual(result, FocusResult(None, False, focus.SENDINPUT_FAILED))

    def test_trigger_button_held_does_not_block_the_click(self) -> None:
        self.api.keys_down = {VK_XBUTTON1}
        self.assertEqual(self.focus().on_confirm("dictation").reason, focus.CLICKED)


class SkipTest(Case):
    def test_command_never_clicks_and_keeps_the_foreground(self) -> None:
        result = self.focus().on_confirm("command")
        self.assert_no_click(result, focus.COMMAND_NO_CLICK)
        self.assertEqual(result.target, Target(OTHER_HWND, OTHER_PID))

    def test_disabled_click_keeps_the_foreground(self) -> None:
        result = self.focus(enabled=False).on_confirm("dictation")
        self.assert_no_click(result, focus.DISABLED)
        self.assertEqual(result.target, Target(OTHER_HWND, OTHER_PID))

    def test_no_foreground_window_gives_no_target(self) -> None:
        self.api.foreground = 0
        self.assertIsNone(self.focus(enabled=False).on_confirm("dictation").target)

    def test_over_quill_own_window(self) -> None:
        self.api.under_pointer = QUILL_HWND
        result = self.focus().on_confirm("dictation")
        self.assert_no_click(result, focus.OWN_WINDOW)
        self.assertIsNone(result.target)

    def test_over_no_window_or_without_pointer(self) -> None:
        self.api.under_pointer = 0
        self.assert_no_click(self.focus().on_confirm("dictation"), focus.NO_WINDOW)
        self.api.under_pointer = TARGET_CHILD
        self.api.cursor = None
        self.assert_no_click(self.focus().on_confirm("send_claude"), focus.NO_POINTER)

    def test_modifier_held_would_make_a_shortcut(self) -> None:
        # Windows reports a held side-specific key and its generic code together.
        held = ((VK_LSHIFT, VK_SHIFT), (VK_RSHIFT, VK_SHIFT), (VK_LCONTROL, VK_CONTROL),
                (VK_RCONTROL, VK_CONTROL), (VK_LMENU, VK_MENU), (VK_RMENU, VK_MENU), (VK_LWIN,))
        for trigger in ("xbutton1", "f13"):
            for keys in held:
                with self.subTest(trigger=trigger, keys=keys):
                    self.setUp()
                    self.api.keys_down = set(keys)
                    result = self.focus().on_confirm("dictation", trigger)
                    self.assert_no_click(result, focus.MODIFIER_HELD)
                    self.assertIsNone(result.target)

    def test_mouse_button_held_would_break_a_drag(self) -> None:
        for vk in (VK_LBUTTON, VK_RBUTTON):
            with self.subTest(vk=vk):
                self.api.keys_down = {vk}
                self.assert_no_click(self.focus().on_confirm("dictation"), focus.BUTTON_HELD)

    def test_a_middle_button_trigger_is_not_a_held_button(self) -> None:
        # send_raw on the middle click: its own button is down while it is confirmed.
        self.api.keys_down = {VK_MBUTTON}
        result = self.focus().on_confirm("send_raw", "middle")
        self.assertEqual(result, FocusResult(TARGET, True, focus.CLICKED))
        self.assertEqual(self.api.mouse_calls, LEFT_CLICK)
        # Another trigger with the middle button held still refuses, and so does a held left button.
        for trigger, keys in (("xbutton2", {VK_MBUTTON}), ("middle", {VK_MBUTTON, VK_LBUTTON})):
            with self.subTest(trigger=trigger):
                self.setUp()
                self.api.keys_down = keys
                self.assert_no_click(self.focus().on_confirm("send_polished", trigger), focus.BUTTON_HELD)


class ModifierTriggerTest(Case):
    """Right Ctrl, Shift and Alt pass through to Windows, so they are down at the confirm."""

    HELD = {"right_ctrl": {VK_RCONTROL, VK_CONTROL}, "right_shift": {VK_RSHIFT, VK_SHIFT},
            "right_alt": {VK_RMENU, VK_MENU}}

    def test_own_modifier_sends_no_click_and_keeps_the_foreground(self) -> None:
        for trigger, keys in self.HELD.items():
            for action in ("dictation", "send_claude", "send_polished", "send_raw"):
                with self.subTest(trigger=trigger, action=action):
                    self.setUp()
                    self.api.keys_down = set(keys)
                    result = self.focus().on_confirm(action, trigger)
                    self.assert_no_click(result, focus.MODIFIER_TRIGGER_NO_CLICK)
                    self.assertEqual(result.target, Target(OTHER_HWND, OTHER_PID))

    def test_command_and_disabled_keep_their_reasons(self) -> None:
        self.api.keys_down = set(self.HELD["right_ctrl"])
        self.assert_no_click(self.focus().on_confirm("command", "right_ctrl"), focus.COMMAND_NO_CLICK)
        self.assert_no_click(self.focus(enabled=False).on_confirm("dictation", "right_ctrl"), focus.DISABLED)

    def test_no_foreground_window_gives_no_target(self) -> None:
        self.api.foreground = 0
        self.api.keys_down = set(self.HELD["right_alt"])
        result = self.focus().on_confirm("dictation", "right_alt")
        self.assert_no_click(result, focus.MODIFIER_TRIGGER_NO_CLICK)
        self.assertIsNone(result.target)

    def test_a_key_trigger_that_is_not_a_modifier_still_clicks(self) -> None:
        self.api.keys_down = {0x7C}  # f13, suppressed by the hook
        self.assertEqual(self.focus().on_confirm("dictation", "f13"), FocusResult(TARGET, True, focus.CLICKED))

    def test_every_pass_through_key_is_a_modifier_trigger(self) -> None:
        self.assertEqual(focus.MODIFIER_TRIGGERS, frozenset(self.HELD))
        self.assertIn(focus.MODIFIER_TRIGGER_NO_CLICK, focus.SKIPPED_WITH_TARGET)


class RealRecordTest(unittest.TestCase):
    """The real layer's INPUT records, built without calling Windows (SendInput is replaced)."""

    def test_send_mouse_records(self) -> None:
        sent: list[tuple[int, int, int, int, int, int, int]] = []

        class FakeDll:
            @staticmethod
            def SendInput(count: int, array: object, size: int) -> int:  # noqa: N802 - Win32 name
                self.assertEqual(size, ctypes.sizeof(win32.INPUT))
                for record in array[:count]:
                    mi = record.u.mi
                    sent.append((record.type, mi.dx, mi.dy, mi.mouseData, mi.dwFlags, mi.time, mi.dwExtraInfo))
                return count

        api = object.__new__(win32.User32)  # the real constructor is disabled in tests
        api._user32 = FakeDll()
        events = focus.click_events(swapped=False)
        self.assertEqual(api.send_mouse(events), 2)
        self.assertEqual(sent, [
            (win32.INPUT_MOUSE, 0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, QUILL_EXTRA_INFO),
            (win32.INPUT_MOUSE, 0, 0, 0, MOUSEEVENTF_LEFTUP, 0, QUILL_EXTRA_INFO),
        ])
        with self.assertRaises(ValueError):
            api.send_mouse([MouseEvent(MOUSEEVENTF_MOVE)])
        with self.assertRaises(ValueError):
            api.send_mouse([MouseEvent(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE)])
        self.assertEqual(len(sent), 2)


if __name__ == "__main__":
    unittest.main()
